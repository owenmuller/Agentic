"""Earnings shadow logger tests.

The claims that matter: the implied move is the market's price and not a model of
one; an unreadable calendar is never recorded as an empty one; the log is
idempotent across passes; the settle phase marks THE SAME contracts rather than
re-deriving a straddle; and — the one that makes the ruling safe — there is no
order path anywhere in the package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import httpx
import pytest

from earnings.calendar import (
    EarningsCalendarError,
    EarningsEvent,
    FinnhubEarningsCalendar,
)
from earnings.config import EarningsConfig
from earnings.implied import atm_straddle
from earnings.realised import realised_move_pct
from earnings.shadow import ShadowLog, ShadowObserver

NOW = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)
PRINT_DATE = date(2026, 9, 2)


@dataclass(frozen=True)
class Quote:
    """Shaped like execution.options_data.OptionQuote — structural typing, no import."""

    occ_symbol: str
    right: str
    expiration: date
    strike: Decimal
    bid: Decimal
    ask: Decimal
    implied_volatility: Optional[Decimal] = None
    open_interest: int = 1000

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> Decimal:
        return (self.ask - self.bid) / self.mid


def chain_at(expiry=date(2026, 9, 4), strikes=(95, 100, 105), premium="4.00"):
    quotes = []
    for strike in strikes:
        for right in ("call", "put"):
            quotes.append(
                Quote(
                    occ_symbol=f"NVDA{expiry:%y%m%d}{right[0].upper()}{strike:08d}",
                    right=right,
                    expiration=expiry,
                    strike=Decimal(strike),
                    bid=Decimal(premium) - Decimal("0.10"),
                    ask=Decimal(premium) + Decimal("0.10"),
                    implied_volatility=Decimal("0.62"),
                )
            )
    return quotes


class FakeChain:
    def __init__(self, chain=None, mids=None):
        self._chain = chain if chain is not None else chain_at()
        self._mids = mids or {}
        self.mid_calls: list[str] = []

    def chain_for(self, underlying, *, min_expiry, max_expiry=None):
        return list(self._chain) if self._chain is not None else None

    def option_mid(self, occ_symbol):
        self.mid_calls.append(occ_symbol)
        return self._mids.get(occ_symbol)


class FakeBars:
    def __init__(self, bars=None):
        self._bars = bars if bars is not None else [
            {"t": "2026-09-01", "c": "100"},
            {"t": "2026-09-02", "c": "100"},
            {"t": "2026-09-03", "c": "118"},
        ]

    def bars(self, symbol, start, end):
        return list(self._bars)


class FakeCalendar:
    def __init__(self, *events, error=None):
        self._events = list(events)
        self._error = error
        self.calls = 0

    def upcoming(self, start, end):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return [e for e in self._events if start <= e.report_date <= end]


def config(**overrides):
    base = {
        "version": 1,
        "enabled": True,
        "universe": ("NVDA",),
        "calendar_window_days": 14,
        "arm_within_days": 3,
        "min_days_after_print": 1,
        "max_days_after_print": 21,
        "min_open_interest": 100,
        "max_spread_pct_of_mid": "0.20",
        "settle_after_days": 1,
    }
    return EarningsConfig.model_validate({**base, **overrides})


def observer(tmp_path, *, calendar=None, chain=None, bars=None, spot="100.00",
             clock=None, cfg=None):
    return ShadowObserver(
        config=cfg or config(),
        calendar=calendar or FakeCalendar(EarningsEvent("NVDA", PRINT_DATE, "amc")),
        chain=chain or FakeChain(),
        bars=bars or FakeBars(),
        spot=lambda symbol: Decimal(spot) if spot else None,
        log=ShadowLog(tmp_path / "shadow.jsonl"),
        clock=clock or (lambda: NOW),
    )


def records(tmp_path, kind=None):
    path: Path = tmp_path / "shadow.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if kind is None or r.get("kind") == kind]


# ================================================================================
# The implied move
# ================================================================================


def test_the_straddle_prices_the_event_at_the_nearest_strike():
    move = atm_straddle(
        chain_at(), Decimal("100"),
        earliest_expiry=date(2026, 9, 3), latest_expiry=date(2026, 9, 23),
    )
    assert move is not None
    assert move.strike == Decimal("100")
    assert move.straddle_cost == Decimal("8.00")  # 4.00 + 4.00
    assert move.implied_move_pct == Decimal("8.00")  # 8 on a 100 spot
    assert move.atm_iv == Decimal("0.62")


def test_a_tie_between_strikes_resolves_the_same_way_every_time():
    """Determinism matters more than which side wins: the same chain must always
    produce the same straddle, or the series is not comparable with itself."""
    spot = Decimal("100")  # exactly between 95 and 105
    chain = chain_at(strikes=(95, 105))
    first = atm_straddle(chain, spot, earliest_expiry=date(2026, 9, 3),
                         latest_expiry=date(2026, 9, 23))
    second = atm_straddle(list(reversed(chain)), spot,
                          earliest_expiry=date(2026, 9, 3),
                          latest_expiry=date(2026, 9, 23))
    assert first.strike == second.strike == Decimal("95")


def test_an_illiquid_straddle_is_no_straddle():
    """Recording a price nobody would fill would poison the series this exists
    to build."""
    thin = [
        Quote(q.occ_symbol, q.right, q.expiration, q.strike, q.bid, q.ask,
              q.implied_volatility, open_interest=10)
        for q in chain_at()
    ]
    assert atm_straddle(
        thin, Decimal("100"), earliest_expiry=date(2026, 9, 3),
        latest_expiry=date(2026, 9, 23), min_open_interest=100,
    ) is None


def test_a_wide_straddle_is_no_straddle():
    wide = [
        Quote(q.occ_symbol, q.right, q.expiration, q.strike,
              Decimal("1.00"), Decimal("9.00"), q.implied_volatility)
        for q in chain_at()
    ]
    assert atm_straddle(
        wide, Decimal("100"), earliest_expiry=date(2026, 9, 3),
        latest_expiry=date(2026, 9, 23),
        max_spread_pct_of_mid=Decimal("0.20"),
    ) is None


def test_no_chain_and_no_spot_produce_nothing_rather_than_a_guess():
    assert atm_straddle([], Decimal("100"), earliest_expiry=date(2026, 9, 3),
                        latest_expiry=date(2026, 9, 23)) is None
    assert atm_straddle(chain_at(), Decimal("0"), earliest_expiry=date(2026, 9, 3),
                        latest_expiry=date(2026, 9, 23)) is None


# ================================================================================
# The realised move
# ================================================================================


def test_the_session_decides_which_close_the_move_is_measured_from():
    bars = [
        {"t": "2026-09-01", "c": "100"},
        {"t": "2026-09-02", "c": "110"},
        {"t": "2026-09-03", "c": "121"},
    ]
    # Reported after the close on the 2nd: the 3rd carries the move.
    assert realised_move_pct(bars, date(2026, 9, 2), "amc") == Decimal("10.00")
    # Reported before the open on the 2nd: the 2nd carries it.
    assert realised_move_pct(bars, date(2026, 9, 2), "bmo") == Decimal("10.00")
    # Session unknown: span both, which cannot understate the move.
    assert realised_move_pct(bars, date(2026, 9, 2), "") == Decimal("21.00")


def test_a_move_with_no_bars_around_it_is_absent_not_zero():
    assert realised_move_pct([{"t": "2026-09-01", "c": "100"}], PRINT_DATE, "amc") is None
    assert realised_move_pct([], PRINT_DATE, "amc") is None


# ================================================================================
# Arming
# ================================================================================


def test_a_print_inside_the_window_is_armed_with_what_the_market_charges(tmp_path):
    report = observer(tmp_path).run()
    assert report.armed == 1

    armed = records(tmp_path, "armed")[0]
    assert armed["symbol"] == "NVDA"
    assert armed["earnings_date"] == "2026-09-02"
    assert armed["implied_move_pct"] == "8.00"
    assert armed["straddle_cost"] == "8.00"
    assert armed["days_before_print"] == 1
    assert armed["call_symbol"] and armed["put_symbol"]


def test_a_print_beyond_the_arming_window_waits(tmp_path):
    far = FakeCalendar(EarningsEvent("NVDA", date(2026, 9, 12), "amc"))
    assert observer(tmp_path, calendar=far).run().armed == 0
    assert records(tmp_path, "armed") == []


def test_a_name_outside_the_universe_is_never_armed(tmp_path):
    off = FakeCalendar(EarningsEvent("TSLA", PRINT_DATE, "amc"))
    assert observer(tmp_path, calendar=off).run().armed == 0


def test_arming_is_idempotent_across_passes(tmp_path):
    """The file is the state. A pass that runs twice writes once."""
    watcher = observer(tmp_path)
    assert watcher.run().armed == 1
    assert watcher.run().armed == 0
    assert len(records(tmp_path, "armed")) == 1


def test_an_unreadable_calendar_is_recorded_as_unreadable_not_as_empty(tmp_path):
    """The failure this exercise cannot afford: an incomplete series that looks
    complete. No key is not "no earnings this fortnight"."""
    blind = FakeCalendar(error=EarningsCalendarError("FINNHUB_API_KEY is not set"))
    report = observer(tmp_path, calendar=blind).run()

    assert report.armed == 0
    assert any("calendar unavailable" in note for note in report.skipped)
    assert any("FINNHUB_API_KEY" in note for note in report.skipped)
    assert records(tmp_path, "armed") == []
    # ...and the IV history keeps accumulating anyway: it needs no calendar, and
    # it is the artefact this system cannot buy. A missing key costs the prints,
    # not the series.
    assert report.iv_snapshots == 1
    assert len(records(tmp_path, "iv")) == 1


def test_a_name_with_no_usable_straddle_records_why(tmp_path):
    report = observer(tmp_path, chain=FakeChain(chain=[])).run()
    assert report.armed == 0
    skipped = records(tmp_path, "arm_skipped")
    assert len(skipped) == 1
    assert "straddle" in skipped[0]["reason"]


def test_the_daily_iv_snapshot_builds_the_history_the_system_lacks(tmp_path):
    """chain-internal IV percentile cannot see a whole surface lifted together.
    An IV rank against history needs stored history, and this is where it starts."""
    watcher = observer(tmp_path)
    assert watcher.run().iv_snapshots == 1
    snapshot = records(tmp_path, "iv")[0]
    assert snapshot["symbol"] == "NVDA"
    assert snapshot["atm_iv"] == "0.62"
    # One per name per day, not one per pass.
    assert watcher.run().iv_snapshots == 0


# ================================================================================
# Settling
# ================================================================================


def settled_observer(tmp_path, **kwargs):
    """Arm on the 1st, then settle on the 3rd."""
    clock = {"now": NOW}
    chain = kwargs.pop("chain", None) or FakeChain(
        mids={
            q.occ_symbol: Decimal("9.00")
            for q in chain_at()
            if q.strike == Decimal("100")
        }
    )
    watcher = observer(tmp_path, chain=chain, clock=lambda: clock["now"], **kwargs)
    watcher.run()
    clock["now"] = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
    return watcher, chain


def test_a_resolved_print_records_implied_against_realised(tmp_path):
    watcher, _ = settled_observer(tmp_path)
    assert watcher.run().settled == 1

    resolved = records(tmp_path, "resolved")[0]
    assert resolved["implied_move_pct"] == "8.00"
    assert resolved["realised_move_pct"] == "18.00"
    # THE claim, in one field.
    assert resolved["realised_exceeded_implied"] is True


def test_the_settle_marks_the_same_two_contracts_not_a_new_straddle(tmp_path):
    """A real mark of what was actually bought, which is why the P&L is not a
    payoff model."""
    watcher, chain = settled_observer(tmp_path)
    watcher.run()

    armed = records(tmp_path, "armed")[0]
    assert set(chain.mid_calls) == {armed["call_symbol"], armed["put_symbol"]}
    resolved = records(tmp_path, "resolved")[0]
    assert resolved["straddle_value_after"] == "18.00"  # 9.00 + 9.00
    assert resolved["hypothetical_straddle_pnl_pct"] == "125.00"  # 8 -> 18
    assert resolved["marks_available"] is True


def test_a_print_with_no_post_marks_still_records_the_move(tmp_path):
    """The underlying's move is the falsification test; the straddle P&L is a
    bonus. Losing the marks must not lose the observation."""
    watcher, _ = settled_observer(tmp_path, chain=FakeChain(mids={}))
    watcher.run()

    resolved = records(tmp_path, "resolved")[0]
    assert resolved["realised_exceeded_implied"] is True
    assert resolved["straddle_value_after"] is None
    assert resolved["hypothetical_straddle_pnl_pct"] is None
    assert resolved["marks_available"] is False


def test_settling_is_idempotent(tmp_path):
    watcher, _ = settled_observer(tmp_path)
    assert watcher.run().settled == 1
    assert watcher.run().settled == 0
    assert len(records(tmp_path, "resolved")) == 1


def test_a_print_that_has_not_reported_yet_is_not_settled(tmp_path):
    watcher = observer(tmp_path)
    watcher.run()
    assert watcher.run().settled == 0  # still 2026-09-01, print is the 2nd


def test_a_realised_move_inside_the_implied_one_is_recorded_as_such(tmp_path):
    """The result that kills the strategy, and it has to be as easy to record as
    the one that supports it."""
    quiet = FakeBars([
        {"t": "2026-09-01", "c": "100"},
        {"t": "2026-09-02", "c": "100"},
        {"t": "2026-09-03", "c": "102"},
    ])
    watcher, _ = settled_observer(tmp_path, bars=quiet)
    watcher.run()
    resolved = records(tmp_path, "resolved")[0]
    assert resolved["realised_move_pct"] == "2.00"
    assert resolved["realised_exceeded_implied"] is False


# ================================================================================
# The switches, and the guarantee
# ================================================================================


def test_disabled_does_nothing_and_says_so(tmp_path):
    report = observer(tmp_path, cfg=config(enabled=False)).run()
    assert report.armed == 0 and report.settled == 0
    assert "disabled" in report.skipped[0]
    assert not (tmp_path / "shadow.jsonl").exists()


def test_an_empty_universe_watches_nothing_rather_than_everything(tmp_path):
    report = observer(tmp_path, cfg=config(universe=())).run()
    assert "universe is empty" in report.skipped[0]


def test_the_config_refuses_an_inverted_expiry_window():
    with pytest.raises(ValueError, match="inverted"):
        config(min_days_after_print=30, max_days_after_print=7)


def test_the_config_refuses_to_arm_beyond_what_it_scans():
    with pytest.raises(ValueError, match="armed before they are ever seen"):
        config(calendar_window_days=2, arm_within_days=10)


def test_the_shipped_config_is_loadable_and_watches_something():
    live = EarningsConfig.load()
    assert live.universe, "the shipped universe must not be empty"
    assert live.arm_within_days <= live.calendar_window_days


def test_the_earnings_package_cannot_reach_an_order_path():
    """The ruling's guarantee, enforced by reading the source. No gate, no
    adapter, no order schema, no LLM — not by discipline, by absence."""
    package = Path(__file__).resolve().parents[1] / "src" / "earnings"
    forbidden = (
        "risk_gate",
        "RiskGate",
        "ApprovedOrder",
        "submit_order",
        "BrokerAdapter",
        "EquityBuyOrder",
        "OptionBuyToOpenOrder",
        "LLMClient",
        "anthropic",
        "from audit",
        "from orchestrator",
        "from sizing",
        "from research",
    )
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in source, f"{path.name} mentions {needle}"


# ================================================================================
# The Finnhub adapter
# ================================================================================


def calendar_with(response: httpx.Response):
    def handler(request):
        calendar_with.last_request = request
        return response

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return FinnhubEarningsCalendar(client, api_key="test-key", sleeper=lambda s: None)


def test_the_calendar_parses_a_finnhub_response():
    payload = {
        "earningsCalendar": [
            {"symbol": "NVDA", "date": "2026-09-02", "hour": "amc",
             "epsEstimate": 1.25},
            {"symbol": "AAPL", "date": "2026-09-04", "hour": "bmo"},
        ]
    }
    events = calendar_with(httpx.Response(200, json=payload)).upcoming(
        date(2026, 9, 1), date(2026, 9, 15)
    )
    assert [e.symbol for e in events] == ["NVDA", "AAPL"]
    assert events[0].session == "amc" and events[0].session_known
    assert events[0].eps_estimate == 1.25


def test_a_row_with_no_session_is_unknown_not_assumed():
    payload = {"earningsCalendar": [{"symbol": "NVDA", "date": "2026-09-02"}]}
    event = calendar_with(httpx.Response(200, json=payload)).upcoming(
        date(2026, 9, 1), date(2026, 9, 15)
    )[0]
    assert event.session == ""
    assert not event.session_known


def test_a_missing_key_refuses_before_any_request(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    client = FinnhubEarningsCalendar(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        sleeper=lambda s: None,
    )
    with pytest.raises(EarningsCalendarError, match="FINNHUB_API_KEY"):
        client.upcoming(date(2026, 9, 1), date(2026, 9, 15))


def test_a_refused_key_and_a_rate_limit_are_distinct_loud_errors():
    with pytest.raises(EarningsCalendarError, match="refused the API key"):
        calendar_with(httpx.Response(401, json={})).upcoming(
            date(2026, 9, 1), date(2026, 9, 15)
        )
    with pytest.raises(EarningsCalendarError, match="60 calls per minute"):
        calendar_with(httpx.Response(429, json={})).upcoming(
            date(2026, 9, 1), date(2026, 9, 15)
        )


def test_a_malformed_body_is_an_error_not_an_empty_calendar():
    with pytest.raises(EarningsCalendarError, match="no earningsCalendar list"):
        calendar_with(httpx.Response(200, json={"detail": "quota"})).upcoming(
            date(2026, 9, 1), date(2026, 9, 15)
        )
