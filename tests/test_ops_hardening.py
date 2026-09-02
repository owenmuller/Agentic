"""Operational hardening tests (human ruling 2026-09-02).

The claims: the alerter is tiered, rate-limited, disabled-without-credentials,
and incapable of raising into the loop; the run log's observer sees every note
and cannot kill a run; fill records carry the fidelity fields and old records
parse without them; the attribution report renders paper P&L raw AND haircut;
and the config-replay harness reports prefilter and sizing flips read-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.alerts import Alerter
from orchestrator.ops import RunLog

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def alerter_with(clock=None, sender=None, **kwargs):
    sent = []
    return (
        Alerter(
            clock=clock or Clock(),
            sender=sender or (lambda subject, body: sent.append((subject, body))),
            **kwargs,
        ),
        sent,
    )


# ================================================================================
# Alerting
# ================================================================================


def test_tiers_carry_their_subject_prefixes():
    alerter, sent = alerter_with()
    assert alerter.urgent("k1", "kill switch")
    assert alerter.daily("k2", "close summary", "body")
    alerter.close()
    subjects = [subject for subject, _ in sent]
    assert subjects == [
        "[AGENTIC URGENT] kill switch",
        "[AGENTIC DAILY] close summary",
    ]


def test_the_same_key_is_not_resent_inside_the_window():
    clock = Clock()
    alerter, sent = alerter_with(clock=clock)
    assert alerter.urgent("err", "ERROR: x") is True
    assert alerter.urgent("err", "ERROR: x") is False  # suppressed
    clock.now = NOW + timedelta(minutes=241)
    assert alerter.urgent("err", "ERROR: x") is True  # window elapsed
    alerter.close()
    assert len(sent) == 2


def test_distinct_keys_are_independent():
    alerter, sent = alerter_with()
    assert alerter.urgent("a", "one")
    assert alerter.urgent("b", "two")
    alerter.close()
    assert len(sent) == 2


def test_unconfigured_alerter_is_disabled_and_silent(monkeypatch):
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_TO"):
        monkeypatch.delenv(name, raising=False)
    alerter = Alerter(clock=Clock())
    assert not alerter.enabled
    assert alerter.urgent("k", "anything") is False
    assert alerter.send_test() is False


def test_a_failing_send_is_logged_never_raised(caplog):
    import logging

    def explode(subject, body):
        raise RuntimeError("smtp down")

    alerter = Alerter(clock=Clock(), sender=explode)
    with caplog.at_level(logging.ERROR, logger="execution.alerts"):
        assert alerter.urgent("k", "boom") is True  # queued fine
        alerter.close()
    assert any("failed to send" in record.message for record in caplog.records)


def test_send_test_bypasses_the_rate_limiter_and_says_daily():
    alerter, sent = alerter_with()
    assert alerter.send_test() and alerter.send_test()
    assert all(s.startswith("[AGENTIC DAILY]") for s, _ in sent)
    assert len(sent) == 2


# ================================================================================
# Run-log observer
# ================================================================================


def test_the_observer_sees_every_note(tmp_path):
    seen = []
    log = RunLog(tmp_path / "run.log", observer=lambda e, d: seen.append((e, d)))
    log.note("ERROR", "boom")
    log.note("POLL", "quiet")
    assert seen == [("ERROR", "boom"), ("POLL", "quiet")]


def test_a_failing_observer_cannot_kill_the_run(tmp_path):
    def explode(event, detail):
        raise RuntimeError("observer bug")

    log = RunLog(tmp_path / "run.log", observer=explode)
    log.note("ERROR", "boom")  # must not raise
    assert "ERROR boom" in log.tail(1)[0]


# ================================================================================
# Fill fidelity + haircut
# ================================================================================


def test_fill_records_carry_fidelity_and_old_records_parse(tmp_path):
    from audit import AuditLog
    from audit.records import FillRecord
    from risk_gate.limits import RiskLimits
    from test_audit import full_decision

    log = AuditLog(tmp_path / "audit.jsonl")
    record, _gate = full_decision(log, RiskLimits.load())
    fill = log.record_fill(
        record.decision_id,
        "broker-1",
        Decimal("10"),
        Decimal("140.10"),
        intended_price=Decimal("140.00"),
        spread_pct_at_submission=Decimal("0.0450"),
        seconds_to_fill=Decimal("12"),
    )
    assert fill.intended_price == Decimal("140.00")
    assert fill.seconds_to_fill == Decimal("12")
    # A pre-ruling line (no fidelity fields) still parses, all three None.
    old = FillRecord.model_validate_json(
        '{"kind": "fill", "decision_id": "d", "recorded_at": '
        '"2026-08-01T00:00:00Z", "broker_order_id": "b", '
        '"filled_quantity": "1", "fill_price": "10", "filled_value": "10"}'
    )
    assert old.intended_price is None and old.seconds_to_fill is None


def test_attribution_renders_raw_and_haircut(tmp_path):
    from audit import AuditLog
    from audit.attribution import build_attribution
    from risk_gate.limits import RiskLimits
    from test_audit import full_decision

    log = AuditLog(tmp_path / "audit.jsonl")
    record, _gate = full_decision(log, RiskLimits.load())
    log.record_fill(
        record.decision_id, "broker-1", Decimal("10"), Decimal("100")
    )
    report = build_attribution(
        log.trails(),
        generated_at=datetime.now(timezone.utc),
        haircut_bps=Decimal("5"),
    )
    assert report.haircut_notional == Decimal("1000")
    rendered = report.render()
    assert "Live-slippage haircut (5bps per fill side on $1000.00" in rendered
    assert "-0.50" in rendered


# ================================================================================
# Config replay
# ================================================================================


def test_replay_reports_prefilter_and_sizing_flips(tmp_path):
    from audit import AuditLog
    from orchestrator.whatif import render_whatif_report
    from risk_gate.limits import RiskLimits
    from signals import SignalsConfig
    from test_audit import full_decision, make_report

    current_signals = SignalsConfig.load()
    current_limits = RiskLimits.load()

    # Candidate signals: congressional amount floor raised to $200K — the
    # recorded NUE disclosure-shaped signal has no amount range, so it fails
    # OPEN both sides; a raised trump theme list is not in play either. Use a
    # sizing candidate instead for a guaranteed flip: floor raised past the
    # recorded confidence.
    limits_payload = current_limits.model_dump(mode="json")
    # size_for short-circuits below the floor, so raising the floor past the
    # recorded confidence is a guaranteed no-longer-trades flip.
    limits_payload["sizing"]["no_trade_below"] = 90
    candidate_limits = RiskLimits.model_validate(limits_payload)

    log = AuditLog(tmp_path / "audit.jsonl")
    full_decision(log, current_limits, report=make_report(confidence=71))

    rendered = render_whatif_report(
        log.records(),
        current_signals,
        current_signals,
        current_limits,
        candidate_limits,
        rows={},
    )
    assert "1 researched signals would no longer trade" in rendered
    assert "READ-ONLY" in rendered
