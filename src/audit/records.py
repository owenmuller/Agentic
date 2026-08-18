"""Audit record types.

The shape of the trail
----------------------
CLAUDE.md wants a complete record per order: signal -> thesis -> confidence ->
size -> risk_gate_result -> fill -> outcome. Those stages do not happen at the same
time, and the store is append-only, so they cannot all live in one mutable row.

Instead the trail is a sequence of immutable records keyed by ``decision_id``:

  ``DecisionRecord``   every stage known at decision time: the signal (raw content
                       included), the research verdict, the sized proposal, and the
                       gate's answer. All four are required — a decision record
                       missing any of them fails validation.
  ``FillRecord``       what the broker actually did. Only ever follows an approval.
  ``OutcomeRecord``    what it was worth when the position closed.
  ``CorrectionRecord`` how a mistake is fixed. Nothing is edited or deleted; a
                       correction is a new record naming the one it supersedes.

``AuditTrail`` assembles those back into the single view CLAUDE.md describes.

Rejections are records, not omissions. A rejected order writes a full
``DecisionRecord`` carrying the typed rejection — "risk-gate rejections are signal,
not noise", and a log that only contains the trades you took cannot tell you what
your rules cost you.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from research.reports import ResearchReport
from risk_gate.gate import ApprovedOrder
from risk_gate.rejections import Rejection
from signals import Signal, SignalClass
from sizing.engine import SizedProposal


class _Record(BaseModel):
    """Immutable and closed. An audit record that can be edited is not an audit record."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------------
# Stage snapshots
# --------------------------------------------------------------------------------


class SignalSnapshot(_Record):
    """The signal as it was when it produced this decision.

    Carries ``raw_content`` as well as ``content`` so a later reader can see the whole
    original post, not just the forward-looking slice that was traded on.
    """

    signal_id: str
    source_id: str
    signal_class: SignalClass
    observed_at: datetime
    content: str
    raw_content: str
    classification: Optional[str] = None
    external_id: Optional[str] = None

    @classmethod
    def of(cls, signal: Signal) -> "SignalSnapshot":
        return cls(
            signal_id=signal.signal_id,
            source_id=signal.source_id,
            signal_class=signal.signal_class,
            observed_at=signal.observed_at,
            content=signal.content,
            raw_content=signal.raw_content,
            classification=(
                str(signal.classification) if signal.classification else None
            ),
            external_id=signal.external_id,
        )


class ResearchSnapshot(_Record):
    """The verdict, in full. Confidence and the manipulation finding are called out
    as their own fields because attribution reads them directly."""

    thesis: str
    tickers: list[str]
    direction: str
    time_horizon: str
    priced_in_analysis: Optional[str]
    confidence: int
    invalidation_condition: str
    manipulation_assessment: Optional[str]
    flagged_manipulation: bool

    @classmethod
    def of(cls, report: ResearchReport) -> "ResearchSnapshot":
        return cls(
            thesis=report.thesis,
            tickers=list(report.tickers),
            direction=str(report.direction),
            time_horizon=str(report.time_horizon),
            priced_in_analysis=report.priced_in_analysis,
            confidence=report.confidence,
            invalidation_condition=report.invalidation_condition,
            manipulation_assessment=report.manipulation_assessment,
            flagged_manipulation=report.flags_manipulation,
        )


class SizingSnapshot(_Record):
    """What sizing proposed, before the gate had its say."""

    instrument: str
    sleeve: str
    confidence: int
    sleeve_nav: Decimal
    fraction_of_sleeve_nav: Decimal
    capital: Decimal
    rationale: str
    strategy: Optional[str] = None

    @classmethod
    def of(cls, proposal: SizedProposal) -> "SizingSnapshot":
        return cls(
            instrument=str(proposal.instrument),
            sleeve=str(proposal.sleeve),
            confidence=proposal.confidence,
            sleeve_nav=proposal.sleeve_nav,
            fraction_of_sleeve_nav=proposal.fraction_of_sleeve_nav,
            capital=proposal.capital,
            rationale=proposal.rationale,
            strategy=str(proposal.strategy) if proposal.strategy else None,
        )


class GateSnapshot(_Record):
    """The gate's answer — an approval or a typed rejection, never a bare boolean."""

    approved: bool
    #: Set on approval.
    order: Optional[dict[str, Any]] = None
    max_loss: Optional[Decimal] = None
    approval_sequence: Optional[int] = None
    approved_at: Optional[datetime] = None
    #: Set on rejection. The code is what attribution counts.
    rejection_code: Optional[str] = None
    rejection_message: Optional[str] = None
    rejection_limit: Optional[Decimal] = None
    rejection_observed: Optional[Decimal] = None

    @classmethod
    def of(cls, decision: Union[ApprovedOrder, Rejection]) -> "GateSnapshot":
        if decision.is_approved:
            return cls(
                approved=True,
                order=decision.order.model_dump(mode="json"),
                max_loss=decision.max_loss,
                approval_sequence=decision.sequence,
                approved_at=decision.approved_at,
            )
        return cls(
            approved=False,
            rejection_code=str(decision.code),
            rejection_message=decision.message,
            rejection_limit=decision.limit,
            rejection_observed=decision.observed,
        )


# --------------------------------------------------------------------------------
# The records themselves
# --------------------------------------------------------------------------------


class RecordKind(StrEnum):
    DECISION = "decision"
    FILL = "fill"
    OUTCOME = "outcome"
    CORRECTION = "correction"


class DecisionRecord(_Record):
    """Everything known when the gate answered. All four stages are required."""

    kind: Literal[RecordKind.DECISION] = RecordKind.DECISION
    decision_id: str
    recorded_at: datetime
    signal: SignalSnapshot
    research: ResearchSnapshot
    sizing: SizingSnapshot
    gate: GateSnapshot

    @property
    def was_approved(self) -> bool:
        return self.gate.approved


class FillRecord(_Record):
    """What the broker did. Only ever written against an approved decision."""

    kind: Literal[RecordKind.FILL] = RecordKind.FILL
    decision_id: str
    recorded_at: datetime
    broker_order_id: str
    filled_quantity: Decimal
    fill_price: Decimal
    #: Cash actually committed, as distinct from the worst case the gate reserved.
    filled_value: Decimal


class OutcomeRecord(_Record):
    """What the position was worth when it closed."""

    kind: Literal[RecordKind.OUTCOME] = RecordKind.OUTCOME
    decision_id: str
    recorded_at: datetime
    closed_at: datetime
    realised_pnl: Decimal
    note: str = ""

    @property
    def won(self) -> bool:
        """A flat close is not a win. Ties go against the source (Constraint #6)."""
        return self.realised_pnl > 0


class CorrectionRecord(_Record):
    """Supersedes an earlier record. The only way to change anything.

    The original stays in the log forever. A reader replaying the file sees both the
    mistake and the fix, in the order they happened.
    """

    kind: Literal[RecordKind.CORRECTION] = RecordKind.CORRECTION
    decision_id: str
    recorded_at: datetime
    #: The record this corrects, by its position in the log.
    supersedes_sequence: int
    reason: str
    corrected_fields: dict[str, Any] = Field(default_factory=dict)


AuditRecord = Annotated[
    Union[DecisionRecord, FillRecord, OutcomeRecord, CorrectionRecord],
    Field(discriminator="kind"),
]


class AuditTrail(_Record):
    """The complete per-order view CLAUDE.md describes, assembled from the log."""

    decision: DecisionRecord
    fills: tuple[FillRecord, ...] = ()
    outcome: Optional[OutcomeRecord] = None
    corrections: tuple[CorrectionRecord, ...] = ()

    @property
    def decision_id(self) -> str:
        return self.decision.decision_id

    @property
    def signal_class(self) -> SignalClass:
        return self.decision.signal.signal_class

    @property
    def is_complete(self) -> bool:
        """Has this order run its full course?

        A rejected order is complete the moment it is rejected — there is nothing
        further to happen to it. An approved one needs a fill and a resolved outcome.
        """
        if not self.decision.was_approved:
            return True
        return bool(self.fills) and self.outcome is not None

    @property
    def realised_pnl(self) -> Optional[Decimal]:
        return self.outcome.realised_pnl if self.outcome else None
