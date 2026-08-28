"""Sizing engine tests.

Band boundaries get a case each, because (lower, upper] versus [lower, upper) is a
one-character change that silently doubles a position at exactly 70 and exactly 85.
The property test at the bottom covers everything between them, plus every out-of-range
score, against the one invariant that must hold whatever the research layer says.
"""

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from execution import BrokerAdapter
from research import ResearchReport
from risk_gate import RiskLimits, Sleeve
from sizing import EventStrategy, InstrumentKind, SizedProposal, SizingEngine

EQUITY_SLEEVE_NAV = Decimal("90000")
PREDICTION_SLEEVE_NAV = Decimal("10000")

BASE_REPORT = {
    "thesis": "Domestic steel names benefit from the announced tariff.",
    "tickers": ["NUE"],
    "direction": "long",
    "time_horizon": "weeks",
    "priced_in_analysis": None,
    "confidence": 62,
    "invalidation_condition": "Exemption granted.",
    "manipulation_assessment": "none detected",
    "catalyst_within_horizon": None,
}


@pytest.fixture(scope="session")
def limits() -> RiskLimits:
    return RiskLimits.load()


@pytest.fixture
def engine(limits) -> SizingEngine:
    return SizingEngine(limits)


def report(confidence: int) -> ResearchReport:
    return ResearchReport.model_validate({**BASE_REPORT, "confidence": confidence})


# ================================================================================
# Band boundaries — (lower, upper], ties take the smaller size
# ================================================================================


@pytest.mark.parametrize(
    "confidence,expected",
    [
        (0, "0"),
        (49, "0"),  # below the floor (risk-on calibration 2026-08-28: 55 -> 50)
        (50, "0.010"),  # floor is inclusive: "< 50 | No trade" is explicit
        (54, "0.010"),  # the band the calibration opened: small shot, not none
        (56, "0.010"),
        (70, "0.010"),  # boundary -> smaller band (Constraint #6)
        (71, "0.025"),
        (85, "0.025"),  # boundary -> smaller band
        (86, "0.070"),  # top band 5% -> 7%
        (100, "0.070"),
    ],
)
def test_every_band_boundary(engine, confidence, expected):
    proposal = engine.propose_equity(report(confidence), EQUITY_SLEEVE_NAV)
    assert proposal.fraction_of_sleeve_nav == Decimal(expected)


def test_below_the_floor_is_no_trade_not_a_token_position(engine):
    """CLAUDE.md: "Do not take token positions on weak signals.\""""
    proposal = engine.propose_equity(report(49), EQUITY_SLEEVE_NAV)
    assert proposal.capital == 0
    assert proposal.is_tradeable is False
    assert "below the 50 floor" in proposal.rationale


def test_capital_is_the_fraction_of_the_nav_it_was_given(engine):
    proposal = engine.propose_equity(report(71), EQUITY_SLEEVE_NAV)
    assert proposal.capital == Decimal("2250.00")  # 2.5% of 90,000
    assert proposal.sleeve is Sleeve.EQUITY
    assert proposal.instrument is InstrumentKind.EQUITY


def test_rounding_never_increases_exposure(engine):
    """An awkward NAV must round the dollars down, not to nearest."""
    proposal = engine.propose_equity(report(55), Decimal("1234.567"))
    assert proposal.capital == Decimal("12.34")  # 1% = 12.34567, rounded down
    assert proposal.capital <= proposal.sleeve_nav * proposal.fraction_of_sleeve_nav


# ================================================================================
# Options — same table, halved
# ================================================================================


@pytest.mark.parametrize("confidence", [50, 55, 70, 71, 85, 86, 100])
def test_option_sizing_is_exactly_half_the_equity_size(engine, confidence):
    equity = engine.propose_equity(report(confidence), EQUITY_SLEEVE_NAV)
    option = engine.propose_option(report(confidence), EQUITY_SLEEVE_NAV)
    assert option.fraction_of_sleeve_nav == equity.fraction_of_sleeve_nav / 2
    assert option.capital == equity.capital / 2


def test_option_at_maximum_confidence_is_capped_at_half_the_top_band(engine):
    """7% top band, halved: options carry embedded leverage and the table must
    not double it (risk-on calibration 2026-08-28)."""
    option = engine.propose_option(report(100), EQUITY_SLEEVE_NAV)
    assert option.fraction_of_sleeve_nav == Decimal("0.035")
    assert option.capital == Decimal("3150.00")
    assert "halved for embedded option leverage" in option.rationale


def test_a_weak_signal_buys_no_options_either(engine):
    assert engine.propose_option(report(49), EQUITY_SLEEVE_NAV).capital == 0


def test_option_capital_is_premium_at_risk(engine):
    """The figure is premium, not notional — the whole point of halving it."""
    option = engine.propose_option(report(86), EQUITY_SLEEVE_NAV)
    assert option.capital == Decimal("3150.00")
    assert option.instrument is InstrumentKind.OPTION
    assert option.sleeve is Sleeve.EQUITY


# ================================================================================
# Event contracts — strategy-tagged caps
# ================================================================================


def test_arb_positions_are_capped_at_half_a_percent(engine, limits):
    proposal = engine.propose_event_contract(
        report(100), PREDICTION_SLEEVE_NAV, EventStrategy.ARB
    )
    assert proposal.fraction_of_sleeve_nav == limits.prediction_sleeve.arbitrage.max_position
    assert proposal.fraction_of_sleeve_nav == Decimal("0.005")
    assert proposal.capital == Decimal("50.00")  # 0.5% of 10,000


def test_directional_positions_are_capped_at_two_percent(engine, limits):
    proposal = engine.propose_event_contract(
        report(100), PREDICTION_SLEEVE_NAV, EventStrategy.DIRECTIONAL
    )
    cap = limits.prediction_sleeve.directional.max_position
    assert proposal.fraction_of_sleeve_nav == cap == Decimal("0.02")
    assert proposal.capital == Decimal("200.00")


def test_the_same_confidence_sizes_differently_by_strategy_tag(engine):
    """The tag is the only difference between these two calls."""
    arb = engine.propose_event_contract(
        report(90), PREDICTION_SLEEVE_NAV, EventStrategy.ARB
    )
    directional = engine.propose_event_contract(
        report(90), PREDICTION_SLEEVE_NAV, EventStrategy.DIRECTIONAL
    )
    assert arb.capital < directional.capital
    assert arb.strategy is EventStrategy.ARB
    assert directional.strategy is EventStrategy.DIRECTIONAL


def test_confidence_still_gates_event_contracts(engine):
    """The strategy cap is a ceiling, not a floor — a weak signal still trades nothing."""
    proposal = engine.propose_event_contract(
        report(49), PREDICTION_SLEEVE_NAV, EventStrategy.ARB
    )
    assert proposal.capital == 0


def test_event_proposals_land_in_the_prediction_sleeve(engine):
    proposal = engine.propose_event_contract(
        report(80), PREDICTION_SLEEVE_NAV, EventStrategy.DIRECTIONAL
    )
    assert proposal.sleeve is Sleeve.PREDICTION
    assert proposal.instrument is InstrumentKind.EVENT_CONTRACT


# ================================================================================
# Out-of-range confidence is rejected, not clamped
# ================================================================================


@pytest.mark.parametrize("bad", [101, 150, 1000, -1, -50])
def test_out_of_range_confidence_is_rejected(engine, bad):
    """Clamping 150 to 100 would silently size at the maximum — the worst failure."""
    with pytest.raises(ValueError, match="refusing to clamp"):
        engine._table_fraction(bad)


def test_the_report_schema_already_blocks_out_of_range_scores():
    """Defence in depth: the engine's check is a backstop, not the only one."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        report(150)


def test_negative_sleeve_nav_is_rejected(engine):
    with pytest.raises(ValueError, match="negative"):
        engine.propose_equity(report(70), Decimal("-1"))


# ================================================================================
# Sizing proposes, the gate disposes
# ================================================================================


def test_a_sized_proposal_is_not_an_order(engine):
    proposal = engine.propose_equity(report(86), EQUITY_SLEEVE_NAV)
    assert not hasattr(proposal, "symbol")
    assert not hasattr(proposal, "quantity")


def test_a_broker_refuses_a_sized_proposal(engine):
    """The only thing an adapter accepts is an ApprovedOrder from the gate."""
    proposal = engine.propose_equity(report(86), EQUITY_SLEEVE_NAV)
    with pytest.raises(TypeError, match="ApprovedOrder"):
        BrokerAdapter._require_approved(proposal)


def test_sizing_exposes_no_way_to_submit_anything(engine):
    surface = {name for name in dir(engine) if not name.startswith("_")}
    for forbidden in ("submit", "approve", "send", "execute", "order", "broker"):
        assert not any(forbidden in name for name in surface), (
            f"sizing engine exposes {forbidden!r}"
        )


# ================================================================================
# The invariant: no proposal ever exceeds the hard cap
# ================================================================================


@given(confidence=st.integers(min_value=-1000, max_value=1000))
@settings(max_examples=500, deadline=None)
def test_no_proposal_ever_exceeds_the_hard_cap(confidence):
    """Whatever the research layer claims, 5% of sleeve NAV is the ceiling.

    In-range scores produce a proposal at or under the cap; out-of-range scores produce
    no proposal at all. There is no third outcome, and in particular no outcome where a
    confidence of 900 buys more than a confidence of 90.
    """
    limits = RiskLimits.load()
    engine = SizingEngine(limits)
    navs = (Decimal("0"), Decimal("1000"), Decimal("90000"), Decimal("10000000"))

    if not 0 <= confidence <= 100:
        with pytest.raises(ValueError):
            engine._table_fraction(confidence)
        return

    scored = report(confidence)
    for nav in navs:
        for proposal in (
            engine.propose_equity(scored, nav),
            engine.propose_option(scored, nav),
            engine.propose_event_contract(scored, nav, EventStrategy.ARB),
            engine.propose_event_contract(scored, nav, EventStrategy.DIRECTIONAL),
        ):
            assert proposal.fraction_of_sleeve_nav <= limits.sizing.hard_cap
            assert proposal.capital <= nav * limits.sizing.hard_cap
            assert proposal.capital >= 0


@given(
    low=st.integers(min_value=0, max_value=100),
    high=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=200, deadline=None)
def test_size_is_monotonic_in_confidence(low, high):
    """More confidence never buys less. A non-monotonic table is a mis-typed band."""
    if low > high:
        low, high = high, low
    engine = SizingEngine(RiskLimits.load())
    smaller = engine.propose_equity(report(low), EQUITY_SLEEVE_NAV)
    larger = engine.propose_equity(report(high), EQUITY_SLEEVE_NAV)
    assert smaller.capital <= larger.capital


# ================================================================================
# Determinism
# ================================================================================


def test_sizing_is_a_pure_function_of_its_inputs(engine):
    first = engine.propose_equity(report(71), EQUITY_SLEEVE_NAV)
    second = engine.propose_equity(report(71), EQUITY_SLEEVE_NAV)
    assert first == second


# ================================================================================
# direction == no_position — zero at any confidence
# ================================================================================


def no_trade_report(confidence: int) -> ResearchReport:
    return ResearchReport.model_validate(
        {**BASE_REPORT, "direction": "no_position", "confidence": confidence}
    )


@pytest.mark.parametrize("confidence", [0, 54, 55, 70, 71, 85, 86, 95, 100])
def test_no_position_sizes_to_zero_at_every_confidence(engine, confidence):
    """The verdict is the verdict. The number does not overrule it.

    95 is the case that matters: it sits in the top band, so reading confidence alone
    would produce the largest position the table allows out of a report whose entire
    content is "do not take this trade".
    """
    proposal = engine.propose_equity(no_trade_report(confidence), EQUITY_SLEEVE_NAV)
    assert proposal.capital == Decimal("0")
    assert proposal.fraction_of_sleeve_nav == Decimal("0")
    assert not proposal.is_tradeable


def test_no_position_at_ninety_five_is_not_a_top_band_position(engine):
    """Stated against the concrete alternative, because that is the failure mode."""
    directional = engine.propose_equity(report(95), EQUITY_SLEEVE_NAV)
    assert directional.capital == Decimal("6300.00")  # 7% of 90k, the top band

    abstaining = engine.propose_equity(no_trade_report(95), EQUITY_SLEEVE_NAV)
    assert abstaining.capital == Decimal("0")


@pytest.mark.parametrize("confidence", [56, 71, 86, 100])
def test_no_position_is_zero_for_options_too(engine, confidence):
    proposal = engine.propose_option(no_trade_report(confidence), EQUITY_SLEEVE_NAV)
    assert proposal.capital == Decimal("0")
    assert not proposal.is_tradeable


@pytest.mark.parametrize("strategy", list(EventStrategy))
def test_no_position_is_zero_for_event_contracts_too(engine, strategy):
    proposal = engine.propose_event_contract(
        no_trade_report(100), PREDICTION_SLEEVE_NAV, strategy
    )
    assert proposal.capital == Decimal("0")
    assert not proposal.is_tradeable


def test_no_position_says_why_in_the_rationale(engine):
    """The audit record has to distinguish "no trade" from "weak signal"."""
    abstaining = engine.propose_equity(no_trade_report(95), EQUITY_SLEEVE_NAV)
    assert "no_position" in abstaining.rationale
    assert "95" in abstaining.rationale

    weak = engine.propose_equity(report(30), EQUITY_SLEEVE_NAV)
    assert "no_position" not in weak.rationale
    assert "floor" in weak.rationale


@given(confidence=st.integers(min_value=0, max_value=100))
@settings(max_examples=200)
def test_no_confidence_score_makes_a_no_position_report_tradeable(limits, confidence):
    """The property, over the whole range: there is no score that unlocks it."""
    engine = SizingEngine(limits)
    for propose, nav in (
        (engine.propose_equity, EQUITY_SLEEVE_NAV),
        (engine.propose_option, EQUITY_SLEEVE_NAV),
    ):
        assert propose(no_trade_report(confidence), nav).capital == Decimal("0")


def test_an_event_proposal_on_a_zero_sleeve_is_no_trade():
    """100/0 allocation (2026-08-21): a $0 prediction sleeve sizes every event
    proposal to zero capital — no trade, no exception, rationale intact."""
    engine = SizingEngine(RiskLimits.load())
    for strategy in (EventStrategy.ARB, EventStrategy.DIRECTIONAL):
        proposal = engine.propose_event_contract(report(90), Decimal("0"), strategy)
        assert not proposal.is_tradeable
        assert proposal.capital == Decimal("0")
