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
import time
from datetime import datetime, timezone, timedelta

from audit.attribution import DEFAULT_WINDOW_DAYS, build_attribution
from audit.log import default_data_dir
from execution import AlpacaPriceSource
from signals import (
    Form13FFetcher,
    QuiverCongressFetcher,
    SignalClass,
    SourceRouter,
    XRecentSearchFetcher,
)

from orchestrator.bootstrap import preflight, start
from orchestrator.ops import (
    InstanceLock,
    RunLog,
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

    costs = {
        SignalClass(key): monthly
        for key, monthly in checks.signals_config.monthly_feed_costs().items()
    }
    generated_at = checks.clock()
    report = build_attribution(
        checks.audit.trails(),
        generated_at=generated_at,
        feed_costs=costs,
        research_costs=checks.audit.research_costs_by_class(
            generated_at - timedelta(days=DEFAULT_WINDOW_DAYS)
        ),
    )
    print(report.render())
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
    run_log = RunLog(data_dir / "run.log")

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
    quiver = None
    x_search = None
    prices = None
    try:
        if not is_trading_weekday(now):
            run_log.note("STOPPED", "not a trading weekday; nothing to do")
            return 0
        open_utc, close_utc = session_bounds(now)
        if now >= close_utc:
            run_log.note("STOPPED", "started after the close; nothing to do")
            return 0

        # Checks first — a misconfigured run should fail before it waits for a bell.
        checks = preflight()
        logger.info("startup state:\n%s", checks.describe())

        # Dedup seeded from the log: research already paid for is never re-bought.
        researched = checks.audit.researched_external_ids()

        def seen_for(source_id):
            return {eid for (sid, eid) in researched if sid == source_id}

        edgar = Form13FFetcher(seen=seen_for("form_13f"))
        quiver = QuiverCongressFetcher(seen=seen_for("congressional_disclosures"))
        x_search = XRecentSearchFetcher(
            seen=seen_for("nolimitgains"),
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
                "nolimitgains": logged("nolimitgains", x_search),
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

        prices = AlpacaPriceSource(
            feed=checks.orchestrator_config.market_data.feed,
            max_quote_age_seconds=(
                checks.orchestrator_config.market_data.max_quote_age_seconds
            ),
        )
        startup = start(fetcher=fetcher, prices=prices, checks=checks)
        loop = startup.loop

        while datetime.now(timezone.utc) < open_utc:
            time.sleep(min(30, (open_utc - datetime.now(timezone.utc)).total_seconds()))
        run_log.note("SESSION", f"open; trading until {close_utc.isoformat(timespec='seconds')}")

        interval = checks.orchestrator_config.tick_interval_seconds
        while datetime.now(timezone.utc) < close_utc:
            loop.tick()
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
        if edgar is not None:
            edgar.close()
        if quiver is not None:
            quiver.close()
        if x_search is not None:
            x_search.close()
        lock.release()


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
    print(
        f"unknown command {command!r}: expected 'check', 'health', 'run', or "
        f"'attribution'",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
