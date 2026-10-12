"""Every Discord message is a plain-English card (human ruling 2026-10-12).
Each builder is held to the rules: verdict title, 3-6 human lines, colour by
status, whole dollars and one-decimal percents, SPY beside every return, red
cards end with what to tell Claude Code, detail attached, nobody pinged."""
from __future__ import annotations

import re

import pytest

from execution import cards

ALL = {
    "error": cards.error_card("Main book", "Oct 13, 10:42 ET", "Traceback ... KeyError: 'symbol'", "full log"),
    "cost": cards.cost_card("Main book", "Oct 13, 11:00 ET", "research spend $9.40 > tripwire $8"),
    "billing": cards.billing_card("Main book", "Oct 13, 11:00 ET", "provider reports 52 passes, ledger 40"),
    "kill": cards.kill_switch_card("AI trader", 87950.4, 100050.0, -0.0321, -0.0112,
                                   'python -m lab resume ai-trader --ack "your name"'),
    "drawdown": cards.drawdown_card("Leverage ladder", 74800.0, 100200.0, -0.252, -0.121),
    "guard": cards.guard_card("AI trader", True, "Oct 13, 10:00 ET", "refused: production unit ... may not write"),
    "guard_open": cards.guard_card("Main book", False, "Oct 13, 10:00 ET", "guard ERROR ... ALLOWED"),
    "crash": cards.crash_card("AI trader", "Oct 13, 12:01 ET", "TimeoutError: request timed out"),
    "unmanaged": cards.unmanaged_card(["XYZ (10 held)", "ABC (3 held)"]),
    "pending": cards.pending_card(2, "order a\norder b"),
    "breaker": cards.breaker_card("MECH BREAKER sleeve drawdown 25.4%"),
    "halt": cards.halt_card("Main book", "operator halted: testing"),
    "exits": cards.exits_card(2, 1),
    "first": cards.first_entry_card("judged", 1),
    "close": cards.close_card(100334.2, 0.0012, 0.0031, 0.004, 34, 6.2, False, "detail"),
    "weekly": cards.weekly_main_card(100334.2, 0.0021, 0.008, "the report"),
    "ladder": cards.ladder_card("October", True, {"SPY": 16583.33, "SSO": 49749.99, "QLD": 33166.66}, {},
                                99987.0, -0.0001, 0.0002, {"1.5x SPY": 33329.1, "2x SPY": 33331.0,
                                                           "2x QQQ": 33326.9}, 0, "fills"),
    "ladder_partial": cards.ladder_card("October", False, {"SPY": 100.0}, {"QLD": 50.0}, 99987.0, None, None,
                                        {"a": 1.0}, 1, "x"),
    "ai_day": cards.ai_day_card(-0.0041, -412.3, 0.0062, 3, 2, 1, 1, ["AAPL stock", "SPY call"], 9, 3, 1.85,
                                3.0, 0, False, "detail"),
    "ai_day_halted": cards.ai_day_card(-0.13, -13000, -0.01, 1, 1, 0, 1, [], 3, 1, 0.5, 3.0, 1, True, "d"),
    "exec": cards.exec_test_card("Oct 13", 1, 20, -12.4, -9.1, 0.0043, "11 of 12", 6, 4.2, [], "detail"),
    "exec_faults": cards.exec_test_card("Oct 13", 2, 20, 3.0, 4.0, None, "12 of 12", 4, None, ["late 10:30"]),
    "check_ok": cards.check_card("ladder-day1", "Leverage ladder", "the account's state", True, "Oct 13, 10:20 ET",
                                 0, "output", "~/checks/x.txt"),
    "check_fail": cards.check_card("ladder-day1", "Leverage ladder", "the account's state", False,
                                   "Oct 13, 10:20 ET", 3, "output", "~/checks/x.txt"),
    "scoreboard": cards.scoreboard_card("Oct 16", [
        cards.BoardLine("Leverage ladder", 0.0123, 0.0081, "Oct 12"),
        cards.BoardLine("AI trader", -0.0042, 0.0081, "Oct 13", "7 trades, 4 won"),
        cards.BoardLine("Execution test", extra="4 sessions, -$12 live vs -$9 in the backtest model"),
        cards.BoardLine("Main book", 0.0011, 0.0081, "Oct 12"),
    ], 4, 20, 60, "TABLE"),
}

TITLE = re.compile(r"^[A-Z][^:]+: .+, (nothing to do|worth a look|action needed)$")
SNAKE = re.compile(r"\b[a-z]+_[a-z_]+\b")
CENTS = re.compile(r"\$\d[\d,]*\.\d")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_card_follows_the_rules(name):
    card = ALL[name]
    assert TITLE.match(card.title), card.title
    assert card.title.endswith(cards.VERDICTS[card.status])
    assert 1 <= len(card.shown_lines) <= 6
    body = card.description
    assert "```" not in body and "{" not in body and "}" not in body
    assert not SNAKE.search(body.split("What to do:")[0]), SNAKE.search(body)
    # whole dollars for trading P&L; spend lines carry cents (card review 2026-10-12)
    pnl_lines = [line for line in card.shown_lines if not re.match(r"(Model|Research) spend", line)]
    assert not CENTS.search("\n".join(pnl_lines)), body
    if card.status == cards.LOOK:
        assert card.shown_lines[0].startswith("Worth a look: ") and card.shown_lines[0].endswith(".")
        assert sum(line.startswith("Worth a look") for line in card.shown_lines) == 1
    else:
        assert not any(line.startswith("Worth a look") for line in card.shown_lines)
    for number in PERCENT.findall(body):
        assert "." in number and len(number.split(".")[1]) == 1, (name, number)
    for line in card.shown_lines:
        if re.search(r"(Today|Since start|This week|since Oct|%\s*\()", line) and "%" in line and "Down" not in line:
            assert "SPY" in line, (name, line)
    if card.status == cards.ACTION:
        assert body.split("\n")[-1].startswith("What to do: Tell Claude Code: \""), body
    else:
        assert "What to do" not in body
    assert card.color == cards.COLORS[card.status]


def test_colours_and_channels():
    assert ALL["error"].color == 0xE74C3C and ALL["error"].channel == cards.URGENT
    assert ALL["drawdown"].color == 0xF1C40F and ALL["drawdown"].channel == cards.URGENT
    assert ALL["ladder"].color == 0x2ECC71 and ALL["ladder"].channel == cards.DAILY
    assert ALL["ladder_partial"].status == cards.LOOK and ALL["exec_faults"].status == cards.LOOK
    assert ALL["check_ok"].channel == cards.DAILY and ALL["check_fail"].channel == cards.URGENT
    assert ALL["ai_day_halted"].status == cards.ACTION


def test_numbers_are_rounded_and_returns_carry_spy():
    assert cards.usd(16583.33) == "$16,583" and cards.usd(-412.6, signed=True) == "-$413"
    assert cards.pct(0.01234) == "+1.2%" and cards.pct(-0.00004) == "+0.0%" and cards.pct(None) == "n/a"
    assert cards.with_spy(0.0123, 0.0081) == "+1.2% (SPY +0.8%)"
    assert "Today: -0.4% (SPY +0.6%), -$412." in ALL["ai_day"].shown_lines
    assert "Model spend: $1.85 of $3 daily cap." in ALL["ai_day"].shown_lines
    assert cards.cents(1.8) == "$1.80" and cards.cents(3.0) == "$3"


def test_detail_goes_to_the_attachment_and_overflow_lines_too():
    card = cards.Card("X", "y", cards.GOOD, tuple(f"line {i}." for i in range(9)), detail="more")
    assert len(card.shown_lines) == 6 and "line 8." not in card.description
    name, text = card.attachment
    assert name == "details.txt" and "line 8." in text and "more" in text
    assert ALL["scoreboard"].attachment[1] == ALL["scoreboard"].title + "\n\nTABLE\n"
    stamped = cards.scoreboard_card("Oct 16", [], 1, 20, 60, "T", file_stamp="2026-10-16")
    assert stamped.attachment[0] == "scoreboard-2026-10-16.txt"


def test_too_early_to_judge_is_said_plainly():
    assert ALL["scoreboard"].shown_lines[-1].startswith("Too early to judge: 4 of 20 sessions")


def test_a_red_card_without_what_to_do_cannot_exist():
    with pytest.raises(ValueError, match="what to do"):
        cards.Card("X", "y", cards.ACTION, ("a.",))


def test_sample_cards_say_so_in_the_title():
    card = cards.exits_card(1, 0, sample=True)
    assert card.title.startswith("SAMPLE - Main book:")


def test_three_verdicts_and_the_yellow_line():
    """Card review 2026-10-12: green 'nothing to do', yellow 'worth a look'
    with one line saying what, red 'action needed'."""
    assert ALL["first"].title.endswith(", nothing to do")
    assert ALL["ladder_partial"].title.endswith(", worth a look")
    assert ALL["ladder_partial"].shown_lines[0] == (
        "Worth a look: some orders did not fill, and the next run (10:00 ET) finishes the rebalance; "
        "1 problem(s) were recorded (in the attached detail).")
    assert ALL["ai_day"].title == "AI trader: market closed, nothing to do"
    yellow_ai = cards.ai_day_card(0.001, 100, 0.002, 1, 1, 1, 0, [], 3, 1, 3.0, 3.0, 2, False, "d")
    assert yellow_ai.title == "AI trader: market closed, worth a look"
    assert yellow_ai.shown_lines[0].startswith("Worth a look: 2 problem(s) were recorded today")
    assert "the model used its whole daily spend cap" in yellow_ai.shown_lines[0]
    assert ALL["error"].title.endswith(", action needed")
    with pytest.raises(ValueError, match="worth a look"):
        cards.Card("X", "y", cards.LOOK, ("a.",))
    with pytest.raises(ValueError, match="only a yellow"):
        cards.Card("X", "y", cards.GOOD, ("a.",), look_at="z")
