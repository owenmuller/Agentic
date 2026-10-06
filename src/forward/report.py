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
from datetime import date, timedelta
from decimal import Decimal
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


def _weighted_line(
    label: str,
    entries: list[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    horizon: int,
) -> str:
    """Row mean AND ticker-weighted mean, side by side (verdict package for
    2026-10-15, requested 2026-09-22): a disclosure re-emitted by several
    filers, or one filer's spree, is many funnel rows on one name, and two
    names carried the >50K band to +12% at 20d while the other eleven read
    negative. The line also counts distinct tickers and observation days, so a
    cell that is one date's cross-section says so."""
    marked: list[tuple[FunnelEntry, float]] = []
    for entry in entries:
        ticker = entry.primary_ticker
        if ticker is None:
            continue
        row = rows.get((ticker.upper(), entry.observed_at.date()))
        mark = row.marks.get(horizon) if row is not None else None
        if mark is not None and mark.excess_pct is not None:
            marked.append((entry, float(mark.excess_pct)))
    if not marked:
        return f"  {label}, {horizon}d: no resolved marks yet"
    values = [v for _, v in marked]
    by_ticker: dict[str, list[float]] = {}
    for entry, value in marked:
        by_ticker.setdefault(entry.primary_ticker.upper(), []).append(value)  # type: ignore[union-attr]
    weighted = [statistics.mean(v) for v in by_ticker.values()]
    top = sorted(by_ticker.items(), key=lambda kv: -len(kv[1]))[:2]
    top_share = sum(len(v) for _, v in top) / len(values)
    days = len({entry.observed_at.date() for entry, _ in marked})
    hit = sum(1 for v in values if v > 0) / len(values)
    weighted_hit = sum(1 for v in weighted if v > 0) / len(weighted)
    note = " (small sample)" if len(by_ticker) < 20 else ""
    return (
        f"  {label}, {horizon}d: rows mean {statistics.mean(values):+.2f}%, median "
        f"{statistics.median(values):+.2f}%, hit {hit:.0%} (n={len(values)}) | "
        f"ticker-weighted mean {statistics.mean(weighted):+.2f}%, median "
        f"{statistics.median(weighted):+.2f}%, hit {weighted_hit:.0%} "
        f"({len(by_ticker)} tickers, {days} observation day{'s' if days != 1 else ''}) | "
        f"top-2 tickers {top_share:.0%} of rows: "
        + ", ".join(f"{t} x{len(v)} ({statistics.mean(v):+.1f}%)" for t, v in top)
        + note
    )


def _populates_line(
    label: str, entries: list[FunnelEntry], horizon: int, min_tickers: int = 20
) -> str:
    """When a slice's ``horizon``-day cell first exists and when it reaches
    ``min_tickers`` distinct tickers — from the arrivals already in the funnel,
    so the review knows whether the cell can exist on its date."""
    seen: set[str] = set()
    first_due = None
    enough_due = None
    for entry in sorted(entries, key=lambda e: e.observed_at):
        ticker = entry.primary_ticker
        if ticker is None:
            continue
        due = entry.observed_at.date() + timedelta(days=horizon)
        if first_due is None:
            first_due = due
        seen.add(ticker.upper())
        if len(seen) >= min_tickers:
            enough_due = due
            break
    if first_due is None:
        return f"{label}, {horizon}d: no arrivals yet"
    line = f"{label}, {horizon}d cell: first mark due {first_due.isoformat()}"
    if enough_due is not None:
        line += f"; {min_tickers} distinct tickers marked from {enough_due.isoformat()}"
    else:
        line += (
            f"; only {len(seen)} distinct ticker(s) have arrived — {min_tickers} "
            f"needs more arrivals before a date can be named"
        )
    return line + " (marks land at the first Friday refresh on or after the due date)"


def source_excess_summary(
    entries: Iterable[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    horizons: tuple[int, ...] = (5, 20),
) -> dict[str, dict[int, tuple[float, int]]]:
    """Per source: (mean excess %, n) at each horizon over every funnel row —
    the forward columns of the feed-spend table."""
    entries = list(entries)
    out: dict[str, dict[int, tuple[float, int]]] = {}
    for source in sorted({e.source_id for e in entries}):
        members = [e for e in entries if e.source_id == source]
        for horizon in horizons:
            values = _excess_values(members, rows, horizon)
            if values:
                out.setdefault(source, {})[horizon] = (statistics.mean(values), len(values))
    return out


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
    # for 2026-10-27; moved from 10-15 by the scheduling ruling 2026-09-29 so
    # the 60d marks landing ~10-25 are in the package), >50K — at 5d, 20d
    # and 60d. The slice the review rules on.
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
                "excess at 5d / 20d / 60d; the 2026-10-27 verdict package (moved "
                "from 10-15 so the 60d marks are in it): row "
                "means beside ticker-weighted means, so no band is carried by two "
                "names:",
            ]
        )
        for ceiling, label in _FLOOR_BANDS:
            members = [
                e
                for e in purchases
                if (band := _floor_band(e.amount_range)) is not None and band == label
            ]
            if members:
                for horizon in (5, 20, 60):
                    lines.append("  " + _weighted_line(label, members, rows, horizon))
        lines.append("  " + _weighted_line("all purchases", purchases, rows, 20))
        live = [e for e in purchases if e.observed_at.date() >= date(2026, 9, 1)]
        lines.append(
            "  " + _weighted_line("live flow only (observed since 2026-09-01)", live, rows, 20)
        )

    # Form 4 cluster rule (ruling 2026-09-02): the prefiltered singles are the
    # control group. If singles' forward returns match clusters', the >=2-insider
    # requirement is filtering noise-free signal and a human should hear it.
    # Verdict package for 2026-10-15 / 10-27 (requested 2026-09-28): 5/20/60d,
    # row means beside ticker-weighted means, distinct tickers and observation
    # days, the caveats printed, and when the 60d cells populate.
    form4 = [e for e in with_ticker if e.source_id == "form4_insiders"]
    if form4:
        c_suite = [e for e in form4 if e.form4_qualification == "c_suite_single"]
        singles = [e for e in form4 if e.code == "no_cluster"]
        bearish = [e for e in form4 if e.code == "bearish_measurement"]
        clustered = [
            e
            for e in form4
            if e.code not in ("no_cluster", "bearish_measurement") and e not in c_suite
        ]
        capped_codes = ("source_cap", "class_cap", "aged_out_capped", "same_name_today")
        lines.extend(
            [
                "",
                "Form 4 doors — verdict package (review 2026-10-15 / 10-27): excess "
                "at 5d / 20d / 60d, row means beside ticker-weighted means. Does "
                "requiring >=2 insiders earn its keep against the singles control, "
                "and do C-suite singles (ruling 2026-09-15) earn theirs?",
            ]
        )
        for label, members in (
            ("clusters (>=2 insiders), all funnel rows", clustered),
            ("clusters researched", [e for e in clustered if e.code not in capped_codes]),
            ("clusters capped, never researched", [e for e in clustered if e.code in capped_codes]),
            ("C-suite singles >= $250K", c_suite),
            ("singles (prefiltered control)", singles),
            ("sell clusters (bearish measurement; negative = right)", bearish),
        ):
            if not members:
                continue
            for horizon in (5, 20, 60):
                lines.append("  " + _weighted_line(label, members, rows, horizon))
        lines.extend(
            [
                "  caveats: a cell whose observation days are few is ONE market path, "
                "however many tickers it spans; under 20 distinct tickers is a small "
                "sample; the literature's cluster effect is measured at 1-12 MONTHS, "
                "not 20 days — 20d and 60d are the earliest reads, not the test.",
                "  " + _populates_line("clusters (>=2 insiders)", clustered, 60),
                "  " + _populates_line("singles control", singles, 60),
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
        # Regime concentration (2026-09-22 check): the backfilled stress windows
        # are three market paths, not a sample of them — a tier that pays in the
        # 2020 V-recovery and loses in Q4 2018 is a regime read. By observation
        # year, core tier, with the ticker-weighted mean beside the row mean.
        core = [e for e in overreaction if facts(e) and facts(e).tier == "core"]
        if core:
            lines.append(
                "  core tier by observation year (regime concentration; ticker-"
                "weighted beside rows):"
            )
            for year in sorted({e.observed_at.year for e in core}):
                members = [e for e in core if e.observed_at.year == year]
                lines.append("  " + _weighted_line(f"core {year}", members, rows, 60))

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
    lines.extend(gov_award_section(with_ticker, rows))
    return "\n".join(lines)


def _open_excess_values(
    entries: Iterable[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    horizon: int,
) -> list[float]:
    """Excess from the NEXT-SESSION OPEN (ruling 2026-10-06), where the row has it."""
    values: list[float] = []
    for entry in entries:
        ticker = entry.primary_ticker
        if ticker is None:
            continue
        row = rows.get((ticker.upper(), entry.observed_at.date()))
        if row is None:
            continue
        mark = row.marks.get(horizon)
        if mark is None or mark.open_excess_pct is None:
            continue
        values.append(float(mark.open_excess_pct))
    return values


def _pre_drift_values(
    entries: Iterable[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
) -> list[float]:
    """Close(t-5) -> close(t0) excess, read off the symbol's OWN earlier rows
    where the funnel happens to hold one five calendar days before; otherwise
    absent. The backtest computes it directly from bars; the weekly reports
    what the cache can say."""
    values: list[float] = []
    for entry in entries:
        ticker = entry.primary_ticker
        if ticker is None:
            continue
        earlier = rows.get((ticker.upper(), entry.observed_at.date() - timedelta(days=5)))
        if earlier is None:
            continue
        mark = earlier.marks.get(5)
        if mark is None or mark.excess_pct is None:
            continue
        values.append(float(mark.excess_pct))
    return values


def _award_line(
    label: str,
    members: list[FunnelEntry],
    rows: dict[tuple[str, date], ForwardRow],
    horizon: int,
) -> str:
    """Close-to-close beside open-to-close at one horizon: the gap nobody
    could trade is the difference between the two."""
    close_values = _excess_values(members, rows, horizon)
    open_values = _open_excess_values(members, rows, horizon)
    if not close_values and not open_values:
        return f"  {label} {horizon}d: no resolved marks yet"

    def part(name: str, values: list[float]) -> str:
        if not values:
            return f"{name} --"
        hit = sum(1 for v in values if v > 0) / len(values)
        return (
            f"{name} mean {statistics.mean(values):+.2f}% med "
            f"{statistics.median(values):+.2f}% hit {hit:.0%} (n={len(values)})"
        )

    tickers = len({e.primary_ticker for e in members if e.primary_ticker})
    return (
        f"  {label} {horizon}d: {part('close->close', close_values)} | "
        f"{part('next-open->close', open_values)} | tickers={tickers}"
    )


def gov_award_section(
    entries: list[FunnelEntry], rows: dict[tuple[str, date], ForwardRow]
) -> list[str]:
    """Government contract awards by subtype (ruling 2026-10-06): every award
    in the funnel, researched or not, by the determinants the literature names
    — relative size, new vs modification, multiple-award / IDIQ / ceiling,
    sole-source, agency, awardee size, feed — with the tradeable next-open
    path beside close-to-close. Measurement-first: this table, not the
    category average, decides whether a trading path is built."""
    awards = [e for e in entries if e.source_id == "gov_contract_awards" and e.gov_award is not None]
    if not awards:
        return []
    lines = [
        "",
        "Government contract awards by subtype (ruling 2026-10-06, measurement-first; "
        "excess vs SPY; the digest posts after the close, so next-open->close is the "
        "tradeable path and close->close includes the gap):",
    ]
    facts = lambda e: e.gov_award  # noqa: E731 - local alias for readability
    slices: list[tuple[str, list[FunnelEntry]]] = [
        ("all awards", awards),
        ("tier research", [e for e in awards if facts(e).tier == "research"]),
        ("tier measure", [e for e in awards if facts(e).tier == "measure"]),
        ("tier below_floor (control)", [e for e in awards if facts(e).tier == "below_floor"]),
        ("new award", [e for e in awards if facts(e).kind == "new"]),
        ("modification / option / ceiling", [e for e in awards if facts(e).kind != "new"]),
        ("single awardee, not IDIQ", [e for e in awards if not facts(e).multiple_award and not facts(e).idiq]),
        ("multiple-award or IDIQ", [e for e in awards if facts(e).multiple_award or facts(e).idiq]),
        ("ceiling value stated", [e for e in awards if facts(e).ceiling_stated]),
        ("sole-source language", [e for e in awards if facts(e).sole_source]),
        ("military", [e for e in awards if facts(e).military]),
        ("civilian", [e for e in awards if not facts(e).military]),
        ("feed dod_digest", [e for e in awards if facts(e).feed == "dod_digest"]),
        ("feed fpds", [e for e in awards if facts(e).feed == "fpds"]),
    ]
    for band in ("<0.2%", "0.2-1%", "1-5%", ">=5%", "unknown"):
        slices.append((f"award/mcap {band}", [e for e in awards if facts(e).rel_band == band]))
    for band in ("small", "mid", "mega", "unknown"):
        slices.append((f"awardee cap {band}", [e for e in awards if facts(e).mcap_band == band]))
    slices.append(
        (
            "PRE-REGISTERED RULE: new + single + >=1% of cap",
            [e for e in awards if facts(e).kind == "new" and not facts(e).multiple_award and facts(e).rel_mcap is not None and facts(e).rel_mcap >= Decimal("1")],
        )
    )
    for label, members in slices:
        if not members:
            continue
        lines.append(f"  {label}: {len(members)} rows")
        for horizon in (1, 5, 20, 60):
            lines.append(_award_line("   ", members, rows, horizon))
    pre = _pre_drift_values(awards, rows)
    if pre:
        lines.append(_stat_line("  pre-announcement drift t-5->t0 (where the cache holds the earlier row)", pre))
    researched = [e for e in awards if e.confidence is not None]
    lines.append(
        f"  researched: {len(researched)} rows; verdict codes: "
        + ", ".join(f"{code}={n}" for code, n in sorted(_count_codes(researched).items()))
    )
    return lines


def _count_codes(entries: Iterable[FunnelEntry]) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in entries:
        key = entry.code or entry.bucket
        out[key] = out.get(key, 0) + 1
    return out


def wanted_pairs(entries: list[FunnelEntry]) -> set[tuple[str, date]]:
    """The (symbol, observed date) pairs a report over these entries needs."""
    return {
        (entry.primary_ticker.upper(), entry.observed_at.date())
        for entry in entries
        if entry.primary_ticker is not None
    }
