"""The alert ledger and log redaction (human rulings 2026-10-14, audit items
4 and 13): every card attempt is one ledger line - delivered, failed with its
reason, or refused - and no log record ever carries a webhook token, an ntfy
topic or a token-like query value."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from execution import cards
from execution.alerts import Alerter
from execution.logredact import redact

from test_alerts_discord import REPORTS_HOOK, URGENT_HOOK, ok


def test_redact_removes_every_secret_shape_and_keeps_the_rest():
    line = (f"POST {REPORTS_HOOK} 200; https://ntfy.sh/my-topic-123; "
            "GET https://finnhub.io/api/v1/quote?symbol=SPY&token=abc123 200")
    out = redact(line)
    assert "reports-secret" not in out and "my-topic-123" not in out and "abc123" not in out
    assert "/api/webhooks/2/<redacted>" in out and "symbol=SPY" in out and "token=<redacted>" in out


def test_every_logger_is_redacted_at_the_record(caplog):
    # httpx logs the full URL at INFO; the record factory cleans it, for any logger
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 204"', URGENT_HOOK)
        logging.getLogger("execution.alerts").error("failed: Client error for url '%s'", URGENT_HOOK)
    assert caplog.records and all("urgent-secret" not in r.getMessage() for r in caplog.records)
    assert "/api/webhooks/1/<redacted>" in caplog.records[0].getMessage()


def _alerter(monkeypatch, tmp_path, post, ntfy=False):
    monkeypatch.setenv("DISCORD_WEBHOOK_URGENT", URGENT_HOOK)
    monkeypatch.setenv("DISCORD_WEBHOOK_REPORTS", REPORTS_HOOK)
    if ntfy:
        monkeypatch.setenv("NTFY_TOPIC", "agentic-test")
    else:
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
    for name in ("NTFY_SERVER", "SENDGRID_API_KEY", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return Alerter(clock=lambda: datetime(2026, 10, 14, 17, tzinfo=timezone.utc), http_post=post,
                   ledger_path=tmp_path / "alerts_ledger.jsonl")


def _rows(tmp_path):
    return [json.loads(line) for line in (tmp_path / "alerts_ledger.jsonl").read_text().splitlines()]


def _card():
    return cards.Card("Main book", "weekly report", cards.GOOD, ("This week: +1%.",))


def test_a_delivered_card_and_a_rate_limited_repeat_are_both_recorded(monkeypatch, tmp_path):
    alerter = _alerter(monkeypatch, tmp_path, ok([]))
    assert alerter.card("weekly-2026-10-16", _card())
    alerter.close()
    assert not alerter.card("weekly-2026-10-16", _card())  # inside the resend window
    rows = _rows(tmp_path)
    assert rows[0] == {"at": "2026-10-14T17:00:00+00:00", "channel": "#reports", "tier": "DAILY",
                       "type": "weekly-2026-10-16", "title": "Main book: weekly report, nothing to do",
                       "delivered": True, "transport": "discord", "reason": ""}
    assert rows[1]["delivered"] is False and rows[1]["reason"].startswith("rate-limited")


def test_a_failed_card_records_its_reason_without_the_secret(monkeypatch, tmp_path):
    alerter = _alerter(monkeypatch, tmp_path, lambda url, **kw: SimpleNamespace(status_code=401, text="no"))
    red = cards.Card("Main book", "an error was logged", cards.ACTION, ("x",), what_to_do="Tell Claude Code: y",
                     channel=cards.URGENT)
    alerter.card("ERROR:x", red)
    alerter.close()
    row = _rows(tmp_path)[0]
    assert row["channel"] == "#urgent" and row["delivered"] is False
    assert "401" in row["reason"] and "secret" not in row["reason"]


def test_a_fallback_delivery_names_its_transport(monkeypatch, tmp_path):
    def post(url, **kwargs):
        if "discord" in url:
            return SimpleNamespace(status_code=500, text="down")
        return SimpleNamespace(status_code=200, text="")
    alerter = _alerter(monkeypatch, tmp_path, post, ntfy=True)
    alerter.card("weekly-x", _card())
    alerter.close()
    row = _rows(tmp_path)[0]
    assert row["delivered"] is True and row["channel"] == "ntfy (fallback for #reports)"


def test_an_unconfigured_alerter_records_the_refusal(monkeypatch, tmp_path):
    for name in ("DISCORD_WEBHOOK_URGENT", "DISCORD_WEBHOOK_REPORTS", "NTFY_TOPIC", "SENDGRID_API_KEY",
                 "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD", "ALERT_TO"):
        monkeypatch.delenv(name, raising=False)
    alerter = Alerter(http_post=ok([]), ledger_path=tmp_path / "alerts_ledger.jsonl")
    assert alerter.card("weekly-y", _card()) is False
    assert _rows(tmp_path)[0]["reason"] == "alerting not configured"
