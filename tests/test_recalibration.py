"""Risk-on recalibration + short-dated options test (human ruling 2026-09-15).

The claims under test: the shipped caps carry the ruling; a conviction verdict
buys an option through the same selector without a catalyst; a days-horizon
thesis may buy down to 7 DTE, is tagged short_dated_option, closes at T-1, and
draws on its own 0.05 premium pool inside the 0.20 aggregate; a no-ticker
Class 1 post is researched with its mapped ETF proposed and tagged theme_etf
when the model takes it; the prompt names both doors; attribution and the
forward funnel carry the tags on their own rows with a same-thesis-as-stock
line per contract.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from audit.attribution import build_attribution
from audit.records import ExitReason
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from forward import funnel_entries
from research.config import ResearchConfig
from research.prompts import SYSTEM_PROMPT, build_user_prompt
from research.reports import REPORT_TOOL_NAME
from risk_gate.gate import RiskGate
from risk_gate.limits import RiskLimits
from risk_gate.rejections import RejectionCode
from risk_gate.schema import LimitExecution, OptionBuyToOpenOrder
from risk_gate.state import AccountState, Sleeve
from signals.config import SignalsConfig
from signals.records import Signal, SignalClass
from signals.themes import ThemeEtfMap

from test_exits import RoutingLLM
from test_options import CATALYST, FakeChain, catalyst_report, good_chain, quote, wire
from test_orchestrator import (
    NOW,
    REPORT,
    FakeBroker,
    FakeClock,
    build,
    feed,
    orchestrator_config,
    prices_of,
    structured,
)

TODAY = NOW.date()
ZERO = Decimal("0")


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="module")
def limits() -> RiskLimits:
    return RiskLimits.load()


@pytest.fixture(scope="module")
def signals_config() -> SignalsConfig:
    return SignalsConfig.load()


@pytest.fixture(scope="module")
def research_config() -> ResearchConfig:
    return ResearchConfig.load()


def short_dated_chain(days_out: int = 10) -> list:
    """A liquid chain whose nearest expiry sits inside the short-dated window,
    alongside the standard 70-day strikes."""
    return good_chain() + [
        quote(strike="135", days_out=days_out, delta="0.74", iv="0.34"),
        quote(strike="140", days_out=days_out, delta="0.68", iv="0.35"),
        quote(strike="145", days_out=days_out, delta="0.61", iv="0.36"),
    ]


def short_dated_config():
    """Leash stretched so the expiry rule, not the time stop, is what fires."""
    return orchestrator_config(
        exits={
            "max_loss_fraction": "0.50",
            "time_stop_days": {"days": 200, "weeks": 200, "months": 200},
            "leash_bounds": {
                "days": {"floor": 3, "ceiling": 400},
                "weeks": {"floor": 14, "ceiling": 400},
                "months": {"floor": 60, "ceiling": 400},
            },
        }
    )


# ================================================================================
# The shipped configuration carries the ruling
# ================================================================================


def test_the_shipped_caps_carry_the_recalibration(limits):
    sizing = limits.sizing
    assert (sizing.size_for(50), sizing.size_for(71), sizing.size_for(86)) == (
        Decimal("0.020"), Decimal("0.050"), Decimal("0.100")
    )
    assert sizing.hard_cap == Decimal("0.10")
    sleeve = limits.equity_sleeve
    assert sleeve.max_single_position == Decimal("0.10")
    assert sleeve.max_daily_deployment == Decimal("0.25")
    assert sleeve.max_sector_exposure == Decimal("0.25")
    assert sleeve.max_short_dated_premium_at_risk == Decimal("0.05")
    assert sleeve.max_options_premium_at_risk == Decimal("0.20")
    options = limits.options_selection
    assert options.min_expiry_days.days == 7
    assert options.conviction_min_confidence == 80
    assert (options.short_dated_dte, options.short_dated_close_before_expiry_days) == (21, 1)
    assert options.close_before_expiry_days == 5  # the standard window is unchanged


def test_the_short_dated_pool_must_sit_inside_the_aggregate(limits):
    raw = limits.model_dump()
    raw["equity_sleeve"]["max_short_dated_premium_at_risk"] = Decimal("0.25")
    with pytest.raises(ValueError, match="INSIDE the aggregate"):
        RiskLimits.model_validate(raw)


# ================================================================================
# Door 1: conviction
# ================================================================================


def test_a_conviction_verdict_buys_an_option_without_a_catalyst(
    tmp_path, limits, signals_config, research_config
):
    """Confidence 86, weeks horizon, no catalyst: the conviction door opens; the
    same selector, gates and halved sizing apply (10% band / 2 = 5% of 75k)."""
    conviction = {**REPORT, "confidence": 86, "catalyst_within_horizon": None}
    chain = FakeChain(good_chain())
    started, broker = wire(
        tmp_path, limits, signals_config, research_config, report=conviction, chain=chain
    )
    result = started.loop.tick().processed[0]
    assert result.traded
    trail = started.audit.trail(result.decision_id)
    order = trail.decision.gate.order
    assert order["kind"] == "option_buy_to_open"
    assert trail.decision.sizing.instrument == "option"
    assert trail.decision.sizing.capital == Decimal("3750.00")  # 10% / 2 of 75k
    assert order["contracts"] == 15  # 3,750 at mid 2.50 x 100
    expression = trail.decision.expression
    assert (expression.door, expression.tag) == ("conviction", "conviction_option")
    assert expression.underlying_price == Decimal("140.00")
    assert expression.expiration == (TODAY + timedelta(days=70)).isoformat()


def test_below_the_conviction_floor_without_a_catalyst_is_still_stock(
    tmp_path, limits, signals_config, research_config
):
    patient = {**REPORT, "confidence": 79, "catalyst_within_horizon": None}
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=patient, chain=FakeChain(good_chain()),
    )
    result = started.loop.tick().processed[0]
    assert result.traded
    trail = started.audit.trail(result.decision_id)
    assert trail.decision.gate.order["kind"] == "equity_buy"
    assert trail.decision.expression.fallback_reason == "no_catalyst"
    assert trail.decision.expression.door is None
    assert trail.decision.expression.tag is None


def test_the_catalyst_door_is_unchanged_and_records_its_door(
    tmp_path, limits, signals_config, research_config
):
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report=catalyst_report(), chain=FakeChain(good_chain()),
    )
    result = started.loop.tick().processed[0]
    expression = started.audit.trail(result.decision_id).decision.expression
    assert expression.chosen == "option"
    assert (expression.door, expression.tag) == ("catalyst", None)  # 70 DTE: not short-dated


# ================================================================================
# Door 2: short-dated
# ================================================================================


def enter_short_dated(tmp_path, limits, signals_config, research_config, **overrides):
    clock = FakeClock()
    report = catalyst_report(time_horizon="days", **overrides)
    started, broker = wire(
        tmp_path, limits, signals_config, research_config,
        report=report, chain=FakeChain(short_dated_chain()),
        clock=clock, config=short_dated_config(),
    )
    result = started.loop.tick()
    assert result.processed[0].traded
    return started, broker, clock


def test_a_days_horizon_catalyst_thesis_buys_down_to_seven_dte(
    tmp_path, limits, signals_config, research_config
):
    """min_expiry_days.days 14 -> 7: the 10-day expiry now qualifies and, being
    the shortest qualifying, is the pick. Tagged short_dated_option."""
    started, broker, _ = enter_short_dated(tmp_path, limits, signals_config, research_config)
    trail = started.audit.trail("dec-1")
    expression = trail.decision.expression
    assert expression.chosen == "option"
    assert expression.expiration == (TODAY + timedelta(days=10)).isoformat()
    assert (expression.door, expression.tag) == ("catalyst", "short_dated_option")
    position = started.exits.tracked[0]
    assert position.is_option and position.short_dated is True


def test_a_conviction_days_thesis_without_a_catalyst_is_short_dated_too(
    tmp_path, limits, signals_config, research_config
):
    """Days horizon, no catalyst, confidence 86: the conviction door admits it,
    and at 10 DTE the short-dated tag wins while the door stays on the record."""
    started, _, _ = enter_short_dated(
        tmp_path, limits, signals_config, research_config,
        confidence=86, catalyst_within_horizon=None,
    )
    expression = started.audit.trail("dec-1").decision.expression
    assert (expression.door, expression.tag) == ("conviction", "short_dated_option")


def test_a_days_thesis_below_eighty_without_a_catalyst_is_stock(
    tmp_path, limits, signals_config, research_config
):
    started, _ = wire(
        tmp_path, limits, signals_config, research_config,
        report={**REPORT, "time_horizon": "days", "confidence": 79, "catalyst_within_horizon": None},
        chain=FakeChain(short_dated_chain()),
    )
    result = started.loop.tick().processed[0]
    assert result.traded
    assert started.audit.trail(result.decision_id).decision.gate.order["kind"] == "equity_buy"


def test_a_short_dated_contract_closes_at_t_minus_one_not_five(
    tmp_path, limits, signals_config, research_config
):
    started, _, clock = enter_short_dated(tmp_path, limits, signals_config, research_config)
    expiry = TODAY + timedelta(days=10)
    # Five days out: the standard window would fire here. The short-dated
    # window does not.
    clock.advance(days=5)
    assert (expiry - clock.now.date()).days == 5
    assert started.loop.tick().exits_started == 0
    clock.advance(days=3)  # T-2
    assert started.loop.tick().exits_started == 0
    clock.advance(days=1)  # T-1
    report = started.loop.tick()
    assert report.exits_started == 1
    started.loop.tick()
    trail = started.audit.trail("dec-1")
    assert trail.exits[-1].reason is ExitReason.EXPIRY_CLOSE
    assert "T-1" in trail.exits[-1].detail
    assert trail.outcome is not None
    # The exit fill carries the underlying's quote: the counterfactual's exit leg.
    sell = [f for f in trail.fills if f.side == "sell"][-1]
    assert sell.underlying_price == Decimal("140.00")


def test_a_standard_contract_still_closes_at_t_minus_five(
    tmp_path, limits, signals_config, research_config
):
    """The 70-day catalyst contract keeps the standard window: not short-dated."""
    from test_options import enter_option_position

    started, clock, _ = enter_option_position(tmp_path, limits, signals_config, research_config)
    assert started.exits.tracked[0].short_dated is False
    expiry = TODAY + timedelta(days=70)
    clock.advance(days=(expiry - TODAY).days - 6)
    assert started.loop.tick().exits_started == 0
    clock.advance(days=1)  # T-5
    assert started.loop.tick().exits_started == 1


def test_the_short_dated_pool_is_capped_inside_the_aggregate(limits):
    """5% of the 75k sleeve = 3,750 of short-dated premium. A 10-DTE buy over it
    is refused with its own code while a 60-DTE buy of the same premium passes
    on the 20% aggregate alone."""
    clock = FakeClock()
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        clock=clock,
    )
    today = clock.now.date()

    def contract(days_out, contracts, price="2.00", root="NUE"):
        expiration = today + timedelta(days=days_out)
        return OptionBuyToOpenOrder(
            symbol=f"{root}{expiration.strftime('%y%m%d')}C00140000",
            underlying=root,
            right="call",
            expiration=expiration,
            strike=Decimal("140"),
            contracts=contracts,
            execution=LimitExecution(limit_price=Decimal(price)),
        )

    # 15 x 2.00 x 100 = 3,000 short-dated: inside the 3,750 pool.
    first = gate.submit(contract(10, 15))
    assert first.is_approved, first
    position = gate.state.position(("option", first.order.symbol))
    assert position.short_dated_at_entry is True and position.expiration == today + timedelta(days=10)
    # Another 1,000 short-dated would reach 4,000 > 3,750: refused, by the sub-cap.
    over = gate.submit(contract(12, 5, root="AAPL"))
    assert not over.is_approved
    assert over.code is RejectionCode.MAX_SHORT_DATED_PREMIUM_EXCEEDED
    assert over.limit == Decimal("3750.00")
    # The same 1,000 at 60 DTE is ordinary premium: 4,000 of 15,000 aggregate.
    standard = gate.submit(contract(60, 5, root="AAPL"))
    assert standard.is_approved, standard
    assert gate.state.position(("option", standard.order.symbol)).short_dated_at_entry is False
    assert gate.state.short_dated_premium_at_risk(today, 21) == Decimal("3000.00")
    assert gate.state.options_premium_at_risk == Decimal("4000.00")


def test_a_seeded_contract_inside_the_window_counts_toward_the_pool():
    """After a restart the entry timing is unknown: a seeded contract inside the
    window TODAY counts (over-counting is the safe error); one outside does not."""
    from execution.base import BrokerPosition
    from orchestrator.state import occ_expiration, position_from_broker

    today = date(2026, 9, 15)
    near = position_from_broker(
        BrokerPosition("NUE260925C00140000", Decimal("3"), Decimal("750"), Decimal("750"), "us_option"),
        today,
    )
    far = position_from_broker(
        BrokerPosition("NUE261218C00140000", Decimal("3"), Decimal("750"), Decimal("750"), "us_option"),
        today,
    )
    assert near.is_option and near.expiration == date(2026, 9, 25)
    assert near.short_dated_at_entry is None
    state = AccountState(
        cash=Decimal("1000"), high_water_mark=Decimal("1000"),
        positions={near.key: near, far.key: far},
    )
    assert state.short_dated_premium_at_risk(today, 21) == Decimal("750")
    assert occ_expiration("NUE") is None
    assert occ_expiration("SPY261218P00500000") == date(2026, 12, 18)


# ================================================================================
# Enabler: theme -> ETF expression
# ================================================================================

TARIFF_POST = (
    "Big tariffs on imported steel and aluminum are coming Monday, the biggest "
    "in decades. Foreign dumping ends now and American industry wins again."
)


def test_a_no_ticker_theme_post_carries_its_etf_proposal_into_the_prompt(signals_config):
    themes = ThemeEtfMap.from_config(signals_config)
    assert "trump_posts" in themes.sources
    signal = Signal(
        signal_id="s1", source_id="trump_posts", signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=NOW, content=TARIFF_POST, raw_content=TARIFF_POST,
        priority=__import__("signals.records", fromlist=["Priority"]).Priority.for_class(
            SignalClass.CLASS_1_REALTIME
        ),
        metadata={"tickers": ""},
    )
    matched = themes.match(signal)
    assert (matched.theme, matched.etf) == ("tariffs", "XLI")
    stamped = themes.apply(signal)
    assert (stamped.metadata["theme"], stamped.metadata["theme_etf"]) == ("tariffs", "XLI")
    prompt = build_user_prompt(stamped)
    assert "theme -> ETF proposal" in prompt and "XLI" in prompt
    assert "DECLINE the mapping" in prompt
    # No proposal without the stamp; none for a post that names its instrument.
    assert "theme -> ETF" not in build_user_prompt(signal)
    named = replace(signal, metadata={"tickers": "NUE"})
    assert themes.match(named) is None


def test_two_matching_themes_are_ambiguous_and_get_no_mapping(signals_config):
    from signals.records import Priority

    themes = ThemeEtfMap.from_config(signals_config)
    text = "Tariffs on chips and semiconductors from overseas start next week."
    signal = Signal(
        signal_id="s2", source_id="trump_posts", signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=NOW, content=text, raw_content=text,
        priority=Priority.for_class(SignalClass.CLASS_1_REALTIME), metadata={"tickers": ""},
    )
    assert themes.match(signal) is None  # tariffs AND semis: the fewer trades
    assert themes.apply(signal) is signal


def test_a_theme_post_expressed_through_its_etf_is_tagged_theme_etf(
    tmp_path, limits, signals_config, research_config
):
    """End to end: no ticker, tariff theme, the model takes XLI. The decision
    carries tag theme_etf and the theme; the forward funnel sees the tag."""
    llm = RoutingLLM(**{REPORT_TOOL_NAME: structured({
        **REPORT,
        "tickers": ["XLI"],
        "thesis": "Steel and aluminum tariffs lift domestic industrials; XLI is the liquid expression.",
        "target_price": "150",
    })})
    started = build(
        tmp_path, limits, signals_config, research_config,
        llm=llm, prices=prices_of(NUE="140.00", XLI="120.00"),
        fetcher=feed(trump_posts=[TARIFF_POST]),
    )
    result = started.loop.tick().processed[0]
    assert result.traded, result
    assert "theme -> ETF proposal" in llm.calls[0]["user"]
    trail = started.audit.trail(result.decision_id)
    assert trail.decision.gate.order["symbol"] == "XLI"
    expression = trail.decision.expression
    assert expression is not None
    assert (expression.tag, expression.theme) == ("theme_etf", "tariffs")
    assert expression.underlying_price == Decimal("120.00")
    entries = funnel_entries(started.audit.records())
    assert [e.expression_tag for e in entries if e.decision_id == result.decision_id] == ["theme_etf"]


def test_a_model_that_declines_the_mapping_leaves_no_tag(
    tmp_path, limits, signals_config, research_config
):
    llm = RoutingLLM(**{REPORT_TOOL_NAME: structured({
        **REPORT, "tickers": [], "direction": "no_position", "confidence": 70,
        "thesis": "The post is rhetoric; industrials are not the instrument here.",
    })})
    started = build(
        tmp_path, limits, signals_config, research_config,
        llm=llm, prices=prices_of(XLI="120.00"), fetcher=feed(trump_posts=[TARIFF_POST]),
    )
    result = started.loop.tick().processed[0]
    assert not result.traded
    assert "theme -> ETF proposal" in llm.calls[0]["user"]


# ================================================================================
# The prompt names both doors and carries the new table
# ================================================================================


def test_the_system_prompt_names_both_doors_and_the_new_cap():
    assert "Conviction door" in SYSTEM_PROMPT and "Short-dated" in SYSTEM_PROMPT
    assert "hard 10% cap" in SYSTEM_PROMPT and "5% cap" not in SYSTEM_PROMPT
    assert "Confidence below 50" in SYSTEM_PROMPT
    assert "Never inflate confidence or invent a catalyst" in SYSTEM_PROMPT


# ================================================================================
# Measurement: attribution rows and the counterfactual-equity line
# ================================================================================


def test_attribution_carries_the_tag_row_with_a_same_thesis_stock_line(
    tmp_path, limits, signals_config, research_config
):
    started, _, clock = enter_short_dated(tmp_path, limits, signals_config, research_config)
    clock.advance(days=9)  # T-1
    started.loop.tick()
    started.loop.tick()
    trail = started.audit.trail("dec-1")
    assert trail.outcome is not None

    report = build_attribution(started.audit.trails(), generated_at=clock.now)
    rows = {row.tag: row for row in report.by_expression_tag}
    assert set(rows) == {"short_dated_option"}
    row = rows["short_dated_option"]
    assert (row.open, row.closed) == (0, 1)
    assert row.by_reason == (("expiry_close", 1, trail.outcome.realised_pnl),)
    # The underlying did not move (140 in, 140 out): the same dollars as stock
    # made exactly nothing, and the line says so.
    assert row.counterfactual_equity_pnl == Decimal("0.00")
    assert len(row.contracts) == 1
    assert row.contracts[0].equity_pnl == Decimal("0.00")
    text = report.render()
    assert "Options doors and theme->ETF expressions" in text
    assert "same-thesis stock +0.00" in text
