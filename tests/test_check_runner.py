"""Server-side one-shot checks (human ruling 2026-10-10), reported as cards
(ruling 2026-10-12): a pass is a green card for #reports, a failure a red one
for #urgent that says what to tell Claude Code."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

from execution import cards

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_runner", ROOT / "ops" / "check_runner.py")
check_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_runner)

NOW = datetime(2026, 10, 13, 14, 25, tzinfo=timezone.utc)


def test_a_passing_check_is_written_and_sent_as_a_green_card(tmp_path):
    sent = []
    code = check_runner.run_check(
        "ladder-day1", [sys.executable, "-c", "print('HTTP Request: noise'); print('ladder ok')"], out_dir=tmp_path,
        alert=sent.append, clock=lambda: NOW)
    card = sent[0]
    assert code == 0 and card.status == cards.GOOD and card.channel == cards.DAILY
    assert card.title == "Leverage ladder: scheduled check 'ladder-day1' ran, nothing to do"
    assert "When: Oct 13, 10:25 ET." in card.shown_lines
    text = (tmp_path / "ladder-day1-20261013T142500Z.txt").read_text()
    body = text.split("\n\n", 1)[1]  # the header echoes the command itself
    assert body.strip() == "ladder ok"
    assert "ladder ok" in card.attachment[1]


def test_a_failing_or_unstartable_check_is_a_red_card(tmp_path):
    sent = []
    assert check_runner.run_check("ai-trader-day1", [sys.executable, "-c", "raise SystemExit(3)"],
                                  out_dir=tmp_path, alert=sent.append, clock=lambda: NOW) == 3
    assert check_runner.run_check("gone", ["/no/such/binary"], out_dir=tmp_path, alert=sent.append,
                                  clock=lambda: NOW) == 127
    assert [c.status for c in sent] == [cards.ACTION, cards.ACTION]
    assert sent[0].account == "AI trader" and sent[1].account == "Server" and sent[0].channel == cards.URGENT
    assert "~/checks/ai-trader-day1-20261013T142500Z.txt" in sent[0].what_to_do


def test_the_check_says_what_it_checked_in_words():
    assert check_runner.describe("ladder-day1", ["python", "-m", "lab", "status", "ladder"]) == (
        "Leverage ladder", "the leverage ladder account's state, trades and problems so far")


def test_the_scheduler_sets_no_trading_variable():
    script = (ROOT / "ops" / "vps" / "schedule_check.sh").read_text()
    assert "PAPER_MODE" not in script.replace("sets no trading variable", "") and "LIVE_TRADING" not in script
    # installed and persistent (human ruling 2026-10-14): a check survives a reboot
    assert "OnCalendar=" in script and "America/New_York" in script and "Persistent=true" in script
    assert ".config/systemd/user" in script and "systemctl --user enable --now" in script
    assert "systemd-run" not in script
