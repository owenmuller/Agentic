"""Calibration feedback block (human ruling 2026-10-07, item 4; approved in
the adaptive-standards ruling 2026-10-06 and missed at that build).

The research prompt may be shown this system's OWN measured record - hit
rate and mean forward excess by source, confidence band and stated horizon -
as evidence to weigh. It is keyed on the VOTED median confidence (the
self-consistency vote, same ruling), never on a single-pass confidence,
because the five-fold replay showed a single pass's confidence band carries
little information. A cell renders only at n >= 20; below that the block is
empty and the prompt is byte-identical to the one without it.

What this is NOT (CLAUDE.md § Standards Move With the Opportunity Set): a
target, a P&L, a deficit, or anything about how the book is doing. The cells
are forward returns of past verdicts, framed as data. Topology: this module
reads the forward engine's rows and the funnel entries handed to it - it
imports nothing from attribution, spend, the audit log or any target module
(tests/test_scoreboard_constraint.py fences it).
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Optional

from forward.funnel import FunnelEntry
from forward.returns import ForwardRow

#: The sizing table's bands, named as the model will read them.
BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 45, "<45"),
    (45, 55, "45-55"),
    (55, 70, "55-70"),
    (70, 85, "70-85"),
    (85, 101, "85+"),
)
MIN_N = 20
HORIZONS = (5, 20)


def band_of(confidence: int) -> str:
    for low, high, name in BANDS:
        if low <= confidence < high:
            return name
    return "85+"


@dataclass(frozen=True, slots=True)
class CalibrationCell:
    source_id: str
    band: str
    horizon: str
    direction: str
    n: int
    hit_5d: float
    mean_5d: float
    n_20d: int
    mean_20d: Optional[float]

    def line(self) -> str:
        twenty = f"{self.mean_20d:+.2f}% (n={self.n_20d})" if self.mean_20d is not None else "not yet marked"
        return (
            f"- {self.direction} verdicts, voted confidence {self.band}, horizon {self.horizon}: "
            f"n={self.n}, 5d hit {self.hit_5d:.0%}, mean 5d excess {self.mean_5d:+.2f}%, mean 20d excess {twenty}"
        )


def _excess(rows: dict[tuple[str, date], ForwardRow], entry: FunnelEntry, horizon: int) -> Optional[float]:
    ticker = entry.primary_ticker
    if ticker is None:
        return None
    row = rows.get((ticker.upper(), entry.observed_at.date()))
    if row is None:
        return None
    mark = row.marks.get(horizon)
    if mark is None or mark.excess_pct is None:
        return None
    return float(mark.excess_pct)


def measured_cells(
    entries: Iterable[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    min_n: int = MIN_N,
) -> list[CalibrationCell]:
    """Every (source, band, horizon, direction) cell with n >= min_n marks at
    5d, over VOTED entries only. Direction is part of the key because a decline's
    forward return reads the opposite way from a long's."""
    groups: dict[tuple[str, str, str, str], list[FunnelEntry]] = defaultdict(list)
    for entry in entries:
        if entry.voted_confidence is None or not entry.time_horizon or not entry.direction:
            continue
        groups[(entry.source_id, band_of(entry.voted_confidence), entry.time_horizon, entry.direction)].append(entry)
    out: list[CalibrationCell] = []
    for (source, band, horizon, direction), members in sorted(groups.items()):
        five = [v for v in (_excess(rows, e, 5) for e in members) if v is not None]
        if len(five) < min_n:
            continue
        twenty = [v for v in (_excess(rows, e, 20) for e in members) if v is not None]
        out.append(
            CalibrationCell(
                source_id=source,
                band=band,
                horizon=horizon,
                direction=direction,
                n=len(five),
                hit_5d=sum(1 for v in five if v > 0) / len(five),
                mean_5d=statistics.mean(five),
                n_20d=len(twenty),
                mean_20d=statistics.mean(twenty) if twenty else None,
            )
        )
    return out


HEADER = (
    "MEASURED RECORD (this system's own forward returns for this source, by the "
    "voted-median confidence band and the stated horizon, n >= 20 only; excess over "
    "SPY from the observation date; evidence to weigh, nothing more):"
)


def render_measured_record(cells: Iterable[CalibrationCell], source_id: str) -> str:
    """The fenced block for one source, or "" when no cell has reached n."""
    mine = [c for c in cells if c.source_id == source_id]
    if not mine:
        return ""
    return "\n".join([HEADER, "```", *(c.line() for c in mine), "```"])


class CalibrationRecord:
    """Holds the cells and answers the research pass per signal. Built at
    startup from the funnel entries and the forward engine's cached rows
    (the paper unit is a fresh process each morning); ``refresh`` recomputes."""

    def __init__(self, entries: Iterable[FunnelEntry] = (), rows: Optional[dict] = None, min_n: int = MIN_N) -> None:
        self._min_n = min_n
        self._cells: list[CalibrationCell] = []
        self.refresh(entries, rows or {})

    def refresh(self, entries: Iterable[FunnelEntry], rows: dict[tuple[str, date], ForwardRow]) -> None:
        self._cells = measured_cells(entries, rows, self._min_n)

    @property
    def cells(self) -> tuple[CalibrationCell, ...]:
        return tuple(self._cells)

    def for_signal(self, signal) -> str:
        return render_measured_record(self._cells, str(getattr(signal, "source_id", "")))

    def for_source(self, source_id: str) -> str:
        return render_measured_record(self._cells, source_id)


def provider_from(record: CalibrationRecord) -> Callable[[object], str]:
    return record.for_signal
