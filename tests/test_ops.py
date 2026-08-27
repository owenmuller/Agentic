"""Operational layer tests: health, run log, session bounds, crash recovery.

The two claims that matter most for unattended operation:

  1. **Health is read-only.** It is the command a human runs every day without
     thinking; if it could mutate anything, one absent-minded morning would corrupt
     state. Proven here byte-for-byte: audit log, session state, and budget are
     identical before and after.
  2. **A process that dies mid-tick is recoverable.** The specific worst case: an
     order was submitted to the broker and the process died before reconciling it —
     the reservation protecting it lived in an ApprovedOrder that died with the
     process. The next startup's orphan sweep cancels it. Alongside the existing
     restart tests (kill switch, budget, deployment, open positions), that is the
     whole crash story.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.base import BrokerPosition
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from orchestrator import (
    RunLog,
    health_report,
    is_trading_weekday,
    session_bounds,
    start,
    unmanaged_exposure,
)
from orchestrator.bootstrap import preflight
from test_exits import MutablePrices, RoutingLLM, enter_position
from test_orchestrator import (
    QUOTE,
    FakeBroker,
    FakeClock,
    counter,
    feed,
    orchestrator_config,
)


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


def preflight_kwargs(tmp_path, limits, signals_config, research_config, clock=None):
    return dict(
        limits=limits,
        signals_config=signals_config,
        research_config=research_config,
        orchestrator_config=orchestrator_config(),
        data_dir=tmp_path,
        clock=clock or FakeClock(),
    )


def inert_engine(checks):
    """The engine health builds: replay-only, nothing live behind it."""
    from orchestrator.exits import ExitEngine

    engine = ExitEngine(
        gate=checks.gate,
        adapter=checks.adapter,
        audit=checks.audit,
        prices=lambda symbol: None,
        review_pass=None,
        budget=checks.budget,
        config=checks.orchestrator_config.exits,
        clock=checks.clock,
    )
    engine.replay(checks.audit.trails())
    return engine


# ================================================================================
# The run log
# ================================================================================


def test_the_run_log_appends_timestamped_grepable_lines(tmp_path):
    clock = FakeClock()
    log = RunLog(tmp_path / "run.log", clock=clock)
    log.note("STARTED", "pid=42")
    clock.advance(hours=6)
    log.note("STOPPED", "market close; settled_or_released=1")

    lines = (tmp_path / "run.log").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "2026-08-17T14:30:00+00:00 STARTED pid=42",
        "2026-08-17T20:30:00+00:00 STOPPED market close; settled_or_released=1",
    ]


def test_last_finds_the_most_recent_line_per_event(tmp_path):
    log = RunLog(tmp_path / "run.log", clock=FakeClock())
    log.note("POLL", "form_13f ok items=3")
    log.note("STARTED", "pid=1")
    log.note("POLL", "form_13f ok items=0")

    assert log.last("POLL").endswith("items=0")
    assert log.last("ERROR") is None
    assert log.tail(2)[-1].endswith("items=0")


def test_a_missing_run_log_reads_as_empty_not_an_error(tmp_path):
    log = RunLog(tmp_path / "never-written.log")
    assert log.tail() == []
    assert log.last("STARTED") is None


# ================================================================================
# Session bounds — computed in ET at runtime, whatever this machine's zone is
# ================================================================================


def test_summer_session_bounds_in_utc():
    """August: ET is UTC-4, so 9:30/16:00 ET = 13:30/20:00 UTC."""
    now = datetime(2026, 8, 18, 6, 0, tzinfo=timezone.utc)
    open_utc, close_utc = session_bounds(now)
    assert open_utc == datetime(2026, 8, 18, 13, 30, tzinfo=timezone.utc)
    assert close_utc == datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)


def test_winter_session_bounds_in_utc():
    """January: ET is UTC-5, so the same wall-clock session shifts an hour in UTC.
    This is the case a registration-time offset would get wrong twice a year."""
    now = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
    open_utc, close_utc = session_bounds(now)
    assert open_utc == datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
    assert close_utc == datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)


def test_weekdays_trade_and_weekends_do_not():
    assert is_trading_weekday(datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc))  # Tue
    assert not is_trading_weekday(datetime(2026, 8, 22, 15, 0, tzinfo=timezone.utc))  # Sat
    assert not is_trading_weekday(datetime(2026, 8, 23, 15, 0, tzinfo=timezone.utc))  # Sun


def test_a_late_utc_evening_is_still_the_same_et_trading_day():
    """23:30 UTC Tuesday is 19:30 ET Tuesday — after close, but still Tuesday's
    session, not Wednesday's."""
    now = datetime(2026, 8, 18, 23, 30, tzinfo=timezone.utc)
    open_utc, close_utc = session_bounds(now)
    assert open_utc.day == 18 and close_utc.day == 18
    assert now > close_utc


# ================================================================================
# Health — the daily look, and its read-only guarantee
# ================================================================================


def test_health_shows_positions_with_their_armed_stops(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock()
    started, _, _ = enter_position(
        tmp_path, limits, signals_config, research_config, clock=clock
    )
    started.loop.shutdown()

    checks = preflight(
        adapter=FakeBroker(
            cash=Decimal("97760"),
            positions=[
                BrokerPosition("NUE", Decimal("16"), Decimal("2240"), Decimal("2240"))
            ],
        ),
        id_factory=counter("b"),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    report = health_report(checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log"))

    assert "open positions: 1" in report
    assert "NUE" in report
    assert "stop 119.00" in report
    assert "leash 0/45d (weeks)" in report
    assert "kill switch: clear" in report
    assert "research budget: 1 of 40" in report
    assert "drawdown 0.00%" in report
    assert "last EDGAR poll:     none on record" in report
    assert "(stage_rejection)" not in report  # last audit record is the entry fill
    assert "last audit record:" in report


def test_health_surfaces_an_unmanaged_position_loudly(
    tmp_path, limits, signals_config, research_config
):
    """A holding with no audit trail has no stops. Health must not present it as
    covered — it is the one line on the screen that demands a human."""
    checks = preflight(
        adapter=FakeBroker(
            positions=[
                BrokerPosition("AAPL", Decimal("100"), Decimal("22000"), Decimal("20000"))
            ]
        ),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config),
    )
    report = health_report(checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log"))

    assert "UNMANAGED" in report
    assert "AAPL" in report
    assert "NO STOPS ARMED" in report


def test_health_reads_the_run_log_tail(tmp_path, limits, signals_config, research_config):
    run_log = RunLog(tmp_path / "run.log", clock=FakeClock())
    run_log.note("STARTED", "pid=7")
    run_log.note("POLL", "form_13f ok items=3")

    checks = preflight(
        adapter=FakeBroker(),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config),
    )
    report = health_report(checks, inert_engine(checks).tracked, run_log)

    assert "form_13f ok items=3" in report
    assert "STARTED pid=7" in report


def test_health_is_read_only(tmp_path, limits, signals_config, research_config):
    """Byte-for-byte: running health changes no file and spends no budget."""
    clock = FakeClock()
    started, _, _ = enter_position(
        tmp_path, limits, signals_config, research_config, clock=clock
    )
    started.loop.shutdown()

    audit_before = (tmp_path / "audit.jsonl").read_bytes()
    session_before = (tmp_path / "session_state.json").read_bytes()

    for _ in range(2):  # twice: the second run also sees no drift from the first
        checks = preflight(
            adapter=FakeBroker(
                positions=[
                    BrokerPosition("NUE", Decimal("16"), Decimal("2240"), Decimal("2240"))
                ]
            ),
            id_factory=counter("h"),
            **preflight_kwargs(tmp_path, limits, signals_config, research_config, clock),
        )
        health_report(checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log"))
        assert checks.budget.spent == 1  # replayed, not spent

    assert (tmp_path / "audit.jsonl").read_bytes() == audit_before
    assert (tmp_path / "session_state.json").read_bytes() == session_before
    assert not (tmp_path / "run.log").exists()  # health never writes the run log


# ================================================================================
# Crash recovery — the mid-tick death, explicitly
# ================================================================================


def test_a_crash_between_submit_and_reconcile_is_swept_at_the_next_startup(
    tmp_path, limits, signals_config, research_config
):
    """The worst mid-tick death: the order reached the broker, the process died
    before reconciling, and the reservation protecting it died too (it lived in an
    ApprovedOrder, unforgeably). No shutdown ran — that is the crash. The next
    startup must cancel the orphan rather than let it fill unreserved."""
    clock = FakeClock()
    broker = FakeBroker(fill="new")  # the entry order rests at the broker
    started = start(
        fetcher=feed(trump_posts=["Buying $NUE here. Entry: 140, stop: 130."]),
        prices=MutablePrices(NUE=str(QUOTE)),
        llm_client=RoutingLLM(),
        adapter=broker,
        id_factory=counter("a"),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    report = started.loop.tick()
    assert report.traded == 1
    assert broker.open_orders() == ["brk-1"]
    # CRASH. No shutdown, no cancel — the loop object is simply abandoned.

    restarted = start(
        fetcher=feed(),
        prices=MutablePrices(),
        llm_client=RoutingLLM(),
        adapter=broker,  # same broker: the orphan is still resting there
        id_factory=counter("b"),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )

    assert broker.cancelled == ["brk-1"], "the orphan sweep must cancel it"
    assert broker.open_orders() == []
    # And the restarted gate carries no phantom reservation for it.
    assert restarted.gate.state.reserved_cash == Decimal("0")
    restarted.loop.shutdown()


def test_a_clean_startup_sweeps_nothing(
    tmp_path, limits, signals_config, research_config
):
    broker = FakeBroker()
    started = start(
        fetcher=feed(),
        prices=MutablePrices(),
        llm_client=RoutingLLM(),
        adapter=broker,
        **preflight_kwargs(tmp_path, limits, signals_config, research_config),
    )
    assert broker.cancelled == []
    started.loop.shutdown()


def test_unmanaged_exposure_math(tmp_path, limits, signals_config, research_config):
    """Tracked quantities subtract per symbol; only the uncovered excess surfaces."""
    clock = FakeClock()
    started, _, _ = enter_position(
        tmp_path, limits, signals_config, research_config, clock=clock
    )
    started.loop.shutdown()

    # The broker holds 20 NUE but the trail only accounts for 13 — plus 100 AAPL
    # nothing accounts for at all.
    checks = preflight(
        adapter=FakeBroker(
            positions=[
                BrokerPosition("NUE", Decimal("20"), Decimal("2800"), Decimal("2800")),
                BrokerPosition("AAPL", Decimal("100"), Decimal("22000"), Decimal("20000")),
            ]
        ),
        id_factory=counter("b"),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    engine = inert_engine(checks)

    assert unmanaged_exposure(checks.gate, engine.tracked) == {"NUE": 7, "AAPL": 100}


# ================================================================================
# Single-instance protection
# ================================================================================


def test_a_second_instance_is_refused_while_the_first_holds_the_lock(tmp_path):
    from orchestrator import InstanceLock

    first = InstanceLock(tmp_path / "orchestrator.lock")
    second = InstanceLock(tmp_path / "orchestrator.lock")
    assert first.acquire() is True

    assert second.acquire() is False
    # The refused instance can still say who holds it, for the log line.
    assert "pid=" in second.holder()

    first.release()


def test_a_stale_lock_from_a_crashed_process_does_not_brick_the_next_run(tmp_path):
    """The file survives a crash; the OS lock does not. A leftover file with a dead
    pid in it must be exactly as acquirable as no file at all."""
    from orchestrator import InstanceLock

    path = tmp_path / "orchestrator.lock"
    path.write_text(
        "pid=99999999 started=2026-08-17T09:30:00+00:00\n"
        "held by a live orchestrator run; released automatically when it exits\n",
        encoding="utf-8",
    )

    lock = InstanceLock(path)
    assert lock.acquire() is True, "a crashed process's lock file bricked the run"
    assert "pid=99999999" not in lock.holder()  # rewritten with the live holder
    lock.release()


def test_release_makes_the_lock_acquirable_again(tmp_path):
    from orchestrator import InstanceLock

    path = tmp_path / "orchestrator.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_the_lock_is_a_context_manager_that_raises_when_held(tmp_path):
    from orchestrator import InstanceLock

    path = tmp_path / "orchestrator.lock"
    with InstanceLock(path):
        with pytest.raises(RuntimeError, match="another instance"):
            with InstanceLock(path):
                pass  # pragma: no cover
    # Released on exit:
    with InstanceLock(path):
        pass


# ================================================================================
# Permission preflight — the day-one misconfiguration flag
# ================================================================================


def test_preflight_warns_when_the_account_permits_more_than_the_code(
    tmp_path, limits, signals_config, research_config, caplog
):
    import logging

    from execution.base import BrokerPermissions

    broker = FakeBroker()
    broker.granted = BrokerPermissions(
        options_level=3, shorting_enabled=True, margin_multiplier=Decimal("2")
    )
    with caplog.at_level(logging.WARNING, logger="orchestrator.bootstrap"):
        checks = preflight(
            adapter=broker,
            **preflight_kwargs(tmp_path, limits, signals_config, research_config),
        )

    warnings = [r.message for r in caplog.records if "PERMITS MORE" in r.message]
    assert len(warnings) == 3
    assert "EXCEEDS" in checks.describe()


def test_preflight_is_quiet_for_a_matched_account(
    tmp_path, limits, signals_config, research_config, caplog
):
    import logging

    with caplog.at_level(logging.WARNING, logger="orchestrator.bootstrap"):
        checks = preflight(
            adapter=FakeBroker(),  # clean by default: level 2, no shorting, 1x
            **preflight_kwargs(tmp_path, limits, signals_config, research_config),
        )

    assert [r for r in caplog.records if "PERMITS MORE" in r.message] == []
    assert "matched to the system" in checks.describe()
    assert "options level 2" in checks.describe()


def test_preflight_flags_an_account_that_cannot_buy_options(
    tmp_path, limits, signals_config, research_config, caplog
):
    import logging

    from execution.base import BrokerPermissions

    broker = FakeBroker()
    broker.granted = BrokerPermissions(
        options_level=0, shorting_enabled=False, margin_multiplier=Decimal("1")
    )
    with caplog.at_level(logging.WARNING, logger="orchestrator.bootstrap"):
        preflight(
            adapter=broker,
            **preflight_kwargs(tmp_path, limits, signals_config, research_config),
        )

    assert any("cannot BUY calls or puts" in r.message for r in caplog.records)


def test_health_shows_the_permission_line(
    tmp_path, limits, signals_config, research_config
):
    checks = preflight(
        adapter=FakeBroker(),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config),
    )
    report = health_report(checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log"))
    assert "broker permits: options level 2, shorting disabled, margin 1x" in report


def test_health_report_shows_the_allocation_with_the_inactive_marker(
    tmp_path, limits, signals_config, research_config
):
    """The daily check must show the allocation, not just the startup log —
    a zero-weight sleeve reads as a ruling ("inactive"), never as dead capital."""
    checks = preflight(
        adapter=FakeBroker(),
        id_factory=counter("h"),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config),
    )
    report = health_report(
        checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log")
    )
    assert "sleeves: equity 75%, mechanical 25%, prediction 0% (inactive)" in report
