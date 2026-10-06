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
- **Open split (ruling 2026-10-06):** beside the close base, every row carries
  the NEXT-SESSION OPEN after the observation date (``open_date``,
  ``base_open``) and each mark carries the return and SPY-excess from that
  open. For anything published after the close — the DoD contracts digest at
  17:00 ET, an 8-K filed at 18:00 — the close-to-close number includes a gap
  nobody could trade; open-to-close is the tradeable path. Both are reported.
- Closes are split-adjusted (the ``AlpacaDailyBars`` default), the same basis the
  rest of the system reads. These are price returns; dividends are not added
  back, and signal and benchmark are measured identically so the excess is fair.
- A horizon whose day has not arrived, or whose series ended first, is ABSENT —
  never zero. "No data" and "0%" are different facts.
- Excess is the same-window SPY return subtracted; without a SPY mark the excess
  is absent, never guessed.

The cache is append-only JSONL (``data/forward_returns.jsonl``): recomputing a
row appends a fuller one, the reader keeps the last per (symbol, observed) key,
and nothing is ever rewritten — same discipline as the audit log. Rows written
before the open split lack ``base_open`` and recompute once at the next refresh.
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

#: A bars source: (symbol, start, end) -> list of {"t": iso, "o": open, "c": close, ...}.
BarsSource = Callable[[str, datetime, datetime], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class HorizonMark:
    """One resolved horizon: the session that marked it and what it returned."""

    marked_on: date
    close: Decimal
    return_pct: Decimal
    #: Return over SPY's same-window return. Absent when SPY had no mark.
    excess_pct: Optional[Decimal]
    #: From the next-session OPEN after observation (ruling 2026-10-06).
    #: Absent when the row has no open base or the mark precedes it.
    open_return_pct: Optional[Decimal] = None
    open_excess_pct: Optional[Decimal] = None


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
    #: The next session strictly after ``observed`` and its open (ruling
    #: 2026-10-06). None on rows computed before the split or when the series
    #: had no session within the gap rule.
    open_date: Optional[date] = None
    base_open: Optional[Decimal] = None
    #: True once a run computed this row WITH the open split (whatever it
    #: found). Rows from before the split are False and recompute exactly once.
    open_checked: bool = False

    @property
    def has_base(self) -> bool:
        return self.base_close is not None and self.base_close > ZERO

    @property
    def has_open_base(self) -> bool:
        return self.base_open is not None and self.base_open > ZERO

    @property
    def complete(self) -> bool:
        """Every horizon resolved — nothing left for a later run to add. A row
        never computed with the open split is not complete: it recomputes once."""
        return all(n in self.marks for n in HORIZONS) and self.open_checked

    def to_json(self) -> str:
        return json.dumps(
            {
                "symbol": self.symbol,
                "observed": self.observed.isoformat(),
                "base_date": self.base_date.isoformat() if self.base_date else None,
                "base_close": str(self.base_close) if self.base_close else None,
                "open_date": self.open_date.isoformat() if self.open_date else None,
                "base_open": str(self.base_open) if self.base_open else None,
                "open_checked": self.open_checked,
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
                        "open_return_pct": (
                            str(mark.open_return_pct)
                            if mark.open_return_pct is not None
                            else None
                        ),
                        "open_excess_pct": (
                            str(mark.open_excess_pct)
                            if mark.open_excess_pct is not None
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
                    open_return_pct=(
                        Decimal(raw["open_return_pct"])
                        if raw.get("open_return_pct") is not None
                        else None
                    ),
                    open_excess_pct=(
                        Decimal(raw["open_excess_pct"])
                        if raw.get("open_excess_pct") is not None
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
                open_date=(
                    date.fromisoformat(payload["open_date"])
                    if payload.get("open_date")
                    else None
                ),
                base_open=(
                    Decimal(payload["base_open"])
                    if payload.get("base_open")
                    else None
                ),
                open_checked=bool(payload.get("open_checked", False)),
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


def _opens_of(bars: list[dict[str, Any]]) -> list[tuple[date, Decimal]]:
    """(session date, open) pairs, oldest first; bars without a usable open skipped."""
    out: list[tuple[date, Decimal]] = []
    for bar in bars:
        raw_date = bar.get("t")
        if not isinstance(raw_date, str) or len(raw_date) < 10:
            continue
        try:
            session = date.fromisoformat(raw_date[:10])
            open_ = Decimal(str(bar.get("o")))
        except (ValueError, InvalidOperation, TypeError):
            continue
        if open_ > ZERO:
            out.append((session, open_))
    out.sort(key=lambda pair: pair[0])
    return out


#: How far past its intended day a base or a mark may land and still count:
#: a Friday observation marks on Monday (3 days), a Friday before a Monday
#: holiday on Tuesday (4). Beyond that the series simply did not cover the day
#: — a late-starting history, a halt, a hole in the feed — and the answer is
#: ABSENT, never a bar from weeks later. Incident 2026-09-22: the overreaction
#: screen's backfilled 2018/2020 events took their base from the first bar the
#: fetch returned (months late), every horizon collapsed onto that same bar,
#: and 66% of the slice read an excess of exactly 0.00.
MAX_MARK_GAP_DAYS = 4


def _first_close_on_or_after(
    closes: list[tuple[date, Decimal]],
    day: date,
    max_gap_days: Optional[int] = MAX_MARK_GAP_DAYS,
) -> Optional[tuple[date, Decimal]]:
    for session, close in closes:
        if session >= day:
            if max_gap_days is not None and (session - day).days > max_gap_days:
                return None
            return session, close
    return None


def _first_open_after(
    opens: list[tuple[date, Decimal]],
    day: date,
    max_gap_days: Optional[int] = MAX_MARK_GAP_DAYS,
) -> Optional[tuple[date, Decimal]]:
    """The next session STRICTLY after ``day`` and its open — the first print
    anyone could trade after an after-close publication."""
    for session, open_ in opens:
        if session > day:
            if max_gap_days is not None and (session - day).days > max_gap_days:
                return None
            return session, open_
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
        spy_bars = self._bars(BENCHMARK, start, now)
        spy_closes = _closes_of(spy_bars)
        spy_opens = _opens_of(spy_bars)
        if not spy_closes:
            logger.warning(
                "no %s bars for the excess-return baseline; excess will be absent "
                "this run",
                BENCHMARK,
            )

        for index, (symbol, dates) in enumerate(sorted(pending.items())):
            if index and self._pace_seconds > 0:
                self._sleep(self._pace_seconds)
            bars = self._bars(symbol, start, now)
            closes = _closes_of(bars)
            opens = _opens_of(bars)
            for observed in dates:
                row = self._compute(
                    symbol, observed, closes, spy_closes, now, opens, spy_opens
                )
                key = (symbol, observed)
                previous = self._cache.get(key)
                out[key] = row
                # Append only when the run learned something: a new row, a base
                # that appeared, another horizon resolved, or the open base
                # (ruling 2026-10-06) added to a row computed before it existed.
                if (
                    previous is None
                    or len(row.marks) > len(previous.marks)
                    or (row.has_base and not previous.has_base)
                    or (row.has_open_base and not previous.has_open_base)
                    or (row.open_checked and not previous.open_checked)
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
        opens: Optional[list[tuple[date, Decimal]]] = None,
        spy_opens: Optional[list[tuple[date, Decimal]]] = None,
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
                open_checked=True,
            )
        base_date, base_close = base
        spy_base = _first_close_on_or_after(spy_closes, observed)
        open_base = _first_open_after(opens or [], observed)
        spy_open_base = _first_open_after(spy_opens or [], observed)

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
            spy_mark = None
            if spy_base is not None:
                spy_mark = _first_close_on_or_after(spy_closes, due)
                if spy_mark is not None and spy_base[1] > ZERO:
                    spy_return = (spy_mark[1] / spy_base[1] - 1) * 100
                    excess = (return_pct - spy_return).quantize(CENTS)
            open_return = None
            open_excess = None
            if open_base is not None and marked_on >= open_base[0]:
                open_return = ((close / open_base[1] - 1) * 100).quantize(CENTS)
                if spy_open_base is not None and spy_mark is not None and spy_open_base[1] > ZERO:
                    spy_open_return = (spy_mark[1] / spy_open_base[1] - 1) * 100
                    open_excess = (open_return - spy_open_return).quantize(CENTS)
            marks[n] = HorizonMark(
                marked_on=marked_on,
                close=close,
                return_pct=return_pct,
                excess_pct=excess,
                open_return_pct=open_return,
                open_excess_pct=open_excess,
            )
        return ForwardRow(
            symbol=symbol,
            observed=observed,
            base_date=base_date,
            base_close=base_close,
            marks=marks,
            computed_at=now,
            open_date=open_base[0] if open_base else None,
            base_open=open_base[1] if open_base else None,
            open_checked=True,
        )
