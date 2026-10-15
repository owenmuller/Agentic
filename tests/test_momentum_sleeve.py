"""The momentum sleeve's in-session engine (human ruling 2026-10-13).

The claims: a plan that breaks the sleeve's caps is refused whole; nothing
trades before the plan's day or outside 10:00-15:30 ET; a rebalance sells
first and buys in rank order, one working order at a time, through the gate,
every buy a ``strategy="momentum"`` DecisionRecord; a name that left the book
is sold with a ``momentum_rebalance`` exit and its lot resolves; entries off
or a tripped kill switch stop the buys and never the sells; a stale order is
cancelled and re-priced; a short-funded month closes after its sessions and
says so; a stale plan is skipped, not chased; lots and the month survive a
restart in their own sleeve.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from audit.log import AuditLog
from audit.records import ExitReason
from execution.base import BrokerPosition
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from orchestrator.config import MomentumSleeveConfig
from orchestrator.momentum_sleeve import MomentumSleeve, PlanError, load_plan, plan_path
from orchestrator.sweep import momentum_spendable
from risk_gate import RiskGate, RiskLimits
from risk_gate.state import AccountState, Sleeve

from test_exits import MutablePrices
from test_orchestrator import FakeBroker, FakeClock, counter

D = Decimal
# Monday 2 November 2026, 10:30 New York (EST, UTC-5): the first trading day.
OPEN = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)
TENTH = D("0.1")


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="module")
def limits():
    from config_overrides import step_one_limits

    base = step_one_limits()  # the engine mechanics are sized on a 15% sleeve (step 2: test_momentum_gate)
    caps = base.momentum_sleeve.model_copy(update={"entries_enabled": True})
    return base.model_copy(update={"momentum_sleeve": caps})


def write_plan(directory, month="2026-11", day="2026-11-02", targets=None, **extra):
    targets = targets if targets is not None else [("AAPL", "0.1", 1), ("MSFT", "0.1", 2)]
    payload = {
        "version": 1,
        "month": month,
        "rebalance_date": day,
        "data_through": "2026-10-30",
        "computed_at": "2026-10-31T00:00:00+00:00",
        "buys_allowed": True,
        "exposure": "1.0",
        "targets": [{"symbol": s, "weight": w, "rank": r, "sector": "x"} for s, w, r in targets],
    }
    payload.update(extra)
    directory.mkdir(parents=True, exist_ok=True)
    path = plan_path(directory, month)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class Rig:
    def __init__(self, tmp_path, limits, *, cash="100000", clock=None, broker=None, gate=None,
                 audit=None, spendable=None, config=None, ids="m"):
        self.clock = clock or FakeClock(OPEN)
        self.limits = limits
        self.gate = gate or RiskGate(
            limits, AccountState(cash=D(cash), high_water_mark=D(cash)), self.clock
        )
        self.broker = broker or FakeBroker()
        self.audit = audit or AuditLog(tmp_path / "audit.jsonl", clock=self.clock, id_factory=counter(ids))
        self.prices = MutablePrices(AAPL="200.00", MSFT="400.00", NVDA="100.00")
        self.notes: list[str] = []
        self.plans = tmp_path / "momentum"
        self.engine = MomentumSleeve(
            gate=self.gate,
            adapter=self.broker,
            audit=self.audit,
            prices=self.prices,
            limits=limits.momentum_sleeve,
            config=config or MomentumSleeveConfig(),
            plan_dir=self.plans,
            state_path=tmp_path / "momentum_state.json",
            spendable=spendable or (lambda: momentum_spendable(self.gate, limits.cash_management)),
            clock=self.clock,
            id_factory=counter(ids),
            note=self.notes.append,
        )

    def run(self, ticks=12, step=timedelta(seconds=30)):
        placed = 0
        for _ in range(ticks):
            placed += self.engine.tick(self.clock())
            self.clock.advance(seconds=step.total_seconds())
        return placed

    def held(self, symbol):
        position = self.gate.state.position(("momentum", symbol))
        return position.quantity if position is not None else D(0)


# ================================================================================
# The plan file
# ================================================================================


def test_a_valid_plan_parses(tmp_path, limits):
    plan = load_plan(write_plan(tmp_path), limits.momentum_sleeve)
    assert [t.symbol for t in plan.targets] == ["AAPL", "MSFT"]
    assert plan.weight("AAPL") == TENTH and plan.weight("NVDA") == 0


@pytest.mark.parametrize(
    "targets, extra",
    [
        ([("AAPL", "0.11", 1)], {}),                                      # past the 10% cap
        ([(f"N{i}", "0.05", i) for i in range(16)], {}),                  # 16 names
        ([("AAPL", "0.05", 1), ("AAPL", "0.05", 2)], {}),                 # duplicate
        ([("AAPL", "0", 1)], {}),                                         # zero weight
        ([("AAPL", "0.1", 1)], {"version": 2}),
        ([("AAPL", "0.1", 1)], {"rebalance_date": "2026-12-01"}),         # not in its month
        ([("AAPL", "0.1", 1)], {"exposure": "1.2"}),
    ],
)
def test_a_plan_that_breaks_a_cap_is_refused_whole(tmp_path, limits, targets, extra):
    path = write_plan(tmp_path, targets=targets)
    raw = json.loads(path.read_text())
    raw.update(extra)
    path.write_text(json.dumps(raw))
    with pytest.raises(PlanError):
        load_plan(path, limits.momentum_sleeve)


# ================================================================================
# When it trades
# ================================================================================


def test_nothing_trades_before_ten_or_before_the_plan_day(tmp_path, limits):
    rig = Rig(tmp_path, limits, clock=FakeClock(OPEN - timedelta(minutes=45)))  # 09:45 ET
    write_plan(rig.plans)
    assert rig.engine.tick(rig.clock()) == 0
    early = Rig(tmp_path / "b", limits, clock=FakeClock(OPEN))
    write_plan(early.plans, day="2026-11-03")
    assert early.engine.tick(early.clock()) == 0 and early.broker.submitted == []


def test_the_first_build_buys_in_rank_order_through_the_gate(tmp_path, limits):
    rig = Rig(tmp_path, limits)
    write_plan(rig.plans, targets=[("MSFT", "0.1", 2), ("AAPL", "0.1", 1)])
    assert rig.engine.tick(rig.clock()) == 1
    assert rig.broker.payloads[0]["symbol"] == "AAPL"  # rank 1 first
    assert rig.engine.tick(rig.clock()) == 1           # first settled, then the next
    assert rig.broker.payloads[1]["symbol"] == "MSFT"
    rig.run(4)
    # sleeve NAV 15,000: 10% = 1,500 each at a 0.2% marketable buffer
    assert rig.held("AAPL") == 7 and rig.held("MSFT") == 3
    assert rig.gate.state.momentum_deployed_today > 0
    decisions = [d for d in rig.audit.decisions() if d.sizing.strategy == "momentum"]
    assert len(decisions) == 2 and all(d.sizing.sleeve == "momentum" and d.research is None for d in decisions)
    state = rig.engine.month_state
    assert state.month == "2026-11" and state.done and "complete" in state.note
    assert json.loads((tmp_path / "momentum_state.json").read_text())["done"] is True
    # done is done: a later tick that day places nothing
    assert rig.run(3) == 0


def test_next_month_sells_first_then_buys_and_the_lot_resolves(tmp_path, limits):
    rig = Rig(tmp_path, limits)
    write_plan(rig.plans)
    rig.run(6)
    assert rig.held("AAPL") == 7 and rig.held("MSFT") == 3
    rig.clock.now = datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc)
    write_plan(rig.plans, month="2026-12", day="2026-12-01",
               targets=[("MSFT", "0.1", 1), ("NVDA", "0.1", 2)], sold={"AAPL": "fell to rank 41, past 30"})
    assert rig.engine.tick(rig.clock()) == 1
    assert rig.broker.payloads[-1]["symbol"] == "AAPL"  # the sell goes first
    rig.run(6)
    assert rig.held("AAPL") == 0 and rig.held("NVDA") == 14 and rig.held("MSFT") == 3
    aapl = next(t for t in rig.audit.trails() if (t.decision.gate.order or {}).get("symbol") == "AAPL")
    assert aapl.exits[-1].reason is ExitReason.MOMENTUM_REBALANCE
    assert "fell to rank 41" in aapl.exits[-1].detail
    assert aapl.outcome is not None
    assert rig.engine.month_state.month == "2026-12" and rig.engine.month_state.done


def test_entries_off_or_the_kill_switch_stop_buys_never_sells(tmp_path, limits):
    rig = Rig(tmp_path, limits)
    write_plan(rig.plans)
    rig.run(6)
    rig.gate.state.kill_switch_tripped = True
    rig.clock.now = datetime(2026, 12, 1, 15, 30, tzinfo=timezone.utc)
    write_plan(rig.plans, month="2026-12", day="2026-12-01", targets=[("NVDA", "0.1", 1)])
    rig.run(8)
    assert rig.held("AAPL") == 0 and rig.held("MSFT") == 0  # sold by rule
    assert rig.held("NVDA") == 0                            # never bought
    assert any("kill switch" in note for note in rig.notes)
    assert rig.engine.month_state.done

    off = limits.model_copy(update={"momentum_sleeve": limits.momentum_sleeve.model_copy(
        update={"entries_enabled": False})})
    quiet = Rig(tmp_path / "off", off)
    write_plan(quiet.plans)
    quiet.run(4)
    assert quiet.broker.submitted == [] and any("entries switched off" in n for n in quiet.notes)


def test_a_stale_order_is_cancelled_and_repriced(tmp_path, limits):
    broker = FakeBroker(fill=None)  # never fills
    rig = Rig(tmp_path, limits, broker=broker)
    write_plan(rig.plans, targets=[("AAPL", "0.1", 1)])
    assert rig.engine.tick(rig.clock()) == 1
    rig.clock.advance(seconds=60)
    assert rig.engine.tick(rig.clock()) == 0 and broker.cancelled == []
    rig.clock.advance(seconds=61)
    rig.engine.tick(rig.clock())  # past 120 s: cancel requested
    assert broker.cancelled == ["brk-1"]
    assert rig.engine.tick(rig.clock()) == 1  # cancel settled, reservation released, re-priced
    assert rig.gate.state.reserved_cash == D("1402.80")  # one order's worth, not two
    assert any(t.stage_rejections for t in rig.audit.trails())


def test_a_short_funded_month_closes_after_its_sessions_and_says_so(tmp_path, limits, caplog):
    rig = Rig(tmp_path, limits, spendable=lambda: D("0"))
    write_plan(rig.plans)
    for day in (2, 3, 4, 5):
        rig.clock.now = datetime(2026, 11, day, 15, 30, tzinfo=timezone.utc)
        with caplog.at_level(logging.ERROR, logger="orchestrator.momentum_sleeve"):
            rig.run(2)
    state = rig.engine.month_state
    assert state.sessions == ["2026-11-02", "2026-11-03", "2026-11-04"] and state.done
    assert "work left undone" in state.note and "not bought: AAPL, MSFT" in state.note
    assert any("work left undone" in r.message for r in caplog.records)
    assert rig.broker.submitted == []


def test_a_stale_plan_is_skipped_not_chased(tmp_path, limits, caplog):
    rig = Rig(tmp_path, limits, clock=FakeClock(datetime(2026, 11, 10, 15, 30, tzinfo=timezone.utc)))
    write_plan(rig.plans)
    with caplog.at_level(logging.ERROR, logger="orchestrator.momentum_sleeve"):
        assert rig.engine.tick(rig.clock()) == 0
    assert rig.engine.month_state.done and "skipped" in rig.engine.month_state.note
    assert rig.broker.submitted == []


def test_a_missing_plan_is_an_error_once_a_day(tmp_path, limits, caplog):
    rig = Rig(tmp_path, limits)
    with caplog.at_level(logging.ERROR, logger="orchestrator.momentum_sleeve"):
        rig.run(5)
    assert sum("no usable plan" in r.message for r in caplog.records) == 1
    assert not rig.engine.month_state.done


def test_lots_and_the_month_survive_a_restart(tmp_path, limits):
    rig = Rig(tmp_path, limits)
    write_plan(rig.plans)
    rig.run(6)
    # a new process: the gate re-seeded with the sleeve's holdings, lots from the log
    from orchestrator.state import SessionState, seed_account_state

    positions = [BrokerPosition("AAPL", D(7), D("1402.80"), D("1402.80")),
                 BrokerPosition("MSFT", D(3), D("1202.40"), D("1202.40"))]
    account = seed_account_state(
        cash=rig.gate.state.cash, positions=positions, session=SessionState(path=tmp_path / "session_state.json"),
        deployed_today=D(0), today=OPEN.date(),
        momentum_open=rig.audit.strategy_open_positions("momentum"),
    )
    assert account.position(("momentum", "AAPL")).quantity == 7
    assert account.position(("equity", "AAPL")) is None
    gate = RiskGate(limits, account, rig.clock)
    again = Rig(tmp_path, limits, gate=gate, audit=rig.audit, ids="r")
    assert again.engine.replay(rig.audit.trails()) == 2
    assert {lot.symbol for lot in again.engine.lots} == {"AAPL", "MSFT"}
    assert again.engine.month_state.done           # the month is not re-run
    assert again.run(3) == 0


# ================================================================================
# Wired into the session
# ================================================================================


def test_the_session_runs_the_plan_and_a_restart_wakes_it_up_in_its_own_sleeve(tmp_path, limits):
    from signals.config import SignalsConfig
    from research.config import ResearchConfig
    from test_exits import RoutingLLM
    from test_orchestrator import build

    def session(broker, prefix):
        return build(
            tmp_path, limits, SignalsConfig.load(), ResearchConfig.load(),
            fetcher=lambda source: [], llm=RoutingLLM(), broker=broker,
            prices=MutablePrices(AAPL="200.00", MSFT="400.00", SPY="500.00", SGOV="100.40"),
            clock=clock, id_factory=counter(prefix),
        )

    clock = FakeClock(OPEN)
    write_plan(tmp_path / "momentum")
    first = session(FakeBroker(), "a")
    assert first.momentum is not None and first.loop.momentum is first.momentum
    for _ in range(8):
        first.loop.tick()
        clock.advance(seconds=30)
    aapl = first.gate.state.position(("momentum", "AAPL"))
    msft = first.gate.state.position(("momentum", "MSFT"))
    assert aapl.quantity == 7 and msft.quantity == 3
    assert first.momentum.month_state.done
    first.loop.shutdown()

    venue = FakeBroker(
        cash=first.gate.state.cash,
        positions=[BrokerPosition("AAPL", D(7), aapl.cost_basis, aapl.cost_basis),
                   BrokerPosition("MSFT", D(3), msft.cost_basis, msft.cost_basis)],
    )
    again = session(venue, "b")
    assert again.gate.state.position(("momentum", "AAPL")).quantity == 7
    assert again.gate.state.position(("equity", "AAPL")) is None
    assert {lot.symbol for lot in again.momentum.lots} == {"AAPL", "MSFT"}
    # judged exits never see the momentum names
    assert all(p.key[0] != "momentum" for p in again.exits.tracked)
    again.loop.tick()
    assert venue.submitted == [] or all(p["symbol"] not in ("AAPL", "MSFT") for p in venue.payloads)


# ================================================================================
# The order-path round trip (run live in the main paper account)
# ================================================================================


def test_the_round_trip_buys_one_share_and_sells_it_with_a_full_trail(tmp_path, limits):
    from types import SimpleNamespace

    from orchestrator import momentum_roundtrip

    rig = Rig(tmp_path, limits)
    rig.prices.set("F", "11.20")
    checks = SimpleNamespace(gate=rig.gate, audit=rig.audit, adapter=rig.broker)
    result = momentum_roundtrip.run(checks, rig.prices, "F", tmp_path / "rt", clock=rig.clock,
                                    sleep=lambda s: rig.clock.advance(seconds=s), timeout=30, step=1,
                                    id_factory=counter("rt"))
    assert result.passed, result.steps
    assert [p["symbol"] for p in rig.broker.payloads] == ["F", "F"]
    assert [p["qty"] for p in rig.broker.payloads] == [D(1), D(1)]
    assert rig.gate.state.position(("momentum", "F")) is None


def test_the_round_trip_refuses_a_gate_with_entries_off(tmp_path, limits):
    from types import SimpleNamespace

    from orchestrator import momentum_roundtrip

    off = limits.model_copy(update={"momentum_sleeve": limits.momentum_sleeve.model_copy(
        update={"entries_enabled": False})})
    rig = Rig(tmp_path, off)
    result = momentum_roundtrip.run(SimpleNamespace(gate=rig.gate, audit=rig.audit, adapter=rig.broker),
                                    rig.prices, "AAPL", tmp_path / "rt", clock=rig.clock)
    assert not result.passed and rig.broker.submitted == []


def test_a_plan_written_after_ten_is_picked_up_without_a_restart(tmp_path, limits):
    # 2026-10-14: a late catch-up prep must still trade the same day; the
    # engine used to cache "missing" for the whole month at its first look.
    rig = Rig(tmp_path, limits)
    assert rig.run(3) == 0                      # 10:30, no plan yet: nothing
    write_plan(rig.plans)
    assert rig.run(4) >= 1                      # the plan lands: it trades
    assert rig.held("AAPL") == 7


def test_a_rejected_plan_is_re_read_when_the_file_changes(tmp_path, limits, caplog):
    rig = Rig(tmp_path, limits)
    path = write_plan(rig.plans, targets=[("AAPL", "0.11", 1)])  # past the 10% cap: rejected
    with caplog.at_level(logging.ERROR, logger="orchestrator.momentum_sleeve"):
        rig.run(3)
    assert sum("plan rejected" in r.message for r in caplog.records) == 1  # once, not every tick
    assert rig.broker.submitted == []
    import os
    import time
    write_plan(rig.plans)                       # the corrected rerun (--force)
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    rig.run(4)
    assert rig.held("AAPL") == 7
