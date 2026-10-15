"""The momentum sleeve's weekly line (ruling 2026-10-13; v1.1 vs SPY and SPMO,
2026-10-14): Modified Dietz on the live book from real fills, shadows as
software books with costs, the card."""
from __future__ import annotations

import json

import pytest

from execution import cards
from orchestrator import momentum_report as mr

DAYS = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]


def test_dietz_matches_a_simple_return_and_weights_flows_by_time():
    assert mr.dietz(100.0, 110.0, []) == pytest.approx(0.10)
    # a buy of 100 at the very start of a period from nothing, worth 105 at the end
    assert mr.dietz(0.0, 105.0, [(1.0, 100.0)]) == pytest.approx(0.05)
    # a mid-period deposit counts half
    assert mr.dietz(100.0, 160.0, [(0.5, 50.0)]) == pytest.approx(10 / 125)
    assert mr.dietz(0.0, 0.0, []) is None


def test_a_software_book_rebalances_at_the_close_and_pays_its_costs():
    prices = {"A": {d: 10.0 + i for i, d in enumerate(DAYS)}, "B": {d: 20.0 for d in DAYS}}
    path = mr.book_path([("2026-11-02", {"A": 0.5, "B": 0.5})], prices, DAYS, cost=0.001)
    assert path["2026-11-02"] == pytest.approx(1 - 0.001)          # bought 100% of NAV: 10 bp
    # half in A (11 -> 16), half in B (flat), from 0.999
    assert path["2026-10-30"] == 1.0                                # cash before the first rebalance
    assert path["2026-11-09"] == pytest.approx(0.999 * (0.5 * 16 / 11 + 0.5))
    partial = mr.book_path([("2026-11-02", {"A": 0.5})], prices, DAYS, cost=0.0)
    assert partial["2026-11-09"] == pytest.approx(0.5 * 16 / 11 + 0.5)  # the rest stays cash


def test_the_live_sleeve_that_filled_its_plan_matches_the_modelled_book(tmp_path):
    prices = {"A": {d: 10.0 + i for i, d in enumerate(DAYS)}, "SPY": {d: 500.0 + i for i, d in enumerate(DAYS)},
              "SPMO": {d: 100.0 for d in DAYS}}
    (tmp_path / "plan-2026-11.json").write_text(json.dumps({
        "rebalance_date": "2026-11-02", "targets": [{"symbol": "A", "weight": "1.0"}]}))
    (tmp_path / "shadows-2026-11.json").write_text(json.dumps({
        "rebalance_date": "2026-11-02", "books": {"spmo": {"weights": {"SPMO": 1.0}},
                                                   "v1_top_1000": {"weights": {"A": 0.5}}}}))
    fills = [mr.Fill("2026-11-02", "A", 100.0, 1100.0)]  # bought at the close: no fill gap
    report = mr.build(tmp_path, fills, prices, DAYS, "2026-11-02", "2026-11-09")
    assert report.started == "2026-11-02" and report.holdings == 1 and report.invested == pytest.approx(1600.0)
    assert report.since["v1.1 live"] == pytest.approx(16 / 11 - 1)
    assert report.since["v1.1 modelled (plan at closes)"] == pytest.approx(0.999 * 16 / 11 - 1, rel=1e-6)
    assert report.since["spmo"] == pytest.approx(-0.001, abs=1e-6)
    assert report.since["SPY"] == pytest.approx(6 / 500)
    assert "SPMO is the binding comparison" in report.table()
    card = cards.momentum_week_card(report)
    assert card.status == cards.GOOD and card.title == "Momentum: weekly report, nothing to do"
    text = " ".join(card.all_lines)
    assert "+45.5%" in text and "SPMO" in text and "0 of 2 ahead" in text


def test_before_the_first_trade_the_card_says_so(tmp_path):
    report = mr.build(tmp_path, [], {}, DAYS, "2026-11-02", "2026-11-09")
    card = cards.momentum_week_card(report)
    assert card.status == cards.GOOD and "has not traded yet" in card.all_lines[0]


def test_step2_reports_and_applies_nothing(tmp_path):
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    from audit.log import AuditLog
    from orchestrator import momentum_step2
    from risk_gate import RiskGate, RiskLimits
    from risk_gate.state import AccountState

    limits = RiskLimits.load()
    gate = RiskGate(limits, AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")))
    checks = SimpleNamespace(gate=gate, audit=AuditLog(tmp_path / "audit.jsonl"),
                             orchestrator_config=SimpleNamespace(execution_test=SimpleNamespace(max_sessions=20)))
    state = tmp_path / "execution_test_state.json"
    state.write_text(json.dumps({"sessions": [f"d{i}" for i in range(12)]}))
    early = momentum_step2.report(checks, state, date(2026, 10, 28))
    assert not early.ready and "12 of 20" in early.text and "NOT READY" in early.text
    state.write_text(json.dumps({"sessions": [f"d{i}" for i in range(20)]}))
    done = momentum_step2.report(checks, state, date(2026, 11, 6), ["2026-11-06", "2026-12-01"])
    assert done.ready and "READY to decide" in done.text
    assert "allocation $25,000 -> $10,000" in done.text and "allocation $15,000 -> $30,000" in done.text
    assert "2026-12-01" in done.text and "nothing has been changed" in done.text
    assert limits.portfolio.sleeves.momentum == Decimal("0.15")  # untouched


def test_the_daily_card_reports_today_week_to_date_and_since_start(tmp_path):
    prices = {"A": {d: 10.0 + i for i, d in enumerate(DAYS)}, "SPY": {d: 500.0 + i for i, d in enumerate(DAYS)},
              "SPMO": {d: 100.0 for d in DAYS}}
    (tmp_path / "plan-2026-11.json").write_text(json.dumps({
        "rebalance_date": "2026-11-02", "targets": [{"symbol": "A", "weight": "1.0"}]}))
    fills = [mr.Fill("2026-11-02", "A", 100.0, 1100.0)]
    report = mr.build(tmp_path, fills, prices, DAYS, "2026-11-09", "2026-11-09")
    assert report.day["v1.1 live"] == pytest.approx(16 / 15 - 1)      # 15 -> 16 on the last day
    assert report.day["SPY"] == pytest.approx(506 / 505 - 1)
    assert report.since["v1.1 live"] == pytest.approx(16 / 11 - 1)
    card = cards.momentum_daily_card(report)
    assert card.title == "Momentum: daily report, nothing to do"
    lines = card.all_lines
    assert lines[0].startswith("Today: +6.7%") and lines[1].startswith("Week to date:") and "Since start" in lines[2]
    assert "SPMO" in lines[0] and "today" in report.table() and "week to date" in report.table()
