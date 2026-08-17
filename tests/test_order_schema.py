"""Order schema tests.

The claim under test is structural: writing an option is *unrepresentable*, and every
representable order has a finite, non-negative worst case. Expectations are derived
from the order's own fields or from the union itself rather than hand-computed, so
adding a new order type surfaces here instead of silently passing.
"""

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from risk_gate.schema import (
    ORDER_KINDS,
    UNREPRESENTABLE_ACTIONS,
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    EventContractSellToCloseOrder,
    LimitExecution,
    MarketExecution,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
    parse_order,
)

JUSTIFICATION = "Earnings gap; limit would not fill before the move completes."

money = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("10000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
event_price = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("0.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
quantities = st.integers(min_value=1, max_value=10_000)
expiries = st.dates(min_value=date(2026, 1, 1), max_value=date(2030, 1, 1))


@st.composite
def limit_or_market(draw, price=money):
    p = draw(price)
    if draw(st.booleans()):
        return LimitExecution(limit_price=p)
    return MarketExecution(justification=JUSTIFICATION, max_price=p)


@st.composite
def equity_buys(draw):
    return EquityBuyOrder(
        symbol="AAPL", quantity=draw(quantities), execution=draw(limit_or_market())
    )


@st.composite
def option_buys(draw):
    return OptionBuyToOpenOrder(
        symbol="AAPL260117C00250000",
        underlying="AAPL",
        right=draw(st.sampled_from(["call", "put"])),
        expiration=draw(expiries),
        strike=draw(money),
        contracts=draw(quantities),
        execution=draw(limit_or_market()),
    )


@st.composite
def event_buys(draw):
    return EventContractBuyOrder(
        market_ticker="PRES-2028-D",
        outcome=draw(st.sampled_from(["yes", "no"])),
        contracts=draw(quantities),
        strategy=draw(st.sampled_from(["arb", "directional"])),
        execution=draw(limit_or_market(price=event_price)),
    )


opening_orders = st.one_of(equity_buys(), option_buys(), event_buys())


# --------------------------------------------------------------------------------
# Constraint #2 — writing an option is unrepresentable
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(UNREPRESENTABLE_ACTIONS))
def test_forbidden_actions_do_not_parse(action):
    payload = {
        "kind": action,
        "symbol": "AAPL",
        "quantity": 1,
        "execution": {"style": "limit", "limit_price": "10.00"},
    }
    with pytest.raises(ValidationError):
        parse_order(payload)


def test_no_order_kind_names_a_short_or_write():
    forbidden_words = ("sell_to_open", "write", "short", "margin")
    offenders = [k for k in ORDER_KINDS if any(w in k for w in forbidden_words)]
    assert offenders == []


def test_close_only_kinds_are_the_only_sells():
    sell_kinds = {k for k in ORDER_KINDS if "sell" in k}
    assert all(k.endswith("_sell_to_close") for k in sell_kinds)


def test_unmodelled_fields_are_rejected():
    """extra="forbid" is what stops a side= field riding along past the discriminator."""
    with pytest.raises(ValidationError):
        parse_order(
            {
                "kind": "option_buy_to_open",
                "symbol": "AAPL260117C00250000",
                "underlying": "AAPL",
                "right": "call",
                "expiration": "2026-01-17",
                "strike": "250.00",
                "contracts": 1,
                "side": "sell_to_open",
                "execution": {"style": "limit", "limit_price": "3.20"},
            }
        )


@given(order=opening_orders)
def test_orders_are_frozen(order):
    with pytest.raises(ValidationError):
        order.execution = LimitExecution(limit_price=Decimal("1.00"))


# --------------------------------------------------------------------------------
# Constraint #1 — every order has a finite, non-negative worst case
# --------------------------------------------------------------------------------


@given(order=opening_orders)
def test_opening_orders_have_positive_bounded_max_loss(order):
    loss = order.max_loss()
    assert loss > 0
    assert loss.is_finite()
    assert order.is_opening


@given(price=money, quantity=quantities)
def test_equity_buy_max_loss_is_its_own_outlay(price, quantity):
    order = EquityBuyOrder(
        symbol="AAPL", quantity=quantity, execution=LimitExecution(limit_price=price)
    )
    assert order.max_loss() == order.execution.price_bound * order.quantity


@given(order=option_buys())
def test_option_max_loss_is_premium_paid(order):
    assert order.max_loss() == (
        order.execution.price_bound * order.contracts * order.multiplier
    )


@given(order=event_buys())
def test_event_contract_max_loss_is_contracts_times_price(order):
    assert order.max_loss() == order.execution.price_bound * order.contracts
    # An event contract settles at 1 at best, so max loss can never exceed the
    # contract count.
    assert order.max_loss() < order.contracts


@given(price=money, small=quantities, extra=st.integers(min_value=1, max_value=1_000))
def test_max_loss_is_monotonic_in_quantity(price, small, extra):
    execution = LimitExecution(limit_price=price)
    smaller = EquityBuyOrder(symbol="AAPL", quantity=small, execution=execution)
    larger = EquityBuyOrder(symbol="AAPL", quantity=small + extra, execution=execution)
    assert larger.max_loss() > smaller.max_loss()


def test_closing_orders_risk_no_new_cash():
    closers = [
        EquitySellToCloseOrder(
            symbol="AAPL",
            quantity=10,
            execution=LimitExecution(limit_price=Decimal("250.00")),
        ),
        OptionSellToCloseOrder(
            symbol="AAPL260117C00250000",
            underlying="AAPL",
            right="call",
            expiration=date(2026, 1, 17),
            strike=Decimal("250.00"),
            contracts=2,
            execution=LimitExecution(limit_price=Decimal("3.20")),
        ),
        EventContractSellToCloseOrder(
            market_ticker="PRES-2028-D",
            outcome="yes",
            contracts=50,
            strategy="arb",
            execution=LimitExecution(limit_price=Decimal("0.42")),
        ),
    ]
    for order in closers:
        assert order.max_loss() == 0
        assert not order.is_opening


def test_every_order_kind_is_covered_by_these_tests():
    """Adding a new order type must not slip past this file unnoticed."""
    exercised = {
        "equity_buy",
        "equity_sell_to_close",
        "option_buy_to_open",
        "option_sell_to_close",
        "event_contract_buy",
        "event_contract_sell_to_close",
    }
    assert exercised == set(ORDER_KINDS)


# --------------------------------------------------------------------------------
# Execution style
# --------------------------------------------------------------------------------


def test_market_order_requires_justification_and_price_bound():
    with pytest.raises(ValidationError):
        MarketExecution(max_price=Decimal("10.00"))
    with pytest.raises(ValidationError):
        MarketExecution(justification="fast", max_price=Decimal("10.00"))
    with pytest.raises(ValidationError):
        MarketExecution(justification=JUSTIFICATION)


@given(price=st.decimals(min_value=Decimal("1.00"), max_value=Decimal("5.00"), places=2))
def test_event_contract_price_must_be_below_one(price):
    with pytest.raises(ValidationError):
        EventContractBuyOrder(
            market_ticker="PRES-2028-D",
            outcome="yes",
            contracts=10,
            strategy="directional",
            execution=LimitExecution(limit_price=price),
        )


@pytest.mark.parametrize("strategy", ["copy_trade", "hedge", "", None])
def test_event_contract_strategy_must_be_arb_or_directional(strategy):
    """No default: the two strategies carry different caps, so it must be stated."""
    with pytest.raises(ValidationError):
        EventContractBuyOrder(
            market_ticker="PRES-2028-D",
            outcome="yes",
            contracts=10,
            strategy=strategy,
            execution=LimitExecution(limit_price=Decimal("0.50")),
        )


def test_event_contract_strategy_is_required():
    with pytest.raises(ValidationError):
        EventContractBuyOrder(
            market_ticker="PRES-2028-D",
            outcome="yes",
            contracts=10,
            execution=LimitExecution(limit_price=Decimal("0.50")),
        )


@pytest.mark.parametrize("bad_quantity", [0, -1, -100])
def test_quantity_must_be_positive(bad_quantity):
    with pytest.raises(ValidationError):
        EquityBuyOrder(
            symbol="AAPL",
            quantity=bad_quantity,
            execution=LimitExecution(limit_price=Decimal("250.00")),
        )


@pytest.mark.parametrize("bad_price", [Decimal("0"), Decimal("-1.00")])
def test_price_must_be_positive(bad_price):
    with pytest.raises(ValidationError):
        LimitExecution(limit_price=bad_price)
