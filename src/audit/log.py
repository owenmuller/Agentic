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
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Optional

from pydantic import TypeAdapter

from audit.records import (
    AuditRecord,
    AuditTrail,
    CorrectionRecord,
    DecisionRecord,
    FillRecord,
    GateSnapshot,
    OutcomeRecord,
    ResearchSnapshot,
    SignalSnapshot,
    SizingSnapshot,
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

    def record_fill(
        self,
        decision_id: str,
        broker_order_id: str,
        filled_quantity: Decimal,
        fill_price: Decimal,
        filled_value: Optional[Decimal] = None,
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

        if decision is None:
            raise AuditLogError(f"no decision recorded for {decision_id}")
        return AuditTrail(
            decision=decision,
            fills=tuple(fills),
            outcome=outcome,
            corrections=tuple(corrections),
        )

    def trails(self) -> list[AuditTrail]:
        return [self.trail(d.decision_id) for d in self.decisions()]
