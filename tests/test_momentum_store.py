"""The momentum store builder (human rulings 2026-10-14): ONE builder for the
live plan and the sanity backtest, the same universe on the same date
whatever the window, the Finnhub market-cap fallback, and failed fetches
never cached as answers."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from momentum import engine, feeds
from momentum.store import build

SPLIT_DAY = "2018-03-01"   # C splits 2-for-1 (before both decision dates)
LIVE_END = "2019-06-28"    # the live plan's data date
BACKTEST_END = "2019-12-31"


def weekdays(start: str, end: str) -> list[str]:
    day, out = date.fromisoformat(start), []
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


DAYS = weekdays("2016-01-04", BACKTEST_END)


def raw_close(sym: str, k: int, day: str) -> float:
    base = {"SPY": 200.0, "A": 50.0, "B": 40.0, "C": 300.0, "D": 60.0, "E": 30.0, "F": 2.0}[sym]
    price = base * (1.0004 ** k) * (1 + 0.01 * ((k * 7 + len(sym)) % 5 - 2) / 2)
    if sym == "C" and day >= SPLIT_DAY:
        price /= 2
    return price


#: true shares outstanding (C doubles at its split)
SHARES = {"A": 4e9, "B": 3e9, "C": 1e9, "D": 5e9, "E": 6e9, "F": 1e9}


def true_shares(sym: str, day: str) -> float:
    return SHARES[sym] * (2 if sym == "C" and day >= SPLIT_DAY else 1)


class FakeAlpaca:
    def __init__(self, cache) -> None:
        self.cache = cache

    def assets(self):
        return [{"id": s, "symbol": s, "name": f"{s} Corp", "status": "active", "exchange": "NYSE"}
                for s in ("A", "B", "C", "D", "E", "F")]

    def bars(self, symbols, start, end, adjustment):
        out = {}
        for sym in symbols:
            rows = []
            for k, day in enumerate(DAYS):
                if start[:10] <= day <= end[:10]:
                    p = raw_close(sym, k, day)
                    if adjustment == "all" and sym == "C" and day < SPLIT_DAY:
                        p /= 2  # split-adjusted history
                    rows.append([day, p])
            out[sym] = rows
        return out


CIKS = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}


class FakeSec:
    def __init__(self, cache) -> None:
        self.cache = cache
        self.failures: list[str] = []

    def company_ciks(self):
        return dict(CIKS)

    def name_index(self, wanted):
        return {}

    def filings(self, cik):
        periodic = weekdays("2015-02-02", BACKTEST_END)[::63]          # a filer all along
        if cik == 5:
            periodic = [d for d in periodic if d < "2017-01-01"]           # E: a 40-F filer since 2017
        return dict(cik=cik, sic=3571, us_filer=True, earnings=[], mergers=[], deal_8ks=[], periodic=periodic)

    def shares(self, cik):
        sym = {v: k for k, v in CIKS.items()}[cik]
        if sym in ("A", "E", "F"):   # filed every quarter, 40 days after the period
            ends = weekdays("2015-03-31", BACKTEST_END)[::63]
            return [[e, (date.fromisoformat(e) + timedelta(days=40)).isoformat(), true_shares(sym, e)]
                    for e in ends]
        if sym == "B":               # stopped tagging the count in 2016 (became multi-class)
            return [["2016-06-30", "2016-08-05", SHARES["B"]]]
        return []                    # C, D: never in the SEC API


def finnhub_factory(today: str, calls: list):
    k = DAYS.index(today)

    class FakeFinnhub:
        def __init__(self, cache) -> None:
            self.failures: list[str] = []

        def market_cap(self, symbol):
            calls.append(symbol)
            if symbol == "D":
                return None  # no profile
            return true_shares(symbol, today) * raw_close(symbol, k, today)

    return FakeFinnhub


def build_at(tmp_path, monkeypatch, name, start, end, version=2, calls=None):
    calls = [] if calls is None else calls
    monkeypatch.setattr(feeds, "Alpaca", FakeAlpaca)
    monkeypatch.setattr(feeds, "Sec", FakeSec)
    monkeypatch.setattr(feeds, "Finnhub", finnhub_factory(end, calls))
    store, stats = build(tmp_path / name, start=start, end=end, log=lambda m: None, benchmarks=(),
                         universe_version=version)
    return store, stats


def ranked(store, t):
    out = []
    for seg in engine.universe(store, t, 10, 5.0):
        shares = engine.cap_shares(store, seg, t - 1, store.raw[seg.key], store.adj[seg.key])
        out.append((seg.symbol, shares * store.raw[seg.key][t - 1]))
    return out


def test_live_plan_and_backtest_rank_the_same_universe_on_the_same_date(tmp_path, monkeypatch):
    # The live prep builds a 2.5-year window ending at its data date; the
    # backtest builds from 2016 to a later end. The decision on the session
    # after LIVE_END must rank the same companies at the same caps.
    live, live_stats = build_at(tmp_path, monkeypatch, "live", "2017-01-01", LIVE_END)
    back, _ = build_at(tmp_path, monkeypatch, "back", "2016-01-01", BACKTEST_END)
    decision_day = DAYS[DAYS.index(LIVE_END) + 1]
    view = engine.live_view(live, decision_day)
    got_live = ranked(view, len(view.calendar) - 1)
    got_back = ranked(back, back.index[decision_day])
    assert [s for s, _ in got_live] == [s for s, _ in got_back]
    assert [c for _, c in got_live] == pytest.approx([c for _, c in got_back], rel=1e-6)
    # C (fallback, never in the SEC API), A, B (fallback: its SEC count is
    # stale); D has neither, E no longer files 10-K/10-Q, F trades under $5
    assert [s for s, _ in got_live] == ["C", "A", "B"]
    caps = dict(got_live)
    k = DAYS.index(LIVE_END)
    for sym in ("A", "B", "C"):
        assert caps[sym] == pytest.approx(true_shares(sym, LIVE_END) * raw_close(sym, k, LIVE_END), rel=1e-6)
    assert live_stats["fallback_wanted"] == 3 and live_stats["fallback_found"] == 2  # B, C found; D none


def test_the_fallback_carries_back_through_splits_only(tmp_path, monkeypatch):
    back, _ = build_at(tmp_path, monkeypatch, "back", "2016-01-01", BACKTEST_END)
    seg = next(s for s in back.segments if s.symbol == "C")
    t = back.index["2017-06-30"]  # before the 2-for-1 split: half today's shares
    shares = engine.cap_shares(back, seg, t, back.raw[seg.key], back.adj[seg.key])
    assert shares == pytest.approx(true_shares("C", "2017-06-30"), rel=1e-5)


def test_version_1_is_the_universe_as_traded_in_october(tmp_path, monkeypatch):
    # No filer-window rule, the latest SEC count however old, no Finnhub call.
    calls: list = []
    store, stats = build_at(tmp_path, monkeypatch, "v1", "2016-01-01", BACKTEST_END, version=1, calls=calls)
    assert calls == [] and "fallback_found" not in stats
    assert all(seg.fallback is None for seg in store.segments)
    e = next(s for s in store.segments if s.symbol == "E")
    assert not engine.current_filer(e, store.calendar[-1])  # its dates are kept (the brief shows them)...
    t = len(store.calendar) - 1
    # ...but version 1 ranks on the old flag: E still a "US filer"; B on its 2016 count
    assert [s for s, _ in ranked(store, t)] == ["A", "E", "B"]


def test_a_cache_folder_serves_one_window(tmp_path, monkeypatch):
    build_at(tmp_path, monkeypatch, "f", "2017-01-01", LIVE_END)
    with pytest.raises(ValueError, match="fresh folder"):
        build_at(tmp_path, monkeypatch, "f", "2016-01-01", LIVE_END)


def test_stock_bars_ending_before_the_calendar_are_refused(tmp_path, monkeypatch):
    # SPY is fetched fresh, the stock bars come from a cache filled for an
    # earlier end: every stock would be blank on the last session.
    class Stale(FakeAlpaca):
        def bars(self, symbols, start, end, adjustment):
            got = super().bars(symbols, start, end, adjustment)
            return got if symbols == ["SPY"] else {s: rows[:-1] for s, rows in got.items()}

    monkeypatch.setattr(feeds, "Alpaca", Stale)
    monkeypatch.setattr(feeds, "Sec", FakeSec)
    with pytest.raises(ValueError, match="stock bars end 2019-06-27"):
        build(tmp_path / "s", start="2017-01-01", end=LIVE_END, log=lambda m: None, benchmarks=(),
              universe_version=1)


class _Response:
    def __init__(self, status: int, payload=None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, statuses: list) -> None:
        self.statuses = statuses

    def get(self, url):
        return _Response(self.statuses.pop(0))


def test_a_failed_share_fetch_is_never_cached_as_no_shares(tmp_path, monkeypatch):
    monkeypatch.setattr(feeds.time, "sleep", lambda s: None)
    sec = feeds.Sec(feeds.Cache(tmp_path / "c"), min_interval=0)
    sec._client = _Client([503] * 10)          # both concepts fail after 5 tries each
    assert sec.shares(42) == [] and sec.failures
    assert sec.cache.get("sec_shares_42") is None
    sec._client = _Client([404, 404])          # the SEC has no such fact: a definitive answer
    assert sec.shares(42) == []
    assert sec.cache.get("sec_shares_42") == []
