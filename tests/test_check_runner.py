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


def _remove(tmp_path, name):
    """Run ``schedule_check.sh --remove`` in a fake HOME with a stub systemctl."""
    import os
    import subprocess

    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    (stub / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (stub / "systemctl").chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{stub}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(ROOT / "ops" / "vps" / "schedule_check.sh"), "--remove", name],
                          env=env, capture_output=True, text=True)


def _install(tmp_path, name):
    units, bin_ = tmp_path / ".config" / "systemd" / "user", tmp_path / "checks" / "bin"
    units.mkdir(parents=True, exist_ok=True)
    bin_.mkdir(parents=True, exist_ok=True)
    for path in (units / f"check-{name}.timer", units / f"check-{name}.service", bin_ / f"{name}.sh"):
        path.write_text("x\n")
    return units, bin_


def test_remove_takes_the_bare_or_the_prefixed_name_and_fails_on_nothing(tmp_path):
    # 2026-11-10: "--remove check-deploy-b5735af" printed "removed" and removed nothing
    units, bin_ = _install(tmp_path, "deploy-b5735af")
    result = _remove(tmp_path, "check-deploy-b5735af")
    assert result.returncode == 0 and "removed check-deploy-b5735af" in result.stdout, result.stderr
    assert not any(units.iterdir()) and not any(bin_.iterdir())
    _install(tmp_path, "momentum-step2")
    assert _remove(tmp_path, "momentum-step2").returncode == 0 and not any(units.iterdir())
    missing = _remove(tmp_path, "momentum-step2")
    assert missing.returncode == 1 and "no check named momentum-step2" in missing.stderr
    assert _remove(tmp_path, "check-nothing").returncode == 1


def test_remove_refuses_an_ambiguous_name(tmp_path):
    units, _ = _install(tmp_path, "check-x")      # a check whose own name starts with check-
    _install(tmp_path, "x")
    result = _remove(tmp_path, "check-x")
    assert result.returncode == 1 and "ambiguous" in result.stderr
    assert len(list(units.iterdir())) == 4        # nothing removed
