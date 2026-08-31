"""The shadow log: arm before the print, mark after it, never trade.

One append-only JSONL file, separate from the audit trail by design. The audit
log is the record of decisions this system made; nothing here is a decision, and
mixing observations into it would make "every order has a complete record" harder
to read rather than easier.

The loop, per pass
------------------
1. ``arm`` — for each configured name with a print inside the arming window and
   no armed record yet: fetch spot and the chain, price the ATM straddle, and
   write what the market is charging for the event. Also writes a daily ``iv``
   snapshot for every tracked name, which is the IV history this system does not
   otherwise have.
2. ``settle`` — for each armed record whose print has passed and whose settle
   session has arrived: re-fetch THOSE EXACT TWO CONTRACTS, and record what they
   are now worth alongside where the underlying actually went. The straddle P&L
   is a real mark rather than a payoff model, which is the whole reason to mark
   the same OCC symbols rather than re-derive a straddle.

Idempotence
-----------
Both phases replay from the log, so a pass that runs twice in a day writes
nothing the second time and a crash loses at most the pass it was in. Same
philosophy as the research budget and the fetcher dedup sets: the file is the
state, not a counter in memory.

What is never inferred
----------------------
A missing spot, an absent chain, an illiquid straddle, a name that does not
report when the calendar said: each is recorded as absent. The series this
produces is only worth having if its gaps are visible.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

from earnings.calendar import EarningsCalendar, EarningsCalendarError, EarningsEvent
from earnings.config import EarningsConfig
from earnings.implied import ImpliedMove, atm_straddle
from earnings.realised import realised_move_pct

logger = logging.getLogger("earnings.shadow")

ZERO = Decimal("0")


class ChainSource(Protocol):
    def chain_for(
        self, underlying: str, *, min_expiry: date, max_expiry: Optional[date] = None
    ) -> Optional[list[Any]]:
        ...

    def option_mid(self, occ_symbol: str) -> Optional[Decimal]:
        ...


class BarSource(Protocol):
    def bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class PassReport:
    """What one pass did. Printed by the CLI; nothing downstream reads it."""

    armed: int = 0
    settled: int = 0
    iv_snapshots: int = 0
    skipped: tuple[str, ...] = ()

    def summary(self) -> str:
        line = (
            f"armed {self.armed}, settled {self.settled}, "
            f"IV snapshots {self.iv_snapshots}"
        )
        if self.skipped:
            line += f"; skipped: {'; '.join(self.skipped)}"
        return line


class ShadowLog:
    """Append-only JSONL. Every write is one line, flushed."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        if "\n" in line:  # pragma: no cover - json.dumps never emits newlines
            raise ValueError("record serialised to multiple lines")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def records(self) -> Iterable[dict[str, Any]]:
        if not self._path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    logger.warning("unparseable shadow-log line skipped")
        return out

    def armed_records(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r.get("kind") == "armed"]

    def settled_keys(self) -> set[tuple[str, str]]:
        return {
            (str(r.get("symbol")), str(r.get("earnings_date")))
            for r in self.records()
            if r.get("kind") == "resolved"
        }

    def armed_keys(self) -> set[tuple[str, str]]:
        return {
            (str(r.get("symbol")), str(r.get("earnings_date")))
            for r in self.armed_records()
        }

    def iv_days(self) -> set[tuple[str, str]]:
        return {
            (str(r.get("symbol")), str(r.get("observed_at", ""))[:10])
            for r in self.records()
            if r.get("kind") == "iv"
        }


class ShadowObserver:
    """Observation only. There is no order path in this class or its package."""

    def __init__(
        self,
        *,
        config: EarningsConfig,
        calendar: EarningsCalendar,
        chain: ChainSource,
        bars: BarSource,
        spot: Callable[[str], Optional[Decimal]],
        log: ShadowLog,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config
        self._calendar = calendar
        self._chain = chain
        self._bars = bars
        self._spot = spot
        self._log = log
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- one pass --------------------------------------------------------------------

    def run(self) -> PassReport:
        if not self._config.enabled:
            return PassReport(skipped=("earnings shadow logging is disabled",))
        if not self._config.universe:
            return PassReport(skipped=("universe is empty; nothing is watched",))
        now = self._clock()
        settled, settle_notes = self._settle(now)
        armed, arm_notes = self._arm(now)
        # Deliberately OUTSIDE the calendar's failure path: the daily IV history
        # needs no calendar, and it is the artefact this system cannot buy. A
        # missing Finnhub key must not also cost us the series.
        iv_count = self._snapshot_iv(now)
        return PassReport(
            armed=armed,
            settled=settled,
            iv_snapshots=iv_count,
            skipped=tuple(settle_notes) + tuple(arm_notes),
        )

    # -- phase 1: arm ----------------------------------------------------------------

    def _arm(self, now: datetime) -> tuple[int, list[str]]:
        today = now.date()
        notes: list[str] = []
        try:
            events = self._calendar.upcoming(
                today, today + timedelta(days=self._config.calendar_window_days)
            )
        except EarningsCalendarError as error:
            # An unreadable calendar is not an empty one, and the difference has
            # to survive into the log or the series looks complete when it is not.
            logger.warning("earnings calendar unavailable: %s", error)
            return 0, [f"calendar unavailable: {error}"]

        universe = {symbol.upper() for symbol in self._config.universe}
        armed_already = self._log.armed_keys()
        armed = 0

        for event in sorted(events, key=lambda e: (e.report_date, e.symbol)):
            if event.symbol not in universe:
                continue
            days_out = (event.report_date - today).days
            if days_out < 0 or days_out > self._config.arm_within_days:
                continue
            key = (event.symbol, event.report_date.isoformat())
            if key in armed_already:
                continue

            spot = self._spot(event.symbol)
            straddle = self._straddle_for(event, spot)
            if straddle is None:
                self._log.append(
                    {
                        "kind": "arm_skipped",
                        "symbol": event.symbol,
                        "earnings_date": event.report_date.isoformat(),
                        "session": event.session,
                        "observed_at": now.isoformat(),
                        "spot": str(spot) if spot is not None else None,
                        "reason": (
                            "no spot price"
                            if spot is None
                            else "no straddle met the liquidity floors"
                        ),
                    }
                )
                notes.append(f"{event.symbol}: no usable straddle")
                continue

            self._log.append(
                {
                    "kind": "armed",
                    "symbol": event.symbol,
                    "earnings_date": event.report_date.isoformat(),
                    "session": event.session,
                    "session_known": event.session_known,
                    "observed_at": now.isoformat(),
                    "days_before_print": days_out,
                    "spot": str(straddle.spot),
                    "expiry": straddle.expiry.isoformat(),
                    "strike": str(straddle.strike),
                    "call_symbol": straddle.call_symbol,
                    "put_symbol": straddle.put_symbol,
                    "call_mid": str(straddle.call_mid),
                    "put_mid": str(straddle.put_mid),
                    "straddle_cost": str(straddle.straddle_cost),
                    "implied_move_pct": str(straddle.implied_move_pct),
                    "atm_iv": str(straddle.atm_iv) if straddle.atm_iv else None,
                    "open_interest": straddle.open_interest,
                    "worst_spread_pct": (
                        str(straddle.worst_spread_pct)
                        if straddle.worst_spread_pct is not None
                        else None
                    ),
                    "eps_estimate": event.eps_estimate,
                }
            )
            armed += 1
        return armed, notes

    def _snapshot_iv(self, now: datetime) -> int:
        """One at-the-money IV reading per tracked name per day.

        The series ``options_selection.max_iv_percentile`` cannot have: a
        chain-internal percentile is blind to a whole surface lifted together, and
        ranking today's IV against its own past needs a past that only exists if
        something writes it down. This is that something, and it runs whether or
        not there is an earnings calendar to read.
        """
        today = now.date()
        done = self._log.iv_days()
        count = 0
        for symbol in sorted({s.upper() for s in self._config.universe}):
            if (symbol, today.isoformat()) in done:
                continue
            snapshot = self._iv_snapshot(symbol, now)
            if snapshot is not None:
                self._log.append(snapshot)
                count += 1
        return count

    def _straddle_for(
        self, event: EarningsEvent, spot: Optional[Decimal]
    ) -> Optional[ImpliedMove]:
        if spot is None or spot <= ZERO:
            return None
        earliest = event.report_date + timedelta(
            days=self._config.min_days_after_print
        )
        latest = event.report_date + timedelta(days=self._config.max_days_after_print)
        chain = self._chain.chain_for(
            event.symbol, min_expiry=earliest, max_expiry=latest
        )
        if not chain:
            return None
        return atm_straddle(
            chain,
            spot,
            earliest_expiry=earliest,
            latest_expiry=latest,
            min_open_interest=self._config.min_open_interest,
            max_spread_pct_of_mid=self._config.max_spread_pct_of_mid,
        )

    def _iv_snapshot(self, symbol: str, now: datetime) -> Optional[dict[str, Any]]:
        spot = self._spot(symbol)
        if spot is None or spot <= ZERO:
            return None
        today = now.date()
        earliest = today + timedelta(days=self._config.min_days_after_print)
        latest = today + timedelta(days=self._config.max_days_after_print)
        chain = self._chain.chain_for(symbol, min_expiry=earliest, max_expiry=latest)
        if not chain:
            return None
        straddle = atm_straddle(
            chain, spot, earliest_expiry=earliest, latest_expiry=latest
        )
        if straddle is None or straddle.atm_iv is None:
            return None
        return {
            "kind": "iv",
            "symbol": symbol,
            "observed_at": now.isoformat(),
            "spot": str(spot),
            "expiry": straddle.expiry.isoformat(),
            "days_to_expiry": (straddle.expiry - today).days,
            "strike": str(straddle.strike),
            "atm_iv": str(straddle.atm_iv),
            "implied_move_pct": str(straddle.implied_move_pct),
        }

    # -- phase 2: settle -------------------------------------------------------------

    def _settle(self, now: datetime) -> tuple[int, list[str]]:
        today = now.date()
        done = self._log.settled_keys()
        notes: list[str] = []
        settled = 0

        for record in self._log.armed_records():
            symbol = str(record.get("symbol"))
            earnings_date = _date_or_none(record.get("earnings_date"))
            if earnings_date is None:
                continue
            if (symbol, earnings_date.isoformat()) in done:
                continue
            if today < earnings_date + timedelta(days=self._config.settle_after_days):
                continue

            session = str(record.get("session") or "")
            bars = self._bars_for(symbol, earnings_date, today)
            realised = realised_move_pct(bars, earnings_date, session)
            if realised is None:
                notes.append(f"{symbol}: no bars spanning the print yet")
                continue

            call_after = self._chain.option_mid(str(record.get("call_symbol")))
            put_after = self._chain.option_mid(str(record.get("put_symbol")))
            cost = _decimal_or_none(record.get("straddle_cost"))
            value_after = (
                call_after + put_after
                if call_after is not None and put_after is not None
                else None
            )
            implied = _decimal_or_none(record.get("implied_move_pct"))

            self._log.append(
                {
                    "kind": "resolved",
                    "symbol": symbol,
                    "earnings_date": earnings_date.isoformat(),
                    "session": session,
                    "resolved_at": now.isoformat(),
                    "implied_move_pct": str(implied) if implied is not None else None,
                    "realised_move_pct": str(realised),
                    "abs_realised_move_pct": str(abs(realised)),
                    # THE claim under test, in one field: did the move exceed what
                    # the straddle charged for it?
                    "realised_exceeded_implied": (
                        bool(abs(realised) > implied) if implied is not None else None
                    ),
                    "straddle_cost": str(cost) if cost is not None else None,
                    "straddle_value_after": (
                        str(value_after) if value_after is not None else None
                    ),
                    # A real mark of the same two contracts, not a payoff model.
                    "hypothetical_straddle_pnl_pct": (
                        str(((value_after / cost - 1) * 100).quantize(Decimal("0.01")))
                        if value_after is not None and cost is not None and cost > ZERO
                        else None
                    ),
                    "marks_available": value_after is not None,
                }
            )
            settled += 1
        return settled, notes

    def _bars_for(
        self, symbol: str, earnings_date: date, today: date
    ) -> list[dict[str, Any]]:
        return self._bars.bars(
            symbol,
            _as_datetime(earnings_date - timedelta(days=10)),
            _as_datetime(today + timedelta(days=1)),
        )


def _as_datetime(when: date) -> datetime:
    return datetime(when.year, when.month, when.day, tzinfo=timezone.utc)


def _date_or_none(raw: object) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _decimal_or_none(raw: object) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return None
