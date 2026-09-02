"""Forward returns from daily bars: lazy, cached, and honest about gaps.

The design insight this module rests on (ruling 2026-09-01): forward returns need
no daily job. Daily bars are historical, so "what did NUE do in the 20 days after
2026-08-18" is a pure lookup, computable whenever a report wants it. The cache
exists to keep report runs cheap, not to keep the data alive.

Conventions, all pinned by the ruling:

- Horizons are CALENDAR days from observation; each mark is the first session
  close ON OR AFTER observation + n days (the base is the first close on or
  after the observation date itself — the first price the system could have
  acted near).
- Closes are split-adjusted (the ``AlpacaDailyBars`` default), the same basis the
  rest of the system reads. These are price returns; dividends are not added
  back, and signal and benchmark are measured identically so the excess is fair.
- A horizon whose day has not arrived, or whose series ended first, is ABSENT —
  never zero. "No data" and "0%" are different facts.
- Excess is the same-window SPY return subtracted; without a SPY mark the excess
  is absent, never guessed.

The cache is append-only JSONL (``data/forward_returns.jsonl``): recomputing a
row appends a fuller one, the reader keeps the last per (symbol, observed) key,
and nothing is ever rewritten — same discipline as the audit log.
"""

from __future__ import annotations

import json
import logging
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("forward.returns")

#: Calendar-day horizons. 3d added by the disclosure-reaction ruling
#: (2026-09-02) — the pop, if it exists, lives in the first sessions after
#: publication. Existing complete cache rows recompute once to pick it up.
HORIZONS = (1, 3, 5, 20, 60, 120)

BENCHMARK = "SPY"

ZERO = Decimal("0")
CENTS = Decimal("0.01")

#: A bars source: (symbol, start, end) -> list of {"t": iso, "c": close, ...}.
BarsSource = Callable[[str, datetime, datetime], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class HorizonMark:
    """One resolved horizon: the session that marked it and what it returned."""

    marked_on: date
    close: Decimal
    return_pct: Decimal
    #: Return over SPY's same-window return. Absent when SPY had no mark.
    excess_pct: Optional[Decimal]


@dataclass(frozen=True, slots=True)
class ForwardRow:
    """Forward returns for one (symbol, observation date). Horizons resolve as
    the calendar catches up; absent means not-yet-or-never, never zero."""

    symbol: str
    observed: date
    base_date: Optional[date]
    base_close: Optional[Decimal]
    marks: dict[int, HorizonMark]
    computed_at: datetime

    @property
    def has_base(self) -> bool:
        return self.base_close is not None and self.base_close > ZERO

    @property
    def complete(self) -> bool:
        """Every horizon resolved — nothing left for a later run to add."""
        return all(n in self.marks for n in HORIZONS)

    def to_json(self) -> str:
        return json.dumps(
            {
                "symbol": self.symbol,
                "observed": self.observed.isoformat(),
                "base_date": self.base_date.isoformat() if self.base_date else None,
                "base_close": str(self.base_close) if self.base_close else None,
                "marks": {
                    str(n): {
                        "marked_on": mark.marked_on.isoformat(),
                        "close": str(mark.close),
                        "return_pct": str(mark.return_pct),
                        "excess_pct": (
                            str(mark.excess_pct)
                            if mark.excess_pct is not None
                            else None
                        ),
                    }
                    for n, mark in self.marks.items()
                },
                "computed_at": self.computed_at.isoformat(),
            }
        )

    @classmethod
    def from_json(cls, line: str) -> Optional["ForwardRow"]:
        try:
            payload = json.loads(line)
            marks: dict[int, HorizonMark] = {}
            for key, raw in (payload.get("marks") or {}).items():
                marks[int(key)] = HorizonMark(
                    marked_on=date.fromisoformat(raw["marked_on"]),
                    close=Decimal(raw["close"]),
                    return_pct=Decimal(raw["return_pct"]),
                    excess_pct=(
                        Decimal(raw["excess_pct"])
                        if raw.get("excess_pct") is not None
                        else None
                    ),
                )
            return cls(
                symbol=payload["symbol"],
                observed=date.fromisoformat(payload["observed"]),
                base_date=(
                    date.fromisoformat(payload["base_date"])
                    if payload.get("base_date")
                    else None
                ),
                base_close=(
                    Decimal(payload["base_close"])
                    if payload.get("base_close")
                    else None
                ),
                marks=marks,
                computed_at=datetime.fromisoformat(payload["computed_at"]),
            )
        except (KeyError, ValueError, InvalidOperation, TypeError):
            logger.warning("unreadable forward-return cache line; skipped")
            return None


def _closes_of(bars: list[dict[str, Any]]) -> list[tuple[date, Decimal]]:
    """(session date, close) pairs, oldest first, non-positive closes dropped."""
    out: list[tuple[date, Decimal]] = []
    for bar in bars:
        raw_date = bar.get("t")
        if not isinstance(raw_date, str) or len(raw_date) < 10:
            continue
        try:
            session = date.fromisoformat(raw_date[:10])
            close = Decimal(str(bar.get("c")))
        except (ValueError, InvalidOperation, TypeError):
            continue
        if close > ZERO:
            out.append((session, close))
    out.sort(key=lambda pair: pair[0])
    return out


def _first_close_on_or_after(
    closes: list[tuple[date, Decimal]], day: date
) -> Optional[tuple[date, Decimal]]:
    for session, close in closes:
        if session >= day:
            return session, close
    return None


class ForwardReturns:
    """Computes and caches forward-return rows for (symbol, observed date) pairs.

    One bars fetch per symbol per run — a symbol's whole series covers every
    observation of it — plus one for SPY. Rows already complete in the cache are
    never recomputed and never refetched.
    """

    def __init__(
        self,
        bars: BarsSource,
        cache_path: Path,
        clock: Optional[Callable[[], datetime]] = None,
        pace_seconds: float = 0.35,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._bars = bars
        self._path = cache_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        #: Pause between symbol fetches. The funnel names hundreds of distinct
        #: symbols and Alpaca's free data tier allows ~200 requests/minute; the
        #: first production run tripped HTTP 429 halfway through the alphabet
        #: (2026-09-01). 0.35s keeps a full sweep under the limit — a weekly
        #: report can afford minutes; it cannot afford half a scoreboard.
        self._pace_seconds = pace_seconds
        self._sleep = sleep
        self._cache: dict[tuple[str, date], ForwardRow] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = ForwardRow.from_json(line)
                if row is not None:
                    # Last write wins: a later, fuller row supersedes.
                    self._cache[(row.symbol, row.observed)] = row

    def _append(self, row: ForwardRow) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(row.to_json() + "\n")
            handle.flush()

    def rows_for(
        self, wanted: Iterable[tuple[str, date]]
    ) -> dict[tuple[str, date], ForwardRow]:
        """Rows for every requested (symbol, observed) pair.

        Cache hits that are complete return as-is. Everything else is recomputed
        from one bars fetch per symbol; newly fuller rows are appended to the
        cache. A symbol whose fetch fails yields a row with no base — absent,
        never zero — and is retried at the next run by construction.
        """
        now = self._clock()
        pending: dict[str, list[date]] = {}
        out: dict[tuple[str, date], ForwardRow] = {}
        for symbol, observed in set(wanted):
            key = (symbol.upper(), observed)
            cached = self._cache.get(key)
            if cached is not None and cached.complete:
                out[key] = cached
                continue
            pending.setdefault(key[0], []).append(observed)

        if not pending:
            return out

        earliest = min(min(dates) for dates in pending.values())
        start = datetime.combine(
            earliest - timedelta(days=7), time.min, tzinfo=timezone.utc
        )
        spy_closes = _closes_of(self._bars(BENCHMARK, start, now))
        if not spy_closes:
            logger.warning(
                "no %s bars for the excess-return baseline; excess will be absent "
                "this run",
                BENCHMARK,
            )

        for index, (symbol, dates) in enumerate(sorted(pending.items())):
            if index and self._pace_seconds > 0:
                self._sleep(self._pace_seconds)
            closes = _closes_of(self._bars(symbol, start, now))
            for observed in dates:
                row = self._compute(symbol, observed, closes, spy_closes, now)
                key = (symbol, observed)
                previous = self._cache.get(key)
                out[key] = row
                # Append only when the run learned something: a new row, a base
                # that appeared, or another horizon resolved.
                if (
                    previous is None
                    or len(row.marks) > len(previous.marks)
                    or (row.has_base and not previous.has_base)
                ):
                    self._append(row)
                    self._cache[key] = row
                else:
                    out[key] = previous
        return out

    def _compute(
        self,
        symbol: str,
        observed: date,
        closes: list[tuple[date, Decimal]],
        spy_closes: list[tuple[date, Decimal]],
        now: datetime,
    ) -> ForwardRow:
        base = _first_close_on_or_after(closes, observed)
        if base is None:
            return ForwardRow(
                symbol=symbol,
                observed=observed,
                base_date=None,
                base_close=None,
                marks={},
                computed_at=now,
            )
        base_date, base_close = base
        spy_base = _first_close_on_or_after(spy_closes, observed)

        marks: dict[int, HorizonMark] = {}
        today = now.date()
        for n in HORIZONS:
            due = observed + timedelta(days=n)
            if due > today:
                continue  # not yet — absent, not zero
            mark = _first_close_on_or_after(closes, due)
            if mark is None:
                continue  # series ended first (delisted, halted) — absent
            marked_on, close = mark
            return_pct = ((close / base_close - 1) * 100).quantize(CENTS)
            excess = None
            if spy_base is not None:
                spy_mark = _first_close_on_or_after(spy_closes, due)
                if spy_mark is not None and spy_base[1] > ZERO:
                    spy_return = (spy_mark[1] / spy_base[1] - 1) * 100
                    excess = (return_pct - spy_return).quantize(CENTS)
            marks[n] = HorizonMark(
                marked_on=marked_on,
                close=close,
                return_pct=return_pct,
                excess_pct=excess,
            )
        return ForwardRow(
            symbol=symbol,
            observed=observed,
            base_date=base_date,
            base_close=base_close,
            marks=marks,
            computed_at=now,
        )
