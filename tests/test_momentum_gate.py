"""The momentum sleeve in the risk gate (human ruling 2026-10-13): its own
cap table, the 15/15 allocation step, cash-secured with T+1 on today's sale
proceeds, the book-level kill switch."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from risk_gate import RiskGate, RiskLimits
from risk_gate.rejections import RejectionCode
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import AccountState, Sleeve

D = Decimal
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def shipped():
    return RiskLimits.load()


@pytest.fixture(scope="module")
def live(shipped):
    caps = shipped.momentum_sleeve.model_copy(update={"entries_enabled": True})
    return shipped.model_copy(update={"momentum_sleeve": caps})


def gate(limits, cash=D("100000")):
    return RiskGate(limits, AccountState(cash=cash, high_water_mark=cash), lambda: NOW)


def buy(symbol, qty, price, sleeve="momentum"):
    return EquityBuyOrder(symbol=symbol, quantity=D(qty), execution=LimitExecution(limit_price=D(price)),
                          sleeve=sleeve)


def test_the_shipped_allocation_is_step_one(shipped):
    w = shipped.portfolio.sleeves
    assert (w.equity, w.momentum, w.aggressive, w.mechanical, w.baseline) == (
        D("0.15"), D("0.15"), D("0.25"), D("0.15"), D("0.30"))
    caps = shipped.momentum_sleeve
    assert (caps.max_positions, caps.max_single_position, caps.max_sector_exposure, caps.max_daily_deployment) == (
        15, D("0.10"), D("0.30"), D("1.00"))
    assert caps.entries_enabled is False  # until the round trip passes


def test_entries_off_refuses_every_momentum_buy(shipped):
    decision = gate(shipped).submit(buy("AAPL", 1, "200"))
    assert not decision.is_approved and decision.code is RejectionCode.SLEEVE_ALLOCATION_EXCEEDED


def test_a_position_is_capped_at_ten_percent_of_the_sleeve(live):
    g = gate(live)  # sleeve NAV 15,000: cap 1,500
    assert g.submit(buy("AAPL", 7, "200")).is_approved            # 1,400
    refused = g.submit(buy("MSFT", 4, "400"))                        # 1,600
    assert refused.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED


def test_at_most_fifteen_names(live):
    g = gate(live, cash=D("1000000"))  # sleeve NAV 150,000: plenty of room per name
    for i in range(15):
        assert g.submit(buy(f"N{i:02d}A", 10, "100")).is_approved
    assert g.submit(buy("XTRA", 10, "100")).code is RejectionCode.MAX_POSITIONS_EXCEEDED


def test_the_allocation_ceiling_is_fifteen_plus_drift(live):
    # The daily cap (100% of the sleeve) binds first inside one day; the
    # ceiling binds across days: a sleeve already at 17% of NAV from earlier
    # buys may add at most one more point of drift.
    g = gate(live, cash=D("830000"))
    held = g.state.ensure_position(("momentum", "OLD"), Sleeve.MOMENTUM, 1, False)
    held.quantity, held.cost_basis, held.market_value = D(1700), D("170000"), D("170000")  # NAV 1,000,000
    assert g.submit(buy("NEWA", 90, "100")).is_approved          # 179,000: inside 18%
    refused = g.submit(buy("NEWB", 20, "100"))                    # 181,000: past 15% + 3%
    assert refused.code is RejectionCode.SLEEVE_ALLOCATION_EXCEEDED


def test_today_sale_proceeds_cannot_fund_a_momentum_buy(live):
    g = gate(live, cash=D("2000"))
    g.state.unsettled_proceeds = D("1500")
    g.state.deployment_date = NOW.date()
    decision = g.submit(buy("AAPL", 3, "200"))  # 600 > 500 settled
    assert decision.code is RejectionCode.UNSETTLED_FUNDS


def test_the_kill_switch_stops_buys_but_sells_run(live):
    g = gate(live)
    order = buy("AAPL", 5, "200")
    approved = g.submit(order)
    g.record_fill(approved, D("200"))
    g.state.kill_switch_tripped = True
    assert g.submit(buy("MSFT", 1, "300")).code is RejectionCode.KILL_SWITCH_ACTIVE
    sell = EquitySellToCloseOrder(symbol="AAPL", quantity=D(5), execution=LimitExecution(limit_price=D("199")),
                                  sleeve="momentum")
    assert g.submit(sell).is_approved
    over = EquitySellToCloseOrder(symbol="AAPL", quantity=D(1), execution=LimitExecution(limit_price=D("199")),
                                  sleeve="momentum")
    assert g.submit(over).code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY


def test_judged_and_momentum_hold_the_same_name_apart(live):
    g = gate(live)
    assert g.submit(buy("AAPL", 2, "200", sleeve="equity")).is_approved
    assert g.submit(buy("AAPL", 2, "200")).is_approved
    keys = {p.key for p in g.state.positions.values()}
    assert ("equity", "AAPL") in keys and ("momentum", "AAPL") in keys
    assert g.sleeve_nav(Sleeve.MOMENTUM) == D("15000") and g.sleeve_nav(Sleeve.EQUITY) == D("15000")
