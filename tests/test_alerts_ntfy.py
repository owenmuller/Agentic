"""ntfy push alerts (human ruling 2026-10-12): tried first, priority by tier,
JSON publish (UTF-8 safe), truncated under the message limit, falls through
to the next transport on failure."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from execution.alerts import NTFY_MAX_BYTES, Alerter


def _alerter(monkeypatch, tmp_path, post, sendgrid=False):
    monkeypatch.setenv("NTFY_TOPIC", "agentic-test-topic")
    monkeypatch.delenv("NTFY_SERVER", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URGENT", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_REPORTS", raising=False)
    for name in ("ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    if sendgrid:
        monkeypatch.setenv("SENDGRID_API_KEY", "sg")
        monkeypatch.setenv("ALERT_TO", "ops@example.test")
        monkeypatch.setenv("ALERT_FROM", "agentic@example.test")
    else:
        monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    return Alerter(clock=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc), http_post=post,
                   status_path=tmp_path / "alerts_status.json")


def test_urgent_goes_out_as_a_top_priority_push(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, lambda url, **kw: calls.append((url, kw)) or SimpleNamespace(status_code=200))
    assert alerter.transports == ("ntfy",) and alerter.enabled
    alerter._send(alerter_message := __import__("execution.alerts", fromlist=["_Outbound"])._Outbound(  # noqa: F841
        subject="[AGENTIC URGENT] LAB ladder drawdown 25.1% — from peak", body="x" * 10000, tier="URGENT"))
    url, kw = calls[0]
    assert url == "https://ntfy.sh"
    payload = kw["json"]
    assert payload["topic"] == "agentic-test-topic" and payload["priority"] == 5
    assert payload["title"].endswith("— from peak")  # UTF-8 survives in JSON
    assert len(payload["message"].encode("utf-8")) <= NTFY_MAX_BYTES and payload["message"].endswith("[... truncated]")
    status = json.loads((tmp_path / "alerts_status.json").read_text())
    assert status["last_success"]["transport"] == "ntfy"


def test_daily_is_default_priority(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, lambda url, **kw: calls.append(kw) or SimpleNamespace(status_code=200))
    assert alerter.send_test() is True
    assert calls[0]["json"]["priority"] == 3


def test_a_failed_push_falls_through_to_the_next_transport(monkeypatch, tmp_path):
    calls = []

    def post(url, **kw):
        calls.append(url)
        return SimpleNamespace(status_code=500 if "ntfy" in url else 202, text="")

    alerter = _alerter(monkeypatch, tmp_path, post, sendgrid=True)
    assert alerter.transports == ("ntfy", "sendgrid")
    assert alerter.send_test() is True
    assert calls == ["https://ntfy.sh", "https://api.sendgrid.com/v3/mail/send"]
    assert json.loads((tmp_path / "alerts_status.json").read_text())["last_success"]["transport"] == "sendgrid"
