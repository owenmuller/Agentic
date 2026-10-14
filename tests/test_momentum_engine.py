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
    for name in ("plain_12_1", "inverse_vol", "top_500", "market_filter_only", "vol_scaling_only"):
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
