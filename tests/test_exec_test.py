"""The B execution test (human ruling 2026-10-09 evening).

The SPY noise-area momentum rules run live in the aggressive paper sleeve for
20 sessions to prove the intraday plumbing: entries, trailing stops, the 15:50
close-out, the inverse-ETF leg, the PDT gate. Rule-based, no LLM, its own
bucket outside every alpha line, stops itself.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from audit.attribution import build_attribution
from audit.log import AuditLog
from audit.records import ExitReason
from orchestrator.config import ExecutionTestConfig, OrchestratorConfig
from orchestrator.exec_test import ExecutionTest, bands, decide, sigma_table, by_session
from risk_gate import RiskGate, RiskLimits
from risk_gate.state import AccountState, Sleeve
from test_orchestrator import FakeBroker, FakeClock, counter

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 12)  # a Monday: session 1


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")


class Quotes:
    """Ask and bid per symbol, movable."""

    def __init__(self):
        self.asks, self.bids = {}, {}

    def set(self, symbol, mid, half="0.01"):
        mid, half = Decimal(mid), Decimal(half)
        self.asks[symbol], self.bids[symbol] = mid + half, mid - half

    def __call__(self, symbol):
        return self.asks.get(symbol)

    def bid(self, symbol):
        return self.bids.get(symbol)


def _ts(day, minute):
    return (datetime.combine(day, datetime.min.time(), NY).replace(hour=9, minute=30) + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sessions_before(day, n):
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


class Bars:
    """SIP history where |move from open| at minute k is exactly 0.0001 k and
    every session closes back at its open (so the prior close is 500); today's
    bars are scripted per test. IEX and SIP serve the same bars."""

    def __init__(self, today_spy=None, today_sh=None):
        self.data = {"SPY": [], "SH": []}
        for d in _sessions_before(DAY, 20):
            for k in range(390):
                c = 500 * (1 + 0.0001 * (k + 1)) if k < 389 else 500.0
                self.data["SPY"].append({"t": _ts(d, k), "o": 500.0, "h": c, "l": 500.0, "c": c, "v": 1000, "vw": c})
        for k, c in (today_spy or {}).items():  # the day opens at 500, like every past session
            self.data["SPY"].append({"t": _ts(DAY, k), "o": 500.0 if k == 0 else c, "h": c, "l": c, "c": c, "v": 1000, "vw": c})
        for k, c in (today_sh or {}).items():
            self.data["SH"].append({"t": _ts(DAY, k), "o": c, "h": c, "l": c, "c": c, "v": 1000, "vw": c})
        self.calls = []

    def bars(self, symbol, start, end, feed):
        self.calls.append((symbol, feed))
        lo, hi = start.astimezone(timezone.utc).isoformat()[:16], end.astimezone(timezone.utc).isoformat()[:16]
        return [b for b in self.data.get(symbol, []) if lo <= b["t"][:16] < hi]


def _flat_today(price=500.5, until=390):
    return {k: price for k in range(until)}


def _engine(tmp_path, bars, quotes, clock, cash="100000", **config):
    limits = RiskLimits.load()
    assert limits.aggressive_sleeve.entries_enabled  # ON for the test alone (ruling 2026-10-09 evening)
    gate = RiskGate(limits, AccountState(cash=Decimal(cash), high_water_mark=Decimal(cash)), clock=clock)
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=clock, id_factory=counter("x"))
    broker = FakeBroker(cash=Decimal(cash))
    cfg = ExecutionTestConfig(enabled=True, first_session=DAY, **config)
    engine = ExecutionTest(
        config=cfg, gate=gate, adapter=broker, audit=audit, bars=bars, prices=quotes, bids=quotes.bid,
        clock=clock, id_factory=counter("e"), state_path=tmp_path / "execution_test_state.json",
        daily_path=tmp_path / "execution_test_daily.jsonl", note=lambda m: None,
    )
    return engine, gate, audit, broker


def _at(hh, mm, ss=0, day=DAY):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=NY).astimezone(timezone.utc)


# ---------------------------------------------------------------- the rules


def test_the_shipped_config_runs_twenty_sessions_from_monday_with_attention_off():
    config = OrchestratorConfig.load()
    test = config.execution_test
    assert test.enabled and test.first_session == DAY and test.max_sessions == 20
    assert (test.symbol, test.inverse_symbol, test.risk_fraction, test.close_out) == ("SPY", "SH", Decimal("0.01"), "15:50")
    assert not config.aggressive_sleeve.attention.enabled  # the only aggressive entries are the test's


def test_the_noise_area_is_the_published_one():
    bars = Bars()
    history = [s for d, s in sorted(by_session(bars.data["SPY"]).items())][-14:]
    sigma = sigma_table(history, [30, 60])
    assert sigma[30] == pytest.approx(0.0030)  # the bar ending 10:00 is minute 29: 0.0001 x 30
    ub, lb = bands(500.0, 500.0, sigma[30])
    assert (ub, lb) == (pytest.approx(501.5), pytest.approx(498.5))
    # gap-adjusted: the band widens on the side of the gap
    assert bands(505.0, 500.0, 0.003)[0] == pytest.approx(505.0 * 1.003)
    assert bands(505.0, 500.0, 0.003)[1] == pytest.approx(500.0 * 0.997)


@pytest.mark.parametrize(
    "held, price, vwap, actions",
    [
        (None, 502.0, 500.0, [("enter", "SPY")]),
        (None, 498.0, 500.0, [("enter", "SH")]),
        (None, 500.0, 500.0, []),
        ("SPY", 501.0, 500.0, [("exit", "stop")]),        # under UB, above LB: out
        ("SPY", 501.8, 501.9, [("exit", "stop")]),        # above UB but under VWAP: the trailing stop
        ("SPY", 498.0, 500.0, [("exit", "reversal"), ("enter", "SH")]),
        ("SH", 499.0, 500.0, [("exit", "stop")]),
        ("SH", 502.0, 500.0, [("exit", "reversal"), ("enter", "SPY")]),
        ("SH", 498.0, 500.0, []),
    ],
)
def test_the_rule(held, price, vwap, actions):
    assert decide(held, price, 501.5, 498.5, vwap) == actions


# ------------------------------------------------------------- live plumbing


def test_a_session_enters_stops_takes_the_inverse_leg_and_closes_out_at_1550(tmp_path):
    clock = FakeClock(_at(10, 0, 20))
    bars = Bars(today_spy=_flat_today(500.5), today_sh=_flat_today(40.0))
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    quotes.set("SH", "40.00")
    engine, gate, audit, broker = _engine(tmp_path, bars, quotes, clock)

    # 10:00 - above UB 501.50 (VWAP 500.5): buy SPY, sized by 1% of the sleeve at risk, capped
    assert engine.tick(clock()) == 1
    order = broker.payloads[-1]
    assert order["symbol"] == "SPY"
    nav = gate.sleeve_nav(Sleeve.AGGRESSIVE)
    assert Decimal(order["qty"]) * Decimal("502.01") <= nav * gate.limits.aggressive_sleeve.max_single_position
    clock.advance(seconds=30)
    engine.tick(clock())
    assert engine.held["symbol"] == "SPY"
    assert gate.state.position(("aggressive", "SPY")).quantity == Decimal(order["qty"])
    decision = audit.decisions()[0]
    assert decision.sizing.strategy == "execution_test" and decision.sizing.sleeve == "aggressive"

    # 10:30 - SPY 501.00 is under max(UB 503.00, VWAP): the trailing stop sells it
    clock.now = _at(10, 30, 15)
    quotes.set("SPY", "501.00")
    assert engine.tick(clock()) == 1
    assert broker.payloads[-1]["symbol"] == "SPY"
    clock.advance(seconds=30)
    engine.tick(clock())
    assert engine.held is None
    trail = audit.trail(decision.decision_id)
    assert trail.exits[-1].reason is ExitReason.EXEC_TEST_STOP and trail.outcome is not None
    assert gate.state.day_trades == [DAY]  # a same-day round trip: counted for the PDT gate

    # 11:00 - SPY 495 is under LB 495.5: the inverse leg - BUY SH, never a short
    clock.now = _at(11, 0, 10)
    quotes.set("SPY", "495.00")
    assert engine.tick(clock()) == 1
    assert broker.payloads[-1]["symbol"] == "SH"
    clock.advance(seconds=30)
    engine.tick(clock())
    assert engine.held["symbol"] == "SH"

    # still under the falling mirror stop min(LB, VWAP) at 11:30 (LB 494.00) and
    # 12:00 (LB 492.50): hold the SH
    for hh, mm, spy in ((11, 30, "493.90"), (12, 0, "492.00")):
        clock.now = _at(hh, mm, 5)
        quotes.set("SPY", spy)
        assert engine.tick(clock()) == 0

    # 15:50 - the close-out: out, nothing in
    clock.now = _at(15, 50, 5)
    assert engine.tick(clock()) == 1
    assert broker.payloads[-1]["symbol"] == "SH"
    clock.advance(seconds=30)
    engine.tick(clock())
    assert engine.held is None
    sh_trail = audit.trail(audit.decisions()[-1].decision_id)
    assert sh_trail.exits[-1].reason is ExitReason.EXEC_TEST_CLOSE_OUT
    clock.now = _at(15, 55)
    quotes.set("SPY", "510.00")
    assert engine.tick(clock()) == 0
    assert engine.sessions_run == 1


def test_a_late_decision_is_a_recorded_fault_not_a_trade(tmp_path):
    clock = FakeClock(_at(10, 8))
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    engine, gate, audit, broker = _engine(tmp_path, Bars(today_spy=_flat_today()), quotes, clock)
    assert engine.tick(clock()) == 0
    state = json.loads((tmp_path / "execution_test_state.json").read_text())
    assert any("missed" in f["fault"] for f in state["days"][DAY.isoformat()]["faults"])
    assert broker.payloads == []


def test_it_stops_itself_after_twenty_sessions(tmp_path):
    clock = FakeClock(_at(10, 0, 20))
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    (tmp_path / "execution_test_state.json").write_text(json.dumps({
        "sessions": [(DAY - timedelta(days=40 - i)).isoformat() for i in range(20)],
        "reported": [], "days": {}, "position": None, "complete_noted": False,
    }))
    engine, gate, audit, broker = _engine(tmp_path, Bars(today_spy=_flat_today()), quotes, clock)
    assert not engine.active_on(DAY)
    assert engine.tick(clock()) == 0
    assert broker.payloads == []


def test_nothing_runs_before_the_first_session(tmp_path):
    clock = FakeClock(_at(10, 0, 20, day=DAY - timedelta(days=3)))
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    engine, gate, audit, broker = _engine(tmp_path, Bars(), quotes, clock)
    assert engine.tick(clock()) == 0 and broker.payloads == []


def test_the_test_never_reaches_a_class_the_funnel_or_the_research_budget(tmp_path):
    from forward import funnel_entries

    clock = FakeClock(_at(10, 0, 20))
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    engine, gate, audit, broker = _engine(tmp_path, Bars(today_spy=_flat_today()), quotes, clock)
    engine.tick(clock())
    clock.advance(seconds=30)
    engine.tick(clock())
    assert audit.research_passes_on(DAY) == 0
    assert funnel_entries(audit.records()) == []
    report = build_attribution(audit.trails(), generated_at=clock() + timedelta(days=1))
    assert report.by_class == {}
    assert report.execution_test is not None and report.execution_test.entries == 1
    assert "EXECUTION TEST" in report.render() and "not a strategy" in report.render()


def test_the_morning_report_sets_live_fills_against_the_model(tmp_path):
    clock = FakeClock(_at(10, 0, 20))
    today = _flat_today(500.5)
    today[29] = 502.0  # the SIP bar ending 10:00: the model's fill price
    bars = Bars(today_spy=today)
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    engine, gate, audit, broker = _engine(tmp_path, bars, quotes, clock)
    engine.tick(clock())
    clock.advance(seconds=30)
    engine.tick(clock())
    clock.now = _at(15, 50, 5)
    engine.tick(clock())
    clock.advance(seconds=30)
    engine.tick(clock())
    # the next morning, after 9:35
    next_day = DAY + timedelta(days=1)
    clock.now = _at(9, 40, day=next_day)
    engine.tick(clock())
    line = json.loads((tmp_path / "execution_test_daily.jsonl").read_text().splitlines()[0])
    assert line["day"] == DAY.isoformat() and line["session"] == 1
    assert line["slippage_bp_vs_model"]["entry"]["n"] == 1
    # bought at the 502.01 ask + 0.1% marketable buffer vs the model's 502.00 close
    assert line["slippage_bp_vs_model"]["entry"]["mean"] > 0
    assert "pnl_live" in line and "pnl_modelled_at_live_sizes" in line


def test_a_restart_drops_a_position_the_gate_does_not_hold(tmp_path):
    clock = FakeClock(_at(11, 0))
    (tmp_path / "execution_test_state.json").write_text(json.dumps({
        "sessions": [DAY.isoformat()], "reported": [], "days": {},
        "position": {"symbol": "SPY", "quantity": "5", "decision_id": "gone", "entry": "500", "opened": "x"},
        "complete_noted": False,
    }))
    quotes = Quotes()
    quotes.set("SPY", "500.00")
    engine, gate, audit, broker = _engine(tmp_path, Bars(), quotes, clock)
    engine.replay()
    assert engine.held is None


def test_the_full_loop_wires_the_test_in_and_ticks_it(tmp_path):
    """Bootstrap with the test switched on: the loop builds the engine, ticks it
    each pass, and its order lands in the aggressive sleeve."""
    from orchestrator import start
    from research.config import ResearchConfig
    from signals import SignalsConfig
    from test_orchestrator import orchestrator_config

    clock = FakeClock(_at(10, 0, 20))
    quotes = Quotes()
    quotes.set("SPY", "502.00")
    quotes.set("SH", "40.00")
    from test_exits import RoutingLLM

    started = start(
        fetcher=lambda source: [],
        prices=quotes,
        llm_client=RoutingLLM(),
        adapter=FakeBroker(),
        clock=clock,
        data_dir=tmp_path,
        limits=RiskLimits.load(),
        signals_config=SignalsConfig.load(),
        research_config=ResearchConfig.load(),
        orchestrator_config=orchestrator_config(
            execution_test={"enabled": True, "first_session": DAY.isoformat()},
            aggressive_sleeve={"enabled": True, "attention": {"enabled": False}},
        ),
        id_factory=counter("w"),
        minute_bars=Bars(today_spy=_flat_today(500.5), today_sh=_flat_today(40.0)),
    )
    assert started.execution_test is not None
    report = started.loop.tick()
    assert report.exec_test_orders == 1
    clock.advance(seconds=30)
    started.loop.tick()
    position = started.gate.state.position(("aggressive", "SPY"))
    assert position is not None and position.quantity > 0
    assert (tmp_path / "execution_test_state.json").exists()
