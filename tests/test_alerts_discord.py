"""Discord as the primary alert channel (human rulings 2026-10-12): URGENT to
#urgent, daily and weekly reports to #reports, every message an EMBED (the
plain-English card redesign), detail as an attached file, ntfy as the
fallback, no mentions, webhook URLs never in an error."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from execution import cards
from execution.alerts import DAILY, URGENT, Alerter, _Outbound

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


def _payload(kwargs):
    return kwargs["json"] if "json" in kwargs else json.loads(kwargs["data"]["payload_json"])


def test_a_card_is_an_embed_on_its_channel_with_its_colour(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    red = cards.crash_card("AI trader", "Oct 13, 12:01 ET", "TimeoutError")
    green = cards.first_entry_card("judged", 1)
    for card in (red, green):
        alerter._send(_Outbound(subject=card.title, body=card.text(), tier=card.channel, card=card))
    (u_url, u), (r_url, r) = calls
    assert u_url == URGENT_HOOK and r_url == REPORTS_HOOK
    embed = _payload(u)["embeds"][0]
    assert embed == {"title": red.title, "description": red.description, "color": 0xE74C3C}
    assert "content" not in _payload(u) and "```" not in json.dumps(_payload(u))
    assert _payload(u)["allowed_mentions"] == {"parse": []} and _payload(r)["embeds"][0]["color"] == 0x2ECC71
    assert json.loads((tmp_path / "alerts_status.json").read_text())["last_success"]["transport"] == "discord"


def test_card_detail_rides_as_an_attached_file(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    card = cards.scoreboard_card("Oct 16", [cards.BoardLine("AI trader", 0.01, 0.008, "Oct 13")], 4, 20, 60,
                                 "FULL TABLE", file_stamp="2026-10-16")
    assert alerter.card("scoreboard", card)
    alerter.close()
    url, kw = calls[0]
    assert url == REPORTS_HOOK
    name, data, kind = kw["files"]["files[0]"]
    assert name == "scoreboard-2026-10-16.txt" and "FULL TABLE" in data.decode()
    assert _payload(kw)["embeds"][0]["title"] == card.title


def test_a_legacy_text_alert_is_still_an_embed_never_a_code_block(monkeypatch, tmp_path):
    calls = []
    alerter = _alerter(monkeypatch, tmp_path, ok(calls))
    body = "\n".join(f"line {i:04d} " + "x" * 60 for i in range(200))
    alerter._send(_Outbound(subject="[AGENTIC DAILY] weekly report", body=body, tier=DAILY))
    url, kw = calls[0]
    embed = _payload(kw)["embeds"][0]
    assert embed["title"] == "weekly report" and "```" not in embed["description"]
    assert embed["description"].endswith("(full text attached)") and len(embed["description"]) < 400
    assert kw["files"]["files[0]"][1].decode().endswith(body)


def test_a_discord_failure_falls_back_to_ntfy_with_the_cards_text(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=429 if "discord" in url else 200, text="rate limited")

    alerter = _alerter(monkeypatch, tmp_path, post)
    card = cards.crash_card("AI trader", "Oct 13, 12:01 ET", "TimeoutError")
    alerter._send(_Outbound(subject=f"[AGENTIC URGENT] {card.title}", body=card.text(), tier=URGENT, card=card))
    assert [url for url, _ in calls] == [URGENT_HOOK, "https://ntfy.sh"]
    pushed = calls[1][1]["json"]
    assert pushed["priority"] == 5 and "What to do: Tell Claude Code" in pushed["message"]


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
