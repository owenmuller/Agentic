"""Append-only audit storage.

There is no update. There is no delete. There is no method on ``AuditLog`` that can
change a byte already written — the file is opened for append and nothing else, and
a correction is a new ``CorrectionRecord`` naming the record it supersedes. That is
the whole design: a log you can edit is a log that cannot be trusted about the past,
and the reason to keep one is precisely to be able to reconstruct what was believed at
the moment a decision was made.

Storage is JSONL under ``DATA_DIR`` (``./data``, gitignored). One record per line,
appended and flushed. A reader replays the file in order.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Optional

from pydantic import TypeAdapter

from audit.records import (
    AuditRecord,
    AuditTrail,
    CorrectionRecord,
    DecisionRecord,
    ExitReason,
    ExitRecord,
    FillRecord,
    GateSnapshot,
    OutcomeRecord,
    RejectedStage,
    ResearchSnapshot,
    ReviewOutcome,
    SignalSnapshot,
    SizingSnapshot,
    StageRejectionRecord,
    ThesisReviewRecord,
)
from research.reports import ResearchReport
from signals import Signal
from sizing.engine import SizedProposal

_ADAPTER: TypeAdapter = TypeAdapter(AuditRecord)


class AuditLogError(RuntimeError):
    """A write the log refuses to make."""


def default_data_dir() -> Path:
    configured = os.environ.get("DATA_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data"


class AuditLog:
    """Append-only JSONL record of every order the system considered."""

    def __init__(
        self,
        path: Optional[Path] = None,
        clock: Optional[Callable[[], datetime]] = None,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._path = path or (default_data_dir() / "audit.jsonl")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:16])

    @property
    def path(self) -> Path:
        return self._path

    # -- writing -------------------------------------------------------------------

    def record_decision(
        self,
        signal: Signal,
        report: ResearchReport,
        proposal: SizedProposal,
        gate_decision: object,
        decision_id: Optional[str] = None,
    ) -> DecisionRecord:
        """Write the complete decision-time record. Approved or rejected, both land."""
        record = DecisionRecord(
            decision_id=decision_id or self._id_factory(),
            recorded_at=self._clock(),
            signal=SignalSnapshot.of(signal),
            research=ResearchSnapshot.of(report),
            sizing=SizingSnapshot.of(proposal),
            gate=GateSnapshot.of(gate_decision),  # type: ignore[arg-type]
        )
        self._append(record)
        return record

    def record_stage_rejection(
        self,
        decision_id: str,
        stage: RejectedStage,
        code: str,
        message: str,
        signal: Signal,
        report: Optional[ResearchReport] = None,
        proposal: Optional[SizedProposal] = None,
    ) -> StageRejectionRecord:
        """Record a signal that stopped before the gate, or an order the broker refused.

        Takes whichever stages completed. A research-stage rejection has neither report
        nor proposal; a sizing-stage one has a report; an execution-stage one has both
        and shares its ``decision_id`` with the ``DecisionRecord`` already written.
        """
        record = StageRejectionRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            stage=stage,
            code=code,
            message=message,
            signal=SignalSnapshot.of(signal),
            research=ResearchSnapshot.of(report) if report is not None else None,
            sizing=SizingSnapshot.of(proposal) if proposal is not None else None,
        )
        self._append(record)
        return record

    def record_thesis_review(
        self,
        decision_id: str,
        outcome: ReviewOutcome,
        assessment: Optional[str] = None,
        invalidation_triggered: Optional[bool] = None,
        code: Optional[str] = None,
        message: str = "",
    ) -> ThesisReviewRecord:
        """Record one thesis review of an open position.

        Validated against the entry decision like a fill is: a review of a position
        that was never approved means the review layer is looking at a phantom.
        """
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position to review"
            )
        record = ThesisReviewRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            outcome=outcome,
            assessment=assessment,
            invalidation_triggered=invalidation_triggered,
            code=code,
            message=message,
        )
        self._append(record)
        return record

    def record_exit(
        self,
        decision_id: str,
        reason: ExitReason,
        detail: str,
        gate_decision: object,
        submitted: Optional[bool] = None,
        broker_order_id: Optional[str] = None,
        broker_error: Optional[str] = None,
    ) -> ExitRecord:
        """Record one attempt to close a position - exits are decisions too.

        Written whether the gate approved the sell-to-close or rejected it, and
        whether the broker took it or refused: every attempt is a fact about the
        position, and a retry writes its own record rather than editing this one.
        """
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position to exit"
            )
        record = ExitRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            reason=reason,
            detail=detail,
            gate=GateSnapshot.of(gate_decision),  # type: ignore[arg-type]
            submitted=submitted,
            broker_order_id=broker_order_id,
            broker_error=broker_error,
        )
        self._append(record)
        return record

    def record_fill(
        self,
        decision_id: str,
        broker_order_id: str,
        filled_quantity: Decimal,
        fill_price: Decimal,
        filled_value: Optional[Decimal] = None,
        side: str = "buy",
    ) -> FillRecord:
        """Record a fill. Refused for a decision that was never approved."""
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected by the risk gate and cannot have a fill; "
                f"a fill against a rejected decision means an order bypassed the gate"
            )
        record = FillRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            side=side,
            broker_order_id=broker_order_id,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            filled_value=(
                filled_value
                if filled_value is not None
                else filled_quantity * fill_price
            ),
        )
        self._append(record)
        return record

    def record_outcome(
        self,
        decision_id: str,
        realised_pnl: Decimal,
        closed_at: Optional[datetime] = None,
        note: str = "",
        credibility: Optional[object] = None,
    ) -> OutcomeRecord:
        """Resolve a position, and credit or debit the source that suggested it.

        This is the step that turns a source's hit rate from "not yet available" into
        a number: the outcome is attributed back to the signal that produced the
        decision, so credibility accrues to whoever actually called it.
        """
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position to resolve"
            )
        if self._outcome_for(decision_id) is not None:
            raise AuditLogError(
                f"{decision_id} already has an outcome; correct it with a "
                f"CorrectionRecord rather than resolving it twice"
            )

        record = OutcomeRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            closed_at=closed_at or self._clock(),
            realised_pnl=realised_pnl,
            note=note,
        )
        self._append(record)

        if credibility is not None:
            credibility.record_outcome(  # type: ignore[attr-defined]
                decision.signal.source_id, won=record.won
            )
        return record

    def record_correction(
        self, decision_id: str, supersedes_sequence: int, reason: str, **fields: object
    ) -> CorrectionRecord:
        """Supersede an earlier record. The original is never touched."""
        record = CorrectionRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            supersedes_sequence=supersedes_sequence,
            reason=reason,
            corrected_fields=dict(fields),
        )
        self._append(record)
        return record

    def _append(self, record: object) -> None:
        """The only write path. Append mode, one line, flushed."""
        line = record.model_dump_json()  # type: ignore[attr-defined]
        if "\n" in line:  # pragma: no cover - pydantic never emits newlines
            raise AuditLogError("record serialised to multiple lines")
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    # -- reading -------------------------------------------------------------------

    def records(self) -> Iterator[AuditRecord]:
        """Replay the log in write order."""
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield _ADAPTER.validate_python(json.loads(line))

    def decisions(self) -> list[DecisionRecord]:
        return [r for r in self.records() if isinstance(r, DecisionRecord)]

    def stage_rejections(self) -> list[StageRejectionRecord]:
        return [r for r in self.records() if isinstance(r, StageRejectionRecord)]

    def rejections_for(self, decision_id: str) -> list[StageRejectionRecord]:
        """Every stage rejection recorded against one decision id.

        For a signal that never reached the gate this *is* the whole trail — there is
        no ``AuditTrail`` to assemble, because nothing was decided.
        """
        return [r for r in self.stage_rejections() if r.decision_id == decision_id]

    def researched_external_ids(self) -> set[tuple[str, str]]:
        """(source_id, external_id) for every signal that entered the pipeline.

        Every path through the pipeline writes a record carrying the signal snapshot
        — a decision, or a stage rejection — so this is the set of signals research
        was already spent on. Fetchers seed their dedup sets from it at startup, so a
        restart does not re-emit (and re-pay for) what the log already answers. A
        signal that was queued but never researched left no record, which is the
        right edge: it re-emits and finally gets its pass.
        """
        seen: set[tuple[str, str]] = set()
        for record in self.records():
            signal = getattr(record, "signal", None)
            if signal is not None and signal.external_id:
                seen.add((signal.source_id, signal.external_id))
        return seen

    def first_seen(self) -> dict[str, datetime]:
        """Earliest record time per decision id, in write order."""
        seen: dict[str, datetime] = {}
        for record in self.records():
            recorded_at = record.recorded_at
            existing = seen.get(record.decision_id)
            if existing is None or recorded_at < existing:
                seen[record.decision_id] = recorded_at
        return seen

    def research_passes_on(self, day: date) -> int:
        """How many signals were researched on ``day`` (UTC).

        The research budget has to survive a restart, or a crash loop would spend it
        without limit. Rather than keep a counter somewhere that can drift from
        reality, this derives the figure from the log: the pipeline allocates one
        ``decision_id`` per signal it researches and every path from there writes at
        least one record, so distinct ids first recorded on a day *is* the number of
        passes bought that day. A later record against an earlier decision — a fill, an
        outcome — does not count again, because the id is not new.

        Thesis reviews of open positions spend the budget too, and they run under the
        *entry's* decision_id — an id that is not new — so they are counted by record
        rather than by id: every ``ThesisReviewRecord`` stamped on the day is one pass,
        failed reviews included, because the call was still made.
        """
        new_ids = sum(1 for at in self.first_seen().values() if at.date() == day)
        reviews = sum(
            1
            for record in self.records()
            if isinstance(record, ThesisReviewRecord)
            and record.recorded_at.date() == day
        )
        return new_ids + reviews

    def _decision(self, decision_id: str) -> DecisionRecord:
        for record in self.records():
            if isinstance(record, DecisionRecord) and record.decision_id == decision_id:
                return record
        raise AuditLogError(f"no decision recorded for {decision_id}")

    def _outcome_for(self, decision_id: str) -> Optional[OutcomeRecord]:
        for record in self.records():
            if isinstance(record, OutcomeRecord) and record.decision_id == decision_id:
                return record
        return None

    def trail(self, decision_id: str) -> AuditTrail:
        """Assemble the full per-order view: decision, fills, outcome, corrections."""
        decision: Optional[DecisionRecord] = None
        fills: list[FillRecord] = []
        outcome: Optional[OutcomeRecord] = None
        corrections: list[CorrectionRecord] = []
        rejections: list[StageRejectionRecord] = []
        reviews: list[ThesisReviewRecord] = []
        exits: list[ExitRecord] = []

        for record in self.records():
            if getattr(record, "decision_id", None) != decision_id:
                continue
            if isinstance(record, DecisionRecord):
                decision = record
            elif isinstance(record, FillRecord):
                fills.append(record)
            elif isinstance(record, OutcomeRecord):
                outcome = record
            elif isinstance(record, CorrectionRecord):
                corrections.append(record)
            elif isinstance(record, StageRejectionRecord):
                rejections.append(record)
            elif isinstance(record, ThesisReviewRecord):
                reviews.append(record)
            elif isinstance(record, ExitRecord):
                exits.append(record)

        if decision is None:
            raise AuditLogError(f"no decision recorded for {decision_id}")
        return AuditTrail(
            decision=decision,
            fills=tuple(fills),
            outcome=outcome,
            corrections=tuple(corrections),
            stage_rejections=tuple(rejections),
            reviews=tuple(reviews),
            exits=tuple(exits),
        )

    def trails(self) -> list[AuditTrail]:
        return [self.trail(d.decision_id) for d in self.decisions()]
