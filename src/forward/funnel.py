"""The funnel, flattened: one entry per signal the system ever looked at.

Reads the audit log's decision and stage-rejection records and normalises them
into rows the forward-return report can group: which source, which instruments,
where the signal stopped, and — when research ran — what confidence it scored.

Buckets, in funnel order:

  prefiltered        killed by a deterministic rule before any spend
  triaged_out        the cheap triage gate said no (~$0.02, no pass)
  research_failed    the research CALL produced no verdict (outage, malformed)
  declined           research produced a verdict and sizing resolved it to
                     nothing — the bucket the "does research add value" question
                     compares against trades
  order_construction a size existed but no order could be built
  gate_rejected      the deterministic gate said no
  traded             approved; the position's own P&L is in attribution

Mechanical records are excluded: the control arm's entries are copies of the
judged funnel's inputs, and counting the same disclosure twice would double it.
Execution-stage rejections share their decision_id with a DecisionRecord and are
folded into it rather than counted again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from audit.records import (
    AuditRecord,
    DecisionRecord,
    RejectedStage,
    StageRejectionRecord,
    snapshot_amount_range,
    snapshot_lag_days,
    snapshot_stake_percent,
    snapshot_tickers,
    snapshot_transaction,
)
from signals import SignalClass

_STAGE_BUCKETS = {
    RejectedStage.PRE_FILTER: "prefiltered",
    RejectedStage.TRIAGE: "triaged_out",
    RejectedStage.RESEARCH: "research_failed",
    RejectedStage.SIZING: "declined",
    RejectedStage.ORDER_CONSTRUCTION: "order_construction",
}


@dataclass(frozen=True, slots=True)
class FunnelEntry:
    """One signal's terminal state in the funnel, plus what forward returns need."""

    decision_id: str
    source_id: str
    #: Per-member identity where one exists (congressional), else the source.
    credibility_key: str
    signal_class: SignalClass
    observed_at: datetime
    tickers: tuple[str, ...]
    bucket: str
    #: The rejection code where one exists ("pre_filter", "source_cap", ...).
    code: str
    confidence: Optional[int]
    #: Rendered disclosure lag, congressional rows only.
    lag_days: Optional[int]
    #: The disclosure's transaction and amount range, congressional rows only
    #: (disclosure-reaction ruling 2026-09-02). "" where the content has none.
    transaction: str = ""
    amount_range: str = ""
    #: A 13D's stake percent (bearish-groundwork ruling 2026-09-02): successive
    #: filings by the same activist on a name turn into increase/reduction
    #: events the report grades. None on every other source.
    stake_percent: Optional[Decimal] = None

    @property
    def primary_ticker(self) -> Optional[str]:
        return self.tickers[0] if self.tickers else None


def funnel_entries(records: Iterable[AuditRecord]) -> list[FunnelEntry]:
    """Flatten the log. First record per decision_id wins; execution rejections
    and later records against the same id never add a second entry."""
    seen: set[str] = set()
    out: list[FunnelEntry] = []
    for record in records:
        if isinstance(record, DecisionRecord):
            if record.sizing.strategy in ("mechanical", "cash_sweep"):
                continue  # copies of judged inputs / parked cash — not signals
            if record.decision_id in seen:
                continue
            seen.add(record.decision_id)
            out.append(
                FunnelEntry(
                    decision_id=record.decision_id,
                    source_id=record.signal.source_id,
                    credibility_key=(
                        record.signal.credibility_key or record.signal.source_id
                    ),
                    signal_class=record.signal.signal_class,
                    observed_at=record.signal.observed_at,
                    tickers=snapshot_tickers(record.signal),
                    bucket="traded" if record.was_approved else "gate_rejected",
                    code="" if record.was_approved else (record.gate.rejection_code or ""),
                    confidence=(
                        record.research.confidence
                        if record.research is not None
                        else None
                    ),
                    lag_days=snapshot_lag_days(record.signal),
                    transaction=snapshot_transaction(record.signal),
                    amount_range=snapshot_amount_range(record.signal),
                    stake_percent=snapshot_stake_percent(record.signal),
                )
            )
        elif isinstance(record, StageRejectionRecord):
            bucket = _STAGE_BUCKETS.get(record.stage)
            if bucket is None:
                continue  # execution shares a decision's id; internal_error is a bug
            if record.decision_id in seen:
                continue
            seen.add(record.decision_id)
            out.append(
                FunnelEntry(
                    decision_id=record.decision_id,
                    source_id=record.signal.source_id,
                    credibility_key=(
                        record.signal.credibility_key or record.signal.source_id
                    ),
                    signal_class=record.signal.signal_class,
                    observed_at=record.signal.observed_at,
                    tickers=snapshot_tickers(record.signal),
                    bucket=bucket,
                    code=record.code,
                    confidence=(
                        record.research.confidence
                        if record.research is not None
                        else None
                    ),
                    lag_days=snapshot_lag_days(record.signal),
                    transaction=snapshot_transaction(record.signal),
                    amount_range=snapshot_amount_range(record.signal),
                    stake_percent=snapshot_stake_percent(record.signal),
                )
            )
    return out
