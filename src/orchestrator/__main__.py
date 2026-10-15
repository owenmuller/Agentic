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

import json
import logging
import os
import sys
from decimal import Decimal, InvalidOperation
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import datasafety

from audit.attribution import DEFAULT_WINDOW_DAYS, build_attribution
from audit.log import AuditLog, default_data_dir
from execution import AlpacaDailyBars, AlpacaPriceSource, MarketContextBuilder
from execution.options_data import AlpacaOptionsChain
from signals import (
    Form4InsiderFetcher,
    Form13DFetcher,
    Form8KFetcher,
    Form13FFetcher,
    QuiverCongressFetcher,
    SignalClass,
    SourceRouter,
    XRecentSearchFetcher,
)
from orchestrator.contracts_feed import build_awards_fetcher, pending_path_for

from execution import cards
from execution.alerts import Alerter
from execution.market_data import BenchmarkMoves
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


def exec_test_report() -> int:
    """The B execution test's daily reports and running totals (ruling
    2026-10-09 evening). Read-only; outside production it reads the snapshot."""
    import json as _json

    path = default_data_dir() / "execution_test_daily.jsonl"
    if not path.exists():
        print("execution test: no daily report yet (the first one is written the morning after session 1)")
        return 0
    rows = [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        print(_json.dumps(row))
    live = sum(row.get("pnl_live", 0) for row in rows)
    modelled = sum(row.get("pnl_modelled_at_live_sizes", 0) for row in rows)
    faults = sum(len(row.get("faults") or []) for row in rows)
    print()
    print(
        f"EXECUTION TEST, {len(rows)} sessions reported: P&L live {live:+.2f} vs modelled {modelled:+.2f} "
        f"(gap {live - modelled:+.2f}); faults {faults}"
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


def _main_day(adapter) -> tuple:
    """(account value, today's return) from the broker: equity against the
    previous close's equity. (None, None) when unreadable."""
    try:
        snap = adapter.account_snapshot()
        value, prior = float(snap.get("equity") or 0), float(snap.get("last_equity") or 0)
        return (value or None), (value / prior - 1 if value and prior > 0 else None)
    except Exception:  # noqa: BLE001 - "n/a" on the card, never a crash
        return None, None


def _spy_today():
    try:
        return BenchmarkMoves().today()
    except Exception:  # noqa: BLE001
        return None


def _now_et() -> str:
    """'Oct 13, 10:42 ET' - the time a card names."""
    from zoneinfo import ZoneInfo

    local = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return f"{local:%b} {local.day}, {local:%H:%M} ET"


def _exec_test_card(text: str):
    """The execution test's daily report line (JSON from the engine) as a
    plain-English card, with SPY's move that day beside the result."""
    import json as _json
    from datetime import date as _date

    try:
        line = _json.loads(text)
    except ValueError:
        return None
    if line.get("error"):
        return cards.error_card("Execution test", line.get("day", "?"), f"the daily report failed: {line['error']}")
    day = _date.fromisoformat(line["day"])
    try:
        from orchestrator.config import OrchestratorConfig

        total = OrchestratorConfig.load().execution_test.max_sessions
    except Exception:  # noqa: BLE001
        total = 20
    try:
        spy_day = BenchmarkMoves().between(day - timedelta(days=1), day)
    except Exception:  # noqa: BLE001
        spy_day = None
    slips = line.get("slippage_bp_vs_model") or {}
    # the engine records each problem as {"at": ..., "fault": text}
    faults = [str(f.get("fault", f)) if isinstance(f, dict) else str(f) for f in line.get("faults") or []]
    detail = "\n".join([
        f"Day: {line['day']} (session {line.get('session')} of {total})",
        f"Live result: ${line.get('pnl_live')}; the backtest model at the same sizes: "
        f"${line.get('pnl_modelled_at_live_sizes')}",
        f"Decisions matching the model: {line.get('decisions_agree_with_model')} "
        f"(decisions made live: {line.get('decisions_live')}, late: {line.get('late_decisions')})",
        f"Fills: {line.get('fills')}; average seconds to fill: {line.get('seconds_to_fill_mean')}",
        "Fill cost against the model, by kind (basis points; 1 bp = 0.01%; positive = worse than modelled):",
        *[f"  {kind.replace('_', ' ')}: {cell.get('n')} fill{'' if cell.get('n') == 1 else 's'}, average {cell.get('mean')}"
          for kind, cell in slips.items()],
        f"Day trades in the 5-day window: {line.get('day_trades_in_window')}",
        "Problems:" + ("" if line.get("faults") else " none"),
        *[f"  - {text}" for text in faults],
    ])
    return cards.exec_test_card(
        day=f"{day:%b} {day.day}", session=int(line.get("session") or 0), total=total,
        live=float(line.get("pnl_live") or 0), modelled=float(line.get("pnl_modelled_at_live_sizes") or 0),
        spy_day=spy_day, agree=str(line.get("decisions_agree_with_model") or "n/a").replace("/", " of "),
        fills=int(line.get("fills") or 0), seconds=line.get("seconds_to_fill_mean"),
        faults=faults, detail=detail,
    )


def _weekly_card(report: str, today, gate=None):
    """The Friday report as a card: account value and the week beside SPY's,
    the full attribution report attached (ruling 2026-10-12); the judged
    sleeve's idle cash (human ruling 2026-10-14: research is off for Form 4,
    congressional and 8-K, so it mostly sits idle until a reallocation ruling)."""
    value = week = spy_week = None
    start = today - timedelta(days=7)
    try:
        with AlpacaAdapter() as main:
            value = float(main.account_snapshot().get("equity") or 0) or None
            history = main.portfolio_history(start.isoformat())
        equities = [float(e) for e in history.get("equity") or [] if e]
        if len(equities) >= 2 and equities[0] > 0:
            week = equities[-1] / equities[0] - 1
        spy_week = BenchmarkMoves().between(start, today)
    except Exception:  # noqa: BLE001 - a missing number is "n/a", never a lost report
        pass
    judged = None
    if gate is not None:
        from risk_gate.state import Sleeve

        allocation = float(gate.sleeve_nav(Sleeve.EQUITY))
        deployed = float(gate.state.sleeve_exposure(Sleeve.EQUITY))
        judged = (allocation, deployed)
    return cards.weekly_main_card(value, week, spy_week, report, judged=judged)


def _momentum_card(checks, daily: bool = False):
    """The momentum sleeve's weekly card, or None before its first plan. A
    failure is a yellow card, never a lost main report."""
    from momentum import feeds
    from orchestrator import momentum_report

    try:
        alpaca = feeds.Alpaca(feeds.Cache(datasafety.scratch_data_dir() / "momentum-weekly-cache"))
        report = momentum_report.gather(checks.audit.path.parent / "momentum", checks.audit.trails(),
                                        checks.clock().date(), alpaca)
    except Exception as error:  # noqa: BLE001
        return cards.Card("Momentum", "daily report" if daily else "weekly report", cards.LOOK,
                          ("The momentum report could not be built.",),
                          look_at=f"the momentum report failed: {type(error).__name__}", detail=str(error))
    if report is None:
        return None
    return cards.momentum_daily_card(report) if daily else cards.momentum_week_card(report)


def _momentum_brief_files(directory: Path, cache_root: Path, config, latest: Optional[dict], now: datetime,
                          alpaca, build, log) -> None:
    """The momentum side of the AI trader's brief (human ruling 2026-10-15),
    written by the prep unit and read by the lab as data:

    * ``shortlist.json``, weekly and with each monthly plan (ruling 2026-10-15,
      second): a SATURDAY run rebuilds it from fresh market data through the
      week's last session, on the universe of the next session's rebalance
      version; any other run writes it for the latest plan when the file on
      disk is older than that plan's data (built from the plan's own market
      data, so it ranks exactly as the plan did);
    * one ``daily.jsonl`` line per session: every book's today / week /
      since-start return - the same numbers as the daily card. The evening
      run records the day; a morning run records the previous session if its
      line is missing (a catch-up, so the first scan still sees it).

    Reference data only: a failure here is logged, never a failed prep."""
    from datetime import date as _date, time as dtime

    from momentum import feeds
    from orchestrator import momentum_prep as prep_module
    from orchestrator import momentum_report

    local = now.astimezone(_NY)
    if local.weekday() == 5:
        _weekly_shortlist(directory, cache_root, config, local.date(), alpaca, build, log)
    elif latest and not prep_module.shortlist_covers(directory, latest["data_through"]):
        folder = cache_root / latest["data_through"]
        try:
            if not folder.is_dir():
                log.warning("momentum prep: no market data for the %s plan (%s); shortlist not written",
                            latest["month"], folder)
            else:
                first = _date.fromisoformat(latest["data_through"]).replace(day=1)
                store, _ = build(folder, start=first.replace(year=first.year - config.history_years).isoformat(),
                                 end=latest["data_through"], log=log.info,
                                 universe_version=config.universe_version(latest["rebalance_date"]))
                names = {a["symbol"]: a.get("name") or "" for a in feeds.Cache(folder).get("alpaca_assets") or []}
                prep_module.write_json(directory / prep_module.SHORTLIST_FILE, prep_module.shortlist(
                    store, latest["rebalance_date"], names=names))
                log.info("momentum prep: shortlist written for the %s plan", latest["month"])
                del store
        except Exception as error:  # noqa: BLE001
            log.warning("momentum prep: shortlist not written (%s: %s)", type(error).__name__, error)
    if latest is None:
        return
    try:
        today = local.date()
        sessions = alpaca.sessions((today - timedelta(days=12)).isoformat(), today.isoformat())
        if local.time() >= dtime(16, 30):
            day = today.isoformat() if sessions and sessions[-1] == today.isoformat() else None
        else:  # the morning catch-up: the last completed session
            done = [s for s in sessions if s < today.isoformat()]
            day = done[-1] if done else None
        if day is None or day < latest["rebalance_date"] or momentum_report.recorded(directory, day):
            return
        bars = feeds.Alpaca(feeds.Cache(datasafety.scratch_data_dir() / "momentum-weekly-cache"))
        report = momentum_report.gather(directory, AuditLog(directory.parent / "audit.jsonl").trails(),
                                        _date.fromisoformat(day), bars)
        if report is not None:
            momentum_report.record(directory, report, day)
            log.info("momentum prep: daily scoreboard line recorded for %s", day)
    except Exception as error:  # noqa: BLE001
        log.warning("momentum prep: daily scoreboard line not recorded (%s: %s)", type(error).__name__, error)


def _weekly_shortlist(directory: Path, cache_root: Path, config, today, alpaca, build, log) -> None:
    """The Saturday refresh of the AI trader's momentum shortlist: fresh bars
    and SEC facts through the week's last session, in their own cache folder
    (the newest weekly folder is kept). A failure is logged; the old file stays."""
    import shutil
    from datetime import date as _date

    from momentum import feeds
    from orchestrator import momentum_prep as prep_module

    try:
        past = alpaca.sessions((today - timedelta(days=12)).isoformat(), today.isoformat())
        ahead = alpaca.sessions((today + timedelta(days=1)).isoformat(), (today + timedelta(days=14)).isoformat())
        if not past or not ahead:
            log.warning("momentum prep: no sessions around %s; weekly shortlist not built", today)
            return
        data_through, next_day = past[-1], ahead[0]
        if prep_module.shortlist_covers(directory, data_through):
            log.info("momentum prep: the shortlist already covers %s", data_through)
            return
        folder = cache_root / f"weekly-{data_through}"
        first = _date.fromisoformat(data_through).replace(day=1)
        store, _ = build(folder, start=first.replace(year=first.year - config.history_years).isoformat(),
                         end=data_through, log=log.info, universe_version=config.universe_version(next_day))
        if store.calendar[-1] != data_through:
            log.warning("momentum prep: data ends %s, not %s; weekly shortlist not built", store.calendar[-1],
                        data_through)
            return
        names = {a["symbol"]: a.get("name") or "" for a in feeds.Cache(folder).get("alpaca_assets") or []}
        prep_module.write_json(directory / prep_module.SHORTLIST_FILE,
                               prep_module.shortlist(store, next_day, names=names))
        log.info("momentum prep: weekly shortlist written (data through %s)", data_through)
        del store
        for old in sorted(p for p in cache_root.iterdir() if p.is_dir() and p.name.startswith("weekly-"))[:-1]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception as error:  # noqa: BLE001 - reference data, never a failed prep
        log.warning("momentum prep: weekly shortlist not built (%s: %s)", type(error).__name__, error)


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
    momentum_card = _momentum_card(checks)
    if alerter.enabled and momentum_card is not None:
        alerter.card(f"momentum-weekly-{checks.clock().date()}", momentum_card)
    elif momentum_card is not None:
        print(momentum_card.detail)
    if alerter.enabled:
        sent = alerter.card(f"weekly-{checks.clock().date()}", _weekly_card(text, checks.clock().date(), checks.gate))
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
        bars = AlpacaDailyBars(unserved=_unserved_memo())
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
                # The JUDGED book only (ruling 2026-09-18): the beta this feeds
                # multiplies the judged return in the headline alpha line, so
                # the baseline sleeve's SPY (beta 1 by construction), the parked
                # T-bill ETF and the mechanical arm's slices are all excluded —
                # any of them in the weighting would launder beta into alpha.
                if trail.decision.sizing.strategy in (
                    "mechanical",
                    "cash_sweep",
                    "baseline",
                    "execution_test",
                    "momentum",
                ):
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
    # Deployment is VERDICT-limited (rulings 2026-10-06): the weekly says so in
    # numbers — passes, long verdicts, approvals, long fates — right under the
    # headline, so a thin book is never read as a sizing or budget problem.
    try:
        from orchestrator.ops import deployment_line

        sections.append(deployment_line(checks))
    except Exception as error:  # noqa: BLE001 - a line must never sink the report
        sections.append(f"judged deployment line unavailable: {error}")
    # Self-consistency votes (ruling 2026-10-07): held, overturned by direction
    # and source, extra passes and dollars - the window is the report's.
    try:
        from orchestrator.vote_report import vote_lines

        sections.append("\n".join(vote_lines(checks.audit.records(), window_start)))
    except Exception as error:  # noqa: BLE001
        sections.append(f"self-consistency vote lines unavailable: {error}")

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
    entries = None
    rows = None
    if bars is not None:
        try:
            from forward import (
                ForwardReturns,
                funnel_entries,
                render_forward_report,
                wanted_pairs,
            )

            aliases = {}
            for klass in checks.signals_config.classes.values():
                for source in klass.sources:
                    aliases.update(source.filer_aliases)
            entries = funnel_entries(checks.audit.records(), filer_aliases=aliases)
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

    # Feed spend against what it bought (requested 2026-09-28 for the
    # 2026-10-15 feed-spend ruling, which rules on spend alone; the
    # congressional signal-quality verdict moved to 2026-10-27, ruling
    # 2026-09-29): per source, the subscription, the research it caused, the
    # candidates and passes, the trades it produced and what they realised,
    # the next 90 days' cost, with the source's forward excess when the rows
    # above exist.
    try:
        sections.append(_feed_spend_section(checks, generated_at, entries, rows))
    except Exception as error:  # noqa: BLE001 - the table must never sink the report
        sections.append(f"feed spend table unavailable: {error}")
    return "\n\n".join(sections)


def _feed_spend_section(checks, generated_at, entries=None, rows=None) -> str:
    from audit.spend import feed_spend_rows, render_feed_spend, split_callers_by_trial
    from forward import source_excess_summary

    config = checks.signals_config
    sources = [
        (source.id, key, source.monthly_cost)
        for key, klass in config.classes.items()
        for source in klass.sources
    ]
    # Billed since each source's start date: a window start older than any
    # feed makes feed_cost_breakdown bill from start_date alone.
    since_inception = generated_at - timedelta(days=400)
    feed_to_date = {
        row.source_id: row.cost
        for row in config.feed_cost_breakdown(since_inception, generated_at)
    }
    forward = source_excess_summary(entries, rows) if entries is not None and rows else None
    start_dates = {
        source.id: source.start_date
        for klass in config.classes.values()
        for source in klass.sources
    }
    table = feed_spend_rows(
        checks.audit.records(),
        checks.audit.trails(),
        sources,
        feed_to_date,
        forward,
        start_dates=start_dates,
        as_of=generated_at,
    )
    # The X-fed trade-callers as one line (scheduling ruling 2026-09-29): the
    # accounts whose only platform is X and which are not a mirror of a
    # principal. Their $ lines carry the X pay-per-use meter that also
    # delivers the Trump mirrors — cutting a caller does not cut the meter.
    callers = [
        (source.id, source.start_date)
        for klass in config.classes.values()
        for source in klass.sources
        if tuple(source.platforms) == ("x",) and source.type != "mirror"
    ]
    # A caller (re)wired after the table was requested has no trial inside
    # the ruling's window: its own line, never summed into the finding
    # (human ruling 2026-09-30, citrini).
    graded, untried = split_callers_by_trial(callers)
    groups = {}
    if graded:
        groups["X-fed callers (2026-10-15 feed-spend ruling)"] = graded
    if untried:
        groups[
            "X-fed callers re-wired after the ruling was requested — NO trial, "
            "NOT graded 2026-10-15"
        ] = untried
    return render_feed_spend(table, generated_at, groups or None)


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

        python -m orchestrator golden [--only name] [--limit N] [--single-pass]

    A tradeable verdict inside the boundary-confirmation band buys the
    PRODUCTION second pass (2026-09-17) and the case grades on what would size;
    ``--single-pass`` restores the one-pass replay for variance measurement.
    """
    logging.basicConfig(level=logging.WARNING)
    from execution.environment import load_environment as _load_env

    from orchestrator.config import OrchestratorConfig
    from orchestrator.golden import (
        BoundaryBand,
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
    single_pass = False
    index = 0
    while index < len(args):
        if args[index] == "--only" and index + 1 < len(args):
            only = args[index + 1]
            index += 2
        elif args[index] == "--limit" and index + 1 < len(args):
            limit = int(args[index + 1])
            index += 2
        elif args[index] == "--single-pass":
            single_pass = True
            index += 1
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
        band = None
        vote_config = OrchestratorConfig.load().self_consistency
        if vote_config.enabled and not single_pass:
            band = BoundaryBand(
                floor=RiskLimits.load().sizing.no_trade_below,
                k=vote_config.k,
                margin=vote_config.margin,
                thresholds=tuple(vote_config.thresholds),
            )
        reviews = sum(1 for case in cases if case.kind == "review")
        print(
            f"replaying {len(cases)} golden cases ({reviews} review) through the "
            f"production passes (model {config.model}, screen "
            f"{config.screen.model if config.screen else 'off'}"
            + (
                f", self-consistency vote ({band.describe()})"
                if band is not None
                else ", single pass"
            )
            + ") — real API spend"
        )
        results = run_golden(
            research, cases, review_pass=ExitReviewPass(client), band=band
        )
        print(render_summary(results, band))
        return 0 if all(result.passed for result in results) else 3
    except Exception as error:  # noqa: BLE001
        print(f"GOLDEN REPLAY FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


def priors() -> int:
    """Ground the dispatch priors in the forward-return engine (ruling
    2026-09-18, revised step 2). READ-ONLY by default: prints every slice's
    measured mean excess with n and whether it clears min_n. ``--write``
    rewrites config/dispatch_priors.yaml with grounded slices replaced by their
    measurement and everything else left as ruled - a human commits it. Uses the
    cached forward rows (the weekly report refreshes them); ``--refresh`` fetches
    marks first (Alpaca bars, minutes).

        python -m orchestrator priors [--write] [--refresh]
    """
    import yaml as _yaml

    from orchestrator.scoring import (
        PRIORS_PATH,
        DispatchPriors,
        compute_slice_stats,
        merged_priors_mapping,
        render_slice_stats,
    )
    from orchestrator.whatif import load_cached_rows

    args = sys.argv[2:]
    write = "--write" in args
    refresh = "--refresh" in args
    audit = AuditLog(default_data_dir() / "audit.jsonl")
    records = list(audit.records())
    rows = load_cached_rows(audit.path.parent / "forward_returns.jsonl")
    if refresh:
        from execution.environment import load_environment as _load_env
        from execution.market_data import AlpacaDailyBars
        from forward import ForwardReturns, funnel_entries, wanted_pairs

        _load_env()
        bars = AlpacaDailyBars(feed="sip")
        engine = ForwardReturns(bars.bars, audit.path.parent / "forward_returns.jsonl")
        rows = engine.rows_for(wanted_pairs(funnel_entries(records)))
    current = DispatchPriors.load()
    stats = compute_slice_stats(records, rows, current.horizon_days)
    print(render_slice_stats(stats, current))
    if write:
        raw = _yaml.safe_load(PRIORS_PATH.read_text(encoding="utf-8")) or {}
        merged = merged_priors_mapping(
            raw, stats, datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        header = "\n".join(
            line for line in PRIORS_PATH.read_text(encoding="utf-8").splitlines() if line.startswith("#")
        )
        PRIORS_PATH.write_text(
            header + "\n" + _yaml.safe_dump(merged, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        grounded = sum(
            1 for s in stats if s.facet and s.grounded(current.min_n)
        )
        print(f"\nwrote {PRIORS_PATH} - {grounded} slice(s) grounded; review and commit")
    return 0


def dispatch_whatif() -> int:
    """What a day's research slots WOULD have gone to under global scoring
    versus what they went to (ruling 2026-09-18, revised step 2, item 3).
    READ-ONLY.

        python -m orchestrator dispatch-whatif --day 2026-09-18
    """
    from datetime import date

    from orchestrator.config import OrchestratorConfig
    from orchestrator.scoring import DispatchPriors, whatif_dispatch
    from orchestrator.whatif import load_cached_rows
    from signals import SignalsConfig

    args = sys.argv[2:]
    day = date.today()
    if "--day" in args:
        day = date.fromisoformat(args[args.index("--day") + 1])
    audit = AuditLog(default_data_dir() / "audit.jsonl")
    signals_config = SignalsConfig.load()
    orchestrator_config = OrchestratorConfig.load()
    source_caps = {
        source.id: source.daily_research_cap
        for klass in signals_config.classes.values()
        for source in klass.sources
        if source.daily_research_cap is not None
    }
    print(
        whatif_dispatch(
            audit.records(),
            day,
            DispatchPriors.load(),
            source_caps,
            orchestrator_config.research_class_caps,
            load_cached_rows(audit.path.parent / "forward_returns.jsonl"),
        )
    )
    return 0


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
    # Delivery outcomes go to a status file health reads (ruling 2026-09-28).
    alerter = Alerter(status_path=data_dir / "alerts_status.json")

    def observe(event: str, detail: str) -> None:
        """Run-log events that must reach a phone (errors, cost tripwires,
        billing anomalies, a mechanical breaker trip, an operator halt) and
        the execution test's daily report - each as a plain-English card
        (ruling 2026-10-12)."""
        when = _now_et()
        if event == "ERROR" and detail.startswith("DATASAFETY "):
            kind, _, message = detail[len("DATASAFETY "):].partition(": ")
            alerter.card(f"guard:{detail[:40]}", cards.guard_card("Main book", "REFUSED" in kind, when, message))
        elif event == "ERROR":
            alerter.card(f"ERROR:{detail[:40]}", cards.error_card("Main book", when, detail))
        elif event == "COST":
            alerter.card(f"COST:{detail[:40]}", cards.cost_card("Main book", when, detail))
        elif event == "READS":
            alerter.card(f"READS:{detail[:40]}", cards.billing_card("Main book", when, detail))
        elif event == "MECH" and "BREAKER" in detail.upper():
            alerter.card("mech_breaker", cards.breaker_card(detail))
        elif event == "HALT":
            alerter.card("operator_halt_loop", cards.halt_card("Main book", detail))
        elif "EXEC-TEST daily " in detail:
            card = _exec_test_card(detail.split("EXEC-TEST daily ", 1)[1])
            if card is not None:
                alerter.card(f"exec-test-{card.lines[0]}", card)

    run_log = RunLog(data_dir / "run.log", observer=observe)
    # The data guard's production events (a refused write, or a guard error
    # that failed open) ride the run log's ERROR line, so both reach the same
    # urgent alert and the same health view (ruling 2026-10-10, item 4).
    datasafety.set_alert_sink(lambda kind, message: run_log.note("ERROR", f"DATASAFETY {kind}: {message}"))

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
    contracts = None
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
        sip_bars = AlpacaDailyBars(feed="sip", unserved=_unserved_memo())
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
        # Item-filtered 8-Ks (human ruling 2026-09-15): Class 1 cadence, the
        # fetcher self-throttles to the FTS index's own refresh pace.
        form8k = Form8KFetcher(seen=seen_for("form_8k"))
        # Government contract awards (human ruling 2026-10-06): the DoD digest
        # through the reader proxy (+ the evening one-shot's pending file) and
        # the FPDS civilian feed, behind one callable. None when unconfigured.
        contracts = build_awards_fetcher(
            checks.signals_config,
            seen=seen_for("gov_contract_awards"),
            pending_path=pending_path_for(default_data_dir()),
        )
        # Attention momentum (risk-on redirect, human ruling 2026-10-08, item
        # 6f): a once-a-morning market-data confirmation screen over this
        # system's own recorded events, for the aggressive sleeve. SIP bars,
        # batched. None when the sleeve or the screen is switched off.
        from orchestrator.attention import AttentionMomentumFetcher

        aggressive_config = checks.orchestrator_config.aggressive_sleeve
        attention = (
            AttentionMomentumFetcher(
                records=checks.audit.records,
                bars_many=sip_bars.bars_many,
                config=aggressive_config.attention,
                clock=checks.clock,
                seen=seen_for("attention_momentum"),
            )
            if aggressive_config.enabled and aggressive_config.attention.enabled
            else None
        )
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
                # Item-filtered 8-Ks (human ruling 2026-09-15): Class 1.
                "form_8k": logged("form_8k", form8k),
                # Government contract awards (human ruling 2026-10-06): Class 1,
                # measurement-first; routed only when the source is configured.
                **(
                    {"gov_contract_awards": logged("gov_contract_awards", contracts.fetcher)}
                    if contracts is not None
                    else {}
                ),
                # Attention momentum (human ruling 2026-10-08): Class 2 cadence,
                # self-throttled to once per session day.
                **(
                    {"attention_momentum": logged("attention_momentum", attention)}
                    if attention is not None
                    else {}
                ),
                "nolimitgains": logged("nolimitgains", x_search),
                # Options-flow free taste (human-authorized 2026-08-25).
                "unusual_whales": logged("unusual_whales", x_search),
                # First probation source (human ruling 2026-08-25): researched
                # and credibility-tracked, sized to zero until promoted.
                "optionshawk": logged("optionshawk", x_search),
                # The Trump leg, decided 2026-08-18: X mirror accounts, not the
                # Truth API — the upgrade decision belongs to attribution data.
                # Signals attribute to trump_posts; the audit record keeps the
                # deliverer.
                "trump_mirror_ttox": logged("trump_mirror_ttox", x_search),
                "trump_mirror_tdp": logged("trump_mirror_tdp", x_search),
            },
            # The original account is not polled directly; its content arrives via
            # the mirror sources above. The attention screen is declared unbuilt
            # when the aggressive sleeve or the screen is switched off, so the
            # source in signals.yaml always has a wiring decision.
            unbuilt={"trump_posts"} | ({"attention_momentum"} if attention is None else set()),
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
        daily_bars = AlpacaDailyBars(unserved=_unserved_memo())
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
            alerter.card("unmanaged", cards.unmanaged_card(
                [f"{s} ({q} held)" for s, q in sorted(unmanaged.items())]))
        still_pending = pending_settlement(checks.audit)
        if still_pending:
            alerter.card("pending_settlement", cards.pending_card(
                len(still_pending), "\n".join(str(item) for item in still_pending)))

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
                _, day_return = _main_day(checks.adapter)
                alerter.card("kill_switch", cards.kill_switch_card(
                    "Main book", float(state.nav), float(state.high_water_mark), day_return, _spy_today(),
                    "python -m orchestrator resume (see ops/EMERGENCY.md)"))
            if tick.exits_started:
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                alerter.card(f"exits-{stamp}", cards.exits_card(tick.exits_started, tick.positions_closed))
            if tick.traded and not first_judged_alerted:
                first_judged_alerted = True
                alerter.card(f"first-judged-{now.date()}", cards.first_entry_card("judged", tick.traded))
            if tick.mechanical_entries and not first_mechanical_alerted:
                first_mechanical_alerted = True
                alerter.card(f"first-mechanical-{now.date()}",
                             cards.first_entry_card("mechanical copy-trading", tick.mechanical_entries))
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
        value, day_return = _main_day(checks.adapter)
        alerter.card(f"close-{now.date()}", cards.close_card(
            value=value if value is not None else float(state.nav), today=day_return, spy_today=_spy_today(),
            drawdown=float(state.drawdown()), positions=len(held),
            spend=float(spend) if spend is not None else None, halted=report.halted,
            detail="\n".join([
                f"Account value: ${state.nav}; peak ${state.high_water_mark}",
                f"Kill switch: {'ON' if report.halted else 'off'}",
                f"Orders settled or released at the close: {report.settled}; positions closed: "
                f"{report.positions_closed}",
                "Research spend is an estimate from token prices; the provider's bill is the truth.",
                f"Positions held ({len(held)}):", *[f"  {h}" for h in held]]),
        ))
        # the momentum sleeve's daily line (standing rule 2026-10-14), once it has a plan
        momentum_card = _momentum_card(checks, daily=True)
        if momentum_card is not None:
            alerter.card(f"momentum-daily-{now.date()}", momentum_card)
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
        if contracts is not None:
            contracts.close()
        # Let queued alerts drain before the process exits — bounded, so a dead
        # SMTP host cannot hold the shutdown hostage.
        alerter.close()
        lock.release()


def _unserved_memo():
    """The cross-run memo of symbols the data API refuses (ruling 2026-09-04):
    one 400 per symbol, ever, instead of one per report."""
    from execution.market_data import UnservedSymbols

    return UnservedSymbols(default_data_dir() / "unserved_symbols.json")


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
        if p.quantity > 0
        and p.sleeve in (Sleeve.EQUITY, Sleeve.MECHANICAL, Sleeve.BASELINE)
    ]
    held = {
        sleeve: sum((p.market_value for p in positions if p.sleeve == sleeve), Decimal("0"))
        for sleeve in ("equity", "mechanical", "baseline")
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
    # which is cash in another form — rides flat in the total. The baseline
    # sleeve's index ETF (ruling 2026-09-18) is replayed like any position: it
    # is the book's market beta and a stress window must show it falling.
    other = state.nav - sum(held.values()) - equity_cash - mechanical_cash
    bars = AlpacaDailyBars(feed="sip", unserved=_unserved_memo())

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
            {"equity": equity_cash, "mechanical": mechanical_cash, "baseline": Decimal("0")},
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
    bars = AlpacaDailyBars(feed="sip", unserved=_unserved_memo())
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


def contracts_evening() -> int:
    """Government contract awards evening one-shot (human ruling 2026-10-06):
    read today's DoD digest at 17:05 ET while it is fresh, write the free
    measurement rows now, stash the research-tier awards for the next
    session's first poll. No LLM, no order.

        python -m orchestrator contracts-evening
    """
    import uuid as _uuid

    from orchestrator.contracts_feed import run_evening

    logging.basicConfig(level=logging.WARNING)
    checks = preflight()
    stack = build_awards_fetcher(
        checks.signals_config,
        seen=(),
        pending_path=pending_path_for(checks.audit.path.parent),
        clock=checks.clock,
    )
    if stack is None:
        print("gov_contract_awards is not configured in signals.yaml; nothing to do")
        return 0
    from audit.records import DecisionRecord as _Decision, StageRejectionRecord as _Stage

    recorded = {
        record.signal.external_id
        for record in checks.audit.records()
        if isinstance(record, (_Decision, _Stage))
        and record.signal.source_id == "gov_contract_awards"
        and record.signal.external_id
    }
    try:
        report = run_evening(
            stack=stack,
            config=checks.signals_config,
            audit=checks.audit,
            id_factory=lambda: _uuid.uuid4().hex[:16],
            clock=checks.clock,
            recorded_external_ids=recorded,
        )
    finally:
        stack.close()
    print(report.render())
    return 0


def contracts_backtest() -> int:
    """The pre-registered contract-awards backtest (SESSION_NOTES 2026-10-06)
    over harvested digest HTML, through the production parser, with
    point-in-time market caps and the open split. Read-only, offline except
    bars and SEC facts.

        python -m orchestrator contracts-backtest --digests DIR [--out report.json]
    """
    from orchestrator.contracts_backtest import run_backtest

    logging.basicConfig(level=logging.WARNING)
    args = sys.argv[2:]
    digests = None
    out = None
    index = 0
    while index < len(args):
        if args[index] == "--digests" and index + 1 < len(args):
            digests = args[index + 1]
            index += 2
        elif args[index] == "--out" and index + 1 < len(args):
            out = args[index + 1]
            index += 2
        else:
            print(f"unknown argument {args[index]!r}", file=sys.stderr)
            return 2
    if digests is None:
        print("usage: python -m orchestrator contracts-backtest --digests DIR [--out report.json]", file=sys.stderr)
        return 2
    from pathlib import Path as _Path

    bars = AlpacaDailyBars(feed="sip")
    try:
        text = run_backtest(_Path(digests), bars=bars.bars, out_path=_Path(out) if out else None)
    finally:
        bars.close()
    print(text)
    return 0


#: Emergency operator commands (ops/EMERGENCY.md): a human at a shell must be
#: able to halt and resume production. They alone declare production writes
#: outside a production unit (ruling 2026-10-09, item 4).
def momentum_prep() -> int:
    """The momentum book's monthly prep (human ruling 2026-10-13). Run by its
    own unit; does nothing unless a month is turning (see momentum_prep.due).

        python -m orchestrator momentum-prep
        python -m orchestrator momentum-prep --rebalance-date YYYY-MM-DD [--force] [--no-llm] [--no-card]

    Outside a production unit every write goes to scratch (a dry run)."""
    import shutil
    from datetime import date as _date

    from audit.log import default_data_dir
    from execution import cards
    from momentum import feeds
    from momentum.store import build
    from orchestrator import momentum_prep as prep_module
    from orchestrator.config import OrchestratorConfig
    from orchestrator.momentum_sleeve import plan_path
    from orchestrator.momentum_veto import Reviewer, review_client
    from research.config import ResearchConfig

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("orchestrator.momentum_prep")
    args = sys.argv[2:]
    force, use_llm, send_card = "--force" in args, "--no-llm" not in args, "--no-card" not in args
    wanted = None
    if "--rebalance-date" in args:
        wanted = args[args.index("--rebalance-date") + 1]
    config = OrchestratorConfig.load().momentum_prep
    cache_root = Path(os.path.expanduser(config.cache_dir))
    directory = default_data_dir() / "momentum"
    now = datetime.now(timezone.utc)
    when = now.astimezone(_NY).strftime("%Y-%m-%d %H:%M ET")
    # .env first: the alerter reads its webhooks from the environment, and a
    # prep that fails must be able to say so (2026-10-14: built before the
    # environment, it was silently disabled).
    load_environment()
    alerter = Alerter() if send_card else None

    def fail(what: str, detail: str = "") -> int:
        log.error("momentum prep failed: %s", what)
        if alerter is not None and alerter.enabled:
            alerter.card(f"momentum-prep-{now.date()}", cards.momentum_prep_failed_card(when, what, detail))
            alerter.close()
        return 1

    try:
        alpaca = feeds.Alpaca(feeds.Cache(cache_root / "calendar"))
        today = now.astimezone(_NY).date()
        # A shadow declared after the latest plan starts at the next session,
        # not the next month (standing rule 2026-10-14): backfill it first,
        # whatever else this run does. The plan is never touched.
        latest = prep_module.latest_plan(directory) if wanted is None else None
        missing = prep_module.missing_shadows(directory, latest["month"]) if latest else []
        if missing:
            near = alpaca.sessions((today - timedelta(days=12)).isoformat(), (today + timedelta(days=12)).isoformat())
            begin = prep_module.shadow_start(latest, now, near)
            if begin is None:
                log.warning("momentum prep: cannot place the start of %s; not backfilled", missing)
            else:
                first = _date.fromisoformat(begin.data_through).replace(day=1)
                # the shadows rank on the universe their plan traded on
                shadow_store, _ = build(cache_root / begin.data_through,
                                       start=first.replace(year=first.year - config.history_years).isoformat(),
                                       end=begin.data_through, log=log.info,
                                       universe_version=config.universe_version(latest["rebalance_date"]))
                if shadow_store.calendar[-1] != begin.data_through:
                    log.warning("momentum prep: data ends %s, not %s; shadows %s not backfilled",
                                shadow_store.calendar[-1], begin.data_through, missing)
                else:
                    added = prep_module.backfill_shadows(shadow_store, begin.rebalance_day, missing, directory,
                                                         latest["month"])
                    log.info("momentum prep: shadow(s) %s tracked from %s (data through %s)", added,
                             begin.rebalance_day, begin.data_through)
                del shadow_store
        if wanted is None:
            _momentum_brief_files(directory, cache_root, config, latest, now, alpaca, build, log)
        if wanted is not None:
            sessions = alpaca.sessions((_date.fromisoformat(wanted) - timedelta(days=12)).isoformat(), wanted)
            if not sessions or sessions[-1] != wanted or len(sessions) < 2:
                return fail(f"{wanted} is not a trading day")
            target = prep_module.Due(rebalance_day=wanted, data_through=sessions[-2])
        elif (config.off_cycle_rebalance_date is not None and today <= config.off_cycle_rebalance_date
              and not plan_path(directory, config.off_cycle_rebalance_date.isoformat()[:7]).exists()):
            # the one-time off-cycle plan (ruling 2026-10-14): this session,
            # from the prior close; the monthly schedule is untouched
            off = config.off_cycle_rebalance_date
            sessions = alpaca.sessions((off - timedelta(days=12)).isoformat(), off.isoformat())
            if not sessions or sessions[-1] != off.isoformat() or len(sessions) < 2:
                return fail(f"off-cycle date {off} is not a trading day")
            target = prep_module.Due(rebalance_day=off.isoformat(), data_through=sessions[-2])
            log.info("momentum prep: OFF-CYCLE plan for %s (one-time, ruling 2026-10-14)", off)
        else:
            sessions = alpaca.sessions((today - timedelta(days=12)).isoformat(),
                                       (today + timedelta(days=12)).isoformat())
            target = prep_module.due(now, sessions, evening_after=config.evening_after,
                                     morning_before=config.morning_before)
            if target is None:
                log.info("momentum prep: no month is turning now; nothing to do")
                return 0
        if plan_path(directory, target.rebalance_day[:7]).exists() and not force:
            log.info("momentum prep: the %s plan already exists; nothing to do", target.rebalance_day[:7])
            return 0
        log.info("momentum prep for %s, data through %s", target.rebalance_day, target.data_through)

        start = _date.fromisoformat(target.data_through).replace(day=1)
        start = start.replace(year=start.year - config.history_years).isoformat()
        folder = cache_root / target.data_through
        store, stats = build(folder, start=start, end=target.data_through, log=log.info,
                             universe_version=config.universe_version(target.rebalance_day))
        if store.calendar[-1] != target.data_through:
            return fail(f"the market data ends {store.calendar[-1]}, not {target.data_through}; "
                        f"rerun once the day's bars are published")
        held = sorted(s for s, (qty, _) in AuditLog(directory.parent / "audit.jsonl")
                      .strategy_open_positions("momentum").items() if qty > 0)
        shadow_held, x_pick = prep_module.read_shadow_state(directory)
        names = {a["symbol"]: a.get("name") or "" for a in feeds.Cache(folder).get("alpaca_assets") or []}
        reviewer = None
        if use_llm and config.max_reviews > 0:
            client = review_client(ResearchConfig.load(), max_searches=config.review_max_searches,
                                   search_tool=config.review_search_tool,
                                   timeout_seconds=config.review_timeout_seconds)
            reviewer = Reviewer(client,
                                cap_usd=config.review_cap_usd, reserve_usd=config.review_reserve_usd,
                                search_fee_usd=Decimal("0.01") * config.review_max_searches)
        result = prep_module.compute(
            store, target.rebalance_day, held,
            review=reviewer.review if reviewer is not None else None,
            shadow_held=shadow_held, x_pick=x_pick, names=names, max_reviews=config.max_reviews,
        )
        result.detail["data_stats"] = stats
        for what, label in (("sec_failures", "SEC fetches"), ("fallback_failures", "Finnhub market-cap fetches")):
            if stats.get(what):
                result.problems.append(f"{stats[what]} {label} failed after retries; those companies may be "
                                       f"missing from the universe this month")
        if reviewer is not None:
            result.detail["review_spend_usd"] = str(reviewer.spent)
            result.detail["review_passes"] = reviewer.passes
        path = prep_module.write(directory, result)
        log.info("momentum plan written: %s (%d names)", path, len(result.plan["targets"]))
        try:  # the AI trader's brief (ruling 2026-10-15): reference data, never a reason to fail the plan
            prep_module.write_json(directory / prep_module.SHORTLIST_FILE, prep_module.shortlist(
                store, target.rebalance_day, names=names))
        except Exception as error:  # noqa: BLE001
            log.warning("momentum prep: shortlist not written (%s: %s)", type(error).__name__, error)
        for problem in result.problems:
            log.warning("momentum prep: %s", problem)
        # keep this run's market data and the one before it
        for old in sorted(p for p in cache_root.iterdir() if p.is_dir() and p.name[:2] == "20")[:-2]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception as error:  # noqa: BLE001 - a failed prep is a red card, not a silent month
        import traceback

        return fail(f"{type(error).__name__}: {error}", traceback.format_exc())

    if alerter is not None and alerter.enabled:
        plan, detail = result.plan, result.detail
        month_name = _date.fromisoformat(plan["rebalance_date"]).strftime("%B")
        # the sleeve's size, for the card: book equity x the momentum weight
        sleeve_value = sleeve_weight = None
        try:
            sleeve_weight = float(RiskLimits.load().portfolio.sleeves.momentum)
            with AlpacaAdapter() as main_account:
                sleeve_value = float(main_account.account_snapshot().get("equity") or 0) * sleeve_weight or None
        except Exception:  # noqa: BLE001 - a missing figure is "n/a" on the card, never a lost card
            pass
        scores = {row["symbol"]: row.get("score") for row in detail.get("ranking", [])}
        alerter.card(f"momentum-plan-{plan['rebalance_date']}", cards.momentum_plan_card(
            month_name, plan["rebalance_date"], len(plan["targets"]), detail["selection"]["bought"],
            [s for s, _ in detail["selection"]["sold"]] + [s for s in plan["sold"]
                                                            if s not in {x for x, _ in detail["selection"]["sold"]}],
            [(s, why.replace("_", " ")) for s, why in plan["vetoed"].items()],
            float(plan["exposure"]), plan["buys_allowed"], result.problems,
            float(reviewer.spent) if reviewer is not None else None, float(config.review_cap_usd),
            _plan_attachment(plan, detail),
            scores=scores, sleeve_value=sleeve_value, sleeve_weight=sleeve_weight,
            off_cycle=config.off_cycle_rebalance_date is not None
            and plan["rebalance_date"] == config.off_cycle_rebalance_date.isoformat(),
        ))
        alerter.close()
    return 0


def _plan_attachment(plan: dict, detail: dict) -> str:
    """The plan card's attachment: a readable table first, the raw JSON after."""
    scores = {row["symbol"]: row for row in detail.get("ranking", [])}
    lines = [f"MOMENTUM PLAN {plan['rebalance_date']} (spec {plan['spec']}, data through {plan['data_through']})",
             f"exposure {plan['exposure']}  buys allowed {plan['buys_allowed']}", "",
             f"{'rank':>4}  {'symbol':8} {'weight':>8} {'score':>7} {'residual z':>10} {'earnings z':>10}  sector"]
    for target in plan["targets"]:
        row = scores.get(target["symbol"], {})
        fmt = lambda x: "-" if x is None else f"{x:+.2f}"  # noqa: E731
        lines.append(f"{target.get('rank') or '-':>4}  {target['symbol']:8} {float(target['weight']):>8.2%} "
                     f"{fmt(row.get('score')):>7} {fmt(row.get('residual_z')):>10} {fmt(row.get('earnings_z')):>10}  "
                     f"{target.get('sector', '')}")
    lines += ["", "Vetoes:"] + [f"  {s}: {why}" for s, why in plan["vetoed"].items()] + (
        [] if plan["vetoed"] else ["  none"])
    lines += ["", "Reviews (sentiment logged only):"]
    for review in detail.get("reviews", []):
        lines.append(f"  {review['symbol']:8} {'NEW ' if review['new_entrant'] else 'held'} veto={review['applies']} "
                     f"{review['category']:22} sentiment {review['sentiment_6m']}  {review.get('fault') or ''}")
    lines += ["", "Skipped by v1's rules (top of the ranking):"] + [
        f"  {s}: {why}" for s, why in detail["selection"]["skipped"][:20]]
    lines += ["", "Problems:"] + [f"  {p}" for p in detail.get("problems", [])] + (
        [] if detail.get("problems") else ["  none"])
    return "\n".join(lines) + "\n\nRAW\n" + json.dumps({"plan": plan, "detail": detail}, indent=2, default=str)


def momentum_fills() -> int:
    """Fills vs plan for a momentum rebalance day (ruling 2026-10-14).
    Read-only. Exit 1 while any plan name is under 90% filled.

        python -m orchestrator momentum-fills [--date YYYY-MM-DD]"""
    from datetime import date as _date

    from orchestrator import momentum_fills as fills_module

    logging.basicConfig(level=logging.WARNING)
    args = sys.argv[2:]
    day = (_date.fromisoformat(args[args.index("--date") + 1]) if "--date" in args
           else datetime.now(timezone.utc).astimezone(_NY).date())
    audit = AuditLog(default_data_dir() / "audit.jsonl")
    complete, text = fills_module.report(audit, default_data_dir() / "momentum", day)
    print(text)
    return 0 if complete else 1


def momentum_veto_golden() -> int:
    """Golden replay of the momentum veto prompt through the production
    request path (ruling 2026-10-13). Spends real API dollars; writes nothing.

        python -m orchestrator momentum-veto-golden [--only NAME]"""
    from momentum import feeds
    from orchestrator import momentum_golden
    from orchestrator.momentum_veto import Reviewer, review_client
    from research.config import ResearchConfig

    logging.basicConfig(level=logging.WARNING)
    print(f"module: {momentum_golden.__file__}")
    args = sys.argv[2:]
    cases = momentum_golden.load_cases()
    if "--only" in args:
        cases = [c for c in cases if c["name"] == args[args.index("--only") + 1]]
    alpaca = feeds.Alpaca(feeds.Cache(datasafety.scratch_data_dir() / "momentum-golden-cache"))
    from orchestrator.config import OrchestratorConfig

    prep_config = OrchestratorConfig.load().momentum_prep
    searches = prep_config.review_max_searches
    client = review_client(ResearchConfig.load(), max_searches=searches, search_tool=prep_config.review_search_tool,
                           timeout_seconds=prep_config.review_timeout_seconds)
    reviewer = Reviewer(client, cap_usd=Decimal("3"),
                        reserve_usd=Decimal("0.40"), search_fee_usd=Decimal("0.01") * searches)
    outcomes = momentum_golden.run(cases, reviewer.review, momentum_golden.alpaca_closes(alpaca))
    print(momentum_golden.summary(outcomes, reviewer.spent))
    for outcome in outcomes:
        print(json.dumps(outcome.review.as_record()))
    return 0 if all(o.passed for o in outcomes) else 1


def momentum_roundtrip() -> int:
    """One share bought and sold through the momentum engine and gate path in
    the main paper account (ruling 2026-10-13). Run in session, 10:00-15:30 ET.
    Entries are switched on IN THIS PROCESS ONLY; the config is untouched.

        python -m orchestrator momentum-roundtrip [--symbol F]"""
    from orchestrator import momentum_roundtrip as roundtrip

    logging.basicConfig(level=logging.WARNING)
    print(f"module: {roundtrip.__file__}")
    args = sys.argv[2:]
    symbol = args[args.index("--symbol") + 1].upper() if "--symbol" in args else "F"
    shipped = RiskLimits.load()
    limits = shipped.model_copy(update={
        "momentum_sleeve": shipped.momentum_sleeve.model_copy(update={"entries_enabled": True})})
    checks = preflight(limits=limits)
    if not checks.paper:
        print("REFUSED: the round trip runs in paper only", file=sys.stderr)
        return 2
    prices = AlpacaPriceSource(
        feed=checks.orchestrator_config.market_data.feed,
        max_quote_age_seconds=checks.orchestrator_config.market_data.max_quote_age_seconds,
    )
    work = datasafety.scratch_data_dir() / "momentum-roundtrip" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result = roundtrip.run(checks, prices, symbol, work)
    for step in result.steps:
        print(step)
    print("ROUND TRIP PASSED" if result.passed else "ROUND TRIP FAILED")
    return 0 if result.passed else 1


def momentum_step2_report() -> int:
    """Momentum step 2's report (ruling 2026-10-13): REPORTS, applies nothing.
    Exit 0 when the execution test has finished and step 2 is ready to decide."""
    from orchestrator import momentum_step2

    logging.basicConfig(level=logging.WARNING)
    checks = preflight()
    sessions = None
    try:
        from momentum import feeds

        today = checks.clock().astimezone(_NY).date()
        alpaca = feeds.Alpaca(feeds.Cache(datasafety.scratch_data_dir() / "momentum-step2-cache"))
        sessions = alpaca.sessions(today.isoformat(), (today + timedelta(days=45)).isoformat())
    except Exception:  # noqa: BLE001 - without the calendar, the first weekday of next month stands
        pass
    result = momentum_step2.report(checks, checks.audit.path.parent / "execution_test_state.json",
                                   checks.clock().astimezone(_NY).date(), sessions)
    print(result.text)
    return 0 if result.ready else 1


def alerts_ledger() -> int:
    """Every card attempt from every process (ruling 2026-10-14, audit item
    4): the main book, each lab account and the scratch ledger (checks and
    one-off scripts), merged by time. Read-only.

        python -m orchestrator alerts [--days N]"""
    args = sys.argv[2:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else 3
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    production = datasafety.production_data_dir()
    paths = [datasafety.scratch_data_dir() / "alerts_ledger.jsonl"]
    if production is not None:
        paths.append(production / "alerts_ledger.jsonl")
        paths += sorted((production.parent / "data-lab").glob("*/alerts_ledger.jsonl"))
    rows = []
    for path in paths:
        if not path.exists():
            continue
        source = path.parent.name if path.parent.parent.name == "data-lab" else (
            "main" if production is not None and path.parent == production else "scratch")
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("at", "") >= since:
                rows.append((row["at"], source, row))
    for at, source, row in sorted(rows, key=lambda r: r[0]):
        status = "delivered" if row.get("delivered") else f"FAILED: {row.get('reason', '')}"
        print(f"{at}  {source:10s} {row.get('channel', ''):28s} {row.get('type', ''):32s} {status}  | {row.get('title', '')[:90]}")
    if not rows:
        print(f"no card attempts recorded in the last {days} days (the ledger starts 2026-10-14)")
    return 0


_NY = ZoneInfo("America/New_York")
OPERATOR_COMMANDS = frozenset({"halt", "resume"})


def _data_safety(command: str) -> Optional[int]:
    """Ruling 2026-10-09 (item 4): outside a production unit, `run` is refused
    (a session trading the broker from scratch state could double-trade), the
    operator commands write production by explicit declaration, and every
    other command reads a fresh snapshot of production data in the scratch
    directory and writes there. Returns an exit code to stop, or None."""
    import datasafety

    if datasafety.production_process():
        return None
    if command in OPERATOR_COMMANDS:
        datasafety.declare_operator(command)
        return None
    if command == "run":
        print(
            "REFUSED: `run` trades the broker and writes production state; it runs only "
            "inside the agentic-paper system unit (ruling 2026-10-09). This process is "
            "not a production unit.",
            file=sys.stderr,
        )
        return 2
    snapshot = datasafety.refresh_snapshot()
    print(
        f"[datasafety] not a production unit: reading a snapshot of production data "
        f"in {snapshot or datasafety.scratch_data_dir()}; nothing here writes production.",
        file=sys.stderr,
    )
    return None


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    stop = _data_safety(command)
    if stop is not None:
        return stop
    if command == "contracts-evening":
        return contracts_evening()
    if command == "contracts-backtest":
        return contracts_backtest()
    if command == "check":
        return check()
    if command == "health":
        return health()
    if command == "exec-test":
        return exec_test_report()
    if command == "run":
        return run()
    if command == "attribution":
        return attribution()
    if command == "weekly":
        return weekly()
    if command == "replay":
        return replay()
    if command == "priors":
        return priors()
    if command == "dispatch-whatif":
        return dispatch_whatif()
    if command == "golden":
        return golden()
    if command == "halt":
        return halt()
    if command == "resume":
        return resume()
    if command == "stress":
        return stress()
    if command == "momentum-fills":
        return momentum_fills()
    if command == "alerts":
        return alerts_ledger()
    if command == "momentum-step2-report":
        return momentum_step2_report()
    if command == "momentum-roundtrip":
        return momentum_roundtrip()
    if command == "momentum-veto-golden":
        return momentum_veto_golden()
    if command == "momentum-prep":
        return momentum_prep()
    if command == "overreaction":
        return overreaction()
    print(
        f"unknown command {command!r}: expected check, health, run, attribution, "
        f"weekly, replay, golden, halt, resume, stress, overreaction, "
        f"contracts-evening, or contracts-backtest",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
