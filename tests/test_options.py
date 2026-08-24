"""Options execution (build ruling 2026-08-24).

Four layers under test:
  1. The deterministic selector — same report + same chain = same contract,
     every guardrail boundary, every fallback with its reason and near-miss.
  2. Pipeline expression routing — catalyst gate, full-table equity fallback
     sizing (ruling #2), puts-without-a-contract trades nothing.
  3. Exits — options ride the existing machinery plus the T-minus-expiry close.
  4. The audit trail — ExpressionSnapshot answers "why this contract / why not"
     from records alone.
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from audit.records import ExitReason, RejectedStage
from execution.options_data import OptionQuote
from risk_gate.limits import RiskLimits
from sizing.selection import (
    FallbackReason,
    OptionFallback,
    OptionSelector,
    SelectedOption,
)
from research.reports import REPORT_TOOL_NAME
from research.config import ResearchConfig
from signals.config import SignalsConfig
from test_exits import RoutingLLM
from test_orchestrator import (
    NOW,
    REPORT,
    FakeBroker,
    FakeClock,
    build,
    counter,
    orchestrator_config,
    prices_of,
    structured,
)

TODAY = NOW.date()


@pytest.fixture(scope="module")
def limits() -> RiskLimits:
    return RiskLimits.load()


@pytest.fixture(scope="module")
def selection(limits):
    return limits.options_selection


@pytest.fixture(scope="module")
def selector(selection) -> OptionSelector:
    return OptionSelector(selection)


@pytest.fixture(scope="module")
def signals_config() -> SignalsConfig:
    return SignalsConfig.load()


@pytest.fixture(scope="module")
def research_config() -> ResearchConfig:
    return ResearchConfig.load()


def quote(
    strike: str = "140",
    right: str = "call",
    days_out: int = 70,
    bid: str = "2.45",
    ask: str = "2.55",
    delta: str = "0.68",
    iv: str = "0.35",
    oi: int = 1200,
    expiration: date | None = None,
) -> OptionQuote:
    expiration = expiration or (TODAY + timedelta(days=days_out))
    return OptionQuote(
        occ_symbol=(
            f"NUE{expiration.strftime('%y%m%d')}"
            f"{'C' if right == 'call' else 'P'}{int(Decimal(strike) * 1000):08d}"
        ),
        underlying="NUE",
        right=right,
        expiration=expiration,
        strike=Decimal(strike),
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        delta=Decimal(delta) if delta is not None else None,
        implied_volatility=Decimal(iv) if iv is not None else None,
        open_interest=oi,
    )


def good_chain() -> list[OptionQuote]:
    """A liquid chain: three strikes x two rights at a 70-day expiry (the REPORT
    horizon is weeks -> 60-day floor), plus a too-near expiry that must lose."""
    return [
        quote(strike="135", delta="0.74", iv="0.34"),
        quote(strike="140", delta="0.68", iv="0.35"),
        quote(strike="145", delta="0.61", iv="0.36"),
        quote(strike="135", right="put", delta="-0.26", iv="0.37"),
        quote(strike="140", right="put", delta="-0.62", iv="0.38"),
        quote(strike="145", right="put", delta="-0.71", iv="0.39"),
        quote(strike="140", days_out=30, delta="0.67", iv="0.33"),
    ]


def select(selector, chain, *, direction="long", horizon="weeks", confidence=71):
    return selector.select(
        direction=direction,
        time_horizon=horizon,
        confidence=confidence,
        chain=chain,
        today=TODAY,
    )


class FakeChain:
    """A chain source with a canned answer; records what was asked."""

    def __init__(self, chain, mid: str | None = "2.50"):
        self._chain = chain
        self._mid = Decimal(mid) if mid is not None else None
        self.requests: list[tuple[str, date]] = []

    def chain_for(self, underlying, *, min_expiry, max_expiry=None):
        self.requests.append((underlying, min_expiry))
        return self._chain

    def option_mid(self, occ_symbol):
        return self._mid


CATALYST = {
    "present": True,
    "description": "Section 232 tariff ruling scheduled inside the horizon.",
}


def catalyst_report(**overrides):
    return {**REPORT, "catalyst_within_horizon": CATALYST, **overrides}


# ================================================================================
# 1. Selector: determinism
# ================================================================================


def test_same_report_and_chain_always_pick_the_same_contract(selector):
    first = select(selector, good_chain())
    assert isinstance(first, SelectedOption)
    for _ in range(5):
        again = select(selector, good_chain())
        assert again.quote.occ_symbol == first.quote.occ_symbol


def test_chain_order_cannot_change_the_pick(selector):
    baseline = select(selector, good_chain()).quote.occ_symbol
    shuffled = good_chain()
    for seed in range(10):
        random.Random(seed).shuffle(shuffled)
        assert select(selector, list(shuffled)).quote.occ_symbol == baseline


def test_confidence_71_picks_the_delta_closest_to_its_band_midpoint(selector):
    """Band (70, 85] -> [0.60, 0.75], midpoint 0.675: the 0.68-delta strike."""
    picked = select(selector, good_chain())
    assert picked.quote.delta == Decimal("0.68")


def test_confidence_90_buys_nearer_the_money(selector):
    """Band (85, 100] -> [0.50, 0.65]: the 0.61-delta strike wins instead."""
    picked = select(selector, good_chain(), confidence=90)
    assert picked.quote.delta == Decimal("0.61")


def test_a_put_thesis_selects_a_put_by_absolute_delta(selector):
    picked = select(selector, good_chain(), direction="short_via_puts")
    assert picked.quote.right == "put"
    assert picked.quote.delta == Decimal("-0.71")  # gap 0.035 to the 0.675 midpoint beats 0.055


def test_shortest_qualifying_expiry_wins(selector):
    """A 90-day expiry qualifies too; the 70-day one is closer and wins."""
    chain = good_chain() + [quote(strike="140", days_out=90, delta="0.66")]
    picked = select(selector, chain)
    assert picked.quote.expiration == TODAY + timedelta(days=70)


# ================================================================================
# 1. Selector: guardrail boundaries
# ================================================================================


def test_expiry_exactly_at_the_floor_qualifies(selector):
    chain = [quote(days_out=60)]
    assert isinstance(select(selector, chain), SelectedOption)
    chain = [quote(days_out=59)]
    fallen = select(selector, chain)
    assert fallen.reason is FallbackReason.NO_EXPIRY_IN_RANGE


def test_open_interest_at_exactly_the_minimum_passes(selector):
    assert isinstance(select(selector, [quote(oi=500)]), SelectedOption)
    fallen = select(selector, [quote(oi=499)])
    assert fallen.reason is FallbackReason.ILLIQUID_CHAIN
    assert fallen.near_miss is not None
    assert fallen.near_miss.open_interest == 499


def test_spread_at_exactly_ten_percent_of_mid_passes(selector):
    at_cap = quote(bid="2.375", ask="2.625")  # spread 0.25 on mid 2.50 = 10%
    assert isinstance(select(selector, [at_cap]), SelectedOption)
    over = quote(bid="2.37", ask="2.63")  # 10.4%
    fallen = select(selector, [over])
    assert fallen.reason is FallbackReason.ILLIQUID_CHAIN
    assert fallen.near_miss.spread_pct > Decimal("0.10")


def test_the_delta_floor_holds_in_every_band(selection):
    """No confidence, however low or high its band, buys below |delta| 0.45."""
    for band in selection.delta_bands:
        assert band.delta_min >= selection.min_delta_floor


def test_a_delta_below_the_band_falls_back_with_the_near_miss(selector):
    fallen = select(selector, [quote(delta="0.55")])  # below (70,85]'s 0.60
    assert fallen.reason is FallbackReason.NO_STRIKE_IN_BAND
    assert fallen.near_miss.delta == Decimal("0.55")
    assert fallen.near_miss.killed_by == "no_strike_in_band"


def test_iv_at_the_ceiling_percentile_passes_above_falls_back(selector):
    """The pick's IV rank is computed against the chain's own population."""
    crowd = [
        quote(strike=str(100 + i), delta="0.30", iv=f"0.{20 + i:02d}")
        for i in range(10)
    ]
    modest = quote(iv="0.25")
    assert isinstance(select(selector, crowd + [modest]), SelectedOption)
    extreme = quote(iv="0.99")
    fallen = select(selector, crowd + [extreme])
    assert fallen.reason is FallbackReason.IV_EXTREME
    assert "percentile" in fallen.detail
    assert fallen.near_miss.occ_symbol == extreme.occ_symbol


def test_missing_iv_on_the_pick_falls_back(selector):
    fallen = select(selector, [quote(iv=None)])
    assert fallen.reason is FallbackReason.IV_UNAVAILABLE


def test_missing_greeks_fall_back_with_the_reason(selector):
    fallen = select(selector, [quote(delta=None)])
    assert fallen.reason is FallbackReason.NO_GREEKS


def test_an_empty_or_absent_chain_falls_back(selector):
    assert select(selector, None).reason is FallbackReason.CHAIN_UNAVAILABLE
    assert select(selector, []).reason is FallbackReason.CHAIN_UNAVAILABLE


def test_a_band_configured_below_the_floor_refuses_to_load():
    raw = RiskLimits.load().model_dump()
    raw["options_selection"]["delta_bands"][2]["delta_min"] = "0.30"
    with pytest.raises(ValueError, match="below the 0.45 floor"):
        RiskLimits.model_validate(raw)


# ================================================================================
# 2. Pipeline routing, end to end
# ================================================================================


def wire(tmp_path, limits, signals_config, research_config, *, report, chain, **kw):
    broker = kw.pop("broker", None) or FakeBroker()
    started = build(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=kw.pop("llm", None)
        or RoutingLLM(**{REPORT_TOOL_NAME: structured(report)}),
        prices=prices_of(NUE="140.00"),
        broker=broker,
        options_chain=chain,
        **kw,
    )
    return started, broker


def test_a_catalyst_backed_thesis_buys_the_selected_call(
    tmp_path, limits, signals_config, research_config
):
    chain = FakeChain(good_chain())
    started, broker = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(), chain=chain,
    )
    result = started.loop.tick().processed[0]
    assert result.traded

    # Sized on the HALVED options table: 2.5% x 100k / 2 = 1,250 premium at risk.
    trail = started.audit.trail(result.decision_id)
    assert trail.decision.sizing.instrument == "option"
    assert trail.decision.sizing.capital == Decimal("1250.00")

    # 1,250 at mid 2.50 x 100 = 5 contracts; the gate reserved exactly that.
    order = trail.decision.gate.order
    assert order["kind"] == "option_buy_to_open"
    assert order["contracts"] == 5
    assert trail.decision.gate.max_loss == Decimal("1250.00")

    # The expression snapshot reproduces the pick.
    expression = trail.decision.expression
    assert expression.chosen == "option"
    assert expression.delta == Decimal("0.68")
    assert expression.contract_symbol == order["symbol"]

    # The chain was fetched at the horizon's floor (weeks -> 60 days).
    assert chain.requests == [("NUE", TODAY + timedelta(days=60))]

    # Settlement carried the contract multiplier into real cash.
    assert started.gate.state.cash == Decimal("100000") - Decimal("1250.00")


def test_an_illiquid_chain_falls_back_to_equity_at_the_full_table(
    tmp_path, limits, signals_config, research_config
):
    """Ruling #2: the fallback sizes as if options never entered the picture."""
    illiquid = [quote(oi=50)]
    started, broker = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(), chain=FakeChain(illiquid),
    )
    result = started.loop.tick().processed[0]
    assert result.traded

    trail = started.audit.trail(result.decision_id)
    assert trail.decision.sizing.instrument == "equity"
    assert trail.decision.sizing.capital == Decimal("2500.00")  # FULL 2.5%, not 1,250
    assert trail.decision.gate.order["kind"] == "equity_buy"

    expression = trail.decision.expression
    assert expression.chosen == "equity"
    assert expression.fallback_reason == "illiquid_chain"
    assert expression.near_miss.open_interest == 50


def test_a_patient_thesis_expresses_as_stock_even_on_a_perfect_chain(
    tmp_path, limits, signals_config, research_config
):
    """The catalyst gate: leverage is earned by timing specificity. No catalyst,
    no chain fetch, no option — however favorable the chain would have been."""
    chain = FakeChain(good_chain())
    patient = {**REPORT, "catalyst_within_horizon": {"present": False, "description": ""}}
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=patient, chain=chain,
    )
    result = started.loop.tick().processed[0]
    assert result.traded

    trail = started.audit.trail(result.decision_id)
    assert trail.decision.gate.order["kind"] == "equity_buy"
    assert trail.decision.sizing.capital == Decimal("2500.00")
    assert trail.decision.expression.fallback_reason == "no_catalyst"
    assert chain.requests == []  # the gate refused before any fetch


def test_a_null_catalyst_reads_exactly_like_false(
    tmp_path, limits, signals_config, research_config
):
    chain = FakeChain(good_chain())
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report={**REPORT, "catalyst_within_horizon": None}, chain=chain,
    )
    result = started.loop.tick().processed[0]
    trail = started.audit.trail(result.decision_id)
    assert trail.decision.gate.order["kind"] == "equity_buy"
    assert chain.requests == []


def test_a_catalyst_backed_put_thesis_buys_the_selected_put(
    tmp_path, limits, signals_config, research_config
):
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(direction="short_via_puts"),
        chain=FakeChain(good_chain()),
    )
    result = started.loop.tick().processed[0]
    assert result.traded
    order = started.audit.trail(result.decision_id).decision.gate.order
    assert order["kind"] == "option_buy_to_open"
    assert order["right"] == "put"


def test_a_puts_thesis_without_a_catalyst_trades_nothing(
    tmp_path, limits, signals_config, research_config
):
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report={
            **REPORT,
            "direction": "short_via_puts",
            "catalyst_within_horizon": {"present": False, "description": ""},
        },
        chain=FakeChain(good_chain()),
    )
    result = started.loop.tick().processed[0]
    assert not result.traded

    rejection = started.audit.rejections_for(result.decision_id)[0]
    assert rejection.stage is RejectedStage.ORDER_CONSTRUCTION
    assert rejection.code == "no_catalyst_for_puts"
    assert rejection.expression.chosen == "none"


def test_a_puts_thesis_with_a_bad_chain_trades_nothing_with_the_reason(
    tmp_path, limits, signals_config, research_config
):
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(direction="short_via_puts"),
        chain=FakeChain(None),
    )
    result = started.loop.tick().processed[0]
    assert not result.traded
    rejection = started.audit.rejections_for(result.decision_id)[0]
    assert rejection.code == "chain_unavailable"
    assert rejection.expression.chosen == "none"


def test_premium_exceeding_sized_capital_falls_back_to_equity(
    tmp_path, limits, signals_config, research_config
):
    """1,250 premium at risk cannot buy one 15.00-mid contract (1,500)."""
    pricey = [quote(bid="14.90", ask="15.10")]
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(), chain=FakeChain(pricey),
    )
    result = started.loop.tick().processed[0]
    assert result.traded
    trail = started.audit.trail(result.decision_id)
    assert trail.decision.gate.order["kind"] == "equity_buy"
    assert trail.decision.expression.fallback_reason == "premium_exceeds_size"
    assert trail.decision.sizing.capital == Decimal("2500.00")


# ================================================================================
# 3. Exits: options ride the machinery, plus the T-minus close
# ================================================================================


def enter_option_position(tmp_path, limits, signals_config, research_config, **kw):
    """A filled 5-contract call, 70 days to expiry, leash stretched past it so
    the expiry rule (not the time stop) is what the clock walks into."""
    clock = FakeClock()
    config = orchestrator_config(
        exits={
            "max_loss_fraction": "0.50",
            "time_stop_days": {"days": 200, "weeks": 200, "months": 200},
            "thesis_review_interval_hours": 24,
        }
    )
    chain = kw.pop("chain", None) or FakeChain(good_chain())
    started, broker = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(), chain=chain, clock=clock, config=config,
    )
    report = started.loop.tick()
    assert report.processed[0].traded
    assert len(started.exits.tracked) == 1
    assert started.exits.tracked[0].is_option
    return started, clock, broker


def test_the_expiry_window_boundary_is_exact(
    tmp_path, limits, signals_config, research_config
):
    """Six days out holds; five days out exits, thesis or no thesis."""
    started, clock, _ = enter_option_position(
        tmp_path, limits, signals_config, research_config
    )
    clock.advance(days=64)  # expiry T-6
    started.loop.tick()
    assert started.exits.tracked and started.exits.tracked[0].pending_exit is None

    clock.advance(days=1)  # T-5: inside the window
    first = started.loop.tick()
    second = started.loop.tick()  # settle the exit fill
    assert first.positions_closed + second.positions_closed == 1

    trail = started.audit.trail("dec-1")
    assert trail.exits[-1].reason is ExitReason.EXPIRY_CLOSE
    # P&L carries the multiplier: entered 5 x 2.50 x 100, exited 5 x 2.50 x 100.
    assert trail.outcome.realised_pnl == Decimal("0.00")


def test_the_expiry_close_runs_even_with_the_kill_switch_tripped(
    tmp_path, limits, signals_config, research_config
):
    """A halt stops exposure growing; it must never trap a decaying option."""
    started, clock, _ = enter_option_position(
        tmp_path, limits, signals_config, research_config
    )
    clock.advance(days=65)
    started.gate.state.kill_switch_tripped = True
    first = started.loop.tick()
    second = started.loop.tick()
    assert first.positions_closed + second.positions_closed == 1
    assert started.audit.trail("dec-1").exits[-1].reason is ExitReason.EXPIRY_CLOSE


def test_an_option_stop_fires_on_premium_at_the_equity_fraction(
    tmp_path, limits, signals_config, research_config
):
    """Ruling #1: same stop fraction as equity, applied to the premium mark."""
    chain = FakeChain(good_chain(), mid="1.20")  # entry 2.50, mark 1.20 < 50% stop
    started, clock, _ = enter_option_position(
        tmp_path, limits, signals_config, research_config, chain=chain
    )
    first = started.loop.tick()
    second = started.loop.tick()
    assert first.positions_closed + second.positions_closed == 1
    trail = started.audit.trail("dec-1")
    assert trail.exits[-1].reason is ExitReason.MAX_LOSS_STOP
    # 5 contracts: in at 2.50 x 100, out at 1.20 x 100 -> -650.00.
    assert trail.outcome.realised_pnl == Decimal("-650.00")


def test_an_option_position_survives_a_restart_with_its_contract_identity(
    tmp_path, limits, signals_config, research_config
):
    """Replay rebuilds the option from the audit trail: kind, expiry, multiplier."""
    started, clock, broker = enter_option_position(
        tmp_path, limits, signals_config, research_config
    )
    started.loop.shutdown()

    chain = FakeChain(good_chain())
    restarted, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(), chain=chain, clock=clock,
        broker=FakeBroker(positions=list(broker_positions(broker))),
        id_factory=counter("r"),
    )
    tracked = restarted.exits.tracked
    assert len(tracked) == 1
    assert tracked[0].is_option
    assert tracked[0].multiplier == 100
    assert tracked[0].expiration == TODAY + timedelta(days=70)
    assert tracked[0].entry_price == Decimal("2.50")


def broker_positions(broker: FakeBroker):
    from execution.base import BrokerPosition

    for receipt in broker.submitted:
        yield BrokerPosition(
            symbol=receipt.symbol,
            quantity=receipt.quantity,
            market_value=receipt.quantity * receipt.limit_price * 100,
            cost_basis=receipt.quantity * receipt.limit_price * 100,
            asset_class="us_option",
        )
