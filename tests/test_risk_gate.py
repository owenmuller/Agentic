"""Risk gate tests.

The four properties the gate exists to guarantee are asserted against *sequences* of
orders, not single orders, because every one of them is a whole-history property: any
single order can look fine while the sequence bankrupts you. The stateful machine at
the bottom drives arbitrary interleavings of submit / fill / cancel and re-checks all
four invariants after every step.

Expectations are computed from the loaded limits and the gate's own state rather than
hand-written numbers, so a change to `config/risk_limits.yaml` moves the tests with it
instead of leaving them asserting a stale cap.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from risk_gate import (
    AccountState,
    AccountType,
    ApprovedOrder,
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    LimitExecution,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
    Rejection,
    RejectionCode,
    RiskGate,
    RiskLimits,
    Sleeve,
    position_key,
)
from risk_gate.state import business_days_before
from risk_gate.sectors import SectorMap

ZERO = Decimal("0")
START_CASH = Decimal("100000")
START = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic clock. Tests advance it explicitly."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance_days(self, days: int) -> None:
        self.now += timedelta(days=days)


@pytest.fixture(scope="session")
def limits() -> RiskLimits:
    return RiskLimits.load()


def make_gate(
    limits: RiskLimits,
    cash: Decimal = START_CASH,
    account_type: AccountType = AccountType.CASH,
    clock: FakeClock | None = None,
    sectors: "SectorMap | None" = None,
) -> RiskGate:
    state = AccountState(
        cash=cash, high_water_mark=cash, account_type=account_type
    )
    return RiskGate(limits, state, clock=clock or FakeClock(), sectors=sectors)


def design_limits(equity: str = "0.90", prediction: str = "0.10") -> RiskLimits:
    """The DESIGN allocation, for tests exercising prediction-sleeve mechanics.

    The live config is 100/0 (2026-08-21 ruling: no prediction venue exists), which
    makes every prediction-sleeve cap zero. The mechanisms stay in the codebase and
    stay tested — against the weights they were designed for."""
    raw = RiskLimits.load().model_dump()
    raw["portfolio"]["sleeves"] = {"equity": equity, "prediction": prediction}
    return RiskLimits.model_validate(raw)


def equity_buy(symbol: str = "AAPL", qty: int = 1, price: str = "100.00"):
    return EquityBuyOrder(
        symbol=symbol,
        quantity=qty,
        execution=LimitExecution(limit_price=Decimal(price)),
    )


def equity_sell(symbol: str = "AAPL", qty: int = 1, price: str = "100.00"):
    return EquitySellToCloseOrder(
        symbol=symbol,
        quantity=qty,
        execution=LimitExecution(limit_price=Decimal(price)),
    )


def option_buy(contracts: int = 1, price: str = "1.00", symbol: str = "AAPL260117C00250000"):
    return OptionBuyToOpenOrder(
        symbol=symbol,
        underlying="AAPL",
        right="call",
        expiration=date(2026, 1, 17),
        strike=Decimal("250.00"),
        contracts=contracts,
        execution=LimitExecution(limit_price=Decimal(price)),
    )


def option_sell(contracts: int = 1, price: str = "1.00", symbol: str = "AAPL260117C00250000"):
    return OptionSellToCloseOrder(
        symbol=symbol,
        underlying="AAPL",
        right="call",
        expiration=date(2026, 1, 17),
        strike=Decimal("250.00"),
        contracts=contracts,
        execution=LimitExecution(limit_price=Decimal(price)),
    )


def event_buy(
    contracts: int = 1,
    price: str = "0.50",
    outcome: str = "yes",
    strategy: str = "directional",
):
    return EventContractBuyOrder(
        market_ticker="PRES-2028-D",
        outcome=outcome,
        contracts=contracts,
        strategy=strategy,
        execution=LimitExecution(limit_price=Decimal(price)),
    )


def approve(gate: RiskGate, order) -> ApprovedOrder:
    decision = gate.submit(order)
    assert decision.is_approved, f"expected approval, got {decision}"
    return decision


def reject(gate: RiskGate, order) -> Rejection:
    decision = gate.submit(order)
    assert not decision.is_approved, "expected rejection, got an approval"
    return decision


# ================================================================================
# ApprovedOrder — the no-bypass property
# ================================================================================


def test_approved_order_cannot_be_constructed_outside_the_gate(limits):
    with pytest.raises(PermissionError):
        ApprovedOrder(object(), equity_buy(), Decimal("1"), START, 1)


def test_approved_order_is_immutable(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy())
    with pytest.raises(AttributeError):
        approved._max_loss = Decimal("0")


def test_approval_carries_the_reserved_worst_case(limits):
    gate = make_gate(limits)
    order = equity_buy(qty=10, price="100.00")
    approved = approve(gate, order)
    assert approved.max_loss == order.max_loss()
    assert gate.buying_power == START_CASH - order.max_loss()


# ================================================================================
# Cash-secured buying power
# ================================================================================


def low_cash_gate(limits, cash: Decimal, nav: Decimal) -> RiskGate:
    """A gate whose cash is small relative to NAV.

    In a normal single-cash-pool account the percentage caps bind long before cash
    does — 5% of sleeve NAV is always far below the cash balance — so buying power is
    a backstop that only fires once NAV is mostly deployed. Reaching that honestly
    takes dozens of fills, so these tests seed the value in the prediction sleeve to
    isolate the buying-power branch. The state is synthetic, not reachable.
    """
    state = AccountState(cash=cash, high_water_mark=nav)
    seeded = state.ensure_position(("event", "SEED", "yes"), Sleeve.PREDICTION, 1, False)
    seeded.quantity = 1
    seeded.cost_basis = nav - cash
    seeded.market_value = nav - cash
    return RiskGate(limits, state, clock=FakeClock())


def test_order_beyond_buying_power_is_rejected(limits):
    gate = low_cash_gate(limits, cash=Decimal("100"), nav=Decimal("100000"))
    rejection = reject(gate, equity_buy(qty=5, price="100.00"))
    assert rejection.code is RejectionCode.INSUFFICIENT_BUYING_POWER
    assert rejection.observed == Decimal("500.00")
    assert rejection.limit == Decimal("100")


def test_reservations_are_not_spendable_twice(limits):
    gate = low_cash_gate(limits, cash=Decimal("4000"), nav=Decimal("100000"))
    approve(gate, equity_buy(qty=30, price="100.00"))
    assert gate.buying_power == Decimal("1000")
    # The same dollars must not fund a second order.
    rejection = reject(gate, equity_buy(symbol="MSFT", qty=20, price="100.00"))
    assert rejection.code is RejectionCode.INSUFFICIENT_BUYING_POWER


def test_fill_below_the_reserved_bound_returns_the_difference(limits):
    gate = make_gate(limits)
    order = equity_buy(qty=10, price="100.00")
    approved = approve(gate, order)
    gate.record_fill(approved, Decimal("90.00"))
    assert gate.buying_power == START_CASH - Decimal("900")
    assert gate.state.reserved_cash == 0


# ================================================================================
# The synthetic-short gap — position-aware close validation
# ================================================================================


def test_close_without_a_position_is_rejected(limits):
    gate = make_gate(limits)
    rejection = reject(gate, equity_sell(qty=1))
    assert rejection.code is RejectionCode.POSITION_NOT_HELD


def test_close_larger_than_held_is_rejected(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=10))
    gate.record_fill(approved, Decimal("100.00"))
    rejection = reject(gate, equity_sell(qty=11))
    assert rejection.code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY
    assert rejection.limit == Decimal("10")
    assert rejection.observed == Decimal("11")


def test_two_closes_cannot_each_sell_the_same_shares(limits):
    """The reservation is what makes this a rejection instead of a net short."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=10))
    gate.record_fill(approved, Decimal("100.00"))
    approve(gate, equity_sell(qty=6))
    rejection = reject(gate, equity_sell(qty=6))
    assert rejection.code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY


def test_option_close_exceeding_held_contracts_is_rejected(limits):
    gate = make_gate(limits)
    approved = approve(gate, option_buy(contracts=2, price="1.00"))
    gate.record_fill(approved, Decimal("1.00"))
    assert reject(gate, option_sell(contracts=3)).code is (
        RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY
    )


# ================================================================================
# Caps
# ================================================================================


def test_single_position_cap_is_five_percent_of_sleeve_nav(limits):
    gate = make_gate(limits)
    cap = gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_single_position
    just_over = int(cap / Decimal("100")) + 1
    rejection = reject(gate, equity_buy(qty=just_over, price="100.00"))
    assert rejection.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    assert rejection.limit == cap


def test_single_position_cap_accumulates_across_orders(limits):
    gate = make_gate(limits)
    cap = gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_single_position
    half = int(cap / Decimal("100") / 2)
    approve(gate, equity_buy(qty=half, price="100.00"))
    approve(gate, equity_buy(qty=half, price="100.00"))
    assert reject(gate, equity_buy(qty=half, price="100.00")).code is (
        RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    )


def test_daily_deployment_cap_blocks_further_orders_that_day(limits):
    gate = make_gate(limits)
    sleeve_nav = gate.sleeve_nav(Sleeve.EQUITY)
    single_cap = sleeve_nav * limits.equity_sleeve.max_single_position
    daily_cap = sleeve_nav * limits.equity_sleeve.max_daily_deployment
    per_order = int(single_cap / Decimal("100"))
    # Spread across distinct symbols so the single-position cap is not the binding one.
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]
    codes = []
    for symbol in symbols:
        decision = gate.submit(equity_buy(symbol=symbol, qty=per_order, price="100.00"))
        if not decision.is_approved:
            codes.append(decision.code)
    assert RejectionCode.MAX_DAILY_DEPLOYMENT_EXCEEDED in codes
    assert gate.state.deployed_today <= daily_cap


def test_daily_deployment_resets_on_a_new_day(limits):
    clock = FakeClock()
    gate = make_gate(limits, clock=clock)
    approve(gate, equity_buy(qty=10, price="100.00"))
    assert gate.state.deployed_today > 0
    clock.advance_days(1)
    approve(gate, equity_buy(symbol="MSFT", qty=10, price="100.00"))
    assert gate.state.deployed_today == Decimal("1000")


def test_aggregate_option_premium_cap(limits):
    clock = FakeClock()
    gate = make_gate(limits, clock=clock)
    equity_sleeve = gate.sleeve_nav(Sleeve.EQUITY)
    premium_cap = equity_sleeve * limits.equity_sleeve.max_options_premium_at_risk
    single_cap = equity_sleeve * limits.equity_sleeve.max_single_position
    # Distinct contracts, each under the single-position cap, one per day so the daily
    # deployment cap is not the binding constraint.
    contracts_each = int(single_cap / Decimal("100") / Decimal("1.00"))
    codes = []
    for strike in range(200, 260, 5):
        clock.advance_days(1)
        order = option_buy(
            contracts=contracts_each,
            price="1.00",
            symbol=f"AAPL260117C00{strike}000",
        )
        decision = gate.submit(order)
        if not decision.is_approved:
            codes.append(decision.code)
    assert RejectionCode.MAX_OPTIONS_PREMIUM_EXCEEDED in codes
    assert gate.state.options_premium_at_risk <= premium_cap


def test_directional_event_position_capped_at_two_percent():
    limits = design_limits()
    gate = make_gate(limits)
    prediction_cap = (
        gate.sleeve_nav(Sleeve.PREDICTION)
        * limits.prediction_sleeve.directional.max_position
    )
    contracts = int(prediction_cap / Decimal("0.50")) + 10
    rejection = reject(gate, event_buy(contracts=contracts, strategy="directional"))
    assert rejection.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    assert rejection.limit == prediction_cap


def test_arb_event_position_capped_at_the_tighter_half_percent():
    limits = design_limits()
    gate = make_gate(limits)
    arb_cap = (
        gate.sleeve_nav(Sleeve.PREDICTION)
        * limits.prediction_sleeve.arbitrage.max_position
    )
    contracts = int(arb_cap / Decimal("0.50")) + 10
    rejection = reject(gate, event_buy(contracts=contracts, strategy="arb"))
    assert rejection.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    assert rejection.limit == arb_cap


def test_the_same_size_passes_as_directional_and_fails_as_arb():
    limits = design_limits()
    """The strategy tag is what distinguishes them — nothing else about the order."""
    gate = make_gate(limits)
    arb_cap = (
        gate.sleeve_nav(Sleeve.PREDICTION)
        * limits.prediction_sleeve.arbitrage.max_position
    )
    contracts = int(arb_cap / Decimal("0.50")) + 10
    approve(gate, event_buy(contracts=contracts, strategy="directional"))

    fresh = make_gate(limits)
    assert reject(fresh, event_buy(contracts=contracts, strategy="arb")).code is (
        RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    )


def test_sleeve_allocation_ceiling_is_target_plus_drift():
    """Enough small equity positions must eventually hit the 90+3% ceiling.

    Pinned to the design weights: at the live 100/0 allocation the equity ceiling
    is 103% of NAV, which cash-securing makes unreachable — the mechanism still
    has to be proven for the day 90/10 comes back."""
    limits = design_limits()
    gate = make_gate(limits)
    ceiling = (
        limits.portfolio.sleeves.equity + limits.portfolio.drift_tolerance
    ) * gate.nav
    codes = []
    # Fill across many days so the daily cap is not the binding constraint.
    clock = FakeClock()
    gate = RiskGate(
        limits,
        AccountState(cash=START_CASH, high_water_mark=START_CASH),
        clock=clock,
    )
    for day in range(40):
        clock.advance_days(1)
        for n in range(3):
            order = equity_buy(symbol=f"S{day:02d}{n}", qty=40, price="100.00")
            decision = gate.submit(order)
            if decision.is_approved:
                gate.record_fill(decision, Decimal("100.00"))
            else:
                codes.append(decision.code)
    assert RejectionCode.SLEEVE_ALLOCATION_EXCEEDED in codes
    assert gate.state.sleeve_exposure(Sleeve.EQUITY) <= ceiling


# ================================================================================
# PDT
# ================================================================================


def test_cash_account_is_not_day_trade_counted(limits):
    gate = make_gate(limits, account_type=AccountType.CASH, cash=Decimal("20000"))
    for i in range(5):
        approved = approve(gate, equity_buy(symbol=f"T{i}", qty=1, price="100.00"))
        gate.record_fill(approved, Decimal("100.00"))
        closed = approve(gate, equity_sell(symbol=f"T{i}", qty=1, price="100.00"))
        gate.record_fill(closed, Decimal("100.00"))
    assert gate.kill_switch_tripped is False


def test_sub_threshold_margin_account_is_limited_to_three_day_trades(limits):
    gate = make_gate(limits, account_type=AccountType.MARGIN, cash=Decimal("20000"))
    assert gate.nav < limits.pdt.equity_threshold_usd
    codes = []
    for i in range(5):
        approved = approve(gate, equity_buy(symbol=f"T{i}", qty=1, price="100.00"))
        gate.record_fill(approved, Decimal("100.00"))
        decision = gate.submit(equity_sell(symbol=f"T{i}", qty=1, price="100.00"))
        if decision.is_approved:
            gate.record_fill(decision, Decimal("100.00"))
        else:
            codes.append(decision.code)
    assert codes.count(RejectionCode.PDT_LIMIT_REACHED) == 2
    assert len(gate.state.day_trades) == limits.pdt.max_day_trades_per_window


def test_overnight_hold_is_not_a_day_trade(limits):
    clock = FakeClock()
    gate = make_gate(
        limits, account_type=AccountType.MARGIN, cash=Decimal("20000"), clock=clock
    )
    for i in range(5):
        approved = approve(gate, equity_buy(symbol=f"T{i}", qty=1, price="100.00"))
        gate.record_fill(approved, Decimal("100.00"))
    clock.advance_days(1)
    for i in range(5):
        closed = approve(gate, equity_sell(symbol=f"T{i}", qty=1, price="100.00"))
        gate.record_fill(closed, Decimal("100.00"))
    assert gate.state.day_trades == []


def test_day_trade_window_rolls_off_after_five_business_days(limits):
    today = date(2026, 8, 17)  # a Monday
    assert business_days_before(today, 4).weekday() < 5
    state = AccountState(cash=Decimal("20000"), high_water_mark=Decimal("20000"))
    state.day_trades = [date(2026, 8, 3), today]
    assert state.day_trades_in_window(today, 5) == 1


# ================================================================================
# Kill switch
# ================================================================================


CRASH_SYMBOLS = ["AAA", "BBB", "CCC"]


def deploy_for_crash(gate: RiskGate) -> list:
    """Fill enough equity exposure that marking it to near-zero breaches 12%.

    A single position cannot do it: the 5% single-position cap means one holding going
    to zero is a 4.5% drawdown at worst. Reaching the kill switch takes several
    positions, which is the cap working as intended.
    """
    keys = []
    per_order = int(
        gate.sleeve_nav(Sleeve.EQUITY) * gate.limits.equity_sleeve.max_single_position
        / Decimal("100")
    )
    for symbol in CRASH_SYMBOLS:
        order = equity_buy(symbol=symbol, qty=per_order, price="100.00")
        approved = approve(gate, order)
        gate.record_fill(approved, Decimal("100.00"))
        keys.append(position_key(order))
    return keys


def crash(gate: RiskGate, keys, price: str = "0.01") -> None:
    gate.mark_to_market({key: Decimal(price) for key in keys})


def test_kill_switch_trips_at_the_configured_drawdown(limits):
    gate = make_gate(limits)
    keys = deploy_for_crash(gate)
    assert gate.kill_switch_tripped is False

    crash(gate, keys)
    assert gate.state.drawdown() >= limits.kill_switch.drawdown_from_high_water_mark
    assert gate.kill_switch_tripped is True


def test_a_single_capped_position_cannot_trip_the_kill_switch(limits):
    """The single-position cap bounds one holding's blast radius below the halt."""
    gate = make_gate(limits)
    order = equity_buy(qty=45, price="100.00")
    approved = approve(gate, order)
    gate.record_fill(approved, Decimal("100.00"))
    gate.mark_to_market({position_key(order): Decimal("0.01")})
    assert gate.kill_switch_tripped is False


def test_tripped_kill_switch_rejects_every_opening_order(limits):
    gate = make_gate(limits)
    keys = deploy_for_crash(gate)
    crash(gate, keys)
    assert gate.kill_switch_tripped is True

    for order in (equity_buy(), option_buy(), event_buy()):
        rejection = reject(gate, order)
        assert rejection.code is RejectionCode.KILL_SWITCH_ACTIVE


def test_tripped_kill_switch_still_allows_risk_reducing_closes(limits):
    gate = make_gate(limits)
    keys = deploy_for_crash(gate)
    crash(gate, keys)
    assert gate.kill_switch_tripped is True

    held = gate.state.position(keys[0]).quantity
    approved = approve(gate, equity_sell(symbol="AAA", qty=held))
    assert approved.max_loss == 0


def test_a_halt_cannot_be_used_to_open_a_short(limits):
    """Closes stay fully validated while halted — the exemption is not a bypass."""
    gate = make_gate(limits)
    keys = deploy_for_crash(gate)
    crash(gate, keys)

    held = gate.state.position(keys[0]).quantity
    rejection = reject(gate, equity_sell(symbol="AAA", qty=held + 1))
    assert rejection.code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY

    approve(gate, equity_sell(symbol="AAA", qty=held))
    second = reject(gate, equity_sell(symbol="AAA", qty=1))
    assert second.code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY


def test_kill_switch_does_not_untrip_when_the_market_recovers(limits):
    gate = make_gate(limits)
    keys = deploy_for_crash(gate)
    crash(gate, keys)
    assert gate.kill_switch_tripped is True
    crash(gate, keys, price="500.00")
    assert gate.kill_switch_tripped is True, "recovery must not resume trading"


def test_only_a_manual_reset_resumes_trading(limits):
    clock = FakeClock()
    gate = make_gate(limits, clock=clock)
    keys = deploy_for_crash(gate)
    crash(gate, keys)

    with pytest.raises(ValueError):
        gate.reset_kill_switch("   ")
    assert gate.kill_switch_tripped is True

    gate.reset_kill_switch("owen: reviewed drawdown, resuming")
    assert gate.kill_switch_tripped is False
    # Next session: today's deployment budget was spent before the crash, and the
    # crash shrank the budget itself, so a same-day resume is still capped out.
    clock.advance_days(1)
    approve(gate, equity_buy(symbol="MSFT", qty=1, price="10.00"))


def test_reset_rebaselines_the_high_water_mark(limits):
    """Otherwise the next mark re-trips instantly and a reset could never resume."""
    gate = make_gate(limits)
    keys = deploy_for_crash(gate)
    crash(gate, keys)
    gate.reset_kill_switch("owen")
    crash(gate, keys)
    assert gate.kill_switch_tripped is False


# ================================================================================
# Config integrity
# ================================================================================


def test_confidence_bands_resolve_boundaries_toward_less_risk(limits):
    """Constraint #6: exactly-70 takes 1%, exactly-85 takes 2.5%."""
    sizing = limits.sizing
    assert sizing.size_for(54) == Decimal("0")
    assert sizing.size_for(55) == Decimal("0.010")
    assert sizing.size_for(70) == Decimal("0.010")
    assert sizing.size_for(71) == Decimal("0.025")
    assert sizing.size_for(85) == Decimal("0.025")
    assert sizing.size_for(86) == Decimal("0.050")
    assert sizing.size_for(100) == Decimal("0.050")


def test_no_confidence_score_exceeds_the_hard_cap(limits):
    for confidence in range(0, 101):
        assert limits.sizing.size_for(confidence) <= limits.sizing.hard_cap


def test_gate_refuses_to_start_on_a_config_that_enables_margin(limits):
    raw = limits.model_dump()
    raw["account"]["margin_enabled"] = True
    with pytest.raises(ValueError):
        RiskLimits.model_validate(raw)


def test_gate_refuses_to_start_on_a_config_that_permits_writing(limits):
    raw = limits.model_dump()
    raw["account"]["option_writing"] = "allowed"
    with pytest.raises(ValueError):
        RiskLimits.model_validate(raw)


# ================================================================================
# Stateful properties — no *sequence* of orders can break these
# ================================================================================

SYMBOLS = ["AAA", "BBB", "CCC"]
OPTION_SYMBOLS = ["AAPL260117C00250000", "AAPL260117P00200000"]

#: Two machine symbols share a sector so generated sequences can actually
#: approach the sector cap; the third stays unmapped (its own singleton).
MACHINE_SECTORS = SectorMap({"AAA": "alpha", "BBB": "alpha"})


class GateStateMachine(RuleBasedStateMachine):
    """Drive arbitrary interleavings of submit / fill / cancel against one gate.

    Every invariant below is re-checked after every single step, so a violation is
    reported with the exact shrunk sequence that caused it.
    """

    def __init__(self) -> None:
        super().__init__()
        # Design weights, not the live 100/0: a zero prediction sleeve would turn
        # every buy_event rule into the same degenerate rejection.
        self.limits = design_limits()
        self.clock = FakeClock()
        self.gate = RiskGate(
            self.limits,
            AccountState(cash=START_CASH, high_water_mark=START_CASH),
            clock=self.clock,
            sectors=MACHINE_SECTORS,
        )
        self.pending: list[ApprovedOrder] = []
        #: Caps are fractions of NAV, so NAV moving can breach one with no order
        #: involved — which is drift, handled by the weekly rebalance, not an approval
        #: failure. Set by marks and by realising P&L on a close, after which the cap
        #: invariant no longer isolates approval behaviour. The buying-power and
        #: no-short invariants stay unconditional.
        self.nav_moved = False

    # -- rules -----------------------------------------------------------------

    def _submit(self, order) -> None:
        """Submit and record the outcome.

        The rejection code is reported via ``event()`` so ``--hypothesis-show-statistics``
        shows which branches the generated sequences actually reached. An invariant
        that never sees a near-miss is not evidence of anything, and this is how you
        check that without disabling the guard it is testing.
        """
        decision = self.gate.submit(order)
        if decision.is_approved:
            event("approved")
            self.pending.append(decision)
        else:
            event(f"rejected: {decision.code}")

    @rule(
        symbol=st.sampled_from(SYMBOLS),
        # Fractional shares (2026-08-20): the machine mixes whole and fractional
        # quantities so every invariant below holds across both.
        qty=st.one_of(
            st.integers(min_value=1, max_value=200),
            st.decimals(
                min_value=Decimal("0.001"), max_value=Decimal("200"), places=3
            ),
        ),
        price=st.sampled_from(["1.00", "10.00", "100.00"]),
    )
    def buy_equity(self, symbol, qty, price):
        self._submit(equity_buy(symbol=symbol, qty=qty, price=price))

    @rule(
        symbol=st.sampled_from(OPTION_SYMBOLS),
        contracts=st.integers(min_value=1, max_value=20),
        price=st.sampled_from(["0.50", "2.00"]),
    )
    def buy_option(self, symbol, contracts, price):
        self._submit(option_buy(contracts=contracts, price=price, symbol=symbol))

    @rule(
        contracts=st.integers(min_value=1, max_value=50),
        price=st.sampled_from(["0.10", "0.50", "0.90"]),
        outcome=st.sampled_from(["yes", "no"]),
        strategy=st.sampled_from(["arb", "directional"]),
    )
    def buy_event(self, contracts, price, outcome, strategy):
        self._submit(
            event_buy(
                contracts=contracts, price=price, outcome=outcome, strategy=strategy
            )
        )

    @rule(
        symbol=st.sampled_from(SYMBOLS),
        qty=st.one_of(
            st.integers(min_value=1, max_value=300),
            st.decimals(
                min_value=Decimal("0.001"), max_value=Decimal("300"), places=3
            ),
        ),
    )
    def sell_equity(self, symbol, qty):
        self._submit(equity_sell(symbol=symbol, qty=qty))

    @rule(
        symbol=st.sampled_from(OPTION_SYMBOLS),
        contracts=st.integers(min_value=1, max_value=40),
    )
    def sell_option(self, symbol, contracts):
        self._submit(option_sell(contracts=contracts, symbol=symbol))

    @rule(index=st.integers(min_value=0, max_value=50), discount=st.sampled_from(["1.0", "0.5"]))
    def fill(self, index, discount):
        """Fill at or below the reserved bound — what a limit order guarantees."""
        if not self.pending:
            return
        approved = self.pending.pop(index % len(self.pending))
        price = approved.order.execution.price_bound * Decimal(discount)
        if not approved.order.is_opening:
            self.nav_moved = True  # proceeds may differ from cost basis
        self.gate.record_fill(approved, price)

    @rule(index=st.integers(min_value=0, max_value=50))
    def cancel(self, index):
        if not self.pending:
            return
        self.gate.cancel(self.pending.pop(index % len(self.pending)))

    @rule(days=st.integers(min_value=1, max_value=3))
    def next_day(self, days):
        self.clock.advance_days(days)

    @rule(price=st.sampled_from(["0.01", "50.00", "300.00"]))
    def mark(self, price):
        keys = list(self.gate.state.positions)
        if not keys:
            return
        self.nav_moved = True
        self.gate.mark_to_market({keys[0]: Decimal(price)})

    @rule()
    def human_resets_kill_switch(self):
        if self.gate.kill_switch_tripped:
            self.gate.reset_kill_switch("test operator")

    # -- invariants -------------------------------------------------------------

    @invariant()
    def buying_power_never_goes_negative(self):
        assert self.gate.buying_power >= 0, (
            f"buying power {self.gate.buying_power} — Constraint #1 violated"
        )

    @invariant()
    def no_position_is_ever_short(self):
        for key, position in self.gate.state.positions.items():
            assert position.quantity >= 0, f"{key} went short: {position.quantity}"
            assert position.reserved_close <= position.quantity, (
                f"{key} has {position.reserved_close} units promised against "
                f"{position.quantity} held — a synthetic short"
            )
            assert position.pending_open_units >= 0

    @invariant()
    def no_sector_exceeds_its_cap(self):
        """No sequence of approved orders may put any sector past its cap."""
        if self.nav_moved:
            return  # a market move is drift, not an approval failure
        cap = (
            self.gate.sleeve_nav(Sleeve.EQUITY)
            * self.limits.equity_sleeve.max_sector_exposure
        )
        exposures: dict[str, Decimal] = {}
        for position in self.gate.state.positions.values():
            if position.sleeve is Sleeve.EQUITY and not position.is_option:
                sector = MACHINE_SECTORS.sector_of(position.key[1])
                exposures[sector] = exposures.get(sector, ZERO) + position.exposure
        for sector, exposure in exposures.items():
            assert exposure <= cap, (
                f"sector {sector} at {exposure} exceeds cap {cap} — "
                f"an approval sequence breached the concentration guard"
            )

    @invariant()
    def caps_are_never_breached_by_approvals(self):
        if self.nav_moved:
            return  # a market move is not a cap breach
        gate, limits = self.gate, self.limits
        equity_nav = gate.sleeve_nav(Sleeve.EQUITY)

        assert gate.state.deployed_today <= (
            equity_nav * limits.equity_sleeve.max_daily_deployment
        )
        assert gate.state.options_premium_at_risk <= (
            equity_nav * limits.equity_sleeve.max_options_premium_at_risk
        )
        for key, position in gate.state.positions.items():
            cap_fraction = (
                limits.equity_sleeve.max_single_position
                if position.sleeve is Sleeve.EQUITY
                else limits.prediction_sleeve.directional.max_position
            )
            cap = gate.sleeve_nav(position.sleeve) * cap_fraction
            assert position.exposure <= cap, f"{key} exposure {position.exposure} > {cap}"

        nav = gate.nav
        if nav > 0:
            for sleeve, target in (
                (Sleeve.EQUITY, limits.portfolio.sleeves.equity),
                (Sleeve.PREDICTION, limits.portfolio.sleeves.prediction),
            ):
                ceiling = target + limits.portfolio.drift_tolerance
                assert gate.state.sleeve_exposure(sleeve) / nav <= ceiling

    @invariant()
    def a_tripped_kill_switch_blocks_opens_but_not_closes(self):
        """Every probe here must be one that cannot mutate state.

        Opening orders are rejected outright while halted. The close probe is
        deliberately oversized so it always rejects too — an approved close would
        reserve units and corrupt the sequence being tested.
        """
        if not self.gate.kill_switch_tripped:
            return
        for order in (equity_buy(), option_buy(), event_buy()):
            decision = self.gate.submit(order)
            assert not decision.is_approved
            assert decision.code is RejectionCode.KILL_SWITCH_ACTIVE

        oversized_close = self.gate.submit(equity_sell(symbol="AAA", qty=10**6))
        assert not oversized_close.is_approved
        assert oversized_close.code is not RejectionCode.KILL_SWITCH_ACTIVE, (
            "a halt must not be the reason a close is refused"
        )


# ================================================================================
# Partial fills — a terminal order that filled some of its quantity
# ================================================================================


def test_a_partial_fill_books_only_the_units_that_filled(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=80, price="50.00"))

    gate.record_fill(approved, Decimal("50.00"), filled_units=30)

    position = gate.state.position(("equity", "AAPL"))
    assert position.quantity == 30
    assert position.cost_basis == Decimal("1500.00")


def test_a_partial_fill_releases_the_whole_reservation(limits):
    """The order is terminal. Nothing else is coming, so nothing else needs securing."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=80, price="50.00"))
    assert gate.state.reserved_cash == Decimal("4000.00")

    gate.record_fill(approved, Decimal("50.00"), filled_units=30)

    assert gate.state.reserved_cash == ZERO
    assert gate.state.position(("equity", "AAPL")).pending_open_units == 0
    # Only the filled portion left the account.
    assert gate.state.cash == START_CASH - Decimal("1500.00")
    assert gate.buying_power == START_CASH - Decimal("1500.00")


def test_a_partial_fill_can_only_move_buying_power_the_safe_way(limits):
    """Released reservation is the worst case; cash taken is the part that printed."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=80, price="50.00"))
    before = gate.buying_power

    gate.record_fill(approved, Decimal("50.00"), filled_units=30)

    assert gate.buying_power >= before
    assert gate.buying_power >= ZERO


def test_a_zero_fill_leaves_no_position_and_no_reservation(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=10, price="50.00"))

    gate.record_fill(approved, Decimal("50.00"), filled_units=0)

    assert gate.state.reserved_cash == ZERO
    assert gate.state.position(("equity", "AAPL")) is None
    assert gate.state.cash == START_CASH


def test_a_partial_close_releases_the_units_it_did_not_sell(limits):
    """The unsold shares stay held and stay closeable — not stranded under a reservation."""
    gate = make_gate(limits)
    gate.record_fill(approve(gate, equity_buy(qty=80, price="50.00")), Decimal("50.00"))
    approved = approve(gate, equity_sell(qty=80, price="60.00"))

    gate.record_fill(approved, Decimal("60.00"), filled_units=40)

    position = gate.state.position(("equity", "AAPL"))
    assert position.quantity == 40
    assert position.reserved_close == 0
    assert position.available_to_close == 40


def test_a_partial_fill_does_not_refund_the_daily_deployment_budget(limits):
    """Deliberate: the cap counts capital committed, not capital that happened to print."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=80, price="50.00"))
    assert gate.state.deployed_today == Decimal("4000.00")

    gate.record_fill(approved, Decimal("50.00"), filled_units=1)

    assert gate.state.deployed_today == Decimal("4000.00")


@pytest.mark.parametrize("filled", [-1, 11])
def test_a_fill_outside_the_ordered_quantity_is_refused(limits, filled):
    """Over-filling is not something to absorb quietly: it means a broker or a bug lied."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=10, price="50.00"))

    with pytest.raises(ValueError, match="between 0 and the 10 units"):
        gate.record_fill(approved, Decimal("50.00"), filled_units=filled)


def test_omitting_filled_units_still_settles_the_whole_order(limits):
    """The ordinary case is unchanged."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=10, price="50.00"))

    gate.record_fill(approved, Decimal("49.00"))

    position = gate.state.position(("equity", "AAPL"))
    assert position.quantity == 10
    assert gate.state.reserved_cash == ZERO
    # Filled better than the bound, so the difference came back.
    assert gate.buying_power == START_CASH - Decimal("490.00")


TestGateSequences = GateStateMachine.TestCase
TestGateSequences.settings = settings(
    max_examples=200,
    stateful_step_count=40,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)


@given(
    quantities=st.lists(
        st.one_of(
            st.integers(min_value=1, max_value=500),
            st.decimals(
                min_value=Decimal("0.001"), max_value=Decimal("500"), places=3
            ),
        ),
        min_size=1,
        max_size=25,
    ),
    prices=st.lists(
        st.sampled_from([Decimal("1.00"), Decimal("25.00"), Decimal("400.00")]),
        min_size=1,
        max_size=25,
    ),
)
@settings(max_examples=200, deadline=None)
def test_no_order_sequence_can_overdraw_the_account(quantities, prices):
    """Straight-line version of the buying-power invariant, without fills."""
    limits = RiskLimits.load()
    gate = RiskGate(
        limits, AccountState(cash=START_CASH, high_water_mark=START_CASH), FakeClock()
    )
    for i, qty in enumerate(quantities):
        price = prices[i % len(prices)]
        gate.submit(
            EquityBuyOrder(
                symbol=SYMBOLS[i % len(SYMBOLS)],
                quantity=qty,
                execution=LimitExecution(limit_price=price),
            )
        )
        assert gate.buying_power >= 0
    assert gate.state.reserved_cash <= START_CASH


# ================================================================================
# Sector concentration (2026-08-19): a sector is capped like a position is
# ================================================================================

ALPHA_SECTOR = SectorMap(
    {"AL1": "alpha", "AL2": "alpha", "AL3": "alpha", "AL4": "alpha"}
)


def test_three_same_sector_positions_within_the_cap_are_approved(limits):
    gate = make_gate(limits, sectors=ALPHA_SECTOR)
    for symbol in ("AL1", "AL2", "AL3"):
        decision = gate.submit(equity_buy(symbol=symbol, qty=44, price="100.00"))
        assert decision.is_approved, decision


def test_the_fourth_position_breaching_the_sector_cap_is_rejected(limits):
    gate = make_gate(limits, sectors=ALPHA_SECTOR)
    for symbol in ("AL1", "AL2", "AL3"):
        assert gate.submit(equity_buy(symbol=symbol, qty=44, price="100.00")).is_approved

    fourth = gate.submit(equity_buy(symbol="AL4", qty=44, price="100.00"))
    assert isinstance(fourth, Rejection)
    assert fourth.code is RejectionCode.SECTOR_CONCENTRATION
    assert "alpha" in fourth.message
    assert "sectors.yaml" in fourth.message
    # The cap in the rejection is computed from the loaded limits, not hard-coded.
    expected_cap = (
        gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_sector_exposure
    )
    assert fourth.limit == expected_cap


def test_exactly_at_the_sector_cap_is_approved(limits):
    """The cap is a ceiling, not a boundary short of it: > rejects, == passes."""
    gate = make_gate(limits, sectors=ALPHA_SECTOR)
    sector_cap = (
        gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_sector_exposure
    )
    single_cap = (
        gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_single_position
    )
    per_position = min(single_cap, sector_cap / 3)
    qty = int(per_position / Decimal("100.00"))
    for symbol in ("AL1", "AL2", "AL3"):
        decision = gate.submit(equity_buy(symbol=symbol, qty=qty, price="100.00"))
        assert decision.is_approved, decision
    # One more dollar of alpha exposure is over the line.
    over = gate.submit(equity_buy(symbol="AL4", qty=1, price="100.00"))
    assert isinstance(over, Rejection)
    assert over.code is RejectionCode.SECTOR_CONCENTRATION


def test_unmapped_tickers_never_share_a_bucket(limits):
    """Three unmapped names each sized near the per-name cap are all approved:
    if unknown tickers silently shared a sector, the third would breach it."""
    gate = make_gate(limits, sectors=SectorMap({}))
    for symbol in ("ZZ1", "ZZ2", "ZZ3"):
        decision = gate.submit(equity_buy(symbol=symbol, qty=44, price="100.00"))
        assert decision.is_approved, decision
    assert SectorMap({}).sector_of("ZZ1") == "unmapped:ZZ1"
    assert SectorMap({}).sector_of("zz1") == "unmapped:ZZ1"  # case-insensitive


def test_options_do_not_count_toward_sector_exposure(limits):
    """Options carry their own aggregate-premium cap; the sector guard reads
    equity positions only (mapping option symbols to underlyings would smuggle
    parsing into the gate)."""
    gate = make_gate(limits, sectors=SectorMap({"AAPL": "alpha"}))
    # Fill the alpha sector to its cap with equity...
    sector_cap = (
        gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_sector_exposure
    )
    single_cap = (
        gate.sleeve_nav(Sleeve.EQUITY) * limits.equity_sleeve.max_single_position
    )
    assert gate.submit(
        equity_buy(symbol="AAPL", qty=int(single_cap / Decimal("100")), price="100.00")
    ).is_approved
    # ...an option on the same underlying still clears the sector guard.
    decision = gate.submit(option_buy(contracts=1, price="1.00"))
    assert decision.is_approved, decision


def test_the_production_sector_map_loads_and_is_singular():
    """config/sectors.yaml parses, and a known watchlist name maps to a sector
    while an unknown one stays a singleton."""
    sectors = SectorMap.load()
    assert sectors.sector_of("NVDA") == "semiconductors"
    assert sectors.sector_of("NUE") == "steel_materials"
    assert sectors.sector_of("NOT-A-TICKER") == "unmapped:NOT-A-TICKER"


# ================================================================================
# Fractional shares (2026-08-20): exact Decimal money path, floored not dusted
# ================================================================================

from pydantic import ValidationError  # noqa: E402


def test_a_fractional_buy_reserves_the_exact_decimal_worst_case(limits):
    """No floats anywhere: 0.375 x 79.99 reserves 29.99625, to the digit."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=Decimal("0.375"), price="79.99"))
    assert approved.max_loss == Decimal("29.99625")
    assert gate.buying_power == START_CASH - Decimal("29.99625")


def test_an_opening_equity_order_below_the_notional_floor_is_rejected(limits):
    gate = make_gate(limits)
    rejection = reject(gate, equity_buy(qty=Decimal("0.04"), price="100.00"))
    assert rejection.code is RejectionCode.BELOW_MIN_NOTIONAL
    assert rejection.observed == Decimal("4.00")
    # Nothing was reserved by the refusal.
    assert gate.buying_power == START_CASH


def test_an_order_exactly_at_the_floor_passes(limits):
    """"Below the floor" is the explicit rule. Constraint #6 resolves genuine
    ambiguity; it does not tighten a boundary that already says what it means."""
    gate = make_gate(limits)
    approve(gate, equity_buy(qty=Decimal("0.05"), price="100.00"))


def test_the_floor_never_blocks_a_risk_reducing_close(limits):
    """A dust-sized close is still a close. Trapping it would hold risk open."""
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=Decimal("0.06"), price="100.00"))
    gate.record_fill(approved, Decimal("100.00"))
    approve(gate, equity_sell(qty=Decimal("0.01"), price="100.00"))  # $1 notional


def test_the_floor_does_not_apply_to_the_prediction_sleeve():
    limits = design_limits()
    """Arb is micro-unit by design; a $0.50 event position is not dust."""
    gate = make_gate(limits)
    approve(gate, event_buy(contracts=1, price="0.50", strategy="arb"))


def test_a_fractional_close_cannot_oversell(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=Decimal("1.2"), price="100.00"))
    gate.record_fill(approved, Decimal("100.00"))
    rejection = reject(gate, equity_sell(qty=Decimal("1.5"), price="100.00"))
    assert rejection.code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY


def test_two_fractional_closes_cannot_sell_the_same_fraction(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=Decimal("1.2"), price="100.00"))
    gate.record_fill(approved, Decimal("100.00"))
    approve(gate, equity_sell(qty=Decimal("0.7"), price="100.00"))
    rejection = reject(gate, equity_sell(qty=Decimal("0.7"), price="100.00"))
    assert rejection.code is RejectionCode.CLOSE_EXCEEDS_HELD_QUANTITY


def test_a_fractional_partial_fill_books_exact_units(limits):
    gate = make_gate(limits)
    approved = approve(gate, equity_buy(qty=Decimal("2.5"), price="100.00"))
    gate.record_fill(approved, Decimal("100.00"), filled_units=Decimal("1.25"))
    position = gate.state.position(("equity", "AAPL"))
    assert position.quantity == Decimal("1.25")
    assert gate.state.cash == START_CASH - Decimal("125.00")


def test_share_quantity_refuses_finer_than_broker_precision():
    """Nine decimal places is Alpaca's documented maximum; the schema stops a
    tenth at construction rather than letting a broker reject it later."""
    with pytest.raises(ValidationError):
        equity_buy(qty=Decimal("0.0000000001"), price="100.00")


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("-0.5")])
def test_share_quantity_refuses_zero_and_negative(bad):
    with pytest.raises(ValidationError):
        equity_buy(qty=bad, price="100.00")


def test_contracts_remain_whole_numbers():
    """Fractional applies to equity shares only; a fractional contract count is
    unrepresentable, same as before."""
    with pytest.raises(ValidationError):
        option_buy(contracts=1.5)
    with pytest.raises(ValidationError):
        event_buy(contracts=0.5)


# ================================================================================
# 100/0 allocation (2026-08-21): a zero-weight sleeve refuses cleanly
# ================================================================================


def test_a_zero_weight_sleeve_rejects_orders_with_a_typed_rejection(limits):
    """Live config: prediction weight 0. Every event order dies at the position
    cap (0.x% of a $0 sleeve) — a typed rejection, never a crash or a div-zero."""
    gate = make_gate(limits)
    assert gate.sleeve_nav(Sleeve.PREDICTION) == ZERO
    for strategy in ("arb", "directional"):
        rejection = reject(gate, event_buy(price="0.50", strategy=strategy))
        assert rejection.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    # And nothing was reserved by the refusals.
    assert gate.buying_power == START_CASH


def test_the_equity_sleeve_now_spans_the_whole_nav(limits):
    """100/0: the 5% single-position cap is 5% of full NAV, not of 90%."""
    gate = make_gate(limits)
    assert gate.sleeve_nav(Sleeve.EQUITY) == START_CASH
    approve(gate, equity_buy(qty=50, price="100.00"))  # 5,000 == the new cap
    rejection = reject(gate, equity_buy(symbol="MSFT", qty=51, price="100.00"))
    assert rejection.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED


def test_zero_weight_drift_math_never_divides_by_the_weight(limits):
    """The allocation ceiling divides by NAV, never by a sleeve weight — filling
    the account with equity under 100/0 must not raise."""
    gate = make_gate(limits)
    for n in range(3):
        decision = gate.submit(equity_buy(symbol=f"Z{n}", qty=49, price="100.00"))
        if decision.is_approved:
            gate.record_fill(decision, Decimal("100.00"))
    assert gate.buying_power >= ZERO
