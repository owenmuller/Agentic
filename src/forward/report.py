"""Rendering: the funnel's counterfactual scoreboard.

Sections, each answering one question a human might tune on:

  coverage           how much of the funnel these numbers actually cover
  declined vs taken  does the research layer's judgment beat what it declined?
  by bucket          what each funnel stage's kills went on to do
  by source          per-source decay curves across the horizons
  by filer           congressional members ranked by what their signals did
  by lag bucket      does the disclosure lag rule cut where the data says?

Sample sizes are printed on every line. Nothing here auto-tunes anything —
these numbers argue; a human rules, and rulings are dated.
"""

from __future__ import annotations

import statistics
from datetime import date
from typing import Iterable, Optional

from forward.funnel import FunnelEntry
from forward.returns import HORIZONS, ForwardRow

#: Sections rank groups by this horizon when one number is needed.
KEY_HORIZON = 20

_LAG_BUCKETS = ((0, 7), (8, 14), (15, 30), (31, 45), (46, 10_000))


def _excess_values(
    entries: Iterable[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    horizon: int,
) -> list[float]:
    values: list[float] = []
    for entry in entries:
        ticker = entry.primary_ticker
        if ticker is None:
            continue
        row = rows.get((ticker.upper(), entry.observed_at.date()))
        if row is None:
            continue
        mark = row.marks.get(horizon)
        if mark is None or mark.excess_pct is None:
            continue
        values.append(float(mark.excess_pct))
    return values


def _raw_values(
    entries: Iterable[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    horizon: int,
) -> list[float]:
    values: list[float] = []
    for entry in entries:
        ticker = entry.primary_ticker
        if ticker is None:
            continue
        row = rows.get((ticker.upper(), entry.observed_at.date()))
        if row is None:
            continue
        mark = row.marks.get(horizon)
        if mark is not None:
            values.append(float(mark.return_pct))
    return values


def _stat_line(label: str, values: list[float], suffix: str = "") -> str:
    if not values:
        return f"  {label}: no resolved marks yet{suffix}"
    note = " (small sample)" if len(values) < 20 else ""
    return (
        f"  {label}: mean {statistics.mean(values):+.2f}%, "
        f"median {statistics.median(values):+.2f}% "
        f"(n={len(values)}){note}{suffix}"
    )


def render_forward_report(
    entries: list[FunnelEntry], rows: dict[tuple[str, date], ForwardRow]
) -> str:
    with_ticker = [e for e in entries if e.primary_ticker is not None]
    with_base = [
        e
        for e in with_ticker
        if (row := rows.get((e.primary_ticker.upper(), e.observed_at.date())))
        is not None
        and row.has_base
    ]
    lines = [
        "Forward returns — every signal that entered the funnel, marked at "
        "1/5/20/60/120 calendar days from observation (split-adjusted closes; "
        "excess = same-window SPY subtracted; absent horizons are absent, "
        "never zero)",
        f"Coverage: {len(entries)} funnel entries, {len(with_ticker)} named an "
        f"instrument, {len(with_base)} have price history",
    ]

    # -- does research add value over the funnel? -----------------------------------
    traded = [e for e in with_ticker if e.bucket == "traded"]
    declined = [e for e in with_ticker if e.bucket == "declined"]
    lines.extend(
        [
            "",
            f"Declined vs taken, excess return at {KEY_HORIZON}d (the research "
            "layer earns its keep only if taken beats declined):",
            _stat_line("taken (traded)", _excess_values(traded, rows, KEY_HORIZON)),
            _stat_line(
                "declined by research/sizing",
                _excess_values(declined, rows, KEY_HORIZON),
            ),
        ]
    )

    # -- what each funnel stage's kills went on to do --------------------------------
    lines.extend(["", f"By funnel bucket, excess at {KEY_HORIZON}d:"])
    for bucket in (
        "traded",
        "gate_rejected",
        "declined",
        "order_construction",
        "research_failed",
        "triaged_out",
        "prefiltered",
    ):
        members = [e for e in with_ticker if e.bucket == bucket]
        if members:
            lines.append(
                _stat_line(bucket, _excess_values(members, rows, KEY_HORIZON))
            )

    # -- per-source decay curves -------------------------------------------------------
    lines.extend(
        ["", "By source, mean excess across the horizons (the decay curve):"]
    )
    for source in sorted({e.source_id for e in with_ticker}):
        members = [e for e in with_ticker if e.source_id == source]
        cells = []
        for horizon in HORIZONS:
            values = _excess_values(members, rows, horizon)
            cells.append(
                f"{horizon}d {statistics.mean(values):+.2f}% (n={len(values)})"
                if values
                else f"{horizon}d —"
            )
        lines.append(f"  {source}: " + ", ".join(cells))

    # -- congressional: per filer and per lag bucket ------------------------------------
    congressional = [
        e for e in with_ticker if e.source_id == "congressional_disclosures"
    ]
    if congressional:
        lines.extend(
            ["", f"Congressional, by filer (excess at {KEY_HORIZON}d):"]
        )
        for key in sorted({e.credibility_key for e in congressional}):
            members = [e for e in congressional if e.credibility_key == key]
            filer = key.split("/", 1)[1] if "/" in key else key
            lines.append(
                _stat_line(filer, _excess_values(members, rows, KEY_HORIZON))
            )

        lines.extend(
            [
                "",
                f"Congressional, by disclosure-lag bucket (excess at "
                f"{KEY_HORIZON}d — validates the lag rule with data instead of "
                f"assumption):",
            ]
        )
        for low, high in _LAG_BUCKETS:
            members = [
                e
                for e in congressional
                if e.lag_days is not None and low <= e.lag_days <= high
            ]
            if not members:
                continue
            label = f"{low}-{high}d lag" if high < 10_000 else f"{low}d+ lag"
            lines.append(
                _stat_line(label, _excess_values(members, rows, KEY_HORIZON))
            )

    lines.extend(
        [
            "",
            "These numbers argue; humans rule. Any prefilter, lag, or sizing "
            "change they justify lands as a dated human ruling, never an "
            "auto-tune.",
        ]
    )
    return "\n".join(lines)


def wanted_pairs(entries: list[FunnelEntry]) -> set[tuple[str, date]]:
    """The (symbol, observed date) pairs a report over these entries needs."""
    return {
        (entry.primary_ticker.upper(), entry.observed_at.date())
        for entry in entries
        if entry.primary_ticker is not None
    }
