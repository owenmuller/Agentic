"""``python -m orchestrator`` — the operational entry points.

Three subcommands, in ascending order of consequence:

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

``run`` wires the production seams that exist today: the EDGAR Class 3 fetcher and
the Alpaca IEX price source. Class 1 and Class 2 sources poll nothing until their
credentials are procured — their branch below returns an empty feed, explicitly.

Trading mode is decided by Constraint #4 inside ``preflight()``, first, always:
PAPER_MODE=true is the default and live requires the two human-set variables. This
module adds no mode logic of its own.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from audit.log import default_data_dir
from execution import AlpacaPriceSource
from signals import Form13FFetcher

from orchestrator.bootstrap import preflight, start
from orchestrator.exits import unmanaged_exposure
from orchestrator.ops import RunLog, health_report, is_trading_weekday, session_bounds

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
        "'python -m orchestrator health' for the daily status."
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
    run_log.note("STARTED", f"pid={os.getpid()}")

    now = datetime.now(timezone.utc)
    if not is_trading_weekday(now):
        run_log.note("STOPPED", "not a trading weekday; nothing to do")
        return 0
    open_utc, close_utc = session_bounds(now)
    if now >= close_utc:
        run_log.note("STOPPED", "started after the close; nothing to do")
        return 0

    loop = None
    edgar = None
    prices = None
    try:
        # Checks first — a misconfigured run should fail before it waits for a bell.
        checks = preflight()
        logger.info("startup state:\n%s", checks.describe())

        edgar = Form13FFetcher()

        def fetcher(source):
            if source.id == "form_13f":
                items = edgar(source)
                run_log.note("POLL", f"form_13f ok items={len(items)}")
                return items
            return []  # Class 1/2 feeds await credentials

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


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "check":
        return check()
    if command == "health":
        return health()
    if command == "run":
        return run()
    print(
        f"unknown command {command!r}: expected 'check', 'health', or 'run'",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
