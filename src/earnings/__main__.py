"""``python -m earnings`` — one shadow-logging pass, then a summary.

Two subcommands, both read-only:

  pass     (default) Arm the prints inside the window, mark the ones that have
           reported, snapshot today's at-the-money IV. Writes only to
           ``data/earnings_shadow.jsonl``; touches neither the audit log nor the
           session state, and cannot place an order — there is no order path in
           this package.
  report   Read the log back and say what it has learned so far: how many prints
           resolved, how often the realised move exceeded the implied one, and
           what a long straddle would have done. The verdict this exercise
           exists to produce.

Run it once a day. It is deliberately a separate entry point from the trading
loop: the loop's job is to trade and this thing's job is to watch, and a
scheduled task that cannot reach the broker is a stronger guarantee than one
that merely does not.
"""

from __future__ import annotations

import logging
import statistics
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from execution.environment import load_environment
from execution.market_data import AlpacaDailyBars, AlpacaPriceSource
from execution.options_data import AlpacaOptionsChain

from earnings.calendar import FinnhubEarningsCalendar
from earnings.config import EarningsConfig
from earnings.shadow import ShadowLog, ShadowObserver

logger = logging.getLogger("earnings")

SHADOW_LOG_NAME = "earnings_shadow.jsonl"


def data_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def shadow_pass() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    load_environment()
    config = EarningsConfig.load()
    log = ShadowLog(data_dir() / SHADOW_LOG_NAME)

    calendar = FinnhubEarningsCalendar()
    chain = AlpacaOptionsChain()
    bars = AlpacaDailyBars()
    prices = AlpacaPriceSource()

    def spot(symbol: str):
        """The live quote, or the last daily close when there isn't one.

        This runs on a daily schedule, which means it usually runs outside market
        hours, where every quote is stale and the trading path's answer is
        correctly None. For a logger that is the wrong answer: a stale quote must
        not price an order, but the last close is a perfectly good observation,
        and refusing to record anything after 4pm would mean recording nothing at
        all. Still never invented — no close, no record.
        """
        live = prices(symbol)
        if live is not None:
            return live
        window = bars.bars(
            symbol,
            datetime.now(timezone.utc) - timedelta(days=10),
            datetime.now(timezone.utc),
        )
        for bar in reversed(window):
            close = bar.get("c")
            try:
                value = Decimal(str(close))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if value > 0:
                return value
        return None

    try:
        observer = ShadowObserver(
            config=config,
            calendar=calendar,
            chain=chain,
            bars=bars,
            spot=spot,
            log=log,
        )
        report = observer.run()
    finally:
        calendar.close()
        chain.close()
        bars.close()
        prices.close()
    print(f"earnings shadow pass: {report.summary()}")
    print(f"log: {log.path}")
    return 0


def shadow_report() -> int:
    logging.basicConfig(level=logging.WARNING)
    log = ShadowLog(data_dir() / SHADOW_LOG_NAME)
    records = list(log.records())
    if not records:
        print("no shadow-log records yet")
        return 0

    armed = [r for r in records if r.get("kind") == "armed"]
    resolved = [r for r in records if r.get("kind") == "resolved"]
    ivs = [r for r in records if r.get("kind") == "iv"]
    skipped = [r for r in records if r.get("kind") == "arm_skipped"]

    print(f"Earnings shadow log — {len(records)} records")
    print(f"  armed prints:     {len(armed)}")
    print(f"  resolved prints:  {len(resolved)}")
    print(f"  IV snapshots:     {len(ivs)} "
          f"(across {len({r.get('symbol') for r in ivs})} names)")
    print(f"  skipped (no usable straddle): {len(skipped)}")

    judged = [r for r in resolved if r.get("realised_exceeded_implied") is not None]
    if not judged:
        print("\nNo resolved print carries both an implied and a realised move yet.")
        print("The claim under test needs resolved prints; keep it running.")
        return 0

    exceeded = [r for r in judged if r["realised_exceeded_implied"]]
    print(f"\nTHE CLAIM UNDER TEST: realised move exceeded implied in "
          f"{len(exceeded)} of {len(judged)} prints "
          f"({len(exceeded) / len(judged):.0%})")
    print("  (a long-premium edge needs this materially above 50%, and enough")
    print("   prints that the number means something — two seasons was the ruling)")

    pnls = [
        float(r["hypothetical_straddle_pnl_pct"])
        for r in resolved
        if r.get("hypothetical_straddle_pnl_pct") is not None
    ]
    if pnls:
        print(f"\nHypothetical long-straddle P&L over {len(pnls)} prints "
              f"(real marks of the same contracts, no model):")
        print(f"  mean   {statistics.mean(pnls):+.1f}%")
        print(f"  median {statistics.median(pnls):+.1f}%")
        print(f"  winners {sum(1 for p in pnls if p > 0)} / {len(pnls)}")

    print("\nPer print:")
    for record in sorted(resolved, key=lambda r: str(r.get("earnings_date"))):
        implied = record.get("implied_move_pct")
        realised = record.get("abs_realised_move_pct")
        pnl = record.get("hypothetical_straddle_pnl_pct")
        print(
            f"  {record.get('earnings_date')} {str(record.get('symbol')):6s} "
            f"implied {str(implied) + '%':>8s}  realised {str(realised) + '%':>8s}  "
            f"straddle {str(pnl) + '%' if pnl is not None else 'unmarked':>10s}"
        )
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "pass"
    if command == "pass":
        return shadow_pass()
    if command == "report":
        return shadow_report()
    print(f"unknown command {command!r}: expected 'pass' or 'report'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
