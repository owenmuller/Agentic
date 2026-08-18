"""Audit records and signal attribution.

Every order writes a complete record: signal (raw content included) -> thesis ->
confidence -> manipulation assessment -> sized proposal -> risk-gate result -> fill ->
outcome. Rejected orders are first-class records, because "risk-gate rejections are
signal, not noise" and a log containing only the trades you took cannot tell you what
your rules cost you.

Storage is append-only JSONL under ``DATA_DIR`` (``./data``, gitignored — the audit
trail lives only on this machine and belongs in the backup routine). There is no
update and no delete; a correction is a new record naming the one it supersedes.

The log observes, it never participates. This package reads types from everywhere in
the system so it can describe what happened, and nothing outside it imports it except
a top-level orchestrator — an audit trail that other modules could call into would be
part of the machinery it is supposed to be recording.
"""

from audit.attribution import (
    DEFAULT_WINDOW_DAYS,
    AttributionReport,
    ClassAttribution,
    build_attribution,
)
from audit.log import AuditLog, AuditLogError, default_data_dir
from audit.records import (
    AuditRecord,
    AuditTrail,
    CorrectionRecord,
    DecisionRecord,
    FillRecord,
    GateSnapshot,
    OutcomeRecord,
    RecordKind,
    RejectedStage,
    ResearchSnapshot,
    SignalSnapshot,
    SizingSnapshot,
    StageRejectionRecord,
)

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "AttributionReport",
    "AuditLog",
    "AuditLogError",
    "AuditRecord",
    "AuditTrail",
    "ClassAttribution",
    "CorrectionRecord",
    "DecisionRecord",
    "FillRecord",
    "GateSnapshot",
    "OutcomeRecord",
    "RecordKind",
    "RejectedStage",
    "ResearchSnapshot",
    "SignalSnapshot",
    "SizingSnapshot",
    "StageRejectionRecord",
    "build_attribution",
    "default_data_dir",
]
