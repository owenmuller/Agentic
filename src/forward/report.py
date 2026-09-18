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


#: The floor bands of the 2026-09-18 ruling, by range max.
_FLOOR_BANDS = (
    (15_000, "<=15K"),
    (50_000, "15-50K"),
    (10**12, ">50K"),
)


def _floor_band(rendered: str) -> Optional[str]:
    figures = [int(match.replace(",", "")) for match in _NUMBER.findall(rendered or "")]
    if not figures:
        return None
    top = max(figures)
    for ceiling, label in _FLOOR_BANDS:
        if top <= ceiling:
            return label
    return None


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


_OVERREACTION_HORIZONS = (1, 5, 20, 60)


def _overreaction_line(
    label: str,
    entries: list[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
) -> str:
    """One compact line: n events, then mean excess at 1/5/20/60d with each
    horizon's own n (they resolve as the calendar catches up)."""
    if not entries:
        return f"  {label}: no events"
    parts = []
    for horizon in _OVERREACTION_HORIZONS:
        values = _excess_values(entries, rows, horizon)
        if values:
            parts.append(f"{horizon}d {statistics.mean(values):+.2f}% (n={len(values)})")
        else:
            parts.append(f"{horizon}d —")
    return f"  {label}: {len(entries)} events | " + "  ".join(parts)


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

    # -- options doors / theme->ETF (ruling 2026-09-15) -------------------------------
    tagged = [e for e in with_ticker if e.expression_tag]
    if tagged:
        lines.extend(
            ["", f"By expression tag (options doors, theme->ETF), excess at {KEY_HORIZON}d:"]
        )
        for tag in sorted({e.expression_tag for e in tagged}):
            members = [e for e in tagged if e.expression_tag == tag]
            lines.append(_stat_line(tag, _excess_values(members, rows, KEY_HORIZON)))

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
        unresolved: list[tuple[str, int]] = []
        for key in sorted({e.credibility_key for e in congressional}):
            members = [e for e in congressional if e.credibility_key == key]
            filer = key.split("/", 1)[1] if "/" in key else key
            values = _excess_values(members, rows, KEY_HORIZON)
            if values:
                lines.append(_stat_line(filer, values))
            else:
                unresolved.append((filer, len(members)))
        if unresolved:
            # One line, not one per filer (ruling 2026-09-04): fifty-seven rows of
            # "no resolved marks yet" hid the report.
            shown = ", ".join(f"{name} ({n})" for name, n in unresolved[:8])
            more = f", … {len(unresolved) - 8} more" if len(unresolved) > 8 else ""
            lines.append(
                f"  {len(unresolved)} filer(s) with no resolved {KEY_HORIZON}d marks "
                f"yet ({sum(n for _, n in unresolved)} signals): {shown}{more}"
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

    # Congressional floor band (human ruling 2026-09-18, final at $15,001):
    # EVERY congressional purchase in the funnel, researched or not, by the
    # range max — <=15K (prefiltered since the ruling), 15-50K (under review
    # for 2026-10-15), >50K — at 5d and 20d. The slice the review rules on.
    purchases = [
        e
        for e in with_ticker
        if e.source_id == "congressional_disclosures"
        and e.transaction.lower().startswith("purchase")
    ]
    if purchases:
        lines.extend(
            [
                "",
                "Congressional floor band (ruling 2026-09-18: <=15K prefiltered, "
                "15-50K UNDER REVIEW, >50K kept) — every purchase in the funnel, "
                "excess at 5d / 20d:",
            ]
        )
        for ceiling, label in _FLOOR_BANDS:
            members = [
                e
                for e in purchases
                if (band := _floor_band(e.amount_range)) is not None and band == label
            ]
            if members:
                lines.append("  " + _stat_line(f"{label}, 5d", _excess_values(members, rows, 5)))
                lines.append("  " + _stat_line(f"{label}, 20d", _excess_values(members, rows, 20)))

    # Form 4 cluster rule (ruling 2026-09-02): the prefiltered singles are the
    # control group. If singles' forward returns match clusters', the >=2-insider
    # requirement is filtering noise-free signal and a human should hear it.
    form4 = [e for e in with_ticker if e.source_id == "form4_insiders"]
    if form4:
        c_suite = [e for e in form4 if e.form4_qualification == "c_suite_single"]
        singles = [e for e in form4 if e.code == "no_cluster"]
        clustered = [
            e for e in form4 if e.code != "no_cluster" and e not in c_suite
        ]
        lines.extend(
            [
                "",
                f"Form 4 doors (excess at {KEY_HORIZON}d — does requiring >=2 "
                f"insiders earn its keep, and do C-suite singles (ruling "
                f"2026-09-15) earn theirs? Review 2026-10-15):",
                _stat_line(
                    "clustered (researched)",
                    _excess_values(clustered, rows, KEY_HORIZON),
                ),
                _stat_line(
                    "C-suite singles >= $250K (researched)",
                    _excess_values(c_suite, rows, KEY_HORIZON),
                ),
                _stat_line(
                    "singles (prefiltered control)",
                    _excess_values(singles, rows, KEY_HORIZON),
                ),
            ]
        )

    # Add decisions (ruling 2026-09-16): the adds taken on held names against
    # the signals that were held instead. Same question as every door: did the
    # decision beat its counterfactual?
    adds = [e for e in with_ticker if e.is_add]
    held = [
        e for e in with_ticker if e.code in ("already_held_no_add", "add_no_headroom")
    ]
    if adds or held:
        lines.extend(
            [
                "",
                f"Add decisions (excess at {KEY_HORIZON}d; ruling 2026-09-16 — did "
                f"adding on a held name beat holding it?):",
                _stat_line("adds taken", _excess_values(adds, rows, KEY_HORIZON)),
                _stat_line(
                    "held (already_held_no_add / add_no_headroom)",
                    _excess_values(held, rows, KEY_HORIZON),
                ),
            ]
        )

    # 8-K items (ruling 2026-09-15): one row per whitelisted item an entry
    # carried (a filing with two items counts under both), researched items and
    # the bearish measurement-only ones apart. Hard review 2026-10-15.
    form8k = [e for e in entries if e.source_id == "form_8k" and e.form8k_items]
    if form8k:
        lines.extend(["", f"8-K by item (excess at {KEY_HORIZON}d; ruling 2026-09-15, review 2026-10-15):"])
        for item in sorted({i for e in form8k for i in e.form8k_items}):
            members = [e for e in form8k if item in e.form8k_items]
            bearish_members = [e for e in members if e.code == "bearish_measurement"]
            label = f"item {item}"
            if bearish_members and len(bearish_members) == len(members):
                label += " (bearish, measurement only — negative excess = right)"
            lines.append(_stat_line(label, _excess_values(members, rows, KEY_HORIZON)))

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
    # Overreaction candidates (ruling 2026-09-03): measurement-only rows from
    # the deterministic screen. POSITIVE excess means the drop reverted. Four
    # slices so the prior question is answered where it matters.
    overreaction = [e for e in with_ticker if e.code == "overreaction_candidate"]
    if overreaction:
        lines.extend(
            [
                "",
                "Overreaction candidates (measurement only — POSITIVE excess means "
                "the drop reverted; sharp single-session drops on >=1.5x volume in "
                "our signal universe):",
                _overreaction_line("all (>=6% flag)", overreaction, rows),
            ]
        )
        facts = lambda e: e.overreaction  # noqa: E731 - local alias
        slices = [
            ("core (held or researched)", [e for e in overreaction if facts(e) and facts(e).tier == "core"]),
            ("broad (unresearched signal flow)", [e for e in overreaction if facts(e) and facts(e).tier == "broad"]),
            ("market day (SPY <= -2%)", [e for e in overreaction if facts(e) and facts(e).market_day is True]),
            ("idiosyncratic day", [e for e in overreaction if facts(e) and facts(e).market_day is False]),
            ("held positions", [e for e in overreaction if facts(e) and facts(e).held]),
            ("signalled, not held", [e for e in overreaction if facts(e) and not facts(e).held]),
            (">=7% (the ruled X)", [e for e in overreaction if facts(e) and 7 in facts(e).flags]),
            (">=8%", [e for e in overreaction if facts(e) and 8 in facts(e).flags]),
        ]
        for label, members in slices:
            lines.append(_overreaction_line(label, members, rows))

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
