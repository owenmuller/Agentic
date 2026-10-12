"""Server-side one-shot checks (human ruling 2026-10-10)."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_runner", ROOT / "ops" / "check_runner.py")
check_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_runner)

NOW = datetime(2026, 10, 13, 14, 25, tzinfo=timezone.utc)


def test_a_passing_check_is_written_and_sent_on_the_daily_tier(tmp_path):
    sent = []
    code = check_runner.run_check(
        "demo", [sys.executable, "-c", "print('HTTP Request: noise'); print('ladder ok')"], out_dir=tmp_path,
        alert=lambda ok, subject, body: sent.append((ok, subject, body)), clock=lambda: NOW)
    assert code == 0 and sent[0][0] is True and sent[0][1] == "CHECK demo: PASS (exit 0)"
    text = (tmp_path / "demo-20261013T142500Z.txt").read_text()
    body = text.split("\n\n", 1)[1]  # the header echoes the command itself
    assert body.strip() == "ladder ok"


def test_a_failing_or_unstartable_check_is_urgent(tmp_path):
    sent = []
    alert = lambda ok, subject, body: sent.append((ok, subject))  # noqa: E731
    assert check_runner.run_check("bad", [sys.executable, "-c", "raise SystemExit(3)"], out_dir=tmp_path,
                                  alert=alert, clock=lambda: NOW) == 3
    assert check_runner.run_check("gone", ["/no/such/binary"], out_dir=tmp_path, alert=alert, clock=lambda: NOW) == 127
    assert [ok for ok, _ in sent] == [False, False] and "FAIL" in sent[1][1]


def test_the_scheduler_sets_no_trading_variable():
    script = (ROOT / "ops" / "vps" / "schedule_check.sh").read_text()
    assert "PAPER_MODE" not in script.replace("sets no trading variable", "") and "LIVE_TRADING" not in script
    assert "--on-calendar" in script and "America/New_York" in script and "--user" in script
