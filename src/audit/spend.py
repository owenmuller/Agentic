"""Feed spend against what it bought (requested 2026-09-28 for the 2026-10-15
feed-spend ruling): one row per source — the subscription, the LLM research it
caused, the candidates it delivered, the passes it took, the trades it
produced, what those trades realised, the source's own forward excess, and
what another 90 days of it would cost — so the ruling "does this feed buy
anything measurable" is answerable from one table.

Scheduling ruling 2026-09-29: feed spend rules on 2026-10-15 on spend alone
(the X-fed callers are decidable on passes and trades, not forward returns);
the congressional signal-quality verdict moved to 2026-10-27 so its 60-day
marks are in the package.

Counting rules, all deterministic and all mirrored from the existing counters:
  candidates     distinct decision ids the source delivered to the funnel at
                 all — the forward report's funnel rule: a judged
                 DecisionRecord or a StageRejectionRecord at a funnel stage
                 (pre-filter, triage, research, sizing, order construction),
                 first record per id. Pre-filtered signals count: they were
                 delivered and paid for by the feed, just never researched.
  next 90 days   feed = monthly_cost x 3; research = research $ to date
                 scaled from the source's elapsed days (its start_date, or
                 its first funnel record without one) to 90 days. A
                 projection of the observed rate, nothing more.
  passes         distinct decision ids that bought a research pass — a
                 DecisionRecord (mechanical, sweep and baseline strategies
                 excluded: no LLM ran) or a StageRejectionRecord past the
                 pre-filter and triage stages (``research_passes_by_source_on``).
  research $     est_cost_usd once per decision id (entry pass), plus the
                 triage screen and boundary second-pass estimates where the
                 record carries them, plus every thesis review under that
                 decision id (reviews are their own passes). Estimates — the
                 console bill is truth.
  feed $         config monthly_cost, and the bill since the source's
                 start_date (``SignalsConfig.feed_cost_breakdown``).
  traded/closed  judged trails: approved with a buy fill; closed = has an
                 outcome; realised = the outcomes' P&L, gross of costs.
  net            realised − research $ − feed $ to date. Judged arm only: the
                 mechanical arm copies congressional disclosures through the
                 same feed and is reported on its own line by the attribution.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Optional

from audit.records import (
    AuditRecord,
    AuditTrail,
    DecisionRecord,
    RejectedStage,
    StageRejectionRecord,
    ThesisReviewRecord,
)

ZERO = Decimal("0")
CENTS = Decimal("0.01")
NON_JUDGED_STRATEGIES = ("mechanical", "cash_sweep", "baseline")
#: The stages at which a StageRejectionRecord is a funnel candidate — the
#: forward report's rule (forward.funnel._STAGE_BUCKETS), restated here because
#: audit may not import forward. Execution shares a decision's id and
#: internal_error is a bug, so neither is a candidate.
FUNNEL_STAGES = frozenset(
    {
        RejectedStage.PRE_FILTER,
        RejectedStage.TRIAGE,
        RejectedStage.RESEARCH,
        RejectedStage.SIZING,
        RejectedStage.ORDER_CONSTRUCTION,
    }
)
PROJECTION_DAYS = 90
PRORATION_MONTH_DAYS = 30


@dataclass(frozen=True, slots=True)
class FeedSpendRow:
    source_id: str
    class_key: str
    monthly_cost: Decimal
    #: Billed since the source's start date (prorated), or zero for free feeds.
    feed_to_date: Decimal
    research_cost: Decimal
    passes: int
    traded: int
    open_positions: int
    closed: int
    wins: int
    realised_pnl: Decimal
    #: (mean excess %, n) over every funnel row of the source, or None.
    forward_5d: Optional[tuple[float, int]] = None
    forward_20d: Optional[tuple[float, int]] = None
    #: Funnel candidates the source delivered, researched or not.
    candidates: int = 0
    #: Days the source has been running (start_date, else first funnel record,
    #: to the report's as-of), at least 1; the base of the 90-day projection.
    elapsed_days: int = 0

    @property
    def net(self) -> Decimal:
        return (self.realised_pnl - self.research_cost - self.feed_to_date).quantize(CENTS)

    @property
    def feed_next_90(self) -> Decimal:
        """Another 90 days of the subscription."""
        return (
            self.monthly_cost * Decimal(PROJECTION_DAYS) / Decimal(PRORATION_MONTH_DAYS)
        ).quantize(CENTS)

    @property
    def research_next_90(self) -> Decimal:
        """Research spend at the observed rate, scaled to 90 days; zero when
        the source has no history to scale from."""
        if self.elapsed_days <= 0:
            return ZERO
        return (
            self.research_cost * Decimal(PROJECTION_DAYS) / Decimal(self.elapsed_days)
        ).quantize(CENTS)

    @property
    def next_90(self) -> Decimal:
        return (self.feed_next_90 + self.research_next_90).quantize(CENTS)

    @property
    def cost_per_trade(self) -> Optional[Decimal]:
        if self.traded <= 0:
            return None
        return ((self.research_cost + self.feed_to_date) / self.traded).quantize(CENTS)


def feed_spend_rows(
    records: Iterable[AuditRecord],
    trails: Iterable[AuditTrail],
    sources: Iterable[tuple[str, str, Decimal]],
    feed_to_date: Mapping[str, Decimal],
    forward_by_source: Optional[Mapping[str, Mapping[int, tuple[float, int]]]] = None,
    start_dates: Optional[Mapping[str, Optional[date]]] = None,
    as_of: Optional[datetime] = None,
) -> tuple[FeedSpendRow, ...]:
    """One row per configured source (``(source_id, class_key, monthly_cost)``),
    since inception, sorted by feed cost then research cost, descending.
    ``start_dates`` (source id -> config start_date) and ``as_of`` feed the
    90-day projection; without them a source's elapsed days run from its first
    funnel record to now."""
    records = list(records)
    passes: dict[str, set[str]] = {}
    cost: dict[str, Decimal] = {}
    source_of: dict[str, str] = {}
    entry_costed: set[str] = set()
    candidates: dict[str, set[str]] = {}
    first_seen: dict[str, date] = {}
    for record in records:
        if isinstance(record, (DecisionRecord, StageRejectionRecord)):
            source_of.setdefault(record.decision_id, record.signal.source_id)
    for record in records:
        if isinstance(record, DecisionRecord):
            if record.sizing.strategy in NON_JUDGED_STRATEGIES:
                continue
            source = record.signal.source_id
            _note_candidate(candidates, first_seen, record)
            passes.setdefault(source, set()).add(record.decision_id)
        elif isinstance(record, StageRejectionRecord):
            if record.stage in FUNNEL_STAGES:
                _note_candidate(candidates, first_seen, record)
            if record.stage in (RejectedStage.PRE_FILTER, RejectedStage.TRIAGE):
                # Triage spends dollars but not a pass; its screen cost is
                # picked up below through the record's own estimate.
                source = record.signal.source_id
                _add_cost(cost, source, record, entry_costed)
                continue
            source = record.signal.source_id
            passes.setdefault(source, set()).add(record.decision_id)
        elif isinstance(record, ThesisReviewRecord):
            source = source_of.get(record.decision_id)
            if source is None:
                continue
            review_cost = getattr(record, "est_cost_usd", None)
            if review_cost is not None:
                cost[source] = cost.get(source, ZERO) + review_cost
            continue
        else:
            continue
        _add_cost(cost, source, record, entry_costed)

    traded: dict[str, int] = {}
    open_positions: dict[str, int] = {}
    closed: dict[str, int] = {}
    wins: dict[str, int] = {}
    realised: dict[str, Decimal] = {}
    for trail in trails:
        decision = trail.decision
        if decision.sizing.strategy in NON_JUDGED_STRATEGIES:
            continue
        if not decision.was_approved or not any(f.side == "buy" for f in trail.fills):
            continue
        source = decision.signal.source_id
        traded[source] = traded.get(source, 0) + 1
        if trail.outcome is None:
            open_positions[source] = open_positions.get(source, 0) + 1
        else:
            closed[source] = closed.get(source, 0) + 1
            realised[source] = realised.get(source, ZERO) + trail.outcome.realised_pnl
            if trail.outcome.won:
                wins[source] = wins.get(source, 0) + 1

    forward_by_source = forward_by_source or {}
    start_dates = start_dates or {}
    today = (as_of or datetime.now(timezone.utc)).date()
    rows = []
    for source_id, class_key, monthly in sources:
        forward = forward_by_source.get(source_id, {})
        started = start_dates.get(source_id) or first_seen.get(source_id)
        elapsed = max(1, (today - started).days) if started is not None else 0
        rows.append(
            FeedSpendRow(
                source_id=source_id,
                class_key=class_key,
                monthly_cost=monthly,
                feed_to_date=feed_to_date.get(source_id, ZERO),
                research_cost=cost.get(source_id, ZERO).quantize(CENTS),
                passes=len(passes.get(source_id, ())),
                traded=traded.get(source_id, 0),
                open_positions=open_positions.get(source_id, 0),
                closed=closed.get(source_id, 0),
                wins=wins.get(source_id, 0),
                realised_pnl=realised.get(source_id, ZERO).quantize(CENTS),
                forward_5d=forward.get(5),
                forward_20d=forward.get(20),
                candidates=len(candidates.get(source_id, ())),
                elapsed_days=elapsed,
            )
        )
    rows.sort(key=lambda r: (-r.feed_to_date, -r.research_cost, r.source_id))
    return tuple(rows)


#: The day the 2026-10-15 feed-spend table was requested. A caller whose
#: start_date is later than this was (re)wired after the ruling's window
#: and has no trial inside it (human ruling 2026-09-30: citrini, re-pointed
#: from a protected stranger to @citrini, is not graded at 10-15).
FEED_SPEND_RULING_REQUESTED = date(2026, 9, 28)


def split_callers_by_trial(
    callers: Iterable[tuple[str, Optional[date]]],
    requested: date = FEED_SPEND_RULING_REQUESTED,
) -> tuple[list[str], list[str]]:
    """``(graded, untried)``: callers whose start_date is on or before the
    request date are graded by the ruling; later ones are reported on their
    own line and never summed into the callers' finding. A caller with no
    start_date is graded (it has been billed for the whole window)."""
    graded: list[str] = []
    untried: list[str] = []
    for source_id, started in callers:
        if started is not None and started > requested:
            untried.append(source_id)
        else:
            graded.append(source_id)
    return graded, untried


def _note_candidate(
    candidates: dict[str, set[str]], first_seen: dict[str, date], record
) -> None:
    """A funnel candidate: first record per decision id, keyed on the source."""
    source = record.signal.source_id
    ids = candidates.setdefault(source, set())
    if record.decision_id in ids:
        return
    ids.add(record.decision_id)
    observed = record.signal.observed_at.date()
    if source not in first_seen or observed < first_seen[source]:
        first_seen[source] = observed


def _add_cost(cost: dict[str, Decimal], source: str, record, costed: set[str]) -> None:
    """Entry-side estimates, once per decision id: the pass itself, the triage
    screen and the boundary second pass where the record carries them."""
    if record.decision_id in costed:
        return
    costed.add(record.decision_id)
    total = ZERO
    for field in ("est_cost_usd", "screen_est_cost_usd", "second_est_cost_usd"):
        value = getattr(record, field, None)
        if value is not None:
            total += value
    if total:
        cost[source] = cost.get(source, ZERO) + total


def render_feed_spend(
    rows: Iterable[FeedSpendRow],
    generated_at: datetime,
    groups: Optional[Mapping[str, Iterable[str]]] = None,
) -> str:
    """The table, then one subtotal line per named group of sources (the
    2026-10-15 feed-spend ruling reads the X-fed callers as one line)."""
    rows = list(rows)
    lines = [
        f"Feed spend against what it bought (since inception, to {generated_at.date()}; "
        f"for the 2026-10-15 feed-spend ruling, which rules on spend alone — the "
        f"congressional signal-quality verdict is 2026-10-27; judged arm only, the "
        f"mechanical arm's copies are on the attribution's own line; research $ are "
        f"estimates, the console bill is truth; next 90d = feed x 3 months + "
        f"research at the observed daily rate):",
        "  source                      class    feed $/mo  feed $ to date  research $  cands  passes  traded  open  closed  won  realised $   net $    next 90d $     5d excess (n)     20d excess (n)",
    ]
    for r in rows:
        f5 = f"{r.forward_5d[0]:+.2f}% ({r.forward_5d[1]})" if r.forward_5d else "—"
        f20 = f"{r.forward_20d[0]:+.2f}% ({r.forward_20d[1]})" if r.forward_20d else "—"
        lines.append(
            f"  {r.source_id:26} {r.class_key:8} {r.monthly_cost:>8.2f}  {r.feed_to_date:>14.2f}  "
            f"{r.research_cost:>10.2f}  {r.candidates:>5d}  {r.passes:>6d}  {r.traded:>6d}  "
            f"{r.open_positions:>4d}  {r.closed:>6d}  {r.wins:>3d}  {r.realised_pnl:>+10.2f}  "
            f"{r.net:>+9.2f}  {r.next_90:>11.2f}  {f5:>16}  {f20:>16}"
        )
    paid = [r for r in rows if r.monthly_cost > ZERO]
    if paid:
        lines.append(
            "  paid feeds: "
            + "; ".join(
                f"{r.source_id} ${r.monthly_cost}/mo -> {r.candidates} candidate(s), "
                f"{r.passes} pass(es), {r.traded} trade(s), "
                f"{r.closed} closed, realised {r.realised_pnl:+.2f}, net {r.net:+.2f}"
                + (f", cost per trade {r.cost_per_trade}" if r.cost_per_trade is not None else "")
                + f", next 90d {r.next_90:.2f} (feed {r.feed_next_90:.2f} + research "
                f"{r.research_next_90:.2f} over {r.elapsed_days}d observed)"
                for r in paid
            )
        )
    by_id = {r.source_id: r for r in rows}
    for label, members in (groups or {}).items():
        group = [by_id[m] for m in members if m in by_id]
        if not group:
            continue
        lines.append(
            f"  {label}: {sum(r.candidates for r in group)} candidates, "
            f"{sum(r.passes for r in group)} passes, {sum(r.traded for r in group)} trades, "
            f"realised {sum((r.realised_pnl for r in group), ZERO):+.2f}; "
            f"feed {sum((r.feed_to_date for r in group), ZERO):.2f} + research "
            f"{sum((r.research_cost for r in group), ZERO):.2f} to date; "
            f"next 90d {sum((r.next_90 for r in group), ZERO):.2f} "
            f"(feed {sum((r.feed_next_90 for r in group), ZERO):.2f} + research "
            f"{sum((r.research_next_90 for r in group), ZERO):.2f})"
        )
    total_feed = sum((r.feed_to_date for r in rows), ZERO)
    total_research = sum((r.research_cost for r in rows), ZERO)
    total_realised = sum((r.realised_pnl for r in rows), ZERO)
    total_next = sum((r.next_90 for r in rows), ZERO)
    lines.append(
        f"  all sources: feed {total_feed:.2f} + research {total_research:.2f} = "
        f"{(total_feed + total_research):.2f} spent; realised {total_realised:+.2f}; "
        f"net {(total_realised - total_feed - total_research):+.2f}; "
        f"next 90d {total_next:.2f}"
    )
    return "\n".join(lines)
