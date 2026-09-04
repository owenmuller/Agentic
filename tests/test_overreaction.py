"""Overreaction-fade measurement half (human ruling 2026-09-03).

The claims: detection is exact arithmetic on completed bars (drop, volume
ratio, flags, market day) and fails closed on thin history; the universe tiers
are core = held ∪ researched, broad = other purchase-side names; a row's
labelled content round-trips through the snapshot parser into the funnel with
the slice facts; recording is idempotent per (name, session); measurement rows
never enter the convergence registry; the forward report renders the four
slices; and the shipped config carries the ruled thresholds.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from audit.log import AuditLog
from audit.records import SignalSnapshot, snapshot_overreaction
from forward.funnel import funnel_entries
from forward.report import render_forward_report
from forward.returns import ForwardRow, HorizonMark
from orchestrator.config import ConvergenceConfig, OrchestratorConfig, OverreactionScreenConfig
from orchestrator.overreaction import (
    CODE,
    SOURCE_ID,
    UniverseMember,
    build_universe,
    detect,
    event_signal,
    last_completed_session,
    recorded_external_ids,
    rows_of,
    run_screen,
    weekdays_between,
)
from orchestrator.registry import SignalRegistry
from test_orchestrator import FakeClock

NOW = datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc)
CONFIG = OverreactionScreenConfig()


def bars(closes, volumes, start=date(2026, 8, 3)):
    """Weekday bars, oldest-first, one per (close, volume)."""
    out = []
    day = start
    for close, volume in zip(closes, volumes):
        while day.weekday() >= 5:
            day = date.fromordinal(day.toordinal() + 1)
        out.append({"t": f"{day.isoformat()}T04:00:00Z", "c": str(close), "v": str(volume)})
        day = date.fromordinal(day.toordinal() + 1)
    return out


def flat_then(drop_close, drop_volume, n=25, close=100, volume=1_000_000):
    rows = rows_of(bars([close] * n + [drop_close], [volume] * n + [drop_volume]))
    return rows, rows[-1][0]


CORE = UniverseMember("TEST", "core", held=True)


# ================================================================================
# Detection arithmetic
# ================================================================================


def test_a_qualifying_drop_is_detected_with_its_flags_and_market_day():
    rows, session = flat_then(drop_close=92, drop_volume=2_000_000)
    event = detect(CORE, rows, session, CONFIG, Decimal("-0.50"), "semis")
    assert event is not None
    assert event.drop_pct == Decimal("-8.00")
    assert event.volume_ratio == Decimal("2.00")
    assert event.flags == (6, 7, 8)
    assert event.market_day is False and event.spy_return_pct == Decimal("-0.50")
    assert event.tier == "core" and event.held and event.sector == "semis"
    assert event.external_id == f"TEST:{session.isoformat()}"
    # The same drop on a market day is stamped as one.
    assert detect(CORE, rows, session, CONFIG, Decimal("-2.50"), "").market_day is True
    # No SPY mark: market day unknown, event still recorded.
    assert detect(CORE, rows, session, CONFIG, None, "").market_day is None


def test_flags_follow_the_thresholds_the_drop_actually_met():
    rows, session = flat_then(drop_close=93.5, drop_volume=2_000_000)  # -6.5%
    event = detect(CORE, rows, session, CONFIG, None, "")
    assert event is not None and event.flags == (6,)  # below the ruled 7%
    rows, session = flat_then(drop_close=92.5, drop_volume=2_000_000)  # -7.5%
    assert detect(CORE, rows, session, CONFIG, None, "").flags == (6, 7)


@pytest.mark.parametrize(
    "drop_close,drop_volume",
    [
        (95, 2_000_000),  # -5%: below every flag
        (92, 1_400_000),  # volume 1.4x: below the 1.5x floor
        (108, 3_000_000),  # a rally is not a drop
    ],
)
def test_non_events_produce_nothing(drop_close, drop_volume):
    rows, session = flat_then(drop_close=drop_close, drop_volume=drop_volume)
    assert detect(CORE, rows, session, CONFIG, None, "") is None


def test_thin_history_fails_closed():
    rows, session = flat_then(drop_close=92, drop_volume=2_000_000, n=15)  # < 20 prior
    assert detect(CORE, rows, session, CONFIG, None, "") is None
    assert detect(CORE, rows, date(2030, 1, 1), CONFIG, None, "") is None  # no bar


# ================================================================================
# Universe tiers
# ================================================================================


def test_universe_tiers_core_over_broad():
    universe = build_universe(
        held=["intc"], researched=["AMRN", "INTC"], active_purchase=["AMRN", "RAWA", "rawb"]
    )
    assert universe["INTC"].tier == "core" and universe["INTC"].held
    assert universe["AMRN"].tier == "core" and not universe["AMRN"].held
    assert universe["RAWA"].tier == "broad" and universe["RAWB"].tier == "broad"
    assert len(universe) == 4


# ================================================================================
# Rows, round-trip, idempotency, registry exclusion
# ================================================================================


def test_the_row_round_trips_through_the_snapshot_parser_into_the_funnel(tmp_path):
    rows, session = flat_then(drop_close=92, drop_volume=2_000_000)
    event = detect(CORE, rows, session, CONFIG, Decimal("-2.50"), "semis")
    signal = event_signal(event)
    assert signal.source_id == SOURCE_ID
    assert signal.metadata["measurement_only"] == "true"
    assert signal.external_id == event.external_id
    facts = snapshot_overreaction(SignalSnapshot.of(signal))
    assert facts is not None
    assert facts.drop_pct == Decimal("-8.00") and facts.tier == "core"
    assert facts.held and facts.market_day is True and facts.flags == (6, 7, 8)
    # Other rows parse to None.
    other = SignalSnapshot.of(signal).model_copy(update={"content": "ticker: X\ntransaction: Purchase"})
    assert snapshot_overreaction(other) is None

    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    universe = {"TEST": CORE}
    fetched = []

    def fake_bars(symbol, start, end):
        fetched.append(symbol)
        if symbol == "SPY":
            return bars([500] * 25 + [487], [1] * 26)  # -2.6%: a market day
        return bars([100] * 25 + [92], [1_000_000] * 25 + [2_000_000])

    report = run_screen(
        sessions=[session], universe=universe, bars=fake_bars,
        sector_of=lambda s: "semis", config=CONFIG, audit=audit,
        id_factory=iter(f"ovr-{i}" for i in range(10)).__next__,
    )
    assert report.recorded == 1 and len(report.events) == 1
    assert fetched.count("SPY") == 1  # one benchmark fetch per run
    # Idempotent: the second run finds the row already on record.
    again = run_screen(
        sessions=[session], universe=universe, bars=fake_bars,
        sector_of=lambda s: "semis", config=CONFIG, audit=audit,
        id_factory=iter(f"ovr2-{i}" for i in range(10)).__next__,
    )
    assert again.recorded == 0 and again.skipped_existing == 1
    assert recorded_external_ids(audit) == {event.external_id}

    entries = funnel_entries(audit.records())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.code == CODE and entry.bucket == "prefiltered"
    assert entry.primary_ticker == "TEST"
    assert entry.overreaction is not None and entry.overreaction.market_day is True
    assert entry.observed_at.date() == session
    assert "recorded" in report.render()


def test_measurement_rows_never_enter_the_convergence_registry(tmp_path):
    rows, session = flat_then(drop_close=92, drop_volume=2_000_000)
    event = detect(CORE, rows, session, CONFIG, None, "")
    signal = event_signal(event)
    registry = SignalRegistry(ConvergenceConfig(window_days=400), FakeClock(NOW))
    registry.note_signals([signal])
    assert registry.in_window_symbols() == ()
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    run_screen(
        sessions=[session], universe={"TEST": CORE},
        bars=lambda s, a, b: bars([100] * 25 + [92], [1_000_000] * 25 + [2_000_000]),
        sector_of=lambda s: "", config=CONFIG, audit=audit, id_factory=lambda: "ovr-x",
    )
    seeded = SignalRegistry(ConvergenceConfig(window_days=400), FakeClock(NOW))
    seeded.seed(audit.records())
    assert seeded.in_window_symbols() == ()
    assert seeded.verdict_summary() == {}


# ================================================================================
# Report slices
# ================================================================================


def _row(symbol, observed, excess_by_horizon):
    marks = {
        h: HorizonMark(marked_on=observed, close=Decimal("1"), return_pct=Decimal(str(v)),
                       excess_pct=Decimal(str(v)))
        for h, v in excess_by_horizon.items()
    }
    return ForwardRow(symbol=symbol, observed=observed, base_date=observed,
                      base_close=Decimal("100"), marks=marks, computed_at=NOW)


def test_the_forward_report_renders_the_four_slices(tmp_path):
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    rows_a, session = flat_then(drop_close=92, drop_volume=2_000_000)
    universe = {
        "HELD": UniverseMember("HELD", "core", True),
        "RAW": UniverseMember("RAW", "broad", False),
    }
    run_screen(
        sessions=[session], universe=universe,
        bars=lambda s, a, b: (bars([500] * 25 + [499], [1] * 26) if s == "SPY"
                              else bars([100] * 25 + [92], [1_000_000] * 25 + [2_000_000])),
        sector_of=lambda s: "", config=CONFIG, audit=audit,
        id_factory=iter(f"ovr-{i}" for i in range(10)).__next__,
    )
    entries = funnel_entries(audit.records())
    assert len(entries) == 2
    rows = {
        ("HELD", session): _row("HELD", session, {1: 1.5, 5: 3.0, 20: 4.0, 60: 2.0}),
        ("RAW", session): _row("RAW", session, {1: -1.0, 5: -2.0}),
    }
    text = render_forward_report(entries, rows)
    assert "Overreaction candidates" in text
    assert "all (>=6% flag): 2 events" in text
    assert "core (held or researched): 1 events | 1d +1.50% (n=1)" in text
    assert "broad (unresearched signal flow): 1 events | 1d -1.00% (n=1)" in text
    assert "idiosyncratic day: 2 events" in text
    assert "market day (SPY <= -2%): no events" in text
    assert "held positions: 1 events" in text
    assert ">=8%: 2 events" in text


# ================================================================================
# Sessions and config
# ================================================================================


def test_last_completed_session_respects_the_bar_completion_time():
    # Thursday 2026-09-03 17:00 UTC = 13:00 ET: today's bar is not complete.
    assert last_completed_session(datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc)) == date(2026, 9, 2)
    # 21:00 UTC = 17:00 ET: today's bar is complete.
    assert last_completed_session(datetime(2026, 9, 3, 21, 0, tzinfo=timezone.utc)) == date(2026, 9, 3)
    # Saturday resolves to Friday; Monday morning to Friday.
    assert last_completed_session(datetime(2026, 9, 5, 15, 0, tzinfo=timezone.utc)) == date(2026, 9, 4)
    assert last_completed_session(datetime(2026, 9, 7, 13, 0, tzinfo=timezone.utc)) == date(2026, 9, 4)
    assert weekdays_between(date(2026, 8, 28), date(2026, 9, 1)) == [
        date(2026, 8, 28), date(2026, 8, 31), date(2026, 9, 1)
    ]


def test_the_screen_never_asks_for_bars_newer_than_an_hour_ago(tmp_path):
    """The free data plan refuses SIP queries into the last 15 minutes; the
    first backfill silently scanned 404 names against empty lists."""
    audit = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    asked = []

    def fake_bars(symbol, start, end):
        asked.append(end)
        return []

    report = run_screen(
        sessions=[NOW.date()], universe={"TEST": CORE}, bars=fake_bars,
        sector_of=lambda s: "", config=CONFIG, audit=audit,
        id_factory=lambda: "x", now=NOW,
    )
    assert asked and all(end <= NOW - timedelta(hours=1) for end in asked)
    assert report.empty_symbols == 1
    assert "returned NO bars" in report.render() and "measured nothing" in report.render()


def test_shipped_config_carries_the_ruled_thresholds():
    config = OrchestratorConfig.load().overreaction_screen
    assert config.enabled
    assert config.drop_threshold == Decimal("0.07")
    assert config.flag_thresholds == (Decimal("0.06"), Decimal("0.08"))
    assert config.volume_ratio_min == Decimal("1.5") and config.adv_days == 20
    assert config.market_day_threshold == Decimal("0.02")
