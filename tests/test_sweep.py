"""The idle-cash yield sweep (human ruling 2026-09-02).

The claims: the swept ETF is NEVER buying power and the gate's cash model is
untouched (every sweep buy reserves cash like any order); the cash_management
sleeve is exempt from the alpha caps but not from cash-securing or the kill
switch's halt on opens; unsweep sells stay permitted while halted; the sweeper
defends the ruled buffer and never churns below the minimum notional; lots
survive restarts; sweeps never leak into signal-class attribution, the funnel,
the research-pass budget, or the source caps; and the tax flag on outcomes
reads the long-term boundary correctly.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from audit.records import ExitReason, long_term_boundary
from execution.base import BrokerPosition
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from orchestrator import start
from risk_gate.gate import RiskGate
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import AccountState, Sleeve

import pytest

from test_exits import MutablePrices, RoutingLLM, restart_kwargs
from test_orchestrator import (
    FakeBroker,
    FakeClock,
    build,
    counter,
    feed,
)

ZERO = Decimal("0")


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def limits():
    from risk_gate import RiskLimits

    return RiskLimits.load()


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    from research.config import ResearchConfig

    return ResearchConfig.load()


def sgov_buy(quantity="800", price="100.40"):
    return EquityBuyOrder(
        symbol="SGOV",
        quantity=Decimal(quantity),
        execution=LimitExecution(limit_price=Decimal(price)),
        sleeve="cash_management",
    )


def quiet_build(tmp_path, limits, signals_config, research_config, **kwargs):
    """A session with no signals: only the sweeper has anything to do."""
    prices = kwargs.pop("prices", None) or MutablePrices(SGOV="100.40")
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
# The gate: cash-secured yes, alpha caps no
# ================================================================================


def test_a_cash_management_buy_skips_alpha_caps_but_never_cash_securing(limits):
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        FakeClock(),
    )
    # $80,320 of SGOV: far beyond the 7% single-position cap, the 15% daily
    # deployment, and the sector cap — none of which exist for parked cash.
    decision = gate.submit(sgov_buy())
    assert decision.is_approved
    assert gate.state.reserved_cash == Decimal("80320.00")  # cash-secured
    assert gate.state.deployed_today == ZERO  # no sleeve budget consumed
    assert gate.state.mechanical_deployed_today == ZERO

    # And the never-negative constraint is exactly as binding as anywhere else.
    over = gate.submit(sgov_buy(quantity="400"))
    assert not over.is_approved
    assert str(over.code) == "insufficient_buying_power"


def test_the_kill_switch_halts_sweep_buys_and_permits_unsweeps(limits):
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        FakeClock(),
    )
    approved = gate.submit(sgov_buy(quantity="100"))
    gate.record_fill(approved, Decimal("100.40"))
    gate.state.kill_switch_tripped = True

    blocked = gate.submit(sgov_buy(quantity="10"))
    assert not blocked.is_approved
    assert str(blocked.code) == "kill_switch_active"

    sell = gate.submit(
        EquitySellToCloseOrder(
            symbol="SGOV",
            quantity=Decimal("100"),
            execution=LimitExecution(limit_price=Decimal("100.30")),
            sleeve="cash_management",
        )
    )
    assert sell.is_approved  # risk-reducing: cash stays reachable in a halt


def test_a_judged_exit_can_never_touch_parked_shares(limits):
    """Positions key on (sleeve, symbol): an equity-sleeve sell of SGOV finds
    no position even while the cash sleeve holds plenty."""
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        FakeClock(),
    )
    gate.record_fill(gate.submit(sgov_buy(quantity="100")), Decimal("100.40"))
    rogue = gate.submit(
        EquitySellToCloseOrder(
            symbol="SGOV",
            quantity=Decimal("100"),
            execution=LimitExecution(limit_price=Decimal("100.30")),
            sleeve="equity",
        )
    )
    assert not rogue.is_approved
    assert str(rogue.code) == "position_not_held"


# ================================================================================
# The sweeper: buffer arithmetic, end to end
# ================================================================================


def test_idle_cash_above_the_buffer_sweeps_into_the_etf(
    tmp_path, limits, signals_config, research_config
):
    broker = FakeBroker()
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config, broker=broker
    )
    sweeper = started.loop.sweeper
    assert sweeper is not None
    # Buffer on a quiet $100K NAV: 75000 x 0.15 + 25000 x 0.15 + 0 + 2500.
    assert sweeper.buffer() == Decimal("17500.00")

    report = started.loop.tick()
    assert report.sweep_orders == 1
    payload = broker.payloads[-1]
    assert payload["symbol"] == "SGOV"

    report = started.loop.tick()  # the buy settles
    position = started.gate.state.position(("cash_management", "SGOV"))
    assert position is not None
    # 82500 of excess at a 100.40 limit -> 821 whole shares on this venue.
    assert position.quantity == Decimal("821")
    assert len(sweeper.lots) == 1
    # NAV is unchanged by parking: cash became ETF at cost.
    assert started.gate.state.nav == Decimal("100000")
    # And the trail exists: strategy cash_sweep, no research, sealed nothing.
    trail = next(
        t
        for t in started.audit.trails()
        if t.decision.sizing.strategy == "cash_sweep"
    )
    assert trail.decision.research is None
    assert trail.decision.signal.external_id is None


def test_the_sweeper_never_churns_below_the_minimum(
    tmp_path, limits, signals_config, research_config
):
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config
    )
    started.loop.tick()
    started.loop.tick()  # settle
    # Residual excess after the whole-share round-down is < min_order_notional.
    assert started.loop.tick().sweep_orders == 0
    assert started.loop.tick().sweep_orders == 0


def test_cash_below_the_buffer_unsweeps_and_a_flat_lot_resolves(
    tmp_path, limits, signals_config, research_config
):
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config
    )
    started.loop.tick()
    started.loop.tick()  # sweep settled: cash ~= buffer
    sweeper = started.loop.sweeper
    lot = sweeper.lots[0]

    # A withdrawal-shaped hole: cash drops far below the buffer.
    started.gate.state.cash -= Decimal("90000")
    report = started.loop.tick()
    assert report.sweep_orders == 1
    started.loop.tick()  # the sell settles

    trail = started.audit.trail(lot.decision_id)
    assert trail.exits[-1].reason is ExitReason.CASH_UNSWEEP
    assert trail.exits[-1].submitted is True
    assert [f for f in trail.fills if f.side == "sell"]
    # The buffer is defended EXACTLY, not lot-flattened: the sell restored
    # cash to the (NAV-scaled) buffer and the remainder stays parked.
    assert sweeper.lots and sweeper.lots[0].quantity > 0
    assert started.gate.state.cash >= sweeper.buffer()

    # Drain again until the lot goes flat: the outcome resolves, and its
    # realised P&L is captured yield — SHORT-term by the boundary rule.
    started.gate.state.cash -= Decimal("8000")
    started.loop.tick()
    started.loop.tick()
    trail = started.audit.trail(lot.decision_id)
    assert trail.outcome is not None
    assert trail.outcome.long_term is False
    assert sweeper.lots == ()


def test_a_halt_pauses_sweeping_without_a_record_per_tick(
    tmp_path, limits, signals_config, research_config
):
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config
    )
    started.gate.state.kill_switch_tripped = True
    records_before = len(list(started.audit.records()))
    assert started.loop.tick().sweep_orders == 0
    assert started.loop.tick().sweep_orders == 0
    assert len(list(started.audit.records())) == records_before


def test_swept_lots_survive_a_restart_in_their_own_sleeve(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock()
    first, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config, clock=clock
    )
    first.loop.tick()
    first.loop.tick()  # settle: 821 shares at 100.40
    first.loop.shutdown()

    restarted = start(
        fetcher=feed(),
        prices=MutablePrices(SGOV="100.40"),
        llm_client=RoutingLLM(),
        adapter=FakeBroker(
            cash=Decimal("17571.60"),
            positions=[
                BrokerPosition(
                    "SGOV", Decimal("821"), Decimal("82428.40"), Decimal("82428.40")
                )
            ],
        ),
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    position = restarted.gate.state.position(("cash_management", "SGOV"))
    assert position is not None and position.quantity == Decimal("821")
    assert position.sleeve is Sleeve.CASH_MANAGEMENT
    # The judged sleeve holds none of it: no unmanaged-exposure warning, no
    # phantom equity position.
    assert restarted.gate.state.position(("equity", "SGOV")) is None
    assert len(restarted.loop.sweeper.lots) == 1
    # The exit engine never tracks the parked lot (2026-09-03: it used to fall
    # through replay, look itself up under the JUDGED key, and log a false
    # "broker does not hold" warning every startup).
    assert restarted.exits.tracked == ()


def test_the_exit_engine_ignores_sweep_lots_and_health_shows_parked_cash(
    tmp_path, limits, signals_config, research_config, caplog
):
    import logging as _logging

    from orchestrator.ops import RunLog, health_report

    clock = FakeClock()
    first, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config, clock=clock
    )
    first.loop.tick()
    first.loop.tick()  # settle: 821 shares at 100.40
    first.loop.shutdown()

    with caplog.at_level(_logging.WARNING, logger="orchestrator.exits"):
        restarted = start(
            fetcher=feed(),
            prices=MutablePrices(SGOV="100.45"),  # 821 x 0.05 = 41.05 accrued
            llm_client=RoutingLLM(),
            adapter=FakeBroker(
                cash=Decimal("17571.60"),
                positions=[
                    BrokerPosition(
                        "SGOV", Decimal("821"), Decimal("82469.45"), Decimal("82428.40")
                    )
                ],
            ),
            id_factory=counter("b"),
            **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
        )
    assert "broker does not" not in caplog.text
    assert restarted.exits.tracked == ()

    report = health_report(
        restarted.preflight, restarted.exits.tracked, RunLog(tmp_path / "run.log")
    )
    assert "cash management (SGOV): 821 units parked" in report
    assert "accrued +41.05" in report  # 82469.45 value - 82428.40 cost
    assert "log agrees" in report


# ================================================================================
# Sweeps leak into nothing
# ================================================================================


def test_sweeps_never_reach_class_attribution_funnel_or_registry(
    tmp_path, limits, signals_config, research_config
):
    from audit.attribution import build_attribution
    from forward import funnel_entries
    from test_orchestrator import NOW

    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config
    )
    started.loop.tick()
    started.loop.tick()

    report = build_attribution(started.audit.trails(), generated_at=NOW)
    assert report.by_class == {}  # no signal class saw the sweep
    assert report.cash_management is not None
    assert "cash management (SGOV)" in report.cash_management.summary()
    assert funnel_entries(started.audit.records()) == []


def test_sweeps_and_mechanical_entries_replay_as_zero_research_passes(
    tmp_path, limits, signals_config, research_config
):
    """The defect fix (2026-09-02): no-LLM decisions were replaying as spent
    passes and consuming the judged source caps on every restart."""
    from test_hardening import congressional_feed, disclosure_item
    from test_mechanical import quiet_llm

    started = build(
        tmp_path,
        limits,
        signals_config,
        research_config,
        fetcher=congressional_feed(
            disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")
        ),
        llm=quiet_llm(),
        prices=MutablePrices(NUE="140.00", SGOV="100.40"),
        clock=FakeClock(),
    )
    report = started.loop.tick()
    assert report.mechanical_entries == 1
    assert report.sweep_orders == 1
    day = started.audit.decisions()[0].recorded_at.date()

    # One judged research pass ran (the disclosure); the mechanical entry and
    # the sweep add NOTHING to the replayed budget or the source counts.
    assert started.audit.research_passes_on(day) == 1
    by_source = started.audit.research_passes_by_source_on(day)
    assert by_source.get("congressional_disclosures") == 1
    assert "cash_management" not in by_source


# ================================================================================
# The tax boundary helper
# ================================================================================


def test_the_long_term_boundary_is_anniversary_plus_one_day():
    assert long_term_boundary(date(2026, 9, 2)) == date(2027, 9, 3)
    # Leap-day acquisitions land on Mar 1 + 1: never a day early.
    assert long_term_boundary(date(2028, 2, 29)) == date(2029, 3, 2)
    # A 365-day hold is short; 367 from a non-leap start clears the boundary.
    acquired = date(2026, 9, 2)
    assert acquired + timedelta(days=365) < long_term_boundary(acquired)
    assert acquired + timedelta(days=367) >= long_term_boundary(acquired)
