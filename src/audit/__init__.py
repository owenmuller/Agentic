"""Audit records and signal attribution.

Every order writes a complete record: signal -> thesis -> confidence -> size ->
risk_gate_result -> fill -> outcome. Rejected orders are logged with their rejection
reason; risk-gate rejections are signal, not noise.

Records are written under ``DATA_DIR`` (``./data``), which is gitignored — the audit
trail lives only on this machine, so it belongs in the backup routine.

Not yet implemented.
"""
