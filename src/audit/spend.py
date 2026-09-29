"""Feed spend against what it bought (requested 2026-09-28 for the 2026-10-15
review): one row per source — the subscription, the LLM research it caused,
the passes it took, the trades it produced, what those trades realised, and
the source's own forward excess — so the ruling "does this feed buy anything
measurable" is answerable from one table.

Counting rules, all deterministic and all mirrored from the existing counters:
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
from datetime import datetime
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

    @property
    def net(self) -> Decimal:
        return (self.realised_pnl - self.research_cost - self.feed_to_date).quantize(CENTS)

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
) -> tuple[FeedSpendRow, ...]:
    """One row per configured source (``(source_id, class_key, monthly_cost)``),
    since inception, sorted by feed cost then research cost, descending."""
    records = list(records)
    passes: dict[str, set[str]] = {}
    cost: dict[str, Decimal] = {}
    source_of: dict[str, str] = {}
    entry_costed: set[str] = set()
    for record in records:
        if isinstance(record, (DecisionRecord, StageRejectionRecord)):
            source_of.setdefault(record.decision_id, record.signal.source_id)
    for record in records:
        if isinstance(record, DecisionRecord):
            if record.sizing.strategy in NON_JUDGED_STRATEGIES:
                continue
            source = record.signal.source_id
            passes.setdefault(source, set()).add(record.decision_id)
        elif isinstance(record, StageRejectionRecord):
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
    rows = []
    for source_id, class_key, monthly in sources:
        forward = forward_by_source.get(source_id, {})
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
            )
        )
    rows.sort(key=lambda r: (-r.feed_to_date, -r.research_cost, r.source_id))
    return tuple(rows)


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


def render_feed_spend(rows: Iterable[FeedSpendRow], generated_at: datetime) -> str:
    rows = list(rows)
    lines = [
        f"Feed spend against what it bought (since inception, to {generated_at.date()}; "
        f"for the 2026-10-15 review — judged arm only, the mechanical arm's copies "
        f"are on the attribution's own line; research $ are estimates, the console "
        f"bill is truth):",
        "  source                      class    feed $/mo  feed $ to date  research $   passes  traded  open  closed  won  realised $   net $      5d excess (n)     20d excess (n)",
    ]
    for r in rows:
        f5 = f"{r.forward_5d[0]:+.2f}% ({r.forward_5d[1]})" if r.forward_5d else "—"
        f20 = f"{r.forward_20d[0]:+.2f}% ({r.forward_20d[1]})" if r.forward_20d else "—"
        lines.append(
            f"  {r.source_id:26} {r.class_key:8} {r.monthly_cost:>8.2f}  {r.feed_to_date:>14.2f}  "
            f"{r.research_cost:>10.2f}  {r.passes:>6d}  {r.traded:>6d}  {r.open_positions:>4d}  "
            f"{r.closed:>6d}  {r.wins:>3d}  {r.realised_pnl:>+10.2f}  {r.net:>+9.2f}  "
            f"{f5:>16}  {f20:>16}"
        )
    paid = [r for r in rows if r.monthly_cost > ZERO]
    if paid:
        lines.append(
            "  paid feeds: "
            + "; ".join(
                f"{r.source_id} ${r.monthly_cost}/mo -> {r.traded} trade(s), "
                f"{r.closed} closed, realised {r.realised_pnl:+.2f}, net {r.net:+.2f}"
                + (f", cost per trade {r.cost_per_trade}" if r.cost_per_trade is not None else "")
                for r in paid
            )
        )
    total_feed = sum((r.feed_to_date for r in rows), ZERO)
    total_research = sum((r.research_cost for r in rows), ZERO)
    total_realised = sum((r.realised_pnl for r in rows), ZERO)
    lines.append(
        f"  all sources: feed {total_feed:.2f} + research {total_research:.2f} = "
        f"{(total_feed + total_research):.2f} spent; realised {total_realised:+.2f}; "
        f"net {(total_realised - total_feed - total_research):+.2f}"
    )
    return "\n".join(lines)
