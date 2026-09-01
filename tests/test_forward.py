"""Forward-return tracking (human ruling 2026-09-01).

The claims: marks land on the first session close at or after observation + n
CALENDAR days; a horizon whose day has not arrived — or whose series ended — is
ABSENT, never zero; excess is same-window SPY subtracted and absent without a
SPY mark; the cache is append-only and complete rows are never refetched; the
funnel flattener buckets every record kind correctly, excludes the mechanical
arm, and recovers tickers from records written before the structured field
existed. And nothing in the package can act: that property lives in
test_topology.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from audit.records import (
    DecisionRecord,
    GateSnapshot,
    RejectedStage,
    ResearchSnapshot,
    SignalSnapshot,
    SizingSnapshot,
    StageRejectionRecord,
    snapshot_tickers,
)
from forward import (
    HORIZONS,
    ForwardReturns,
    funnel_entries,
    render_forward_report,
    wanted_pairs,
)
from signals import SignalClass

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
OBSERVED = date(2026, 8, 3)  # a Monday


class FakeBars:
    """Synthetic daily closes: a fixed series per symbol, fetches counted."""

    def __init__(self, series: dict[str, list[tuple[str, str]]]) -> None:
        self.series = series
        self.fetches: list[str] = []

    def __call__(self, symbol: str, start: datetime, end: datetime):
        self.fetches.append(symbol)
        return [
            {"t": f"{day}T04:00:00Z", "c": close}
            for day, close in self.series.get(symbol, [])
            if start.date() <= date.fromisoformat(day) <= end.date()
        ]


def weekday_series(symbol_start: date, days: int, first: float, step: float):
    """(iso day, close) for consecutive WEEKDAYS — gaps where weekends fall."""
    out = []
    day = symbol_start
    close = first
    produced = 0
    while produced < days:
        if day.weekday() < 5:
            out.append((day.isoformat(), f"{close:.2f}"))
            close += step
            produced += 1
        day += timedelta(days=1)
    return out


def engine(tmp_path, bars, now=NOW):
    return ForwardReturns(
        bars, tmp_path / "forward.jsonl", clock=lambda: now, pace_seconds=0
    )


def test_bulk_fetches_are_paced_under_the_rate_limit(tmp_path):
    """The first production run tripped Alpaca's 429 halfway through the
    alphabet; the sweep now pauses between symbols. Sleeps are counted, never
    endured, via an injected sleeper."""
    naps: list[float] = []
    bars = FakeBars(
        {
            "AAA": weekday_series(OBSERVED, 5, 10.0, 0.1),
            "BBB": weekday_series(OBSERVED, 5, 10.0, 0.1),
            "CCC": weekday_series(OBSERVED, 5, 10.0, 0.1),
            "SPY": weekday_series(OBSERVED, 5, 500.0, 0.0),
        }
    )
    paced = ForwardReturns(
        bars, tmp_path / "forward.jsonl", clock=lambda: NOW,
        pace_seconds=0.35, sleep=naps.append,
    )
    paced.rows_for({("AAA", OBSERVED), ("BBB", OBSERVED), ("CCC", OBSERVED)})
    assert naps == [0.35, 0.35]  # between symbols, not before the first


# ================================================================================
# Marks: first close >= D+n, absent when not yet or never
# ================================================================================


def test_marks_land_on_the_first_close_at_or_after_the_horizon(tmp_path):
    bars = FakeBars(
        {
            "NUE": weekday_series(OBSERVED, 40, 100.0, 1.0),
            "SPY": weekday_series(OBSERVED, 40, 500.0, 0.0),
        }
    )
    rows = engine(tmp_path, bars).rows_for({("NUE", OBSERVED)})
    row = rows[("NUE", OBSERVED)]

    assert row.base_date == OBSERVED  # a session day: base is that close
    assert row.base_close == Decimal("100.00")
    # D+1 is Tuesday 08-04, a session: exact hit.
    assert row.marks[1].marked_on == date(2026, 8, 4)
    assert row.marks[1].return_pct == Decimal("1.00")
    # D+5 is Saturday 08-08: the first close AFTER it is Monday 08-10.
    assert row.marks[5].marked_on == date(2026, 8, 10)
    # 20d resolved; 60d and 120d have not arrived by 2026-09-01.
    assert 20 in row.marks
    assert 60 not in row.marks
    assert 120 not in row.marks
    assert not row.complete


def test_an_absent_horizon_is_absent_never_zero(tmp_path):
    """A series that ends (delisting) before D+n yields no mark at n."""
    bars = FakeBars(
        {
            "GONE": weekday_series(OBSERVED, 3, 50.0, -1.0),  # three sessions, then nothing
            "SPY": weekday_series(OBSERVED, 40, 500.0, 0.0),
        }
    )
    rows = engine(tmp_path, bars).rows_for({("GONE", OBSERVED)})
    row = rows[("GONE", OBSERVED)]
    assert row.has_base
    assert 1 in row.marks  # 08-04 exists
    assert 20 not in row.marks  # the series died first — absent, not zero
    for mark in row.marks.values():
        assert mark.return_pct != Decimal("0") or mark.close != Decimal("0")


def test_no_price_history_yields_no_base_and_no_marks(tmp_path):
    bars = FakeBars({"SPY": weekday_series(OBSERVED, 40, 500.0, 0.0)})
    rows = engine(tmp_path, bars).rows_for({("NODATA", OBSERVED)})
    row = rows[("NODATA", OBSERVED)]
    assert not row.has_base
    assert row.marks == {}


def test_excess_is_the_same_window_spy_return_subtracted(tmp_path):
    """NUE +1%/session vs SPY +0.2%/session: the excess is the difference of the
    two window returns, both measured base-to-mark on identical dates."""
    bars = FakeBars(
        {
            "NUE": [(OBSERVED.isoformat(), "100.00"),
                    ((OBSERVED + timedelta(days=1)).isoformat(), "110.00")],
            "SPY": [(OBSERVED.isoformat(), "500.00"),
                    ((OBSERVED + timedelta(days=1)).isoformat(), "505.00")],
        }
    )
    rows = engine(tmp_path, bars).rows_for({("NUE", OBSERVED)})
    mark = rows[("NUE", OBSERVED)].marks[1]
    assert mark.return_pct == Decimal("10.00")
    assert mark.excess_pct == Decimal("9.00")  # 10% - 1%


def test_without_spy_the_excess_is_absent_never_guessed(tmp_path):
    bars = FakeBars({"NUE": weekday_series(OBSERVED, 10, 100.0, 1.0)})
    rows = engine(tmp_path, bars).rows_for({("NUE", OBSERVED)})
    mark = rows[("NUE", OBSERVED)].marks[1]
    assert mark.return_pct is not None
    assert mark.excess_pct is None


# ================================================================================
# The cache: append-only, last row wins, complete rows never refetch
# ================================================================================


def test_the_cache_is_append_only_and_a_fuller_row_supersedes(tmp_path):
    series = {
        "NUE": weekday_series(OBSERVED, 130, 100.0, 0.5),
        "SPY": weekday_series(OBSERVED, 130, 500.0, 0.0),
    }
    early_now = datetime.combine(
        OBSERVED + timedelta(days=10), time(14), tzinfo=timezone.utc
    )
    bars = FakeBars(series)
    first = engine(tmp_path, bars, now=early_now)
    row = first.rows_for({("NUE", OBSERVED)})[("NUE", OBSERVED)]
    assert 5 in row.marks and 20 not in row.marks
    text_after_first = (tmp_path / "forward.jsonl").read_text(encoding="utf-8")

    late_now = datetime.combine(
        OBSERVED + timedelta(days=200), time(14), tzinfo=timezone.utc
    )
    second = engine(tmp_path, bars, now=late_now)
    fuller = second.rows_for({("NUE", OBSERVED)})[("NUE", OBSERVED)]
    assert fuller.complete
    text_after_second = (tmp_path / "forward.jsonl").read_text(encoding="utf-8")
    assert text_after_second.startswith(text_after_first)  # nothing rewritten

    # A third engine reads the cache: the complete row wins, and NO fetch runs.
    quiet = FakeBars(series)
    third = engine(tmp_path, quiet, now=late_now)
    again = third.rows_for({("NUE", OBSERVED)})[("NUE", OBSERVED)]
    assert again.complete
    assert quiet.fetches == []


def test_a_run_that_learns_nothing_appends_nothing(tmp_path):
    bars = FakeBars(
        {
            "NUE": weekday_series(OBSERVED, 10, 100.0, 1.0),
            "SPY": weekday_series(OBSERVED, 10, 500.0, 0.0),
        }
    )
    now = datetime.combine(OBSERVED + timedelta(days=10), time(14), tzinfo=timezone.utc)
    first = engine(tmp_path, bars, now=now)
    first.rows_for({("NUE", OBSERVED)})
    size = (tmp_path / "forward.jsonl").stat().st_size

    second = engine(tmp_path, bars, now=now)  # same clock, same data
    second.rows_for({("NUE", OBSERVED)})
    assert (tmp_path / "forward.jsonl").stat().st_size == size


# ================================================================================
# The funnel flattener
# ================================================================================


def snapshot(source="trump_posts", signal_class=SignalClass.CLASS_1_REALTIME,
             content="Buy $NUE now", tickers=(), credibility_key=None, filer=None):
    return SignalSnapshot(
        signal_id="sig-1",
        source_id=source,
        signal_class=signal_class,
        observed_at=NOW,
        content=content,
        raw_content=content,
        external_id="x-1",
        credibility_key=credibility_key,
        filer=filer,
        tickers=tuple(tickers),
    )


def research_snapshot(confidence=71):
    return ResearchSnapshot(
        thesis="t", tickers=["NUE"], direction="long", time_horizon="weeks",
        priced_in_analysis=None, confidence=confidence, invalidation_condition="i",
        manipulation_assessment="none detected", flagged_manipulation=False,
    )


def sizing_snapshot(strategy=None):
    return SizingSnapshot(
        instrument="equity", sleeve="equity", confidence=71,
        sleeve_nav=Decimal("75000"), fraction_of_sleeve_nav=Decimal("0.025"),
        capital=Decimal("1875"), rationale="r", strategy=strategy,
    )


def decision(decision_id="d-1", approved=True, strategy=None, snap=None):
    return DecisionRecord(
        decision_id=decision_id,
        recorded_at=NOW,
        signal=snap or snapshot(tickers=("NUE",)),
        research=research_snapshot(),
        sizing=sizing_snapshot(strategy),
        gate=(
            GateSnapshot(approved=True, order={"symbol": "NUE"})
            if approved
            else GateSnapshot(approved=False, rejection_code="max_single_position_exceeded")
        ),
    )


def rejection(decision_id, stage, code, snap=None, research=None):
    return StageRejectionRecord(
        decision_id=decision_id,
        recorded_at=NOW,
        stage=stage,
        code=code,
        message="m",
        signal=snap or snapshot(tickers=("NUE",)),
        research=research,
    )


def test_the_funnel_buckets_every_terminal_state():
    entries = funnel_entries(
        [
            decision("d-1", approved=True),
            decision("d-2", approved=False),
            rejection("d-3", RejectedStage.SIZING, "confidence_below_floor",
                      research=research_snapshot(confidence=42)),
            rejection("d-4", RejectedStage.PRE_FILTER, "pre_filter"),
            rejection("d-5", RejectedStage.TRIAGE, "triage_no"),
            rejection("d-6", RejectedStage.RESEARCH, "upstream_error"),
        ]
    )
    by_id = {entry.decision_id: entry for entry in entries}
    assert by_id["d-1"].bucket == "traded"
    assert by_id["d-2"].bucket == "gate_rejected"
    assert by_id["d-2"].code == "max_single_position_exceeded"
    assert by_id["d-3"].bucket == "declined"
    assert by_id["d-3"].confidence == 42
    assert by_id["d-4"].bucket == "prefiltered"
    assert by_id["d-5"].bucket == "triaged_out"
    assert by_id["d-6"].bucket == "research_failed"


def test_mechanical_records_and_execution_retries_never_double_count():
    entries = funnel_entries(
        [
            decision("d-1", approved=True),
            # The broker refused later: shares the id, adds nothing.
            rejection("d-1", RejectedStage.EXECUTION, "rejected"),
            decision("d-2", approved=True, strategy="mechanical"),
        ]
    )
    assert [entry.decision_id for entry in entries] == ["d-1"]


def test_old_congressional_records_recover_their_ticker_from_content():
    """Records written before SignalSnapshot.tickers existed parse the labelled
    line the quiver renderer has always emitted."""
    old = snapshot(
        source="congressional_disclosures",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        content=(
            "Congressional trading disclosure (STOCK Act filing)\n"
            "representative: Test Member (house)\n"
            "ticker: INTC\n"
            "transaction: Purchase\n"
            "disclosure lag: 24 days between the trade and its disclosure"
        ),
        tickers=(),
    )
    entries = funnel_entries([rejection("d-1", RejectedStage.PRE_FILTER, "pre_filter", snap=old)])
    assert entries[0].tickers == ("INTC",)
    assert entries[0].lag_days == 24
    # And the structured field wins when present.
    assert snapshot_tickers(snapshot(tickers=("NUE",))) == ("NUE",)


def test_old_class1_records_recover_cashtags_from_content():
    old = snapshot(content="Loading up on $NVDA calls here", tickers=())
    entries = funnel_entries([rejection("d-1", RejectedStage.TRIAGE, "no", snap=old)])
    assert entries[0].tickers == ("NVDA",)


# ================================================================================
# The report
# ================================================================================


def test_the_report_renders_the_scoreboard(tmp_path):
    entries = funnel_entries(
        [
            decision("d-1", approved=True),
            rejection(
                "d-2",
                RejectedStage.SIZING,
                "confidence_below_floor",
                snap=snapshot(
                    source="congressional_disclosures",
                    signal_class=SignalClass.CLASS_2_MOMENTUM,
                    content="ticker: NUE\ntransaction: Purchase\n"
                    "disclosure lag: 10 days between the trade and its disclosure",
                    credibility_key="congressional_disclosures/Test Member",
                    filer="Test Member",
                ),
                research=research_snapshot(confidence=45),
            ),
        ]
    )
    observed = NOW.date()
    bars = FakeBars(
        {
            "NUE": weekday_series(observed, 40, 100.0, 1.0),
            "SPY": weekday_series(observed, 40, 500.0, 0.0),
        }
    )
    late = datetime.combine(observed + timedelta(days=30), time(14), tzinfo=timezone.utc)
    rows = engine(tmp_path, bars, now=late).rows_for(wanted_pairs(entries))
    report = render_forward_report(entries, rows)

    assert "Coverage: 2 funnel entries" in report
    assert "Declined vs taken" in report
    assert "taken (traded): mean" in report
    assert "declined by research/sizing: mean" in report
    assert "By funnel bucket" in report
    assert "trump_posts:" in report
    assert "Test Member" in report
    assert "8-14d lag" in report
    assert "humans rule" in report


def test_the_report_says_no_marks_rather_than_zero(tmp_path):
    entries = funnel_entries([decision("d-1", approved=True)])
    rows = engine(tmp_path, FakeBars({})).rows_for(wanted_pairs(entries))
    report = render_forward_report(entries, rows)
    assert "no resolved marks yet" in report
    assert "+0.00%" not in report


def test_horizons_are_the_ruled_set():
    assert HORIZONS == (1, 5, 20, 60, 120)
