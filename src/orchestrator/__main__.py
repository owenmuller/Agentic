"""``python -m orchestrator`` — the operational entry points.

Four subcommands, in ascending order of consequence:

  check    (default) The startup checks: mode, configs, broker connectivity, replay.
           Prints what was reconstructed and exits. Places nothing.
  health   The daily ten-second look: open positions with their armed stops, cash and
           drawdown, kill switch, research budget, last EDGAR poll, last audit
           record, last run events. Strictly read-only — it never mutates state,
           places orders, or spends budget.
  run      One trading session. Waits for the market to open (bounds computed in
           America/New_York at runtime, so the scheduled trigger only has to be
           early, never exact), ticks the loop until the close, then shuts down
           cleanly. This is the command the Windows scheduled task runs — and the
           only one of the three that trades.

  attribution
           The weekly report: P&L per signal class, gross and NET of each class's
           prorated feed cost — a signal class must out-earn its own feed, and this
           is where that verdict lives. Read-only.

``run`` wires the production fetchers through ``SourceRouter``: EDGAR for Class 3,
Quiver for Class 2 congressional disclosures, X recent search for the Class 1
@nolimitgains account, and the Trump leg declared unbuilt pending the Truth API
decision. It also holds the ``InstanceLock`` for
the data directory: a second concurrent run would interleave one audit file, double-
spend a budget both replayed as unspent, and trade one account twice — it refuses to
start instead. Fetcher dedup sets are seeded from the audit log at startup, so a
restart never re-buys research the log already answers.

Trading mode is decided by Constraint #4 inside ``preflight()``, first, always:
PAPER_MODE=true is the default and live requires the two human-set variables. This
module adds no mode logic of its own.
"""

from __future__ import annotations

import logging
import os
import sys
from decimal import Decimal, InvalidOperation
import time
from datetime import datetime, timezone, timedelta

from audit.attribution import DEFAULT_WINDOW_DAYS, build_attribution
from audit.log import AuditLog, default_data_dir
from execution import AlpacaDailyBars, AlpacaPriceSource, MarketContextBuilder
from execution.options_data import AlpacaOptionsChain
from signals import (
    Form4InsiderFetcher,
    Form13DFetcher,
    Form13FFetcher,
    QuiverCongressFetcher,
    SignalClass,
    SourceRouter,
    XRecentSearchFetcher,
)

from execution.alerts import Alerter
from execution.atr import AtrSource
from execution.alpaca import AlpacaAdapter
from risk_gate.limits import RiskLimits
from execution.liquidity import AdvSource
from execution.environment import load_environment
from execution.vix import CboeVixSource

from orchestrator.bootstrap import preflight, start
from orchestrator.exits import unmanaged_exposure
from orchestrator.recovery import pending_settlement
from orchestrator.ops import (
    InstanceLock,
    RunLog,
    first_poll_lookback_seconds,
    health_report,
    is_trading_weekday,
    mirror_silence,
    session_bounds,
)

logger = logging.getLogger("orchestrator.run")


def check() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001 - the point is the operator sees why
        print(f"STARTUP CHECK FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(checks.describe())
    print()
    print(
        "Checks passed. Nothing was started or traded — use "
        "'python -m orchestrator run' for a trading session, "
        "'python -m orchestrator health' for the daily status, "
        "'python -m orchestrator attribution' for the weekly report."
    )
    return 0


def health() -> int:
    """Read-only. Builds the same reconstructed state as startup and prints it."""
    logging.basicConfig(level=logging.WARNING)  # health output is the report, not logs
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001
        print(f"HEALTH CHECK FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    # Rebuild open positions the same way a real startup would, on an inert engine:
    # no price source, no review pass, and nothing here ever calls the methods that
    # would need them. replay() only reads the audit log.
    from orchestrator.exits import ExitEngine

    engine = ExitEngine(
        gate=checks.gate,
        adapter=checks.adapter,
        audit=checks.audit,
        prices=lambda symbol: None,
        review_pass=None,  # type: ignore[arg-type] - never invoked on this path
        budget=checks.budget,
        config=checks.orchestrator_config.exits,
        clock=checks.clock,
    )
    engine.replay(checks.audit.trails())

    print(
        health_report(
            checks,
            engine.tracked,
            RunLog(default_data_dir() / "run.log"),
        )
    )
    return 0


def attribution() -> int:
    """The weekly attribution report, gross and net of feed costs. Read-only."""
    logging.basicConfig(level=logging.WARNING)
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001
        print(f"ATTRIBUTION FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(_attribution_text(checks))
    return 0


def weekly() -> int:
    """Friday delivery (ruling 2026-09-02): the attribution + forward report,
    emailed on the DAILY tier. Falls back to stdout when alerting is not
    configured, so the report is never lost to a missing credential."""
    logging.basicConfig(level=logging.WARNING)
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001
        print(f"WEEKLY REPORT FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    text = _attribution_text(checks)
    alerter = Alerter()
    if alerter.enabled:
        sent = alerter.daily(
            f"weekly-{checks.clock().date()}", "Friday report", text
        )
        alerter.close()
        print("weekly report emailed" if sent else "weekly report NOT sent "
              "(rate-limited or queue refused); printing instead")
        if sent:
            return 0
    print(text)
    return 0


def _attribution_text(checks) -> str:
    """The full report text — attribution plus forward returns — for the
    ``attribution`` command's stdout and the ``weekly`` command's email."""
    sections: list[str] = []
    generated_at = checks.clock()
    window_start = generated_at - timedelta(days=DEFAULT_WINDOW_DAYS)

    # Feed costs are billed from each source's start date (ruling 2026-08-28):
    # a 90-day window over a two-week-old feed charges two weeks. Prorating the
    # full window instead fires the keep-or-cut flag on months the experiment
    # was never running.
    costs = {
        SignalClass(key): billed
        for key, billed in checks.signals_config.feed_cost_for_window(
            window_start, generated_at
        ).items()
    }
    breakdown = tuple(
        f"{row.source_id} ({row.class_key}): ${row.monthly_cost}/mo from "
        f"{row.billed_from} = {row.billable_days}d, ${row.cost:.2f}"
        for row in checks.signals_config.feed_cost_breakdown(
            window_start, generated_at
        )
    )

    # SPY over the same window: a bull market must not flatter a signal class.
    # Unavailable degrades to None — the report says so instead of guessing.
    benchmark = None
    bars = None
    price_on = None
    try:
        bars = AlpacaDailyBars()
        benchmark = bars.window_return_pct("SPY", window_start, generated_at)

        def price_on(symbol, when):
            """A close on or shortly after a date, or None. Used by the
            counterfactual-hold comparison; a missing bar is missing, never zero."""
            window = bars.bars(symbol, when, when + timedelta(days=7))
            for bar in window:
                raw = bar.get("c")
                try:
                    close = Decimal(str(raw))
                except (InvalidOperation, ValueError, TypeError):
                    continue
                if close > 0:
                    return close
            return None

    except Exception as error:  # noqa: BLE001 - a report without alpha beats no report
        print(f"benchmark fetch failed: {error}", file=sys.stderr)

    # Directional-bias measurement (ruling 2026-09-02): per-position beta from
    # ~200 calendar days of daily closes vs SPY, value-weighted into a book
    # beta. Open positions are derived from the trails (approved, filled, no
    # outcome); anything unmeasurable renders n/a — absent, never guessed.
    position_betas: list[tuple] = []
    if bars is not None:
        try:
            from audit.attribution import beta_from_closes

            def closes_of(symbol):
                out = []
                for bar in bars.bars(
                    symbol, generated_at - timedelta(days=200), generated_at
                ):
                    day = str(bar.get("t", ""))[:10]
                    try:
                        close = Decimal(str(bar.get("c")))
                    except (InvalidOperation, ValueError, TypeError):
                        continue
                    if day and close > 0:
                        out.append((datetime.fromisoformat(day).date(), close))
                return out

            held: dict[str, Decimal] = {}
            for trail in checks.audit.trails():
                if trail.outcome is not None or not trail.fills:
                    continue
                symbol = str((trail.decision.gate.order or {}).get("symbol") or "")
                if not symbol or "/" in symbol or len(symbol) > 6:
                    continue  # options carry OCC symbols; beta is an equity fact
                units = sum(
                    (
                        f.filled_quantity if f.side == "buy" else -f.filled_quantity
                        for f in trail.fills
                    ),
                    Decimal("0"),
                )
                if units > 0:
                    held[symbol.upper()] = held.get(symbol.upper(), Decimal("0")) + units
            if held:
                spy_closes = closes_of("SPY")
                for symbol in sorted(held):
                    asset_closes = closes_of(symbol)
                    beta = beta_from_closes(asset_closes, spy_closes)
                    last_close = asset_closes[-1][1] if asset_closes else Decimal("0")
                    position_betas.append(
                        (symbol, beta, held[symbol] * last_close)
                    )
        except Exception as error:  # noqa: BLE001 - measurement, never blocking
            print(f"beta measurement unavailable: {error}", file=sys.stderr)

    month_start = generated_at.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    report = build_attribution(
        checks.audit.trails(),
        generated_at=generated_at,
        feed_costs_for_window=costs,
        research_costs=checks.audit.research_costs_by_class(window_start),
        benchmark_return_pct=benchmark,
        mtd_research_cost=checks.audit.research_cost_between(month_start),
        feed_cost_detail=breakdown,
        price_on=price_on,
        # Execution fidelity (ruling 2026-09-02): paper P&L raw AND haircut.
        haircut_bps=checks.orchestrator_config.slippage_haircut_bps,
        position_betas=tuple(position_betas),
    )
    sections.append(report.render())

    # Research upstream errors (ruling 2026-09-03): the code-execution 400 on
    # the opus search phase is accepted as an intermittent typed rejection and
    # its production frequency is tracked here; the submit_research-in-search-
    # phase design is the fix if it recurs.
    try:
        from audit.records import StageRejectionRecord as _SRR

        upstream = [
            r
            for r in checks.audit.records()
            if isinstance(r, _SRR)
            and r.code == "upstream_error"
            and r.recorded_at >= window_start
        ]
        code_exec = [
            r for r in upstream
            if "Code execution requested a client tool" in (r.message or "")
        ]
        sections.append(
            f"Research upstream errors in window: {len(upstream)}; code-execution "
            f"400s (\"client tool not in request\", opus search phase): "
            f"{len(code_exec)} — accepted as intermittent typed rejections "
            f"(ruling 2026-09-03); recurrence in production reopens the "
            f"submit_research-in-search-phase design."
        )
    except Exception as error:  # noqa: BLE001 - a count, never blocking
        sections.append(f"upstream error count unavailable: {error}")

    # Forward returns (ruling 2026-09-01): the funnel's counterfactual
    # scoreboard, computed lazily from bars and cached append-only. A data
    # outage degrades to a sentence — the attribution above never waits on it.
    if bars is not None:
        try:
            from forward import (
                ForwardReturns,
                funnel_entries,
                render_forward_report,
                wanted_pairs,
            )

            entries = funnel_entries(checks.audit.records())
            engine = ForwardReturns(
                bars.bars,
                checks.audit.path.parent / "forward_returns.jsonl",
                clock=checks.clock,
            )
            # Shadowed review closes (probation, 2026-09-02) get forward rows
            # too: the counterfactual is what the price did AFTER the verdict.
            shadows = tuple(checks.audit.shadow_closes())
            pairs = wanted_pairs(entries) | {
                (shadow.symbol.upper(), shadow.recorded_at.date())
                for shadow in shadows
            }
            rows = engine.rows_for(pairs)
            spotlight = tuple(
                name
                for klass in checks.signals_config.classes.values()
                for source in klass.sources
                for name in source.spotlight_filers
            )
            sections.append(
                render_forward_report(
                    entries,
                    rows,
                    spotlight_filers=spotlight,
                    shadow_closes=shadows,
                )
            )
        except Exception as error:  # noqa: BLE001 - a report without it beats no report
            sections.append(f"forward returns unavailable: {error}")
        bars.close()
    else:
        sections.append("forward returns unavailable: no market data this run")
    return "\n\n".join(sections)


def replay() -> int:
    """Config-replay harness (ruling 2026-09-02): what a candidate signals.yaml
    and/or risk_limits.yaml would have changed, over the recorded funnel.
    READ-ONLY AND OFFLINE — the audit log and forward cache are only read, no
    LLM is called, no bars are fetched. Usage:

        python -m orchestrator replay [--signals path] [--limits path]
    """
    logging.basicConfig(level=logging.WARNING)
    from pathlib import Path

    from audit import AuditLog
    from orchestrator.whatif import load_cached_rows, render_whatif_report
    from risk_gate.limits import RiskLimits
    from signals import SignalsConfig

    args = sys.argv[2:]
    signals_path = limits_path = None
    index = 0
    while index < len(args):
        flag = args[index]
        if flag == "--signals" and index + 1 < len(args):
            signals_path = Path(args[index + 1])
            index += 2
        elif flag == "--limits" and index + 1 < len(args):
            limits_path = Path(args[index + 1])
            index += 2
        else:
            print(f"unknown argument {flag!r}", file=sys.stderr)
            return 2
    if signals_path is None and limits_path is None:
        print(
            "nothing to replay: pass --signals and/or --limits with a "
            "candidate config file",
            file=sys.stderr,
        )
        return 2
    try:
        current_signals = SignalsConfig.load()
        candidate_signals = (
            SignalsConfig.load(signals_path) if signals_path else current_signals
        )
        current_limits = RiskLimits.load()
        candidate_limits = (
            RiskLimits.load(limits_path) if limits_path else current_limits
        )
        audit = AuditLog()
        rows = load_cached_rows(audit.path.parent / "forward_returns.jsonl")
        print(
            render_whatif_report(
                audit.records(),
                current_signals,
                candidate_signals,
                current_limits,
                candidate_limits,
                rows,
            )
        )
    except Exception as error:  # noqa: BLE001
        print(f"REPLAY FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


def golden() -> int:
    """Golden-set replay (ruling 2026-09-02): the frozen graded decisions through
    the CURRENT prompt/tier/model. Required before any prompt, tier, or model
    change ships. Spends real API dollars; writes no audit records. Usage:

        python -m orchestrator golden [--only name] [--limit N]
    """
    logging.basicConfig(level=logging.WARNING)
    from execution.environment import load_environment as _load_env

    from orchestrator.golden import (
        build_source_tiers,
        load_cases,
        render_summary,
        run_golden,
    )
    from research.client import AnthropicResearchClient
    from research.config import ResearchConfig
    from research.exit_review import ExitReviewPass
    from research.research_pass import ResearchPass
    from signals import SignalsConfig

    args = sys.argv[2:]
    only = None
    limit = None
    index = 0
    while index < len(args):
        if args[index] == "--only" and index + 1 < len(args):
            only = args[index + 1]
            index += 2
        elif args[index] == "--limit" and index + 1 < len(args):
            limit = int(args[index + 1])
            index += 2
        else:
            print(f"unknown argument {args[index]!r}", file=sys.stderr)
            return 2
    try:
        _load_env()
        cases = load_cases()
        if only is not None:
            cases = [case for case in cases if case.name == only]
            if not cases:
                print(f"no golden case named {only!r}", file=sys.stderr)
                return 2
        if limit is not None:
            cases = cases[:limit]
        config = ResearchConfig.load()
        client = AnthropicResearchClient(config)
        research = ResearchPass(
            client, source_tiers=build_source_tiers(SignalsConfig.load())
        )
        reviews = sum(1 for case in cases if case.kind == "review")
        print(
            f"replaying {len(cases)} golden cases ({reviews} review) through the "
            f"production passes (model {config.model}, screen "
            f"{config.screen.model if config.screen else 'off'}) — real API spend"
        )
        results = run_golden(research, cases, review_pass=ExitReviewPass(client))
        print(render_summary(results))
        return 0 if all(result.passed for result in results) else 3
    except Exception as error:  # noqa: BLE001
        print(f"GOLDEN REPLAY FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


def run() -> int:
    """One supervised-by-schedule trading session: open to close, then shut down."""
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(data_dir / "orchestrator.log", encoding="utf-8"),
        ],
    )
    # Alerting (ruling 2026-09-02): urgent/daily email tiers over Gmail SMTP.
    # Built before the run log so run-log events can route through it; .env is
    # loaded here (idempotently — preflight loads it again) so the credentials
    # are visible. Unconfigured = disabled with one log line, nothing changes.
    load_environment()
    alerter = Alerter()

    def observe(event: str, detail: str) -> None:
        """Run-log events that must reach a phone: errors, cost tripwires,
        billing anomalies, and a mechanical breaker trip riding a MECH line."""
        if event in ("ERROR", "COST", "READS"):
            alerter.urgent(f"{event}:{detail[:40]}", f"{event}: {detail[:120]}", detail)
        elif event == "MECH" and "BREAKER" in detail.upper():
            alerter.urgent("mech_breaker", "mechanical circuit breaker tripped", detail)
        elif event == "HALT":
            alerter.urgent("operator_halt_loop", "OPERATOR HALT honoured by the live session", detail)

    run_log = RunLog(data_dir / "run.log", observer=observe)

    # One instance per data directory: two runs would interleave one audit file,
    # double-spend a budget each replayed as unspent, and trade one account twice.
    # The OS holds the lock, so a crashed run releases it automatically — a stale
    # lock file cannot brick the next scheduled session.
    lock = InstanceLock(data_dir / "orchestrator.lock")
    if not lock.acquire():
        message = f"another orchestrator run holds the lock: {lock.holder()}"
        run_log.note("REFUSED", message)
        logger.error("%s; refusing to start", message)
        return 1

    run_log.note("STARTED", f"pid={os.getpid()}")
    now = datetime.now(timezone.utc)
    loop = None
    edgar = None
    form4 = None
    form13d = None
    quiver = None
    x_search = None
    prices = None
    options_chain = None
    try:
        if not is_trading_weekday(now):
            run_log.note("STOPPED", "not a trading weekday; nothing to do")
            return 0
        open_utc, close_utc = session_bounds(now)
        if now >= close_utc:
            run_log.note("STOPPED", "started after the close; nothing to do")
            return 0

        # Checks first — a misconfigured run should fail before it waits for a bell.
        # The liquidity gate (ruling 2026-09-02) reads CONSOLIDATED volume: the
        # SIP feed, not IEX (IEX-only volume understates ADV ~30x, probed
        # 2026-09-02). Its own bars client; everything else stays on IEX.
        sip_bars = AlpacaDailyBars(feed="sip")
        checks = preflight(
            adv=AdvSource(sip_bars, days=RiskLimits.load().liquidity.adv_days)
        )
        logger.info("startup state:\n%s", checks.describe())

        # Dedup seeded from the log: research already paid for is never re-bought.
        researched = checks.audit.researched_external_ids()

        def seen_for(source_id):
            return {eid for (sid, eid) in researched if sid == source_id}

        edgar = Form13FFetcher(seen=seen_for("form_13f"))
        quiver = QuiverCongressFetcher(seen=seen_for("congressional_disclosures"))
        # Form 4 insider clusters (ruling 2026-09-02): market-wide EDGAR, its
        # rolling cluster window and seen accessions persisted so a restart
        # neither re-buys filings nor forgets a half-formed cluster.
        form4 = Form4InsiderFetcher(
            state_path=default_data_dir() / "form4_state.json",
            seen=seen_for("form4_insiders"),
        )
        # Activist 13Ds (ruling 2026-09-02): market-wide listing, watchlist
        # filter client-side, structured primary_doc.xml facts.
        form13d = Form13DFetcher(seen=seen_for("form_13d"))
        # Session-gap first-poll lookback (ruling 2026-08-26): the old fixed
        # 15-minute window lost every post made between sessions. Floor 15min
        # (a mid-session bounce re-reads almost nothing), cap 24h (X bills per
        # post returned; the cap bounds what a long-idle restart may buy).
        lookback = first_poll_lookback_seconds(
            checks.audit.last_record_at(), datetime.now(timezone.utc)
        )
        run_log.note("LOOKBACK", f"X first-poll lookback={lookback}s")
        x_search = XRecentSearchFetcher(
            first_poll_lookback_seconds=lookback,
            # API max. A gap-sized first poll must not truncate the overnight
            # backlog to the newest 25; billing is per post returned either
            # way, and since_id keeps steady-state polls tiny.
            max_results=100,
            # Post ids are globally unique on X, so one seen-set serves every
            # account the fetcher polls (2026-08-25: + unusual_whales,
            # optionshawk, citrini).
            seen=(
                seen_for("nolimitgains")
                | seen_for("unusual_whales")
                | seen_for("optionshawk")
                | seen_for("citrini")
            ),
            # The billing tripwire writes straight into run.log: a since_id bug
            # must show up there before it shows up on the bill.
            warn_sink=lambda message: run_log.note("READS", message),
        )

        def logged(source_id, inner):
            def fetch(source):
                items = inner(source)
                detail = f"{source_id} ok items={len(items)}"
                reads = getattr(inner, "posts_read_today", None)
                if reads is not None:
                    detail += f" reads_today={reads}"
                run_log.note("POLL", detail)
                return items

            return fetch

        fetcher = SourceRouter(
            routes={
                "form_13f": logged("form_13f", edgar),
                "congressional_disclosures": logged(
                    "congressional_disclosures", quiver
                ),
                # Insider clusters (human ruling 2026-09-02): Class 2 hourly.
                "form4_insiders": logged("form4_insiders", form4),
                # Activist 13Ds (human ruling 2026-09-02): Class 2 hourly.
                "form_13d": logged("form_13d", form13d),
                "nolimitgains": logged("nolimitgains", x_search),
                # Options-flow free taste (human-authorized 2026-08-25).
                "unusual_whales": logged("unusual_whales", x_search),
                # First probation source (human ruling 2026-08-25): researched
                # and credibility-tracked, sized to zero until promoted.
                "optionshawk": logged("optionshawk", x_search),
                # Medium-latency thesis caller: lives in class_2 (hourly), but
                # it is an X account, so the same fetcher serves it.
                "citrini": logged("citrini", x_search),
                # The Trump leg, decided 2026-08-18: X mirror accounts, not the
                # Truth API — the upgrade decision belongs to attribution data.
                # Signals attribute to trump_posts; the audit record keeps the
                # deliverer.
                "trump_mirror_ttox": logged("trump_mirror_ttox", x_search),
                "trump_mirror_tdp": logged("trump_mirror_tdp", x_search),
            },
            # The original account is not polled directly; its content arrives via
            # the mirror sources above.
            unbuilt={"trump_posts"},
        )

        # Mirror health: silence is ambiguous (quiet principal, or dead bot), so it
        # goes to a human via run.log rather than to any automatic action.
        for message in mirror_silence(
            checks.audit, checks.signals_config, datetime.now(timezone.utc)
        ):
            logger.warning("%s", message)
            run_log.note("MIRROR", message)

        options_chain = AlpacaOptionsChain()
        prices = AlpacaPriceSource(
            feed=checks.orchestrator_config.market_data.feed,
            max_quote_age_seconds=(
                checks.orchestrator_config.market_data.max_quote_age_seconds
            ),
        )
        daily_bars = AlpacaDailyBars()
        context_builder = MarketContextBuilder(daily_bars)
        startup = start(
            fetcher=fetcher,
            prices=prices,
            checks=checks,
            market_context=context_builder.context_for,
            cost_warn_sink=lambda message: run_log.note("COST", message),
            error_sink=lambda message: run_log.note("ERROR", message),
            classify_sink=lambda message: run_log.note("CLASSIFY", message),
            mechanical_sink=lambda message: run_log.note("MECH", message),
            halt_sink=lambda message: run_log.note("HALT", message),
            options_chain=options_chain,
            # Regime scalar (rulings 2026-09-01/02): last VIX close from CBOE's
            # public CSV, fetched at most once per UTC day, missing data loud
            # and x1.0 — never a silent halving of the book on a CDN outage.
            vix_close=CboeVixSource(),
            # ATR sizing (ruling 2026-09-02): ATR(14)/price from the same daily
            # bars the market context reads; missing data = the fixed-15% regime.
            atr_fraction=AtrSource(daily_bars),
        )
        loop = startup.loop

        # Startup conditions worth a phone buzz (ruling 2026-09-02): a broker
        # position with no audit trail behind it has no stops armed, and an
        # entry order still unresolved after settlement recovery means a crash
        # left money in an unknown state.
        unmanaged = unmanaged_exposure(startup.gate, startup.exits.tracked)
        if unmanaged:
            alerter.urgent(
                "unmanaged",
                f"UNMANAGED positions: {', '.join(sorted(unmanaged))}",
                "Held at the broker with no audit trail and no stops armed: "
                + ", ".join(f"{q} x {s}" for s, q in sorted(unmanaged.items())),
            )
        still_pending = pending_settlement(checks.audit)
        if still_pending:
            alerter.urgent(
                "pending_settlement",
                f"{len(still_pending)} orders still pending settlement after recovery",
                "\n".join(str(item) for item in still_pending),
            )

        while datetime.now(timezone.utc) < open_utc:
            time.sleep(min(30, (open_utc - datetime.now(timezone.utc)).total_seconds()))
        run_log.note("SESSION", f"open; trading until {close_utc.isoformat(timespec='seconds')}")

        interval = checks.orchestrator_config.tick_interval_seconds
        kill_switch_alerted = startup.gate.kill_switch_tripped
        first_judged_alerted = False
        first_mechanical_alerted = False
        while datetime.now(timezone.utc) < close_utc:
            tick = loop.tick()
            # The 10am tier, from this tick's own facts (ruling 2026-09-02).
            if startup.gate.kill_switch_tripped and not kill_switch_alerted:
                kill_switch_alerted = True
                state = startup.gate.state
                alerter.urgent(
                    "kill_switch",
                    "KILL SWITCH TRIPPED - opening orders halted",
                    f"drawdown {state.drawdown():.2%} from high-water "
                    f"{state.high_water_mark}; NAV {state.nav}. Reset is "
                    f"manual-human-only.",
                )
            if tick.exits_started:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                alerter.urgent(
                    f"exits-{stamp}",
                    f"{tick.exits_started} exit order(s) started",
                    f"stop/ratchet/leash/review exits this tick: "
                    f"{tick.exits_started} started, {tick.positions_closed} "
                    f"closed. Details: run.log and the audit trail.",
                )
            if tick.traded and not first_judged_alerted:
                first_judged_alerted = True
                alerter.daily(
                    f"first-judged-{now.date()}",
                    "first judged entry of the day",
                    f"{tick.traded} judged order(s) approved this tick.",
                )
            if tick.mechanical_entries and not first_mechanical_alerted:
                first_mechanical_alerted = True
                alerter.daily(
                    f"first-mechanical-{now.date()}",
                    "first mechanical entry of the day",
                    f"{tick.mechanical_entries} mechanical order(s) this tick.",
                )
            remaining = (close_utc - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        report = loop.shutdown()
        loop = None
        run_log.note(
            "STOPPED",
            f"market close; settled_or_released={report.settled} "
            f"closed={report.positions_closed} "
            f"kill_switch={'TRIPPED' if report.halted else 'clear'}",
        )
        state = startup.gate.state
        held = sorted(
            f"{p.symbol} x{p.quantity}" for p in startup.exits.tracked
        )
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        spend = checks.audit.research_cost_between(day_start)
        alerter.daily(
            f"close-{now.date()}",
            "close summary",
            f"NAV {state.nav} | drawdown {state.drawdown():.2%} "
            f"(high-water {state.high_water_mark}) | kill switch "
            f"{'TRIPPED' if report.halted else 'clear'}\n"
            f"positions ({len(held)}): {', '.join(held) or 'none'}\n"
            f"research spend today: ${spend or 0} (estimates; console bill is "
            f"truth)\n"
            f"session: settled_or_released={report.settled} "
            f"closed={report.positions_closed}",
        )
        return 0
    except KeyboardInterrupt:
        run_log.note("STOPPED", "interrupted by operator")
        return 0
    except Exception as error:  # noqa: BLE001 - unattended: the log is the operator
        logger.exception("trading session died")
        run_log.note("ERROR", f"{type(error).__name__}: {error}")
        return 1
    finally:
        # A shutdown here is what makes a crash recoverable: cancel working orders,
        # persist the session state. If even this fails, the next startup's orphan
        # sweep and audit replay pick up the pieces — that path is tested.
        if loop is not None:
            try:
                loop.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("shutdown after failure also failed")
                run_log.note("ERROR", "shutdown after failure also failed")
        if prices is not None:
            prices.close()
        if options_chain is not None:
            options_chain.close()
        if edgar is not None:
            edgar.close()
        if form4 is not None:
            form4.close()
        if form13d is not None:
            form13d.close()
        if quiver is not None:
            quiver.close()
        if x_search is not None:
            x_search.close()
        # Let queued alerts drain before the process exits — bounded, so a dead
        # SMTP host cannot hold the shutdown hostage.
        alerter.close()
        lock.release()


def _session_is_live(data_dir) -> bool:
    """Probe the instance lock without holding it."""
    lock = InstanceLock(data_dir / "orchestrator.lock")
    if lock.acquire():
        lock.release()
        return False
    return True


def halt() -> int:
    """The panic button (ruling 2026-09-02): trip the kill switch, cancel every
    open order at the broker, alert, print state. See ops/EMERGENCY.md."""
    import getpass

    from orchestrator.halt import halt_marker_path, perform_halt

    logging.basicConfig(level=logging.WARNING)
    load_environment()
    data_dir = default_data_dir()
    reason = " ".join(sys.argv[2:]).strip() or "operator halt (no reason given)"
    operator = os.environ.get("AGENTIC_OPERATOR") or getpass.getuser()
    live = _session_is_live(data_dir)
    adapter = None
    try:
        adapter = AlpacaAdapter()
    except Exception as error:  # noqa: BLE001 - halt without the broker still halts
        print(f"broker adapter unavailable ({error}); open orders NOT cancelled here")
    alerter = Alerter()
    report = perform_halt(
        marker_path=halt_marker_path(data_dir),
        session_path=data_dir / "session_state.json",
        live_session=live,
        reason=reason,
        operator=operator,
        adapter=adapter,
        alert=alerter.urgent if alerter.enabled else None,
        audit=AuditLog(path=data_dir / "audit.jsonl"),
    )
    RunLog(data_dir / "run.log").note("HALT", f"operator {operator}: {reason}")
    print(report.render())
    try:
        print("")
        print(preflight(adapter=adapter).describe())
    except Exception as error:  # noqa: BLE001
        print(f"state unavailable: {type(error).__name__}: {error}")
    alerter.close()
    return 0 if report.marker_written else 1


def resume() -> int:
    """The human's manual reset (ruling 2026-09-02). Refuses while a session
    is live and without the exact acknowledgement. See ops/EMERGENCY.md."""
    import getpass

    from orchestrator.halt import RESUME_PHRASE, halt_marker_path, perform_resume

    logging.basicConfig(level=logging.WARNING)
    load_environment()
    data_dir = default_data_dir()
    acknowledgement = " ".join(sys.argv[2:]).strip()
    operator = os.environ.get("AGENTIC_OPERATOR") or getpass.getuser()
    try:
        checks = preflight()
        report = perform_resume(
            gate=checks.gate,
            session=checks.session,
            audit=checks.audit,
            marker_path=halt_marker_path(data_dir),
            acknowledgement=acknowledgement,
            operator=operator,
            live_session=_session_is_live(data_dir),
        )
    except (ValueError, RuntimeError) as error:
        print(f"RESUME REFUSED: {error}", file=sys.stderr)
        print(f'usage: python -m orchestrator resume "<your name>: {RESUME_PHRASE}"',
              file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"RESUME FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    RunLog(data_dir / "run.log").note("RESUME", f"operator {operator}: {acknowledgement}")
    print(report.render())
    return 0


def stress() -> int:
    """Historical stress test of the current book (ruling 2026-09-02).
    Report-only: SIP daily bars in, drawdowns out, nothing written."""
    from datetime import date as _date

    from orchestrator.stress import (
        BookPosition,
        StressWindowSpec,
        render_stress_report,
        stress_book,
    )
    from risk_gate.state import Sleeve

    logging.basicConfig(level=logging.WARNING)
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001
        print(f"STRESS TEST FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    state = checks.gate.state
    positions = [
        BookPosition(
            sleeve=str(p.sleeve),
            symbol=p.key[1],
            quantity=p.quantity,
            market_value=p.market_value,
            is_option=p.is_option,
        )
        for p in state.positions.values()
        if p.quantity > 0 and p.sleeve in (Sleeve.EQUITY, Sleeve.MECHANICAL)
    ]
    held = {
        sleeve: sum((p.market_value for p in positions if p.sleeve == sleeve), Decimal("0"))
        for sleeve in ("equity", "mechanical")
    }
    # Each sleeve's cash is its allotment less what it holds (floored at zero);
    # the mechanical ledger is authoritative for its own sleeve when seeded.
    equity_cash = max(Decimal("0"), checks.gate.sleeve_nav(Sleeve.EQUITY) - held["equity"])
    mechanical_cash = checks.session.mechanical_virtual_cash
    if mechanical_cash is None:
        mechanical_cash = max(
            Decimal("0"), checks.gate.sleeve_nav(Sleeve.MECHANICAL) - held["mechanical"]
        )
    # Whatever the sleeves do not account for — including the cash-sweep ETF,
    # which is cash in another form — rides flat in the total.
    other = state.nav - sum(held.values()) - equity_cash - mechanical_cash
    bars = AlpacaDailyBars(feed="sip")

    def closes(symbol: str, start: _date, end: _date):
        rows = []
        for bar in bars.bars(
            symbol,
            datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc),
        ):
            try:
                day = _date.fromisoformat(str(bar.get("t"))[:10])
                close = Decimal(str(bar.get("c")))
            except (InvalidOperation, ValueError, TypeError):
                continue
            rows.append((day, close))
        return rows

    windows = [
        StressWindowSpec(name=w.name, start=w.start, end=w.end)
        for w in checks.orchestrator_config.stress_windows
    ]
    try:
        results = stress_book(
            positions,
            {"equity": equity_cash, "mechanical": mechanical_cash},
            other,
            closes,
            windows,
        )
    finally:
        bars.close()
    print(render_stress_report(results, checks.clock()))
    return 0


def overreaction() -> int:
    """Overreaction-fade MEASUREMENT screen (ruling 2026-09-03). No LLM, no
    trade, no API spend beyond Alpaca bars. Writes measurement-only audit rows.

        python -m orchestrator overreaction                  # last completed session
        python -m orchestrator overreaction --session YYYY-MM-DD
        python -m orchestrator overreaction --backfill FROM TO
        python -m orchestrator overreaction --windows        # the stress windows
    """
    import time as _time
    import uuid as _uuid
    from datetime import date as _date

    from orchestrator.config import ConvergenceConfig
    from orchestrator.exits import ExitEngine
    from orchestrator.overreaction import (
        build_universe,
        last_completed_session,
        run_screen,
        weekdays_between,
    )
    from orchestrator.registry import SignalRegistry
    from risk_gate.sectors import SectorMap

    logging.basicConfig(level=logging.WARNING)
    args = sys.argv[2:]
    sessions: list[_date] = []
    mode = "latest"
    index = 0
    while index < len(args):
        flag = args[index]
        if flag == "--session" and index + 1 < len(args):
            sessions = [_date.fromisoformat(args[index + 1])]
            mode = "session"
            index += 2
        elif flag == "--backfill" and index + 2 < len(args):
            sessions = weekdays_between(
                _date.fromisoformat(args[index + 1]), _date.fromisoformat(args[index + 2])
            )
            mode = "backfill"
            index += 3
        elif flag == "--windows":
            mode = "windows"
            index += 1
        else:
            print(f"unknown argument {flag!r}", file=sys.stderr)
            return 2
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001
        print(f"OVERREACTION SCREEN FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    config = checks.orchestrator_config.overreaction_screen
    if not config.enabled:
        print("overreaction screen disabled in orchestrator.yaml")
        return 0
    now = checks.clock()
    if mode == "latest":
        sessions = [last_completed_session(now)]
    elif mode == "windows":
        sessions = [
            day
            for window in checks.orchestrator_config.stress_windows
            for day in weekdays_between(window.start, window.end)
        ]

    # Universe: a registry seeded over a WIDER window than convergence uses, so
    # a backfill sees the names that were live at the time; held = judged.
    wide = checks.orchestrator_config.convergence.model_copy(
        update={"window_days": max(config.universe_window_days,
                                   checks.orchestrator_config.convergence.window_days)}
    )
    registry = SignalRegistry(wide, checks.clock)
    registry.seed(checks.audit.records())
    engine = ExitEngine(
        gate=checks.gate, adapter=checks.adapter, audit=checks.audit,
        prices=lambda symbol: None, review_pass=None, budget=checks.budget,
        config=checks.orchestrator_config.exits, clock=checks.clock,
    )
    engine.replay(checks.audit.trails())
    held = [p.symbol for p in engine.tracked if not p.is_option]
    researched = [s for symbols in registry.verdict_summary().values() for s in symbols]
    universe = build_universe(held, researched, registry.purchase_symbols())
    sectors = SectorMap.load()
    bars = AlpacaDailyBars(feed="sip")
    try:
        report = run_screen(
            sessions=sessions,
            universe=universe,
            bars=bars.bars,
            sector_of=lambda symbol: sectors.sector_of(symbol) or "",
            config=config,
            audit=checks.audit,
            id_factory=lambda: _uuid.uuid4().hex[:16],
            pace=lambda: _time.sleep(0.35),
        )
    finally:
        bars.close()
    core = sum(1 for m in universe.values() if m.tier == "core")
    print(f"universe: {len(universe)} names ({core} core, {len(universe) - core} broad); "
          f"mode {mode}")
    print(report.render())
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "check":
        return check()
    if command == "health":
        return health()
    if command == "run":
        return run()
    if command == "attribution":
        return attribution()
    if command == "weekly":
        return weekly()
    if command == "replay":
        return replay()
    if command == "golden":
        return golden()
    if command == "halt":
        return halt()
    if command == "resume":
        return resume()
    if command == "stress":
        return stress()
    if command == "overreaction":
        return overreaction()
    print(
        f"unknown command {command!r}: expected check, health, run, attribution, "
        f"weekly, replay, golden, halt, resume, stress, or overreaction",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
