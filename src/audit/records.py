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
  ``StageRejectionRecord``
                       a signal that stopped before the gate ever saw it, or an
                       approved order the broker refused. Carries whichever stages did
                       complete and nothing for the ones that did not.
  ``ThesisReviewRecord``
                       one periodic re-research of an open position: hold, close, or
                       the review failed. Keyed by the entry's decision_id, so the
                       whole life of a position reads as one thread.
  ``ExitRecord``       one attempt to close a position — the typed reason, the gate's
                       answer to the sell-to-close order, and what the broker did with
                       it. Exits are decisions too.
  ``FilerEventRecord`` the filer whose disclosure originated a held position filed a
                       NEW disclosure in that name while we held it (ruling
                       2026-09-01). On a judged position it also forces a review; on
                       a mechanical position it changes nothing — recorded so
                       attribution can later price what ignoring the filer's exit
                       cost the arm that ignores it by design.

``AuditTrail`` assembles those back into the single view CLAUDE.md describes.

Rejections are records, not omissions. A rejected order writes a full
``DecisionRecord`` carrying the typed rejection — "risk-gate rejections are signal,
not noise", and a log that only contains the trades you took cannot tell you what
your rules cost you.

The same reasoning runs one step further back. A signal the research layer could not
score, or one sized to nothing, never reaches the gate and so can never produce a
``DecisionRecord`` — every stage of which is mandatory, deliberately. Without a record
of its own it would leave no trace at all, and "the model failed to parse thirty
signals from this source last week" is exactly the kind of thing the log exists to be
able to answer. ``StageRejectionRecord`` is that trace: same append-only file, same
``decision_id`` key, allocated when the signal is dequeued rather than when the gate
answers, so a signal that dies at stage one is still followable end to end.
"""

from __future__ import annotations

from datetime import date, datetime
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
    #: Invisible-character sanitization (2026-08-25): whether construction
    #: stripped invisible codepoints from ``content``, and how many. The bytes
    #: themselves are still in ``raw_content``.
    sanitized: bool = False
    invisible_stripped: int = 0
    #: Per-member/sub-source credibility key (2026-08-25). None = the source_id
    #: is the credibility identity, which is every source except the
    #: congressional roster (keyed per member so attribution can rank filers).
    credibility_key: Optional[str] = None
    #: For mirrored content: the mirror source that actually delivered it.
    #: ``source_id`` names whose words these are; this names who carried them —
    #: attribution goes to the principal, accountability stays with the mirror.
    delivered_by: Optional[str] = None
    #: Who filed the disclosure this signal renders, when the source has one — a
    #: congressional member or a 13F fund (2026-09-01). Structured here so replay
    #: can match a later disclosure by the same filer to the position this signal
    #: opened, without parsing it back out of the content.
    filer: Optional[str] = None

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
            sanitized=signal.sanitized,
            invisible_stripped=signal.invisible_stripped,
            credibility_key=signal.metadata.get("credibility_key") or None,
            delivered_by=signal.metadata.get("delivered_by") or None,
            filer=(
                signal.metadata.get("representative")
                or signal.metadata.get("fund")
                or None
            ),
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
    #: Catalyst verdict (2026-08-24). Defaults keep every record written before
    #: the field existed parseable — an absent catalyst reads as none reported.
    catalyst_present: Optional[bool] = None
    catalyst_description: Optional[str] = None
    #: When the entry pass expected this thesis to resolve (2026-08-31). The
    #: primary source of the position's leash, so replay must be able to read it
    #: back — a leash rebuilt from the horizon bucket alone would silently revert
    #: a dated position to the fallback after a restart.
    expected_resolution_date: Optional[date] = None

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
            catalyst_present=(
                report.catalyst_within_horizon.present
                if report.catalyst_within_horizon is not None
                else None
            ),
            catalyst_description=(
                report.catalyst_within_horizon.description
                if report.catalyst_within_horizon is not None
                else None
            ),
            expected_resolution_date=report.expected_resolution_date,
        )


class NearMissSnapshot(_Record):
    """The contract a fallback almost picked, and the gate that killed it —
    so "are our liquidity gates too tight?" is answerable from records."""

    occ_symbol: str
    delta: Optional[Decimal]
    open_interest: int
    spread_pct: Optional[Decimal]
    killed_by: str


class ExpressionSnapshot(_Record):
    """How the thesis was expressed: the chosen contract, or why not (2026-08-24)."""

    #: Whether an options expression was even on the table for this decision.
    considered: bool
    #: "option" | "equity" | "none" (puts theses with no valid contract trade nothing).
    chosen: str
    fallback_reason: Optional[str] = None
    detail: Optional[str] = None
    contract_symbol: Optional[str] = None
    delta: Optional[Decimal] = None
    iv_percentile: Optional[Decimal] = None
    expiration: Optional[str] = None
    open_interest: Optional[int] = None
    spread_pct: Optional[Decimal] = None
    near_miss: Optional[NearMissSnapshot] = None


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
    STAGE_REJECTION = "stage_rejection"
    THESIS_REVIEW = "thesis_review"
    EXIT = "exit"
    FILER_EVENT = "filer_event"


class DecisionRecord(_Record):
    """Everything known when the gate answered. All four stages are required."""

    kind: Literal[RecordKind.DECISION] = RecordKind.DECISION
    decision_id: str
    recorded_at: datetime
    signal: SignalSnapshot
    #: None for MECHANICAL entries (ruling 2026-08-27): the mechanical sleeve
    #: has no LLM in its path, and fabricating a research snapshot would put a
    #: verdict in the record nobody produced. ``sizing.strategy`` says
    #: "mechanical" on those records; every judged decision carries research.
    research: Optional[ResearchSnapshot]
    sizing: SizingSnapshot
    #: The mechanical entry's own facts; None on judged decisions.
    mechanical: Optional["MechanicalSnapshot"] = None
    #: How the thesis was expressed (options vs equity), when routing ran.
    expression: Optional[ExpressionSnapshot] = None
    #: Two-stage research (2026-08-25): the stage-one screen draft behind a
    #: verified verdict, with its own cost. None when the pass ended at stage
    #: one (``research`` IS the screen report) or two-stage is off.
    screen_research: Optional[ResearchSnapshot] = None
    screen_est_cost_usd: Optional[Decimal] = None
    gate: GateSnapshot
    #: Estimated LLM spend of the research pass that produced this record —
    #: tokens summed across every API call, dollars from the pricing table in
    #: research.yaml. Estimates for attribution; the console bill is the truth.
    #: None when no research ran (or the model is unpriced, for the cost).
    est_input_tokens: Optional[int] = None
    est_output_tokens: Optional[int] = None
    est_cost_usd: Optional[Decimal] = None

    @property
    def was_approved(self) -> bool:
        return self.gate.approved


class FillRecord(_Record):
    """What the broker did. Only ever written against an approved decision."""

    kind: Literal[RecordKind.FILL] = RecordKind.FILL
    decision_id: str
    recorded_at: datetime
    #: Which way the fill went. An entry is a buy; an exit's fill is a sell. Defaults
    #: to buy so records written before exits existed parse unchanged — every fill in
    #: the log before this field was added was one.
    side: Literal["buy", "sell"] = "buy"
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


class MechanicalSnapshot(_Record):
    """What qualified a mechanical entry (ruling 2026-08-27): the disclosure's
    structured facts and the rule set that admitted it, so attribution can
    compare mechanical vs judged on identical signals and partition history
    across rule changes."""

    ruleset_version: str
    filer: Optional[str] = None
    ticker: Optional[str] = None
    amount_range: Optional[str] = None
    report_date: Optional[str] = None


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


class RejectedStage(StrEnum):
    """Where a signal stopped.

    Named here rather than in the orchestrator because the log has to be readable
    without the code that wrote it: a record whose stage is an integer, or a string
    whose meanings live in another package, is a record that needs an archaeologist.
    The gate is absent from this enum on purpose — a gate rejection is a complete
    decision and writes a ``DecisionRecord``.
    """

    #: Filtered at research dispatch, before a budget pass was spent: the post
    #: named no instrument and touched no configured policy theme. The record is the
    #: point — every filtered post stays readable, so a filter that eats alpha can
    #: be caught by reading what it skipped.
    PRE_FILTER = "pre_filter"
    #: Stopped by the cheap triage gate before the full research pass: the gate
    #: judged the signal untradeable/unverifiable/stale. The gate's one-line
    #: reason is the record's message; the triage call's own cost is stamped on
    #: the record. Spends the COST meter, never the research-pass budget.
    TRIAGE = "triage"
    #: The research pass returned a typed rejection instead of a report.
    RESEARCH = "research"
    #: A report was produced, and sizing resolved it to nothing: confidence below the
    #: floor, or a ``no_position`` verdict.
    SIZING = "sizing"
    #: A size was proposed but no order could be built from it — no price available,
    #: an instrument this system does not execute, or a report naming several tickers
    #: with no basis to choose between them (Constraint #6: surface, do not guess).
    ORDER_CONSTRUCTION = "order_construction"
    #: The gate approved and the broker refused, or the order terminated unfilled.
    #: Shares the ``decision_id`` of the ``DecisionRecord`` that preceded it.
    EXECUTION = "execution"
    #: The pipeline itself raised. A bug is not a verdict about the signal, and
    #: recording it as one would corrupt the source's track record.
    INTERNAL_ERROR = "internal_error"


class StageRejectionRecord(_Record):
    """A signal that stopped short of a completed decision.

    ``research`` and ``sizing`` are populated when those stages ran and absent when
    they did not — the record carries the path the signal actually took rather than a
    fixed set of slots with nulls in them.
    """

    kind: Literal[RecordKind.STAGE_REJECTION] = RecordKind.STAGE_REJECTION
    decision_id: str
    recorded_at: datetime
    stage: RejectedStage
    #: A stable machine-readable reason. Reuses the upstream code where one exists —
    #: ``ResearchRejectionCode`` at the research stage, the broker status at execution.
    code: str
    message: str
    signal: SignalSnapshot
    research: Optional[ResearchSnapshot] = None
    sizing: Optional[SizingSnapshot] = None
    #: Estimated LLM spend of the research pass that produced this record —
    #: tokens summed across every API call, dollars from the pricing table in
    #: research.yaml. Estimates for attribution; the console bill is the truth.
    #: None when no research ran (or the model is unpriced, for the cost).
    #: Expression routing outcome, when the rejection happened at or after it.
    expression: Optional["ExpressionSnapshot"] = None
    #: Two-stage research (2026-08-25): the screen draft behind a graduated pass
    #: that was then rejected downstream. None when no verification ran.
    screen_research: Optional["ResearchSnapshot"] = None
    screen_est_cost_usd: Optional[Decimal] = None
    est_input_tokens: Optional[int] = None
    est_output_tokens: Optional[int] = None
    est_cost_usd: Optional[Decimal] = None


class ExitReason(StrEnum):
    """Why a position was closed. Defined here, like ``RejectedStage``, because the
    log has to be readable without the code that wrote it."""

    #: Deterministic guardrail: price at or below the stop set at entry.
    MAX_LOSS_STOP = "max_loss_stop"
    #: Deterministic guardrail: held longer than the leash for its time horizon.
    TIME_STOP = "time_stop"
    #: The thesis review concluded the position should close — an explicit close
    #: verdict, or a triggered invalidation condition (which closes whatever the
    #: action field said; the contradiction resolves toward the exit).
    THESIS_INVALIDATED = "thesis_invalidated"
    #: Long option inside the configured pre-expiry window. Closed regardless of
    #: thesis state: theta endgame is not a place this system holds (2026-08-24).
    EXPIRY_CLOSE = "expiry_close"
    #: Deterministic backstop (2026-08-31): the position ran far enough to arm a
    #: trailing stop and then gave back the trail distance from its high-water
    #: mark. Its own reason, not MAX_LOSS_STOP, because attribution must be able to
    #: separate "the thesis played out" from "a reversal was caught between
    #: reviews" — they are different claims about where the P&L came from.
    TRAILING_STOP = "trailing_stop"
    #: The mechanical sleeve's only exit (ruling 2026-08-27): hold_days after
    #: fill, no price stop — the stop is the slice size.
    MECHANICAL_TIME_EXIT = "mechanical_time_exit"


class ReviewOutcome(StrEnum):
    HOLD = "hold"
    CLOSE = "close"
    #: The review call failed or returned something malformed. Treated as HOLD by the
    #: caller — a close on bad data is a trade on bad data — but recorded as its own
    #: outcome, because "the model held" and "the model could not answer" are
    #: different facts about the review layer.
    REVIEW_FAILED = "review_failed"


class ThesisReviewRecord(_Record):
    """One periodic re-research of an open position.

    Every review that consumed a research pass writes one, hold or not — the reviews
    that found nothing are the denominator, and the daily research budget is replayed
    from these records after a restart (see ``AuditLog.research_passes_on``).
    """

    kind: Literal[RecordKind.THESIS_REVIEW] = RecordKind.THESIS_REVIEW
    decision_id: str
    recorded_at: datetime
    outcome: ReviewOutcome
    #: The model's prose, verbatim. Absent when the review failed.
    assessment: Optional[str] = None
    invalidation_triggered: Optional[bool] = None
    #: The widened verdict (ruling 2026-08-31). All absent on a failed review.
    validity: Optional[str] = None
    progress: Optional[str] = None
    resolution: Optional[str] = None
    revised_resolution_date: Optional[date] = None
    continuation_thesis: Optional[str] = None
    #: Which contradiction rule overrode a hold, when one did. Recorded rather
    #: than inferred: "the model said hold and the position closed" is exactly the
    #: kind of thing a reader should never have to reconstruct.
    close_contradiction: Optional[str] = None
    #: Why this review ran out of cadence, when it did — the price move that
    #: forced it. None on an ordinary cadence review.
    trigger_reason: Optional[str] = None
    #: The position's leash after this review, in days from entry. Present when
    #: the review moved it; the clamp means this is what was actually applied,
    #: not what was asked for.
    leash_days_after: Optional[int] = None
    #: Failure code when the outcome is REVIEW_FAILED.
    code: Optional[str] = None
    message: str = ""
    #: Estimated LLM spend of the research pass that produced this record —
    #: tokens summed across every API call, dollars from the pricing table in
    #: research.yaml. Estimates for attribution; the console bill is the truth.
    #: None when no research ran (or the model is unpriced, for the cost).
    est_input_tokens: Optional[int] = None
    est_output_tokens: Optional[int] = None
    est_cost_usd: Optional[Decimal] = None


class ExitRecord(_Record):
    """One attempt to close a position, keyed by the entry's decision_id.

    Carries the whole attempt in one record: the typed reason, the gate's answer to
    the sell-to-close order, and — when the gate approved — whether the broker took
    it. A gate rejection or a broker refusal is still a complete record; the attempt
    happened, and retries write their own.
    """

    kind: Literal[RecordKind.EXIT] = RecordKind.EXIT
    decision_id: str
    recorded_at: datetime
    reason: ExitReason
    detail: str
    gate: GateSnapshot
    #: True/False once a broker submission was attempted; None when the gate rejected
    #: and there was nothing to submit.
    submitted: Optional[bool] = None
    broker_order_id: Optional[str] = None
    broker_error: Optional[str] = None


class FilerEventRecord(_Record):
    """A new disclosure by the filer who originated a held position, in that name.

    Keyed by the HELD position's entry ``decision_id`` — the event is a fact about
    the position, threaded into its trail like reviews and exits are. The
    disclosure's own identity is carried so the event is written exactly once per
    (position, disclosure) even though unresearched signals re-emit at startup.

    What it does depends on the arm, and the asymmetry is the experiment:
    ``judged`` positions get a triggered review (the review decides — a filer sale
    is evidence, not a verdict); ``mechanical`` positions ride to their time exit
    untouched, and this record is what lets attribution later measure what that
    discipline cost or saved.
    """

    kind: Literal[RecordKind.FILER_EVENT] = RecordKind.FILER_EVENT
    decision_id: str
    recorded_at: datetime
    #: "judged" | "mechanical" — which arm holds the position.
    arm: str
    filer: str
    symbol: str
    #: The new disclosure's transaction, verbatim from the feed ("Sale",
    #: "Purchase", "Sale (Partial)", ...).
    transaction: str
    #: Identity of the disclosure signal that carried the event.
    disclosure_source_id: str
    disclosure_external_id: Optional[str] = None
    transaction_date: Optional[str] = None
    report_date: Optional[str] = None
    amount_range: Optional[str] = None
    #: The rendered one-line statement handed to the review prompt (judged arm).
    detail: str = ""


AuditRecord = Annotated[
    Union[
        DecisionRecord,
        FillRecord,
        OutcomeRecord,
        CorrectionRecord,
        StageRejectionRecord,
        ThesisReviewRecord,
        ExitRecord,
        FilerEventRecord,
    ],
    Field(discriminator="kind"),
]


class AuditTrail(_Record):
    """The complete per-order view CLAUDE.md describes, assembled from the log."""

    decision: DecisionRecord
    fills: tuple[FillRecord, ...] = ()
    outcome: Optional[OutcomeRecord] = None
    corrections: tuple[CorrectionRecord, ...] = ()
    #: Rejections recorded against this decision after the gate answered — a broker
    #: refusal, or an order that terminated unfilled.
    stage_rejections: tuple[StageRejectionRecord, ...] = ()
    #: Every thesis review this position received, in order.
    reviews: tuple[ThesisReviewRecord, ...] = ()
    #: Every attempt to close it, in order. Usually zero or one; retries accumulate.
    exits: tuple[ExitRecord, ...] = ()
    #: New disclosures by the originating filer in this name while held, in order.
    filer_events: tuple[FilerEventRecord, ...] = ()

    @property
    def decision_id(self) -> str:
        return self.decision.decision_id

    @property
    def signal_class(self) -> SignalClass:
        return self.decision.signal.signal_class

    @property
    def never_executed(self) -> bool:
        """Approved, then stopped at the broker: refused, cancelled, or expired unfilled."""
        return any(
            rejection.stage is RejectedStage.EXECUTION for rejection in self.stage_rejections
        ) and not self.fills

    @property
    def is_complete(self) -> bool:
        """Has this order run its full course?

        Completeness is path-dependent, because "finished" means different things on
        different paths. A rejected order is complete the moment it is rejected — there
        is nothing further to happen to it. An approved order the broker never executed
        is complete too: the reservation was released and no position exists to resolve.
        Only an order that actually filled needs an outcome before it is done.
        """
        if not self.decision.was_approved:
            return True
        if self.never_executed:
            return True
        return bool(self.fills) and self.outcome is not None

    @property
    def realised_pnl(self) -> Optional[Decimal]:
        return self.outcome.realised_pnl if self.outcome else None
