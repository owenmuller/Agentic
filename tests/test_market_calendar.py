"""The exchange calendar (sweep 2026-11-10, item 3): the 2026-11-26 closure,
the 2026-11-27 13:00 early close, and the fallback when it cannot be read."""
from datetime import date, datetime, timedelta, timezone

import datahealth
from execution.market_calendar import NY, MarketCalendar

ROWS = ([{"date": f"2026-11-{d:02d}", "open": "09:30", "close": "16:00"} for d in (23, 24, 25, 30)]
        + [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}])


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 25, 9, 0, tzinfo=NY).astimezone(timezone.utc)

    def __call__(self):
        return self.now


def test_the_holiday_is_no_session_and_the_early_close_moves_every_cutoff():
    asked = []
    calendar = MarketCalendar(lambda start, end: asked.append((start, end)) or ROWS, Clock())
    assert calendar.session(date(2026, 11, 26)) is None
    friday = calendar.session(date(2026, 11, 27))
    assert friday.close == datetime(2026, 11, 27, 13, 0, tzinfo=NY) and friday.early and not friday.assumed
    assert friday.at("15:50") == datetime(2026, 11, 27, 12, 50, tzinfo=NY)
    assert friday.at("15:45") == datetime(2026, 11, 27, 12, 45, tzinfo=NY)
    assert friday.at("16:05") == datetime(2026, 11, 27, 13, 5, tzinfo=NY)
    wednesday = calendar.session(date(2026, 11, 25))
    assert wednesday.at("15:50") == datetime(2026, 11, 25, 15, 50, tzinfo=NY) and not wednesday.early
    assert calendar.previous_session(date(2026, 11, 27)) == date(2026, 11, 25)
    assert calendar.sessions_between(date(2026, 11, 23), date(2026, 11, 27)) == 4
    assert calendar.session(date(2026, 11, 28)) is None                      # a Saturday
    assert len(asked) == 1                                                    # one fetch for the window


def test_an_unreadable_calendar_is_a_yellow_card_and_assumes_an_early_close(monkeypatch):
    reports = []
    monkeypatch.setattr(datahealth, "report", lambda source, message: reports.append((source, message)))
    clock, calls = Clock(), []

    def fetch(start, end):
        calls.append(1)
        raise RuntimeError("Alpaca calendar HTTP 503")
    calendar = MarketCalendar(fetch, clock, source="Test calendar")
    assumed = calendar.session(date(2026, 11, 25))
    assert assumed.assumed and assumed.close == datetime(2026, 11, 25, 13, 0, tzinfo=NY)
    assert assumed.at("15:50") == datetime(2026, 11, 25, 12, 50, tzinfo=NY)      # earlier: less risk
    assert calendar.session(date(2026, 11, 28)) is None                            # a weekend stays closed
    assert reports and reports[0][0] == "Test calendar" and "13:00" in reports[0][1]
    calendar.session(date(2026, 11, 24))
    assert len(calls) == 1                                                         # not hammered meanwhile
    clock.now += timedelta(minutes=16)                                             # never cached as an answer
    calendar.session(date(2026, 11, 24))
    assert len(calls) == 2


def test_an_empty_calendar_is_a_failure_not_a_closed_month(monkeypatch):
    reports = []
    monkeypatch.setattr(datahealth, "report", lambda source, message: reports.append(message))
    calendar = MarketCalendar(lambda start, end: [], Clock())
    assert calendar.session(date(2026, 11, 25)).assumed and reports


def test_without_a_source_every_weekday_is_a_regular_session():
    calendar = MarketCalendar.weekdays(Clock())
    assert calendar.session(date(2026, 11, 26)).close == datetime(2026, 11, 26, 16, 0, tzinfo=NY)
    assert calendar.session(date(2026, 11, 29)) is None


def test_the_main_book_session_and_the_momentum_cutoff_follow_the_calendar():
    from types import SimpleNamespace

    from orchestrator import ops
    from orchestrator.momentum_sleeve import MomentumSleeve

    clock = Clock()
    ops.install_calendar(MarketCalendar(lambda start, end: ROWS, clock))
    try:
        assert not ops.is_trading_weekday(datetime(2026, 11, 26, 10, 0, tzinfo=NY))
        _, close = ops.session_bounds(datetime(2026, 11, 27, 10, 0, tzinfo=NY))
        assert close == datetime(2026, 11, 27, 13, 0, tzinfo=NY)
        sleeve = object.__new__(MomentumSleeve)
        sleeve._config = SimpleNamespace(trade_before="15:30")
        assert sleeve._trade_before(datetime(2026, 11, 27, 10, 0, tzinfo=NY)) == datetime(2026, 11, 27, 12, 30, tzinfo=NY)
        assert sleeve._trade_before(datetime(2026, 11, 25, 10, 0, tzinfo=NY)) == datetime(2026, 11, 25, 15, 30, tzinfo=NY)
    finally:
        ops.install_calendar(None)
    # an unreadable calendar keeps the regular day for the session (exits stay watched)
    ops.install_calendar(MarketCalendar(lambda start, end: [], clock))
    try:
        _, close = ops.session_bounds(datetime(2026, 11, 25, 10, 0, tzinfo=NY))
        assert close == datetime(2026, 11, 25, 16, 0, tzinfo=NY)
    finally:
        ops.install_calendar(None)
