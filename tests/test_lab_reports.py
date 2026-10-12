"""The paper lab's cards from real-shaped ledgers (ruling 2026-10-12)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from execution import cards
from lab import reports
from lab import scoreboard as sb

DAY = date(2026, 10, 13)
LABELS = {"spy_1_5x": "1.5x SPY (SPY + SSO)", "spy_2x": "2x SPY (SSO)", "qqq_2x": "2x QQQ (QLD)"}


def ev(event, at="2026-10-13T14:01:00+00:00", **fields):
    return {"event": event, "at": at, **fields}


def test_the_rebalance_report_names_trades_value_and_spy():
    engine = SimpleNamespace(
        rebalance="complete", month="2026-10", equity="99987.12",
        books={"spy_1_5x": Decimal("33329.10"), "spy_2x": Decimal("33331.00"), "qqq_2x": Decimal("33327.02")},
        fills=[dict(rung="spy_1_5x", symbol="SPY", side="buy", quantity=Decimal("21.29"), price=Decimal("778.1"),
                    limit=Decimal("778.9"), modelled=Decimal("777.33")),
               dict(rung="spy_2x", symbol="SSO", side="buy", quantity=Decimal("460.26"), price=Decimal("71.95"),
                    limit=Decimal("72.06"), modelled=Decimal("71.9"))])
    events = [ev("snapshot", at="2026-10-12T20:10:05+00:00", equity="100000")]
    card = reports.ladder_report(engine, events, LABELS, lambda a, b: 0.0021, DAY)
    assert card.title == "Leverage ladder: October rebalance done, nothing to do" and card.status == cards.GOOD
    assert card.shown_lines[0] == "Trades: bought SPY $16,566 and SSO $33,116."
    assert "Since start: 0% (SPY +0.2%)." in card.shown_lines
    assert "1.5x SPY $33,329" in card.shown_lines[3]
    assert "778.1" in card.attachment[1]


def test_a_snapshot_only_run_sends_nothing():
    engine = SimpleNamespace(rebalance=None, fills=[])
    assert reports.ladder_report(engine, [], LABELS, lambda a, b: None, DAY) is None


def test_the_ai_end_of_day_card_from_a_ledger():
    events = [
        ev("position_opened", pid="a", symbol="AAPL", instrument="stock", horizon="days", stop="194.04",
           target="215.06", thesis="t"),
        ev("settled", pid="a", side="buy", filled_qty="10", avg_price="200", multiplier=1),
        ev("settled", pid="a", side="sell", filled_qty="10", avg_price="210", multiplier=1),
        ev("position_closed", pid="a", symbol="AAPL", reason="target"),
        ev("llm_pass", cost_usd="0.21", ideas=[{}, {}, {}]),
        ev("idea_rejected", symbol="XYZ", reason="stop 0.5% from entry"),
        ev("anchor_check", symbol="AAPL", price_seen="300", live_mid="200.05", price_seen_gap="0.4996"),
    ]
    card = reports.ai_day_report(events, {"equity": "100100", "last_equity": "100000"}, [], 0.003, 3.0, False, DAY)
    assert card.title == "AI trader: market closed, nothing to do"
    assert card.shown_lines[0] == "Today: +0.1% (SPY +0.3%), +$100."
    assert card.shown_lines[1] == "Trades: 1 opened, 1 closed (1 won, 0 lost)."
    assert "the rules took 1 and turned down 2" in card.shown_lines[3]
    detail = card.attachment[1]
    assert "AAPL: target, $100.00" in detail and "XYZ: stop 0.5% from entry" in detail and "+50.0%" in detail


def test_the_scoreboard_card_has_a_line_per_account_and_the_too_early_note():
    ladder = [ev("snapshot", at="2026-10-12T20:10:00+00:00", equity="100000"),
              ev("snapshot", at="2026-10-13T20:10:00+00:00", equity="101000")]
    spy = {date(2026, 10, 12): 600.0, date(2026, 10, 13): 603.0}
    board = sb.build(ladder_events=ladder, ai_events=[], exec_lines=[{"day": "2026-10-13", "fills": 4,
                     "pnl_live": -12.4, "pnl_modelled_at_live_sizes": -9.1, "slippage_bp_vs_model": {}}],
                     spy=spy, main_equity={}, rung_labels={})
    card = reports.scoreboard_report(board, date(2026, 10, 16), "TABLE", 20, 60)
    assert card.shown_lines[0] == "Leverage ladder: +1% (SPY +0.5%) since Oct 12."
    assert any(line.startswith("Execution test: 1 session, -$12 live vs -$9") for line in card.shown_lines)
    assert card.shown_lines[-1].startswith("Too early to judge: 2 of 20 sessions")
    assert card.attachment[0] == "scoreboard-2026-10-16.txt"
