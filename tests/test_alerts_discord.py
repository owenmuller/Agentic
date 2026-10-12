"""Discord as the primary alert channel (human ruling 2026-10-12): URGENT to
#urgent, daily and weekly reports to #reports, long text as an attached file,
ntfy as the fallback, no mentions, webhook URLs never in an error."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from execution.alerts import DAILY, DISCORD_LIMIT, URGENT, Alerter, _Outbound

URGENT_HOOK = "https://discord.test/api/webhooks/1/urgent-secret"
REPORTS_HOOK = "https://discord.test/api/webhooks/2/reports-secret"


def _alerter(monkeypatch, tmp_path, post, ntfy=True, reports=True):
    monkeypatch.setenv("DISCORD_WEBHOOK_URGENT", URGENT_HOOK)
    if reports:
        monkeypatch.setenv("DISCORD_WEBHOOK_REPORTS", REPORTS_HOOK)
    else:
        monkeypatch.delenv("DISCORD_WEBHOOK_REPORTS", raising=False)
    if ntfy:
        monkeypatch.setenv("NTFY_TOPIC", "agentic-test")
    else:
        monkeypatch.delenv("NTFY_TOPIC", raising=False)
    for name in ("NTFY_SERVER", "SENDGRID_API_KEY", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    return Alerter(clock=lambda: datetime(2026, 10, 12, tzinfo=timezone.utc), http_post=post,
                   status_path=tmp_path / "alerts_status.json")


def ok(calls):
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=204, text="")
    return post


def test_each_tier_goes_to_its_own_channel_as_a_code_block(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    assert alerter.transports[:2] == ("discord", "ntfy")
    alerter._send(_Outbound(subject="[AGENTIC URGENT] KILL SWITCH", body="drawdown 12.1%", tier=URGENT))
    alerter._send(_Outbound(subject="[AGENTIC DAILY] close summary", body="all quiet", tier=DAILY))
    (u_url, u), (r_url, r) = calls
    assert u_url == URGENT_HOOK and r_url == REPORTS_HOOK
    assert u["json"]["content"] == "**[AGENTIC URGENT] KILL SWITCH**\n```\ndrawdown 12.1%\n```"
    assert u["json"]["allowed_mentions"] == {"parse": []}
    assert json.loads((tmp_path / "alerts_status.json").read_text())["last_success"]["transport"] == "discord"


def test_long_text_is_attached_in_full_with_a_preview_under_the_limit(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    body = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(200))  # ~14,000 characters
    alerter._send(_Outbound(subject="[AGENTIC DAILY] weekly report", body=body, tier=DAILY))
    url, kw = calls[0]
    payload = json.loads(kw["data"]["payload_json"])
    assert len(payload["content"]) <= DISCORD_LIMIT and "(full text: alert.txt)" in payload["content"]
    name, data, kind = kw["files"]["files[0]"]
    assert name == "alert.txt" and kind == "text/plain"
    assert data.decode("utf-8").endswith(body)  # the WHOLE text, nothing cut


def test_the_scoreboard_rides_as_an_attached_file(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    table = "PAPER LAB SCOREBOARD\nsleeve  return\nAI TRADER +1.00%"
    alerter._send(_Outbound(subject="[AGENTIC DAILY] PAPER LAB SCOREBOARD 2026-10-16", body=table, tier=DAILY,
                            attachment=("scoreboard-2026-10-16.txt", table)))
    url, kw = calls[0]
    assert url == REPORTS_HOOK
    assert kw["files"]["files[0]"][0] == "scoreboard-2026-10-16.txt"
    assert "AI TRADER +1.00%" in json.loads(kw["data"]["payload_json"])["content"]


def test_a_discord_failure_falls_back_to_ntfy_and_never_leaks_the_webhook(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=429 if "discord" in url else 200, text="rate limited")

    alerter = _alerter(monkeypatch, tmp_path, post)
    alerter._send(_Outbound(subject="[AGENTIC URGENT] x", body="y", tier=URGENT))
    assert calls == [URGENT_HOOK, "https://ntfy.sh"]
    status = json.loads((tmp_path / "alerts_status.json").read_text())
    assert status["last_success"]["transport"] == "ntfy"


def test_errors_carry_no_webhook_url(monkeypatch, tmp_path):
    alerter = _alerter(monkeypatch, tmp_path, lambda url, **kw: SimpleNamespace(status_code=404, text="nope"),
                       ntfy=False)
    assert alerter.send_test(URGENT) is False
    detail = json.loads((tmp_path / "alerts_status.json").read_text())["last_failure"]["detail"]
    assert "Discord HTTP 404" in detail and "secret" not in detail and "webhooks" not in detail


def test_a_tier_without_a_webhook_skips_to_ntfy(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls), reports=False)
    alerter._send(_Outbound(subject="[AGENTIC DAILY] r", body="b", tier=DAILY))
    assert [url for url, _ in calls] == ["https://ntfy.sh"]


def test_backticks_in_a_body_cannot_break_the_code_block(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    alerter._send(_Outbound(subject="[AGENTIC DAILY] t", body="a ``` b", tier=DAILY))
    assert calls[0][1]["json"]["content"].count("```") == 2
