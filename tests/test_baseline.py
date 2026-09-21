"""The baseline market-beta sleeve (AGGRESSION RULING 2026-09-18, lever 4;
kill-switch semantics revised the same day).

The claims: the weights are 55/15/30/0 and sum to one; a baseline buy is
cash-secured and bound by its own allocation ceiling but exempt from the alpha
caps; the sleeve builds to target from cash above the liquidity floor with the
sweeper unparking SGOV for the rest; the check is WEEKLY and trades only when
the sleeve sits outside the band, back to target; a tripped kill switch freezes
it in BOTH directions; lots and cadence survive a restart in their own sleeve;
the mechanical arm's 25 -> 15 move forces no trim; and attribution renders the
sleeve on its own line, out of every class, with the headline alpha line.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from audit.records import ExitReason, OutcomeRecord
from execution.base import BrokerPosition
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from orchestrator import start
from orchestrator.baseline import iso_week
from orchestrator.ops import RunLog, health_report
from risk_gate.gate import RiskGate
from risk_gate.limits import RiskLimits, SleeveWeights
from risk_gate.rejections import RejectionCode
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import AccountState, Position, Sleeve

from test_exits import MutablePrices, RoutingLLM, restart_kwargs
from test_orchestrator import NOW, FakeBroker, FakeClock, build, counter, feed
from test_sweep import TwoSidedPrices

ZERO = Decimal("0")


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def limits():
    return RiskLimits.load()


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    from research.config import ResearchConfig

    return ResearchConfig.load()


def spy_buy(quantity="60", price="500.00"):
    return EquityBuyOrder(
        symbol="SPY",
        quantity=Decimal(quantity),
        execution=LimitExecution(limit_price=Decimal(price)),
        sleeve="baseline",
    )


def quiet_build(tmp_path, limits, signals_config, research_config, **kwargs):
    """A session with no signals: only the deterministic sleeves have work."""
    prices = kwargs.pop("prices", None) or TwoSidedPrices(SPY="500.00", SGOV="100.40")
    clock = kwargs.pop("clock", None) or FakeClock()
    started = build(
        tmp_path,
        limits,
        signals_config,
        research_config,
        fetcher=feed(),
        llm=RoutingLLM(),
        prices=prices,
        clock=clock,
        **kwargs,
    )
    return started, prices, clock


# ================================================================================
# The ruling's arithmetic in the cap table
# ================================================================================


def test_the_weights_are_55_15_30_0_and_the_sleeve_is_configured(limits):
    sleeves = limits.portfolio.sleeves
    assert (sleeves.equity, sleeves.mechanical, sleeves.baseline, sleeves.prediction) == (
        Decimal("0.55"), Decimal("0.15"), Decimal("0.30"), Decimal("0"),
    )
    assert limits.baseline_sleeve.enabled is True
    assert limits.baseline_sleeve.symbol == "SPY"
    assert limits.baseline_sleeve.rebalance_band == Decimal("0.05")
    with pytest.raises(ValueError):
        SleeveWeights(equity="0.45", mechanical="0.15", prediction="0", baseline="0.30")


def test_a_pre_ruling_cap_table_without_a_baseline_weight_still_parses():
    weights = SleeveWeights(equity="0.75", mechanical="0.25", prediction="0")
    assert weights.baseline == ZERO


# ================================================================================
# The gate: cash-secured, allocation-bound, alpha caps waived
# ================================================================================


def test_a_baseline_buy_skips_alpha_caps_but_never_cash_securing_or_its_ceiling(limits):
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        FakeClock(),
    )
    # 30,000 of SPY: far beyond the judged 10% single-position cap, the 25%
    # daily deployment and the sector cap — none of which bind the beta sleeve.
    decision = gate.submit(spy_buy())
    assert decision.is_approved
    assert gate.state.reserved_cash == Decimal("30000.00")  # cash-secured
    assert gate.state.deployed_today == ZERO
    assert gate.state.mechanical_deployed_today == ZERO
    # But its own allocation ceiling binds: 30% + 3% drift = 33% of NAV.
    over = gate.submit(spy_buy(quantity="8"))  # would reach 34%
    assert not over.is_approved
    assert over.code is RejectionCode.SLEEVE_ALLOCATION_EXCEEDED
    # And never-negative is as binding as anywhere: 120,000 more is refused.
    broke = gate.submit(spy_buy(quantity="240"))
    assert not broke.is_approved
    assert broke.code is RejectionCode.INSUFFICIENT_BUYING_POWER


def test_the_kill_switch_blocks_baseline_buys_at_the_gate(limits):
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        FakeClock(),
    )
    gate.record_fill(gate.submit(spy_buy(quantity="10")), Decimal("500.00"))
    gate.state.kill_switch_tripped = True
    blocked = gate.submit(spy_buy(quantity="1"))
    assert not blocked.is_approved
    assert blocked.code is RejectionCode.KILL_SWITCH_ACTIVE


def test_a_judged_exit_can_never_touch_baseline_shares(limits):
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        FakeClock(),
    )
    gate.record_fill(gate.submit(spy_buy(quantity="10")), Decimal("500.00"))
    rogue = gate.submit(
        EquitySellToCloseOrder(
            symbol="SPY",
            quantity=Decimal("10"),
            execution=LimitExecution(limit_price=Decimal("499.00")),
            sleeve="equity",
        )
    )
    assert not rogue.is_approved
    assert rogue.code is RejectionCode.POSITION_NOT_HELD


# ================================================================================
# The transition: initial build, funded above the floor and from SGOV
# ================================================================================


def test_the_initial_build_buys_to_target_and_the_sweeper_parks_the_rest(
    tmp_path, limits, signals_config, research_config
):
    broker = FakeBroker()
    started, _, clock = quiet_build(
        tmp_path, limits, signals_config, research_config, broker=broker
    )
    baseline = started.loop.baseline
    assert baseline is not None and baseline.symbol == "SPY"
    assert baseline.week_checked is None

    report = started.loop.tick()
    # Weekly check ran (never checked before): 0% vs 40% is outside the band.
    assert baseline.week_checked == iso_week(clock.now) == "2026-W34"
    assert baseline.rebalancing is True
    # The buy: 30,000 target, all of it above the 18,500 liquidity floor.
    assert report.baseline_orders == 1
    spy = [p for p in broker.payloads if p["symbol"] == "SPY"]
    assert spy and spy[0]["qty"] == 60 and spy[0]["limit_price"] == Decimal("500.00")
    # The sweep ran AFTER it, on cash net of the baseline's reservation:
    # 100,000 - 18,500 floor - 30,000 reserved = 51,500 -> 512 SGOV at 100.40.
    assert report.sweep_orders == 1
    sgov = [p for p in broker.payloads if p["symbol"] == "SGOV"]
    assert sgov[0]["qty"] == 512

    report = started.loop.tick()  # both settle; the sleeve is at target
    position = started.gate.state.position(("baseline", "SPY"))
    assert position is not None and position.quantity == Decimal("60")
    assert position.sleeve is Sleeve.BASELINE
    assert baseline.rebalancing is False
    assert baseline.funding_need() == ZERO
    assert len(baseline.lots) == 1
    assert started.gate.state.nav == Decimal("100000")  # cash became ETF at cost
    # The trail: strategy baseline, no research, sealed nothing.
    trail = next(
        t for t in started.audit.trails() if t.decision.sizing.strategy == "baseline"
    )
    assert trail.decision.research is None
    assert trail.decision.signal.external_id is None
    assert trail.decision.sizing.sleeve == "baseline"
    # Nothing more this week: the sleeve is at target and the check is spent.
    assert started.loop.tick().baseline_orders == 0


def test_the_live_transition_unparks_sgov_to_fund_the_build(
    tmp_path, limits, signals_config, research_config
):
    """The droplet's shape: cash at the old buffer, the rest parked. The
    baseline spends what sits above the NEW floor first, the sweeper unsweeps
    SGOV for the balance, and the buy completes as the cash lands — one
    rebalance, a few ticks, no staging."""
    clock = FakeClock()
    raw = RiskLimits.load().model_dump()
    raw["baseline_sleeve"]["enabled"] = False  # session one: the world before
    before = RiskLimits.model_validate(raw)
    broker = FakeBroker()
    first, _, _ = quiet_build(
        tmp_path, before, signals_config, research_config, broker=broker, clock=clock
    )
    assert first.loop.baseline is None
    first.loop.tick()
    first.loop.tick()  # 81,500 parked: 811 SGOV at 100.40; cash 18,575.60
    assert first.gate.state.position(("cash_management", "SGOV")).quantity == Decimal("811")
    first.loop.shutdown()

    venue = FakeBroker(
        cash=Decimal("18575.60"),
        positions=[
            BrokerPosition("SGOV", Decimal("811"), Decimal("81424.40"), Decimal("81424.40"))
        ],
    )
    restarted = start(
        fetcher=feed(),
        prices=TwoSidedPrices(SPY="500.00", SGOV="100.40"),
        llm_client=RoutingLLM(),
        adapter=venue,
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    baseline = restarted.loop.baseline
    sweeper = restarted.loop.sweeper

    # Tick 1: the check opens the rebalance. Nothing sits above the floor, so
    # no SPY buy yet — but the sweeper's buffer now carries the 30,000 owed and
    # it unsweeps: 18,500 + 30,000 - 18,575.60 = 29,924.40 at the 100.39 bid.
    report = restarted.loop.tick()
    assert baseline.rebalancing is True
    assert report.baseline_orders == 0
    assert baseline.funding_need() == Decimal("30000.000")
    assert sweeper.buffer() == Decimal("48500.000")
    assert report.sweep_orders == 1
    unsweep = venue.payloads[-1]
    assert unsweep["symbol"] == "SGOV" and unsweep["limit_price"] == Decimal("100.39")
    assert unsweep["qty"] == Decimal("299")  # 29,924.40 / 100.39 rounded up

    # Tick 2: the unsweep is settled BEFORE the baseline runs (299 x 100.39 =
    # 30,016.61 lands), so the SPY buy goes out this tick: the whole gap at 500
    # is 59 shares, the ~500 remainder under the min notional.
    report = restarted.loop.tick()
    assert report.baseline_orders == 1
    spy = [p for p in venue.payloads if p["symbol"] == "SPY"]
    assert len(spy) == 1 and spy[0]["qty"] == 59

    # Tick 3: settled; the sleeve is at target and the rebalance closes.
    restarted.loop.tick()
    held = restarted.gate.state.position(("baseline", "SPY"))
    assert held.quantity == Decimal("59")
    assert baseline.rebalancing is False
    assert baseline.funding_need() == ZERO
    sgov = restarted.gate.state.position(("cash_management", "SGOV"))
    # 811 less the 299 that funded the build, plus 5 the sweeper re-parked from
    # the ~590 the unsweep over-delivered (rounded UP to cover the deficit).
    assert sgov.quantity == Decimal("517")
    assert restarted.gate.state.cash >= sweeper.base_buffer()
    assert restarted.loop.tick().baseline_orders == 0
    # One rebalance, three ticks, no staging: exactly one SPY order was placed.
    assert len([p for p in venue.payloads if p["symbol"] == "SPY"]) == 1


# ================================================================================
# Weekly cadence and the band
# ================================================================================


def _built(tmp_path, limits, signals_config, research_config):
    broker = FakeBroker()
    started, prices, clock = quiet_build(
        tmp_path, limits, signals_config, research_config, broker=broker
    )
    started.loop.tick()
    started.loop.tick()
    return started, prices, clock, broker


def test_mid_week_drift_waits_and_the_weekly_check_trades_back_to_target(
    tmp_path, limits, signals_config, research_config
):
    started, prices, clock, broker = _built(
        tmp_path, limits, signals_config, research_config
    )
    baseline = started.loop.baseline
    # SPY 500 -> 650: 39,000 of a 109,000 NAV = 35.8%, outside the 25-35% band.
    prices.table["SPY"] = Decimal("650.00")
    clock.advance(days=1)
    assert started.loop.tick().baseline_orders == 0  # same week: no check
    assert baseline.rebalancing is False

    clock.advance(days=7)  # a new ISO week
    report = started.loop.tick()
    assert baseline.week_checked == "2026-W35"
    assert baseline.rebalancing is True
    assert report.baseline_orders == 1
    sell = broker.payloads[-1]
    assert sell["symbol"] == "SPY"
    # Target 32,700; excess 6,300 at the 649.99 bid -> 9 shares (rounded down).
    assert sell["qty"] == Decimal("9") and sell["limit_price"] == Decimal("649.99")
    trail = started.audit.trail(baseline.lots[0].decision_id)
    assert trail.exits[-1].reason is ExitReason.BASELINE_REBALANCE
    assert trail.exits[-1].submitted is True

    started.loop.tick()  # settles: 51 shares = 33,150 of ~109,000 = 30.4%
    held = started.gate.state.position(("baseline", "SPY"))
    assert held.quantity == Decimal("51")
    assert baseline.rebalancing is False
    assert baseline.lots[0].quantity == Decimal("51")


def test_inside_the_band_the_weekly_check_does_nothing(
    tmp_path, limits, signals_config, research_config
):
    started, prices, clock, broker = _built(
        tmp_path, limits, signals_config, research_config
    )
    baseline = started.loop.baseline
    orders_before = len(broker.payloads)
    # SPY 500 -> 550: 33,000 of 103,000 = 32.0%, inside the band.
    prices.table["SPY"] = Decimal("550.00")
    clock.advance(days=7)
    assert started.loop.tick().baseline_orders == 0
    assert baseline.week_checked == "2026-W35" and baseline.rebalancing is False
    assert [p for p in broker.payloads[orders_before:] if p["symbol"] == "SPY"] == []


def test_sells_relieve_lots_oldest_first_and_a_flat_lot_resolves_as_beta(
    tmp_path, limits, signals_config, research_config
):
    """Two lots (a build the floor cut short, completed after a deposit), then
    a rally that puts the sleeve far over target: the OLDER lot sells flat
    first and writes its outcome — realised beta on its own trail, never a
    signal class."""
    broker = FakeBroker(
        cash=Decimal("30000"),
        positions=[
            BrokerPosition("AAPL", Decimal("700"), Decimal("70000"), Decimal("70000"))
        ],
    )
    started, prices, clock = quiet_build(
        tmp_path, limits, signals_config, research_config, broker=broker
    )
    baseline = started.loop.baseline
    started.loop.tick()  # lot 1: only 11,500 sits above the 18,500 floor -> 23 shares
    started.loop.tick()
    assert [lot.quantity for lot in baseline.lots] == [Decimal("23")]
    assert baseline.rebalancing is True  # still 18,500 short, nothing to unpark

    started.gate.state.cash += Decimal("60000")  # a deposit; NAV 160,000
    started.loop.tick()  # lot 2: target 48,000 - 11,500 held = 36,500 -> 73 shares
    started.loop.tick()
    assert [lot.quantity for lot in baseline.lots] == [Decimal("23"), Decimal("73")]
    assert baseline.rebalancing is False
    first_lot, second_lot = (lot.decision_id for lot in baseline.lots)

    # SPY 500 -> 2000: 192,000 of a ~304,000 NAV; target ~91,200; the excess
    # is ~50 shares at the 1999.99 bid — but one lot per tick, oldest first:
    # the 23-share lot goes flat first.
    prices.table["SPY"] = Decimal("2000.00")
    clock.advance(days=7)
    assert started.loop.tick().baseline_orders == 1
    started.loop.tick()  # settles; the second tranche (16 more) goes out
    trail = started.audit.trail(first_lot)
    assert [f.side for f in trail.fills] == ["buy", "sell"]
    assert trail.fills[-1].filled_quantity == Decimal("23")
    assert trail.outcome is not None
    assert trail.outcome.realised_pnl == Decimal("23") * (Decimal("1999.99") - Decimal("500"))
    assert "baseline lot sold flat" in trail.outcome.note
    assert [lot.decision_id for lot in baseline.lots] == [second_lot]
    outcomes = [r for r in started.audit.records() if isinstance(r, OutcomeRecord)]
    assert first_lot in {r.decision_id for r in outcomes}
    # The second lot is trimmed, not closed: no outcome for it.
    started.loop.tick()
    assert started.audit.trail(second_lot).outcome is None
    assert baseline.lots[0].quantity == Decimal("46")  # 73 less the 27 trimmed


# ================================================================================
# The kill switch: frozen both ways
# ================================================================================


def test_a_tripped_kill_switch_freezes_the_sleeve_in_both_directions(
    tmp_path, limits, signals_config, research_config
):
    started, prices, clock, broker = _built(
        tmp_path, limits, signals_config, research_config
    )
    baseline = started.loop.baseline
    lot_id = baseline.lots[0].decision_id
    orders_before = len(broker.payloads)
    baseline_records_before = len(
        [d for d in started.audit.decisions() if d.sizing.strategy == "baseline"]
    )

    # Above the band with the switch tripped: no sell, no check, no record on
    # the lot. (The SWEEP still unsweeps under a halt — that is the sweep's
    # ruling, not this sleeve's — so only baseline records are counted here.)
    prices.table["SPY"] = Decimal("650.00")
    started.gate.state.kill_switch_tripped = True
    clock.advance(days=7)
    report = started.loop.tick()
    assert report.halted and report.baseline_orders == 0
    assert baseline.week_checked == "2026-W34"  # the week's check did not run
    assert baseline.frozen is True and baseline.funding_need() == ZERO
    # Below the band, still tripped: no buy either (the gate would refuse it
    # anyway; the sleeve does not even ask). SPY 380 = 22,800 of 92,800 =
    # 24.6%, outside the 25-35% band on the low side.
    prices.table["SPY"] = Decimal("380.00")
    assert started.loop.tick().baseline_orders == 0
    assert [p for p in broker.payloads[orders_before:] if p["symbol"] == "SPY"] == []
    assert started.audit.trail(lot_id).exits == ()
    assert (
        len([d for d in started.audit.decisions() if d.sizing.strategy == "baseline"])
        == baseline_records_before
    )
    assert started.gate.state.position(("baseline", "SPY")).quantity == Decimal("60")
    report = health_report(
        started.preflight, started.exits.tracked, RunLog(tmp_path / "run.log")
    )
    assert "baseline (SPY): 60 units" in report
    assert "FROZEN: kill switch tripped" in report

    # A human resets the switch (simulated): the missed check runs at once and
    # the sleeve rebuilds toward target — from cash above the floor, with the
    # sweeper unparking SGOV for whatever the floor holds back.
    # (What the operator's reset does: clears the flag AND re-baselines the
    # high-water mark to current NAV, or the next mark re-trips the switch.)
    started.gate.state.kill_switch_tripped = False
    started.gate.state.high_water_mark = started.gate.state.nav
    started.loop.tick()
    assert baseline.week_checked == "2026-W35" and baseline.rebalancing is True
    assert baseline.funding_need() > ZERO or baseline.held_value() >= baseline.target_value()
    started.loop.tick()
    started.loop.tick()
    assert [p for p in broker.payloads[orders_before:] if p["symbol"] == "SPY"]


# ================================================================================
# Restarts, health, and the mechanical arm
# ================================================================================


def test_lots_and_cadence_survive_a_restart_in_their_own_sleeve(
    tmp_path, limits, signals_config, research_config
):
    started, prices, clock, broker = _built(
        tmp_path, limits, signals_config, research_config
    )
    started.loop.shutdown()
    assert started.session.baseline_week_checked == "2026-W34"
    assert started.session.baseline_rebalancing is False

    restarted = start(
        fetcher=feed(),
        prices=TwoSidedPrices(SPY="510.00", SGOV="100.40"),
        llm_client=RoutingLLM(),
        adapter=FakeBroker(
            cash=Decimal("18595.20"),
            positions=[
                BrokerPosition("SPY", Decimal("60"), Decimal("30600"), Decimal("30000")),
                BrokerPosition("SGOV", Decimal("512"), Decimal("51404.80"), Decimal("51404.80")),
            ],
        ),
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    position = restarted.gate.state.position(("baseline", "SPY"))
    assert position is not None and position.quantity == Decimal("60")
    assert position.sleeve is Sleeve.BASELINE
    assert restarted.gate.state.position(("equity", "SPY")) is None
    assert len(restarted.loop.baseline.lots) == 1
    assert restarted.loop.baseline.week_checked == "2026-W34"
    assert restarted.exits.tracked == ()  # never the exit engine's

    report = health_report(
        restarted.preflight, restarted.exits.tracked, RunLog(tmp_path / "run.log")
    )
    assert "sleeves: equity 55%, mechanical 15%, baseline 30%, prediction 0% (inactive)" in report
    assert "baseline (SPY): 60 units, value 30600.00 = 30.4% of NAV (target 30% +/-5%)" in report
    assert "P&L +600.00 (market beta, not alpha)" in report
    assert "weekly check 2026-W34" in report and "log agrees" in report
    assert "UNMANAGED" not in report
    # Same week, at target: the restart re-runs nothing.
    assert restarted.loop.tick().baseline_orders == 0


def test_the_mechanical_arm_is_not_trimmed_by_the_weight_change_but_cannot_refill(limits):
    """25% -> 15% with 30 slices worth 24% of NAV held (the live book): no sell
    is forced — the gate never touches held positions — and the allocation
    ceiling (15% + 3% drift) refuses new mechanical entries until time exits
    bring the sleeve back under it."""
    state = AccountState(cash=Decimal("76000"), high_water_mark=Decimal("100000"))
    for n in range(30):
        key = ("mechanical", f"M{n:02d}")
        state.positions[key] = Position(
            key=key, sleeve=Sleeve.MECHANICAL, quantity=Decimal("8"),
            cost_basis=Decimal("800"), market_value=Decimal("800"),
        )
    gate = RiskGate(limits, state, FakeClock())
    assert gate.nav == Decimal("100000")
    assert gate.state.sleeve_exposure(Sleeve.MECHANICAL) == Decimal("24000")
    refill = gate.submit(
        EquityBuyOrder(
            symbol="M99", quantity=Decimal("5"),
            execution=LimitExecution(limit_price=Decimal("100.00")), sleeve="mechanical",
        )
    )
    assert not refill.is_approved
    assert refill.code is RejectionCode.SLEEVE_ALLOCATION_EXCEEDED
    # Closes are untouched: a time exit still passes.
    close = gate.submit(
        EquitySellToCloseOrder(
            symbol="M00", quantity=Decimal("8"),
            execution=LimitExecution(limit_price=Decimal("99.00")), sleeve="mechanical",
        )
    )
    assert close.is_approved


# ================================================================================
# Attribution: its own line, out of every alpha calculation
# ================================================================================


def test_the_baseline_never_reaches_a_class_the_funnel_or_the_budget(
    tmp_path, limits, signals_config, research_config
):
    from audit.attribution import build_attribution
    from forward import funnel_entries

    started, prices, clock, broker = _built(
        tmp_path, limits, signals_config, research_config
    )
    report = build_attribution(
        started.audit.trails(), generated_at=NOW, price_on=lambda s, d: Decimal("510")
    )
    assert report.by_class == {}
    assert report.baseline is not None
    assert report.baseline.symbol == "SPY" and report.baseline.open_units == Decimal("60")
    assert report.baseline.pnl == Decimal("600.00")  # 60 x (510 - 500)
    assert "EXCLUDED from every alpha line" in report.baseline.summary()
    assert report.total_pnl == ZERO  # not a cent of beta in the judged total
    assert "HEADLINE ALPHA: not computable" in report.render()
    assert funnel_entries(started.audit.records()) == []
    day = started.audit.decisions()[0].recorded_at.date()
    assert started.audit.research_passes_on(day) == 0
    assert "baseline_sleeve" not in started.audit.research_passes_by_source_on(day)


def test_the_headline_alpha_is_the_judged_return_minus_book_beta_times_spy():
    from audit.attribution import (
        AttributionReport,
        ClassAttribution,
    )
    from signals import SignalClass

    judged = ClassAttribution(
        signal_class=SignalClass.CLASS_1_REALTIME,
        decisions=4, approved=4, rejected=0, resolved=4, wins=3,
        realised_pnl=Decimal("600"), manipulation_flags=0,
        deployed=Decimal("10000"), benchmark_return_pct=Decimal("4.00"),
    )
    report = AttributionReport(
        generated_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        window_days=90,
        window_start=datetime(2026, 6, 23, tzinfo=timezone.utc),
        by_class={SignalClass.CLASS_1_REALTIME: judged},
        benchmark_return_pct=Decimal("4.00"),
        position_betas=(("NUE", Decimal("1.20"), Decimal("5000")),),
        book_beta=Decimal("1.20"),
    )
    # 6.00% judged return - 1.20 x 4.00% = +1.20%.
    assert report.judged_return_pct == Decimal("6.00")
    assert report.headline_alpha_pct == Decimal("1.20")
    rendered = report.render().splitlines()
    assert rendered[2].startswith("HEADLINE ALPHA: beta-adjusted excess +1.20%")
    assert "baseline sleeve's beta is excluded" in rendered[2]
