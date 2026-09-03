"""Standards audit (human ruling 2026-09-02): the liquidity gate, the panic
button, and the stress test.

The claims: dollar ADV is the mean of close x volume over the last N bars and
absent below N; the gate refuses a resulting position above the configured
fraction of ADV, refuses when ADV is unavailable (fails CLOSED), leaves options
and the cash sweep alone, binds both arms, and is skipped only when no source
is wired; the HALT marker makes a live loop trip its kill switch and cancel its
working orders; halt/resume helpers do what the runbook says and refuse what it
says they refuse; operator actions round-trip through the audit log; and the
stress engine's drawdown arithmetic, ladder/kill flags and flat-held listing
are exact.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from audit.log import AuditLog
from audit.records import OperatorActionRecord
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from execution.liquidity import AdvSource, dollar_adv_from_bars
from orchestrator.config import OrchestratorConfig, StressWindow
from orchestrator.halt import (
    RESUME_PHRASE,
    acknowledgement_is_valid,
    clear_halt,
    perform_halt,
    perform_resume,
    read_halt,
    write_halt,
)
from orchestrator.state import SessionState
from orchestrator.stress import (
    BookPosition,
    StressWindowSpec,
    render_stress_report,
    stress_book,
)
from risk_gate import (
    AccountState,
    EquityBuyOrder,
    LimitExecution,
    OptionBuyToOpenOrder,
    RejectionCode,
    RiskGate,
    RiskLimits,
)
from test_exits import enter_position, routing
from test_orchestrator import FakeClock

NOW = datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc)
START_CASH = Decimal("100000")


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


def bars(n: int, close: str = "10", volume: str = "1000000"):
    return [{"c": close, "v": volume, "t": f"2026-08-{i + 1:02d}"} for i in range(n)]


# ================================================================================
# Dollar ADV
# ================================================================================


def test_dollar_adv_is_the_mean_of_close_times_volume_over_the_last_n_bars():
    rows = bars(19, "10", "1000000") + [{"c": "20", "v": "3000000"}]
    # last 20 of 20: 19 x 10M + 60M = 250M / 20 = 12.5M
    assert dollar_adv_from_bars(rows, 20) == Decimal("12500000")
    # Older bars beyond the window are ignored.
    assert dollar_adv_from_bars([{"c": "1", "v": "1"}] * 30 + rows, 20) == Decimal(
        "12500000"
    )


def test_dollar_adv_is_absent_below_the_window_and_skips_junk():
    assert dollar_adv_from_bars(bars(19), 20) is None
    assert dollar_adv_from_bars([], 20) is None
    junk = bars(20) + [{"c": "nope", "v": "x"}, {"c": "0", "v": "5"}]
    assert dollar_adv_from_bars(junk, 20) == Decimal("10000000")


def test_adv_source_caches_per_day_and_never_raises():
    class Bars:
        calls = 0
        ends = []

        def bars(self, symbol, start, end):
            Bars.calls += 1
            Bars.ends.append(end)
            if symbol == "BOOM":
                raise RuntimeError("api down")
            return bars(20)

    clock = FakeClock(NOW)
    source = AdvSource(Bars(), days=20, clock=clock)
    assert source("AAPL") == Decimal("10000000")
    assert source("AAPL") == Decimal("10000000")
    assert Bars.calls == 1
    assert source("BOOM") is None  # missing, never fabricated
    # Completed sessions only: the fetch ends yesterday, never "now" (the free
    # data plan refuses SIP queries into the last 15 minutes).
    assert all(end <= NOW - timedelta(days=1) for end in Bars.ends)


# ================================================================================
# The gate
# ================================================================================


def gate_with(limits, adv):
    return RiskGate(
        limits,
        AccountState(cash=START_CASH, high_water_mark=START_CASH),
        clock=FakeClock(NOW),
        adv=adv,
    )


def buy(symbol="TINY", qty=10, price="100.00", sleeve="equity"):
    return EquityBuyOrder(
        symbol=symbol,
        quantity=qty,
        execution=LimitExecution(limit_price=Decimal(price)),
        sleeve=sleeve,
    )


def test_shipped_limits_carry_the_proposed_liquidity_gate(limits):
    assert limits.liquidity.max_position_fraction_of_adv == Decimal("0.01")
    assert limits.liquidity.adv_days == 20


def test_a_position_above_the_adv_fraction_is_illiquid(limits):
    # $1,000 order against $50,000 daily dollar volume: 2% > 1%.
    gate = gate_with(limits, lambda symbol: Decimal("50000"))
    decision = gate.submit(buy(qty=10, price="100.00"))
    assert not decision.is_approved
    assert decision.code is RejectionCode.ILLIQUID_POSITION
    assert decision.limit == Decimal("500.00")
    assert decision.observed == Decimal("1000.00")


def test_a_liquid_name_passes_and_the_check_uses_the_resulting_position(limits):
    gate = gate_with(limits, lambda symbol: Decimal("150000"))  # cap 1,500
    first = gate.submit(buy(qty=10, price="100.00"))  # 1,000: passes
    assert first.is_approved
    gate.record_fill(first, Decimal("100.00"))
    # Held 1,000 + another 600 = 1,600 resulting: over the 1,500 cap.
    second = gate.submit(buy(qty=6, price="100.00"))
    assert not second.is_approved
    assert second.code is RejectionCode.ILLIQUID_POSITION


def test_a_missing_adv_fails_closed(limits):
    gate = gate_with(limits, lambda symbol: None)
    decision = gate.submit(buy())
    assert not decision.is_approved
    assert decision.code is RejectionCode.ILLIQUID_POSITION
    assert "unavailable" in decision.message
    # A source that RAISES is a missing number too, never a crash in the gate.
    def exploding(symbol):
        raise RuntimeError("feed down")

    assert gate_with(limits, exploding).submit(buy()).code is RejectionCode.ILLIQUID_POSITION


def test_the_mechanical_arm_is_bound_too(limits):
    gate = gate_with(limits, lambda symbol: Decimal("50000"))
    decision = gate.submit(buy(qty=10, price="100.00", sleeve="mechanical"))
    assert not decision.is_approved
    assert decision.code is RejectionCode.ILLIQUID_POSITION


def test_options_and_the_sweep_are_not_liquidity_gated(limits):
    gate = gate_with(limits, lambda symbol: None)  # would fail closed if asked
    option = OptionBuyToOpenOrder(
        symbol="AAPL260918C00150000",
        underlying="AAPL",
        right="call",
        expiration=date(2026, 9, 18),
        strike=Decimal("150"),
        contracts=1,
        multiplier=100,
        execution=LimitExecution(limit_price=Decimal("2.00")),
    )
    assert gate.submit(option).is_approved
    sweep = buy(symbol=limits.cash_management.symbol, qty=10, price="100.00",
                sleeve="cash_management")
    assert gate.submit(sweep).is_approved


def test_a_gate_without_an_adv_source_skips_the_check(limits):
    """Offline construction (tests, read-only commands) — the whole existing
    suite is the proof; this pins the intent."""
    gate = gate_with(limits, None)
    assert gate.submit(buy(symbol="ANYTHING")).is_approved


def test_trip_kill_switch_is_sticky_and_needs_a_reason(limits):
    gate = gate_with(limits, None)
    with pytest.raises(ValueError):
        gate.trip_kill_switch("  ")
    gate.trip_kill_switch("operator halt")
    assert gate.kill_switch_tripped
    decision = gate.submit(buy())
    assert decision.code is RejectionCode.KILL_SWITCH_ACTIVE


# ================================================================================
# The panic button
# ================================================================================


def test_the_halt_marker_round_trips(tmp_path):
    marker = tmp_path / "HALT"
    assert read_halt(marker) is None
    write_halt(marker, "smoke", "owen", NOW)
    text = read_halt(marker)
    assert "operator=owen" in text and "reason=smoke" in text
    assert clear_halt(marker) and read_halt(marker) is None
    assert not clear_halt(marker)


@pytest.mark.parametrize(
    "ack,valid",
    [
        ("Owen: I CONFIRM MANUAL RESET", True),
        ("I CONFIRM MANUAL RESET", False),  # no name
        ("Owen: i confirm manual reset", False),  # case-sensitive phrase
        ("Owen: I CONFIRM", False),
        ("", False),
    ],
)
def test_the_resume_acknowledgement_needs_a_name_and_the_exact_phrase(ack, valid):
    assert acknowledgement_is_valid(ack) is valid
    assert RESUME_PHRASE == "I CONFIRM MANUAL RESET"


def test_a_live_loop_honours_the_halt_marker(
    tmp_path, limits, signals_config, research_config
):
    """The marker is written by another process; the loop trips its own gate,
    cancels its working orders, and persists the halt — within one tick."""
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config, clock=FakeClock(NOW)
    )
    assert not started.gate.kill_switch_tripped
    marker = started.audit.path.parent / "HALT"
    write_halt(marker, "drill", "owen", NOW)

    report = started.loop.tick()
    assert started.gate.kill_switch_tripped
    assert report.halted
    # Persisted by the loop itself, so tomorrow's session starts halted.
    session = SessionState.load(started.audit.path.parent / "session_state.json")
    assert session.kill_switch_tripped
    # Risk-reducing closes still run: the stop fires on a halted book.
    prices.set("NUE", "100.00")  # far below the 119 stop
    assert started.loop.tick().exits_started == 1


def test_perform_halt_without_a_live_session_writes_everything(tmp_path):
    broker = RestingOrders(["o-1", "o-2"])  # left working by an earlier process
    assert len(broker.open_orders()) == 2
    sent = []
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    report = perform_halt(
        marker_path=tmp_path / "HALT",
        session_path=tmp_path / "session_state.json",
        live_session=False,
        reason="drill",
        operator="owen",
        adapter=broker,
        alert=lambda key, subject, body: sent.append(subject) or True,
        audit=audit,
        now=NOW,
    )
    assert report.marker_written and report.session_tripped_here
    assert len(report.orders_cancelled) == 2 and broker.open_orders() == []
    assert report.alert_sent and "OPERATOR HALT" in sent[0]
    assert SessionState.load(tmp_path / "session_state.json").kill_switch_tripped
    actions = audit.operator_actions()
    assert len(actions) == 1 and actions[0].action == "halt"
    assert "written" in report.render()


def test_perform_halt_with_a_live_session_leaves_the_session_file_alone(tmp_path):
    report = perform_halt(
        marker_path=tmp_path / "HALT",
        session_path=tmp_path / "session_state.json",
        live_session=True,
        reason="drill",
        operator="owen",
        now=NOW,
    )
    assert report.marker_written and not report.session_tripped_here
    assert not (tmp_path / "session_state.json").exists()
    assert "LIVE" in report.render()


def test_perform_resume_refuses_a_live_session_and_a_bad_acknowledgement(
    tmp_path, limits
):
    gate = gate_with(limits, None)
    gate.trip_kill_switch("drill")
    session = SessionState(path=tmp_path / "session_state.json")
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    common = dict(gate=gate, session=session, audit=audit,
                  marker_path=tmp_path / "HALT", operator="owen", now=NOW)
    with pytest.raises(RuntimeError):
        perform_resume(acknowledgement=f"Owen: {RESUME_PHRASE}", live_session=True, **common)
    with pytest.raises(ValueError):
        perform_resume(acknowledgement=RESUME_PHRASE, live_session=False, **common)
    assert gate.kill_switch_tripped  # nothing moved


def test_perform_resume_resets_records_and_clears_the_marker(tmp_path, limits):
    gate = gate_with(limits, None)
    gate.trip_kill_switch("drill")
    marker = tmp_path / "HALT"
    write_halt(marker, "drill", "owen", NOW)
    session = SessionState(path=tmp_path / "session_state.json")
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    report = perform_resume(
        gate=gate, session=session, audit=audit, marker_path=marker,
        acknowledgement=f"Owen: {RESUME_PHRASE}", operator="owen",
        live_session=False, now=NOW,
    )
    assert report.was_tripped and report.marker_cleared
    assert not gate.kill_switch_tripped and not marker.exists()
    assert not SessionState.load(session.path).kill_switch_tripped
    record = audit.operator_actions()[-1]
    assert isinstance(record, OperatorActionRecord)
    assert record.action == "resume" and RESUME_PHRASE in record.acknowledgement
    # And it round-trips through the generic reader like every other record.
    assert any(isinstance(r, OperatorActionRecord) for r in audit.records())


class RestingOrders:
    """The two adapter methods the halt uses, over a list of resting ids."""

    def __init__(self, ids):
        self._ids = list(ids)
        self.cancelled = []

    def open_orders(self):
        return list(self._ids)

    def cancel_order(self, order_id):
        self._ids.remove(order_id)
        self.cancelled.append(order_id)


# ================================================================================
# The stress test
# ================================================================================


def _closes_table(table: dict[str, list[str]], start: date):
    """symbol -> daily closes from `start`, as the closes callable."""

    def closes(symbol, window_start, window_end):
        series = table.get(symbol)
        if not series:
            return []
        return [
            (start + timedelta(days=i), Decimal(c))
            for i, c in enumerate(series)
            if window_start <= start + timedelta(days=i) <= window_end
        ]

    return closes


def test_stress_drawdown_arithmetic_and_flags_are_exact():
    start = date(2020, 2, 19)
    closes = _closes_table(
        {
            "AAA": ["100", "110", "55", "80"],  # peak 110 -> 55: 50% drawdown
            "BBB": ["50", "50", "50", "50"],
        },
        start,
    )
    positions = [
        BookPosition("equity", "AAA", Decimal("10"), Decimal("800")),
        BookPosition("mechanical", "BBB", Decimal("20"), Decimal("1000")),
        BookPosition("equity", "OPT", Decimal("1"), Decimal("300"), is_option=True),
        BookPosition("mechanical", "GONE", Decimal("5"), Decimal("200")),
    ]
    window = StressWindowSpec("test", start, start + timedelta(days=3))
    [result] = stress_book(
        positions,
        {"equity": Decimal("1000"), "mechanical": Decimal("0")},
        Decimal("0"),
        closes,
        [window],
    )
    equity = result.sleeves["equity"]
    # equity path: 1000 cash + 300 flat option + AAA x10: 2300, 2400, 1850, 2100
    assert equity.start_value == Decimal("2300")
    assert equity.trough_value == Decimal("1850")
    assert equity.max_drawdown == (Decimal("2400") - Decimal("1850")) / Decimal("2400")
    assert equity.replayed == ("AAA",) and "OPT (option: no equity bars)" in equity.flat_held
    mechanical = result.sleeves["mechanical"]
    assert mechanical.max_drawdown == Decimal("0")
    assert "GONE (no history in window)" in mechanical.flat_held
    # total: 2300+1200=3500, 3600, 3050, 3300 -> 550/3600 = 15.3%: kill switch fires
    assert result.total_max_drawdown == (Decimal("3600") - Decimal("3050")) / Decimal("3600")
    assert result.kill_switch_fired and result.ladder_rung == Decimal("0.5")
    assert not result.mechanical_breaker_fired
    text = render_stress_report([result], NOW)
    assert "kill switch FIRED" in text and "HELD FLAT" in text


def test_stress_with_no_history_says_so():
    closes = _closes_table({}, date(2018, 10, 1))
    [result] = stress_book(
        [BookPosition("equity", "NEW", Decimal("1"), Decimal("10"))],
        {"equity": Decimal("0")},
        Decimal("0"),
        closes,
        [StressWindowSpec("q4", date(2018, 10, 1), date(2018, 12, 24))],
    )
    assert "nothing could be replayed" in result.note
    assert "nothing could be replayed" in render_stress_report([result], NOW)


def test_shipped_config_carries_the_three_windows():
    windows = OrchestratorConfig.load().stress_windows
    assert [w.name for w in windows] == [
        "covid_crash_2020", "rates_selloff_2022", "q4_2018",
    ]
    with pytest.raises(ValueError):
        StressWindow(name="bad", start=date(2020, 3, 1), end=date(2020, 2, 1))
