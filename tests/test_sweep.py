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
from decimal import ROUND_UP, Decimal

from audit.records import ExitReason, RejectedStage, long_term_boundary
from execution.base import BrokerPosition, OrderStatus
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


class TwoSidedPrices(MutablePrices):
    """Ask from the table, bid one cent under: a cent-wide T-bill ETF."""

    def bid(self, symbol):
        ask = self.table.get(symbol)
        return None if ask is None else ask - Decimal("0.01")


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
    # Buffer on a quiet $100K NAV: 55000 x 0.25 + 15000 x 0.15 + 0 + 2500
    # (55/15/30/0 allocation, rulings 2026-09-18/21; judged daily deployment 0.25).
    # No SPY quote in this session, so the baseline sleeve owes nothing yet.
    assert sweeper.buffer() == Decimal("18500.00")

    report = started.loop.tick()
    assert report.sweep_orders == 1
    payload = broker.payloads[-1]
    assert payload["symbol"] == "SGOV"

    report = started.loop.tick()  # the buy settles
    position = started.gate.state.position(("cash_management", "SGOV"))
    assert position is not None
    # 81500 of excess at a 100.40 limit -> 811 whole shares on this venue.
    assert position.quantity == Decimal("811")
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
    first.loop.tick()  # settle: 811 shares at 100.40
    first.loop.shutdown()

    restarted = start(
        fetcher=feed(),
        prices=MutablePrices(SGOV="100.40"),
        llm_client=RoutingLLM(),
        adapter=FakeBroker(
            cash=Decimal("18575.60"),
            positions=[
                BrokerPosition(
                    "SGOV", Decimal("811"), Decimal("81424.40"), Decimal("81424.40")
                )
            ],
        ),
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    position = restarted.gate.state.position(("cash_management", "SGOV"))
    assert position is not None and position.quantity == Decimal("811")
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
    first.loop.tick()  # settle: 811 shares at 100.40
    first.loop.shutdown()

    with caplog.at_level(_logging.WARNING, logger="orchestrator.exits"):
        restarted = start(
            fetcher=feed(),
            prices=MutablePrices(SGOV="100.45"),  # 811 x 0.05 = 40.55 accrued
            llm_client=RoutingLLM(),
            adapter=FakeBroker(
                cash=Decimal("18575.60"),
                positions=[
                    BrokerPosition(
                        "SGOV", Decimal("811"), Decimal("81464.95"), Decimal("81424.40")
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
    assert "cash management (SGOV): 811 units parked" in report
    assert "accrued +40.55" in report  # 81464.95 value - 81424.40 cost
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
            disclosure_item("row-1", "NUE", "$100,001 - $250,000", "2026-08-17")
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


# ================================================================================
# The unsweep incident (2026-09-04/08): sizing, pricing side, release records
# ================================================================================


def _withdraw(started, amount):
    """A hole in cash below the buffer, as five mechanical entries opened one."""
    started.gate.state.cash -= Decimal(amount)


def test_an_unsweep_sizes_to_the_deficit_and_limits_at_the_bid(
    tmp_path, limits, signals_config, research_config
):
    """A $4,046 hole in cash is a ~$3.4K deficit once the NAV-scaled buffer
    moves with it (the buffer carries 0.25 x 0.55 + 0.15 x 0.15 of every NAV
    dollar under the 2026-09-21 weights): 34 whole units at a 100.47 bid on
    this venue, rounded UP so the fill covers the deficit, priced at the BID so
    it prints."""
    broker = FakeBroker()
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config,
        broker=broker, prices=TwoSidedPrices(SGOV="100.48"),
    )
    started.loop.tick()
    started.loop.tick()  # the sweep buy (at the 100.48 ask) settles
    sweeper = started.loop.sweeper
    assert broker.payloads[-1]["limit_price"] == Decimal("100.48")

    _withdraw(started, "4046.29")
    deficit = sweeper.buffer() - started.gate.state.cash
    assert deficit > 0
    assert started.loop.tick().sweep_orders == 1
    payload = broker.payloads[-1]
    assert payload["limit_price"] == Decimal("100.47")  # the bid, not the ask
    assert payload["qty"] == (deficit / Decimal("100.47")).quantize(
        Decimal("1"), rounding=ROUND_UP
    )
    assert payload["qty"] == Decimal("34")
    assert payload["qty"] * payload["limit_price"] >= deficit

    started.loop.tick()  # the sell settles
    assert started.gate.state.cash >= sweeper.buffer()
    trail = started.audit.trail(sweeper.lots[0].decision_id)
    assert [f.side for f in trail.fills] == ["buy", "sell"]
    assert trail.fills[-1].filled_quantity == Decimal("34")


def test_without_a_bid_the_unsweep_limits_a_cent_under_the_ask(
    tmp_path, limits, signals_config, research_config
):
    broker = FakeBroker()
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config,
        broker=broker, prices=MutablePrices(SGOV="100.48"),
    )
    started.loop.tick()
    started.loop.tick()
    _withdraw(started, "4046.29")
    assert started.loop.tick().sweep_orders == 1
    assert broker.payloads[-1]["limit_price"] == Decimal("100.47")


def test_a_zero_quantity_order_is_unrepresentable():
    """The schema, not the sweeper's arithmetic, is what makes "sell 0 SGOV"
    impossible: ShareQuantity is strictly positive and no finer than 1e-9."""
    from pydantic import ValidationError

    for bad in ("0", "0.000000000", "-1", "0.0000000001"):
        with pytest.raises(ValidationError):
            EquitySellToCloseOrder(
                symbol="SGOV",
                quantity=Decimal(bad),
                execution=LimitExecution(limit_price=Decimal("100.47")),
                sleeve="cash_management",
            )
        with pytest.raises(ValidationError):
            sgov_buy(quantity=bad)


def test_an_unsweep_cancelled_unfilled_at_the_close_is_released_and_retried(
    tmp_path, limits, signals_config, research_config
):
    """What happened live: the venue cancelled the day sell at 16:00 with
    nothing filled. The release is written down, the pending row disappears,
    and the same pass retries at the bid."""
    from forward import funnel_entries
    from orchestrator.recovery import pending_settlement

    broker = FakeBroker()
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config,
        broker=broker, prices=TwoSidedPrices(SGOV="100.48"),
    )
    started.loop.tick()
    started.loop.tick()
    sweeper = started.loop.sweeper
    lot_id = sweeper.lots[0].decision_id

    broker.fill = "new"  # the sell will rest
    _withdraw(started, "4046.29")
    assert started.loop.tick().sweep_orders == 1
    exit_id = broker.submitted[-1].broker_order_id
    asked = broker.payloads[-1]["qty"]
    assert asked == Decimal("34")
    # While it works, health shows the quantity the order ASKED for, never 0.
    pending = [p for p in pending_settlement(started.audit) if p.side == "sell"]
    assert [(p.quantity, p.broker_order_id) for p in pending] == [(asked, exit_id)]
    assert f"sell 34 SGOV (cash_management sleeve) order {exit_id}" in pending[0].describe()

    broker.set_status(exit_id, OrderStatus(exit_id, "canceled", ZERO, None))
    broker.fill = "filled"
    report = started.loop.tick()  # settles the cancel, retries in the same pass
    assert report.sweep_orders == 1
    assert broker.payloads[-1]["limit_price"] == Decimal("100.47")

    trail = started.audit.trail(lot_id)
    release = [r for r in trail.stage_rejections if r.broker_order_id == exit_id]
    assert len(release) == 1
    assert release[0].stage is RejectedStage.EXECUTION
    assert release[0].code == "canceled"
    # The cancelled attempt is gone from pending; the retry is pending until it settles.
    still = [p.broker_order_id for p in pending_settlement(started.audit) if p.side == "sell"]
    assert exit_id not in still and len(still) == 1
    assert sweeper.lots[0].quantity == Decimal("811")  # nothing sold, still held
    assert started.gate.state.reserved_cash == ZERO  # nothing left reserved

    started.loop.tick()  # the retry settles
    assert started.gate.state.cash >= sweeper.buffer()
    assert [p for p in pending_settlement(started.audit) if p.side == "sell"] == []
    assert len(started.audit.trail(lot_id).exits) == 2  # two attempts, both recorded
    # The release record is an order's fate, not a signal: it reaches neither
    # the funnel nor the convergence registry.
    assert funnel_entries(started.audit.records()) == []


def test_startup_recovery_clears_an_unsweep_the_venue_cancelled(
    tmp_path, limits, signals_config, research_config
):
    """The droplet's state on 2026-09-08: two unsweep exits submitted, both
    cancelled unfilled at the close, the process gone before it noticed. The
    next startup asks the venue, writes the releases, and health is clean."""
    from orchestrator.recovery import pending_settlement

    clock = FakeClock()
    broker = FakeBroker()
    first, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config,
        broker=broker, prices=TwoSidedPrices(SGOV="100.48"), clock=clock,
    )
    first.loop.tick()
    first.loop.tick()
    lot_id = first.loop.sweeper.lots[0].decision_id
    broker.fill = "new"
    _withdraw(first, "4046.29")
    first.loop.tick()
    exit_id = broker.submitted[-1].broker_order_id
    first.loop.sweeper._working.clear()  # the process died holding the order
    assert [p.side for p in pending_settlement(first.audit)] == ["sell"]

    venue = FakeBroker(
        cash=Decimal("14464.43"),
        positions=[
            BrokerPosition(
                "SGOV", Decimal("811"), Decimal("81489.28"), Decimal("81489.28")
            )
        ],
    )
    venue.set_status(exit_id, OrderStatus(exit_id, "canceled", ZERO, None))
    restarted = start(
        fetcher=feed(),
        prices=TwoSidedPrices(SGOV="100.48"),
        llm_client=RoutingLLM(),
        adapter=venue,
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    trail = restarted.audit.trail(lot_id)
    release = [r for r in trail.stage_rejections if r.broker_order_id == exit_id]
    assert len(release) == 1 and "recovered at startup" in release[0].message
    assert pending_settlement(restarted.audit) == []
    assert restarted.loop.sweeper.lots[0].quantity == Decimal("811")
    # And the deficit is still there, so the first pass retries — at the bid.
    assert restarted.loop.tick().sweep_orders == 1
    assert venue.payloads[-1]["limit_price"] == Decimal("100.47")


def test_a_sweep_buy_is_named_so_recovery_can_ask_about_it(
    tmp_path, limits, signals_config, research_config
):
    broker = FakeBroker()
    started, _, _ = quiet_build(
        tmp_path, limits, signals_config, research_config, broker=broker
    )
    started.loop.tick()
    trail = next(
        t for t in started.audit.trails() if t.decision.sizing.strategy == "cash_sweep"
    )
    assert trail.decision.decision_id in broker.by_client_reference
