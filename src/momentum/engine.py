"""The momentum book's monthly engine (human rulings 2026-10-13), pure over a
``Store``: the SAME ``decide()`` drives the sanity backtest, the live sleeve
(one decision on the first session of the month) and the shadows.

Timing: a decision on session ``t`` (the first session of a month) uses data
through ``t - 1`` (the prior close); the backtest fills at ``t``'s close at
``cost_per_side`` of traded notional. A position whose company stops trading
is sold at its last close (no delisting return beyond it - flagged).
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Optional

from momentum import rules
from momentum.rules import Candidate, Spec

NAN = float("nan")


def ok(x: float) -> bool:
    return x == x and x > 0  # not NaN, positive


def month_starts(calendar: list[str]) -> list[int]:
    return [i for i in range(1, len(calendar)) if calendar[i][:7] != calendar[i - 1][:7]]


class Returns:
    """Lazy daily returns of one series: ``r[k] = p[k] / p[k-1] - 1``."""

    def __init__(self, prices) -> None:
        self.p = prices

    def __getitem__(self, k: int) -> Optional[float]:
        if k <= 0 or k >= len(self.p):
            return None
        a, b = self.p[k - 1], self.p[k]
        return b / a - 1 if ok(a) and ok(b) else None


def shares_at(seg, t: int, raw, adj) -> Optional[float]:
    """Shares outstanding known at session ``t`` (point in time), carried to
    ``t`` through any split since the reported period end."""
    latest = None
    for end_i, filed_i, value in seg.shares:
        if filed_i <= t:
            latest = (end_i, value)
        else:
            break
    if latest is None:
        return None
    end_i, value = latest
    ra, aa = raw[end_i], adj[end_i]
    rb, ab = raw[t], adj[t]
    if not (ok(ra) and ok(aa) and ok(rb) and ok(ab)):
        return value
    return value * (ra / aa) / (rb / ab)


def universe(store, t: int, size: int, min_price: float) -> list:
    """The top ``size`` US companies by market cap at the close before ``t``
    (SEC shares x close), price above ``min_price``; one line per company
    (the lower-priced share class). Memoized per store: every variant asks
    for the same sessions."""
    memo = store.__dict__.setdefault("_universe_memo", {})
    key = (t, size, min_price)
    if key not in memo:
        memo[key] = _universe(store, t, size, min_price)
    return memo[key]


def _universe(store, t: int, size: int, min_price: float) -> list:
    d = t - 1
    best: dict[int, tuple] = {}
    for seg in store.segments:
        if not seg.us_filer or not (seg.first <= d <= seg.last):
            continue
        raw, adj = store.raw[seg.key], store.adj[seg.key]
        price = raw[d]
        if not ok(price) or price <= min_price:
            continue
        shares = shares_at(seg, d, raw, adj)
        if not shares:
            continue
        cap = shares * price
        prior = best.get(seg.cik)
        if prior is None or price < prior[1]:
            best[seg.cik] = (cap, price, seg)
    ranked = sorted(best.values(), key=lambda x: -x[0])[:size]
    return [seg for _, _, seg in ranked]


def live_view(store, decision_day: str):
    """The store as of the morning of ``decision_day`` (the session after its
    last one): the calendar gains that day, the price arrays do not - so a
    decision on it reads only the prior close, and any read of the day itself
    is an IndexError rather than a silent lookahead."""
    if decision_day <= store.calendar[-1]:
        raise ValueError(f"{decision_day} is not after the data's last session {store.calendar[-1]}")
    view = copy.copy(store)
    view.__dict__.pop("_universe_memo", None)
    view.calendar = list(store.calendar) + [decision_day]
    view.index = {d: i for i, d in enumerate(view.calendar)}
    return view


def month_end_closes(prices, starts: list[int], t: int, months: int) -> list:
    """Closes at the last ``months`` month-ends before ``t`` (oldest first)."""
    ends = [s - 1 for s in starts if s <= t][-months:]
    return [prices[e] for e in ends]


def monthly_returns(closes: list) -> list:
    return [b / a - 1 if ok(a) and ok(b) else None for a, b in zip(closes, closes[1:])]


@dataclass
class Decision:
    t: int
    holdings: dict                       # key -> weight AFTER vol scaling
    selection: rules.Selection
    exposure: float
    buys_allowed: bool
    unscaled: dict                       # key -> weight before vol scaling
    details: dict = field(default_factory=dict)


def decide(store, t: int, spec: Spec, held: list, book_returns: list, starts: list[int],
           vetoed: Optional[dict] = None, explain: bool = False) -> Decision:
    d = t - 1
    members = universe(store, t, spec.universe, spec.min_price)
    spy_closes = month_end_closes(store.spy, starts, t, spec.regression_months + 1)
    market = monthly_returns(spy_closes)
    stock_months = {}
    for seg in members:
        closes = month_end_closes(store.adj[seg.key], starts, t, spec.regression_months + 1)
        stock_months[seg.key] = monthly_returns(closes)
    # own-universe industry: equal-weighted mean of the sector's members, by month
    industry: dict[int, list] = {}
    sectors = {seg.key: seg.sector for seg in members}
    for sector in set(sectors.values()):
        series = []
        for m in range(len(market)):
            xs = [stock_months[k][m] for k, s in sectors.items() if s == sector
                  and m < len(stock_months[k]) and stock_months[k][m] is not None]
            series.append(sum(xs) / len(xs) if xs else None)
        industry[sector] = series

    spy_r = Returns(store.spy)
    raw_scores, earn_scores, plain_scores = {}, {}, {}
    for seg in members:
        ys = stock_months[seg.key]
        triples = [(y, x, z) for y, x, z in zip(ys, market, industry[seg.sector])
                   if y is not None and x is not None and z is not None]
        if spec.score == "plain_12_1":
            closes = month_end_closes(store.adj[seg.key], starts, t, 13)
            plain_scores[seg.key] = rules.plain_momentum(closes) if all(ok(c) for c in closes) else None
            continue
        # only an unbroken run of months ending at t-1 is a regression window
        if len(triples) != len(ys):
            tail = []
            for y, x, z in zip(reversed(ys), reversed(market), reversed(industry[seg.sector])):
                if y is None or x is None or z is None:
                    break
                tail.append((y, x, z))
            triples = list(reversed(tail))
        raw_scores[seg.key] = rules.residual_momentum([a for a, _, _ in triples], [b for _, b, _ in triples],
                                                      [c for _, _, c in triples], spec)
        earn_scores[seg.key] = rules.earnings_momentum(seg.events, Returns(store.adj[seg.key]), spy_r, today=t)

    zr: dict = {}
    ze: dict = {}
    if spec.score == "plain_12_1":
        scored = {k: v for k, v in plain_scores.items() if v is not None}
        z = rules.zscores(scored)
        score = dict(z)
    else:
        scored_keys = [k for k, v in raw_scores.items() if v is not None]
        zr = rules.zscores({k: raw_scores[k] for k in scored_keys})
        ze = rules.zscores({k: earn_scores.get(k) for k in scored_keys})
        score = {k: 0.5 * zr[k] + 0.5 * ze[k] for k in scored_keys}

    candidates = []
    vols = {}
    for seg in members:
        if seg.key not in score:
            continue
        adj = store.adj[seg.key]
        lo = max(0, d - 130)
        closes = [adj[i] for i in range(lo, d + 1) if ok(adj[i])]
        rets = [Returns(adj)[i] for i in range(max(1, d - spec.gap_lookback + 1), d + 1)]
        pending = any(a <= d <= b for a, b in seg.pending)
        vol = rules.realized_vol([Returns(adj)[i] for i in range(max(1, d - spec.vol_days + 1), d + 1)],
                                 spec.vol_days)
        vols[seg.key] = vol
        candidates.append(Candidate(seg.key, score[seg.key], seg.sector, vol,
                                    rules.excluded(rets, closes, pending, spec)))

    spy_hist = [store.spy[i] for i in range(max(0, d - 260), d + 1) if ok(store.spy[i])]
    blocked = spec.market_filter and rules.market_filter_blocks(spy_hist)
    selection = rules.select(candidates, held, spec, buys_allowed=not blocked, vetoed=vetoed)
    unscaled = rules.weights(list(selection.holdings), vols, spec)

    scale = 1.0
    current_vol = target_vol = None
    if spec.vol_scaling and unscaled:
        invested = sum(unscaled.values())
        port = []
        for i in range(max(1, d - spec.vol_days + 1), d + 1):
            total, ok_all = 0.0, True
            for k, w in unscaled.items():
                r = Returns(store.adj[k])[i]
                if r is None:
                    continue
                total += w / invested * r
            port.append(total)
        current_vol = rules.realized_vol(port, spec.vol_days)
        history = [r for r in book_returns[-spec.target_vol_days:] if r is not None]
        if len(history) >= spec.min_target_vol_days:
            target_vol = rules.stdev(history) * math.sqrt(252)
        scale = rules.exposure(target_vol, current_vol)
    final = {k: w * scale for k, w in unscaled.items()}
    return Decision(t, final, selection, scale, not blocked, unscaled,
                    dict(universe=len(members), scored=len(score), current_vol=current_vol, target_vol=target_vol,
                         excluded={c.key: c.exclusion for c in candidates if c.exclusion},
                         **(dict(score=score, residual_z=zr, earnings_z=ze) if explain else {})))


@dataclass
class Run:
    spec: Spec
    nav: list                     # (session index, NAV)
    decisions: list
    turnover: float
    delisted: list
    #: the unscaled book's daily returns (vol scaling's target history)
    book_returns: list = field(default_factory=list)


def run(store, spec: Spec, start: int, end: int) -> Run:
    """Simulate ``spec`` from the first month start at/after ``start``."""
    starts = month_starts(store.calendar)
    start_set = set(starts)
    nav = 1.0
    cash = 1.0
    units: dict = {}              # key -> units of adjusted price
    marks: dict = {}              # key -> last known adjusted price
    unscaled: dict = {}           # key -> unscaled weight at the last rebalance
    book_returns: list = []
    out_nav, decisions, delisted = [], [], []
    turnover = 0.0
    first = min(s for s in starts if s >= start)
    for t in range(first, end + 1):
        # mark; a missing session carries the last price; a company that
        # stopped trading is sold at its last close
        value = cash
        for k in list(units):
            seg = store.by_key[k]
            if t > seg.last:
                cash += units[k] * marks[k]
                value += units[k] * marks[k]
                delisted.append((store.calendar[t], k))
                del units[k]
                unscaled.pop(k, None)
                marks.pop(k, None)
                continue
            p = store.adj[k][t]
            if ok(p):
                marks[k] = p
            value += units[k] * marks[k]
        prev_nav = nav
        nav = value if units or cash else nav
        # the unscaled book's return (the vol-scaling target's history)
        if unscaled:
            inv = sum(unscaled.values())
            r = 0.0
            for k, w in unscaled.items():
                x = Returns(store.adj[k])[t]
                if x is not None:
                    r += w / inv * x
            book_returns.append(r)
        if t in start_set and t >= first:
            dec = decide(store, t, spec, list(units), book_returns, starts)
            decisions.append(dec)
            target = {k: w * nav for k, w in dec.holdings.items()}
            current = {k: units[k] * store.adj[k][t] for k in units if ok(store.adj[k][t])}
            traded = sum(abs(target.get(k, 0.0) - current.get(k, 0.0)) for k in set(target) | set(current))
            cost = traded * spec.cost_per_side
            turnover += traded / nav if nav else 0.0
            nav -= cost
            units = {k: v / store.adj[k][t] for k, v in target.items() if ok(store.adj[k][t])}
            marks = {k: store.adj[k][t] for k in units}
            cash = nav - sum(units[k] * marks[k] for k in units)
            unscaled = dict(dec.unscaled)
        out_nav.append((t, nav))
    return Run(spec, out_nav, decisions, turnover, delisted, book_returns)


def x_thread(store, start: int, end: int, universe_size: int = 500) -> tuple[list, list]:
    """The X thread: each January hold 100% in the single top prior-calendar-
    year gainer among the top ``universe_size`` by market cap (a proxy for
    the S&P 500, whose history this data does not have - flagged)."""
    starts = month_starts(store.calendar)
    all_januaries = [s for s in starts if store.calendar[s][5:7] == "01"]
    # a January can pick only when the whole prior calendar year is in the data
    usable = [j for i, j in enumerate(all_januaries) if i >= 1 and j >= start]
    nav, held, units, mark, picks, out = 1.0, None, 0.0, None, [], []
    for t in range(usable[0], end + 1):
        if held is not None:
            seg = store.by_key[held]
            p = store.adj[held][t] if t <= seg.last else NAN
            if ok(p):
                mark = p
            nav = units * mark
            if t > seg.last:
                held = None  # delisted: cash until next January
        if t in usable:
            j = all_januaries.index(t)
            year_end, prior_end = t - 1, all_januaries[j - 1] - 1
            best = None
            for seg in universe(store, t, universe_size, 5.0):
                a, b = store.adj[seg.key][prior_end], store.adj[seg.key][year_end]
                if ok(a) and ok(b) and (best is None or b / a > best[0]):
                    best = (b / a, seg.key)
            if best and ok(store.adj[best[1]][t]):
                held, mark = best[1], store.adj[best[1]][t]
                units = nav / mark
                picks.append((store.calendar[t], best[1], best[0] - 1))
        out.append((t, nav))
    return out, picks
