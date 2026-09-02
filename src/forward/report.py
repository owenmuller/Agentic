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

import re
import statistics
from datetime import date
from typing import Iterable, Optional

from forward.funnel import FunnelEntry
from forward.returns import HORIZONS, ForwardRow
from signals.quiver import _matches_name

#: Sections rank groups by this horizon when one number is needed.
KEY_HORIZON = 20

_LAG_BUCKETS = ((0, 7), (8, 14), (15, 30), (31, 45), (46, 10_000))

#: Disclosure amount bands for the reaction slice (2026-09-02), by range MAX.
_AMOUNT_BANDS = (
    (15_000, "<=15K"),
    (50_000, "15-50K"),
    (100_000, "50-100K"),
    (250_000, "100-250K"),
    (1_000_000, "250K-1M"),
    (10_000_000_000, "1M+"),
)

_NUMBER = re.compile(r"\d[\d,]*")

#: The horizons the reaction slice reads — the pop, if real, is fast.
_REACTION_HORIZONS = (1, 3, 5)


def _amount_band(rendered: str) -> Optional[str]:
    """Band by the range MAX; None when nothing numeric can be read."""
    figures = [int(match.replace(",", "")) for match in _NUMBER.findall(rendered)]
    if not figures:
        return None
    top = max(figures)
    for ceiling, label in _AMOUNT_BANDS:
        if top <= ceiling:
            return label
    return None


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
    entries: list[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    spotlight_filers: tuple[str, ...] = (),
    shadow_closes: tuple = (),
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

    # Disclosure-reaction slice (ruling 2026-09-02): does a publication pop
    # exist on the filers everyone watches? Purchases only, measured from the
    # disclosure's observation, by amount band. MEASUREMENT ONLY — no latency
    # work and no trading path exist until this says the pop is real.
    if spotlight_filers and congressional:
        def is_spotlight(entry: FunnelEntry) -> bool:
            filer = (
                entry.credibility_key.split("/", 1)[1]
                if "/" in entry.credibility_key
                else entry.credibility_key
            )
            return any(_matches_name(filer, name) for name in spotlight_filers)

        spotlight = [
            e
            for e in congressional
            if is_spotlight(e) and "purchase" in e.transaction.lower()
        ]
        lines.extend(
            [
                "",
                f"Disclosure reaction — spotlight-filer PURCHASES "
                f"({len(spotlight)} of {len(congressional)} congressional "
                f"entries), excess at 1/3/5d (does the publication pop exist?):",
            ]
        )
        if not spotlight:
            lines.append("  no spotlight-filer purchases in the log yet")
        else:
            for horizon in _REACTION_HORIZONS:
                lines.append(
                    _stat_line(
                        f"all spotlight purchases, {horizon}d",
                        _excess_values(spotlight, rows, horizon),
                    )
                )
            lines.append("  by amount band (range max), excess at 3d:")
            banded: dict[str, list[FunnelEntry]] = {}
            for entry in spotlight:
                band = _amount_band(entry.amount_range)
                if band is not None:
                    banded.setdefault(band, []).append(entry)
            for _, label in _AMOUNT_BANDS:
                members = banded.get(label)
                if members:
                    lines.append(
                        "  " + _stat_line(label, _excess_values(members, rows, 3))
                    )

    # Form 4 cluster rule (ruling 2026-09-02): the prefiltered singles are the
    # control group. If singles' forward returns match clusters', the >=2-insider
    # requirement is filtering noise-free signal and a human should hear it.
    form4 = [e for e in with_ticker if e.source_id == "form4_insiders"]
    if form4:
        clustered = [e for e in form4 if e.code != "no_cluster"]
        singles = [e for e in form4 if e.code == "no_cluster"]
        lines.extend(
            [
                "",
                f"Form 4 cluster rule (excess at {KEY_HORIZON}d — does "
                f"requiring >=2 insiders earn its keep?):",
                _stat_line(
                    "clustered (researched)",
                    _excess_values(clustered, rows, KEY_HORIZON),
                ),
                _stat_line(
                    "singles (prefiltered control)",
                    _excess_values(singles, rows, KEY_HORIZON),
                ),
            ]
        )

    # Bearish groundwork (ruling 2026-09-02): measurement-only rows, graded
    # before any bearish trading path exists. For BOTH slices a NEGATIVE excess
    # means the bearish signal was right.
    sell_clusters = [
        e
        for e in with_ticker
        if e.source_id == "form4_insiders"
        and e.code == "bearish_measurement"
    ]
    if sell_clusters:
        lines.extend(
            [
                "",
                "Form 4 insider SELL clusters (measurement only — negative "
                "excess means the bearish signal was right):",
                *(
                    _stat_line(
                        f"{horizon}d after the cluster",
                        _excess_values(sell_clusters, rows, horizon),
                    )
                    for horizon in (5, 20, 60)
                ),
            ]
        )
    thirteen_d = sorted(
        (
            e
            for e in with_ticker
            if e.source_id == "form_13d" and e.stake_percent is not None
        ),
        key=lambda e: e.observed_at,
    )
    if thirteen_d:
        last_stake: dict[tuple[str, str], object] = {}
        reductions = []
        increases = []
        for entry in thirteen_d:
            key = (entry.credibility_key, entry.primary_ticker or "")
            previous = last_stake.get(key)
            if previous is not None:
                (reductions if entry.stake_percent < previous else increases).append(
                    entry
                )
            last_stake[key] = entry.stake_percent
        if reductions or increases:
            lines.extend(
                [
                    "",
                    "13D stake changes across successive filings (measurement "
                    "only — a REDUCTION is the bearish event):",
                    _stat_line(
                        f"reductions ({len(reductions)}), {KEY_HORIZON}d",
                        _excess_values(reductions, rows, KEY_HORIZON),
                    ),
                    _stat_line(
                        f"increases ({len(increases)}), {KEY_HORIZON}d",
                        _excess_values(increases, rows, KEY_HORIZON),
                    ),
                ]
            )

    # Exit-authority probation (ruling 2026-09-02): every shadowed review close,
    # graded by what the price did AFTER the model said sell. Negative excess
    # after a shadow means the close would have been right; positive means
    # holding through it paid. The 90-day grant/deny ruling reads this section.
    if shadow_closes:
        lines.extend(
            [
                "",
                f"Shadowed review closes (exit-authority probation): "
                f"{len(shadow_closes)} recorded, graded by the move AFTER the "
                f"close verdict (negative = the close would have been right):",
            ]
        )
        for horizon in (5, 20, 60):
            values: list[float] = []
            for shadow in shadow_closes:
                row = rows.get(
                    (shadow.symbol.upper(), shadow.recorded_at.date())
                )
                if row is None:
                    continue
                mark = row.marks.get(horizon)
                if mark is not None and mark.excess_pct is not None:
                    values.append(float(mark.excess_pct))
            lines.append(_stat_line(f"{horizon}d after the verdict", values))
        for shadow in shadow_closes[-10:]:
            gain = (
                (shadow.mark / shadow.entry_price - 1) * 100
                if shadow.entry_price
                else None
            )
            lines.append(
                f"  {shadow.recorded_at.date()} {shadow.symbol}: shadowed at "
                f"{shadow.mark}"
                + (f" ({gain:+.1f}% over entry)" if gain is not None else "")
                + f", day {shadow.days_held}"
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
