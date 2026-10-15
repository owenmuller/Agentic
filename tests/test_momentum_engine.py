"""The momentum engine on a synthetic store (human ruling 2026-10-13)."""
from __future__ import annotations

import math
import random
from array import array
from datetime import date, timedelta

from momentum import engine, feeds, rules
from momentum.feeds import Segment
from momentum.store import Store

NAN = float("nan")


def make_store(n_stocks=60, sessions=620, seed=7, delist=None, hole=None):
    rng = random.Random(seed)
    day, calendar = date(2016, 1, 4), []
    while len(calendar) < sessions:
        if day.weekday() < 5:
            calendar.append(day.isoformat())
        day += timedelta(days=1)
    spy = array("f", [100.0])
    for _ in range(sessions - 1):
        spy.append(spy[-1] * (1 + rng.gauss(0.0004, 0.01)))
    segs, adj, raw = [], {}, {}
    for i in range(n_stocks):
        drift = rng.gauss(0.0005, 0.0006)
        p = array("f", [20.0 + i])
        for k in range(sessions - 1):
            p.append(p[-1] * (1 + drift + rng.gauss(0, 0.012)))
        key = f"S{i:02d}@{calendar[0]}"
        last = sessions - 1
        if delist and i in delist:
            last = delist[i]
            for k in range(last + 1, sessions):
                p[k] = NAN
        if hole and i == hole[0]:
            p[hole[1]] = NAN
        events = list(range(40 + i % 20, last, 63))
        segs.append(Segment(key=key, symbol=f"S{i:02d}", first=0, last=last, cik=1000 + i, sector=1 + i % 6,
                            us_filer=True, events=events, pending=[], shares=[[0, 0, 1e6 * (1 + i)]]))
        adj[key], raw[key] = p, array("f", p)
    return Store(calendar, spy, {"SPMO": array("f", spy)}, segs, adj, raw)


def test_a_full_run_keeps_the_book_inside_its_rules():
    store = make_store()
    starts = engine.month_starts(store.calendar)
    run = engine.run(store, rules.V1, starts[13], len(store.calendar) - 1)
    assert run.decisions and all(len(d.holdings) <= 15 for d in run.decisions)
    for d in run.decisions:
        assert sum(d.holdings.values()) <= 1 + 1e-9 and all(w <= 0.10 + 1e-9 for w in d.holdings.values())
        assert 0 < d.exposure <= 1
    assert all(math.isfinite(v) and v > 0 for _, v in run.nav)


def test_a_delisted_holding_is_sold_at_its_last_close():
    store = make_store()
    starts = engine.month_starts(store.calendar)
    first = starts[13]
    probe = engine.run(store, rules.V1, first, len(store.calendar) - 1)
    victim = next(iter(probe.decisions[0].holdings))
    idx = int(victim[1:3])
    cut = first + 5
    store2 = make_store(delist={idx: cut})
    run = engine.run(store2, rules.V1, first, len(store2.calendar) - 1)
    assert any(k == victim for _, k in run.delisted)
    assert all(math.isfinite(v) and v > 0 for _, v in run.nav)


def test_a_missing_session_carries_the_last_price():
    store = make_store()
    starts = engine.month_starts(store.calendar)
    first = starts[13]
    probe = engine.run(store, rules.V1, first, first + 30)
    held = next(iter(probe.decisions[0].holdings))
    store2 = make_store(hole=(int(held[1:3]), first + 3))
    run = engine.run(store2, rules.V1, first, first + 30)
    navs = dict(run.nav)
    assert navs[first + 3] > 0.8 * navs[first + 2]  # no collapse to zero on the gap day


def test_plain_and_inverse_vol_variants_run():
    store = make_store()
    starts = engine.month_starts(store.calendar)
    for name in ("plain_12_1", "inverse_vol", "v1_1_no_veto", "v1_top_1000", "market_filter_only", "vol_scaling_only"):
        run = engine.run(store, rules.SHADOWS[name], starts[13], len(store.calendar) - 1)
        assert run.decisions and run.nav[-1][1] > 0


def test_ticker_reuse_is_split_into_companies():
    closes = array("f", [10.0] * 50 + [NAN] * 40 + [5.0] * 30)
    assert feeds.segments("BBBY", closes) == [(0, 49), (90, 119)]


def test_the_x_thread_holds_last_years_top_gainer():
    store = make_store()
    out, picks = engine.x_thread(store, 0, len(store.calendar) - 1, universe_size=60)
    assert picks and out[-1][1] > 0
    day, key, ret = picks[0]
    assert day.endswith("-01-03") or day[5:7] == "01"


def test_after_close_filings_count_to_the_next_session():
    cal = ["2026-10-12", "2026-10-13", "2026-10-14"]
    index = {d: i for i, d in enumerate(cal)}
    assert feeds.session_of("2026-10-13T19:59:00.000Z", cal, index) == 1   # 15:59 ET -> same day
    assert feeds.session_of("2026-10-13T20:05:00.000Z", cal, index) == 2   # 16:05 ET -> next day
    assert feeds.session_of("2026-10-13T11:00:00.000Z", cal, index) == 1   # 07:00 ET pre-market -> same day


def _truncated(store, n):
    segs = [Segment(**{**s.__dict__, "last": min(s.last, n - 1),
                       "events": [e for e in s.events if e < n]}) for s in store.segments if s.first < n]
    return Store(store.calendar[:n], store.spy[:n], {k: v[:n] for k, v in store.benchmarks.items()}, segs,
                 {s.key: store.adj[s.key][:n] for s in segs}, {s.key: store.raw[s.key][:n] for s in segs})


def test_the_live_view_decides_exactly_as_the_full_history_would():
    # The live prep decides on the morning of the rebalance day, with data
    # through the prior close: same holdings, same weights as the backtest's
    # decision on that day - and no read of the day itself is possible.
    store = make_store(sessions=620)
    starts = engine.month_starts(store.calendar)
    t = starts[-2]
    full = engine.run(store, rules.V1, starts[13], t - 1)
    expected = engine.decide(store, t, rules.V1, [], full.book_returns, starts)
    past = _truncated(store, t)
    view = engine.live_view(past, store.calendar[t])
    assert len(view.calendar) == t + 1 and len(view.spy) == t
    live_starts = engine.month_starts(view.calendar)
    assert live_starts[-1] == t
    sim = engine.run(view, rules.V1, live_starts[13], t - 1)
    assert sim.book_returns == full.book_returns
    got = engine.decide(view, t, rules.V1, [], sim.book_returns, live_starts)
    assert got.holdings == expected.holdings and got.exposure == expected.exposure
    try:
        engine.live_view(past, store.calendar[t - 1])
    except ValueError:
        pass
    else:
        raise AssertionError("a decision day inside the data must be refused")


def test_dormant_flat_prints_split_a_reused_ticker():
    # LINE: an old company's dormant OTC prints sat flat for years, then a new
    # company listed under the symbol. Flat runs are blanked, so the reuse is
    # a gap and the new company starts its own series (data review 2026-10-13).
    raw = array("f", [1.0, 0.9, 0.5] + [0.1616] * 60 + [72.5, 73.0, 71.8])
    adj = array("f", raw)
    blanked = feeds.drop_flat_runs(raw, adj)
    assert blanked == 59 and raw[3] == raw[3] and math.isnan(raw[4]) and math.isnan(adj[62])
    assert feeds.segments("LINE", adj) == [(0, 3), (63, 65)]
    short = array("f", [5.0, 5.0, 5.0, 5.1])  # a few equal closes are trading, not dormancy
    assert feeds.drop_flat_runs(short, array("f", short)) == 0


def test_the_universe_admits_only_current_10k_10q_filers_point_in_time():
    # Human ruling 2026-10-14: filed a 10-K or 10-Q within the last 18 months
    # (548 days), on or before the decision's data date.
    seg = Segment(key="X@2016-01-04", symbol="X", first=0, last=10, cik=1, us_filer=True,
                  periodic=["2023-03-01", "2024-02-20"])
    assert engine.current_filer(seg, "2025-08-01")          # 528 days after the last 10-K
    assert not engine.current_filer(seg, "2025-09-01")      # 559 days: lapsed (a 40-F filer now)
    assert not engine.current_filer(seg, "2023-01-02")      # nothing filed yet at that date
    old = Segment(key="Y@2016-01-04", symbol="Y", first=0, last=10, cik=2, us_filer=True)
    assert old.periodic is None and engine.current_filer(old, "2025-09-01")  # a pre-rule store: the old flag
