"""The momentum book's rules (human ruling 2026-10-13), pure functions.

v1 is FROZEN: ``V1`` below is never edited in place. A change is a new
variant with its own name, tracked as a shadow until the human promotes it.

Definitions fixed here (each a reading of the ruling, flagged in
SESSION_NOTES where the ruling left a choice):

  sector / industry   Fama-French 12 groups from the SEC SIC code. The same
                      12 groups are the "own-universe industry" in the
                      residual regression and the sectors of the 30% cap.
  residual momentum   per stock, OLS of monthly returns on the market (SPY)
                      and its own-universe industry (equal-weighted mean of
                      the industry's universe members) over the trailing 36
                      months (fewer, minimum 12, while history is shorter -
                      flagged); score = sum of residuals over months t-12..t-2
                      divided by their standard deviation.
  earnings momentum   sum over the past 12 months of each 8-K 2.02 release's
                      [-1,+1]-day return in excess of SPY; a filing accepted
                      at/after 16:00 ET counts to the next session.
  score               0.5 x z(residual) + 0.5 x z(earnings), z cross-
                      sectional over the eligible names, clipped at +-3; a
                      name with no 2.02 release in 12 months gets earnings
                      z = 0 (neutral, flagged).
  exclusions          gate NEW BUYS only: any |daily return| > 15% in the
                      past 63 sessions; a pending acquisition target; close
                      below its own 100-day average.
  buffer              buy at rank <= 15, hold while rank <= 30 (rank among
                      all scored universe names), sector cap 30% of names.
  market filter (A)   SPY below its 200-day average: no new buys; holdings
                      run to their exits; empty slots stay in cash.
  vol scaling (B)     exposure = min(1, target / current), current = the
                      63-day vol of the new book's weighted constituents,
                      target = the strategy's own realized vol over the
                      trailing 756 sessions (fewer, minimum 126, while its
                      history is shorter; below that no scaling - flagged).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Optional, Sequence

# -- the specification -------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    name: str
    universe: int = 1000
    min_price: float = 5.0
    holdings: int = 15
    sell_rank: int = 30
    sector_cap: float = 0.30
    position_cap: float = 0.10
    weighting: str = "equal"          # equal | inverse_vol
    score: str = "residual+earnings"  # residual+earnings | plain_12_1
    market_filter: bool = True        # risk (A)
    vol_scaling: bool = True          # risk (B)
    gap_limit: float = 0.15
    gap_lookback: int = 63
    trend_days: int = 100
    regression_months: int = 36
    min_regression_months: int = 12
    vol_days: int = 63
    target_vol_days: int = 756
    min_target_vol_days: int = 126
    cost_per_side: float = 0.0010     # 10 bp of traded notional, each side
    ai_veto: bool = True
    #: Shadow-only (human ruling 2026-10-14): the gap filter ignores moves on
    #: 8-K 2.02 earnings days ([-1,+1] around the filing session). False in V1 and V1.1.
    gap_exempt_earnings: bool = False


V1 = Spec(name="v1")

#: v1.1 - THE LIVE SPEC (human ruling 2026-10-14): v1's rules on the top-500
#: universe. Chosen after the sanity backtest (v1 8.55% vs top_500 16.35% CAGR,
#: 2017-03 to 2026-10), on prior evidence that broad momentum lags large-cap
#: momentum in the live fund records. SPMO is the binding comparison. Frozen
#: like V1: a change is a new Spec, never an edit.
V1_1 = replace(V1, name="v1.1", universe=500)
LIVE = V1_1

#: The shadows (ruling item 4; revised 2026-10-14 with v1.1). Names are stable
#: keys in the ledger. Shadows never use the veto (the live book's leg).
#:   v1_1_no_veto   the live spec without the veto: measures the veto
#:   v1_top_1000    the original v1 (top 1,000): the universe comparison
#:   the rest       the declared variants, each ONE rule away from v1.1 (moved to
#:                  the top 500 by human ruling 2026-10-14, second);
#:                  "top_500" is retired - it IS v1.1 without the veto
SHADOWS = {
    "v1_1_no_veto": replace(V1_1, name="v1_1_no_veto", ai_veto=False),
    "v1_top_1000": replace(V1, name="v1_top_1000", ai_veto=False),
    "inverse_vol": replace(V1_1, name="inverse_vol", weighting="inverse_vol", ai_veto=False),
    "market_filter_only": replace(V1_1, name="market_filter_only", vol_scaling=False, ai_veto=False),
    "vol_scaling_only": replace(V1_1, name="vol_scaling_only", market_filter=False, ai_veto=False),
    "plain_12_1": replace(V1_1, name="plain_12_1", score="plain_12_1", ai_veto=False),
    # human ruling 2026-10-14: the gap filter exempting earnings-day moves, tracked from 2026-10-15
    "v1_1_gap_earnings_exempt": replace(V1_1, name="v1_1_gap_earnings_exempt", gap_exempt_earnings=True,
                                        ai_veto=False),
}


# -- sectors -----------------------------------------------------------------------

_FF12 = [
    (1, [(100, 999), (2000, 2399), (2700, 2749), (2770, 2799), (3100, 3199), (3940, 3989)]),
    (2, [(2500, 2519), (2590, 2599), (3630, 3659), (3710, 3711), (3714, 3714), (3716, 3716), (3750, 3751),
         (3792, 3792), (3900, 3939), (3990, 3999)]),
    (3, [(2520, 2589), (2600, 2699), (2750, 2769), (3000, 3099), (3200, 3569), (3580, 3629), (3700, 3709),
         (3712, 3713), (3715, 3715), (3717, 3749), (3752, 3791), (3793, 3799), (3830, 3839), (3860, 3899)]),
    (4, [(1200, 1399), (2900, 2999)]),
    (5, [(2800, 2829), (2840, 2899)]),
    (6, [(3570, 3579), (3660, 3692), (3694, 3699), (3810, 3829), (7370, 7379)]),
    (7, [(4800, 4899)]),
    (8, [(4900, 4949)]),
    (9, [(5000, 5999), (7200, 7299), (7600, 7699)]),
    (10, [(2830, 2839), (3693, 3693), (3840, 3859), (8000, 8099)]),
    (11, [(6000, 6999)]),
]
SECTOR_NAMES = {1: "consumer non-durables", 2: "consumer durables", 3: "manufacturing", 4: "energy",
                5: "chemicals", 6: "business equipment", 7: "telecom", 8: "utilities", 9: "shops",
                10: "health", 11: "money", 12: "other"}


def ff12(sic: Optional[int]) -> int:
    """Fama-French 12 group for an SIC code (12 = other, also for none)."""
    if sic is None:
        return 12
    for group, ranges in _FF12:
        if any(lo <= sic <= hi for lo, hi in ranges):
            return group
    return 12


# -- statistics --------------------------------------------------------------------

def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def ols_residuals(y: Sequence[float], x1: Sequence[float], x2: Sequence[float]) -> Optional[list[float]]:
    """Residuals of y = a + b1 x1 + b2 x2 (closed form); None if singular."""
    n = len(y)
    if n < 4:
        return None
    m_y, m1, m2 = mean(y), mean(x1), mean(x2)
    s11 = sum((a - m1) ** 2 for a in x1)
    s22 = sum((b - m2) ** 2 for b in x2)
    s12 = sum((a - m1) * (b - m2) for a, b in zip(x1, x2))
    s1y = sum((a - m1) * (c - m_y) for a, c in zip(x1, y))
    s2y = sum((b - m2) * (c - m_y) for b, c in zip(x2, y))
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-18:
        # collinear (e.g. the industry IS the market): regress on x1 alone
        if s11 < 1e-18:
            return None
        b1 = s1y / s11
        a = m_y - b1 * m1
        return [c - a - b1 * u for u, c in zip(x1, y)]
    b1 = (s22 * s1y - s12 * s2y) / det
    b2 = (s11 * s2y - s12 * s1y) / det
    a = m_y - b1 * m1 - b2 * m2
    return [c - a - b1 * u - b2 * v for u, v, c in zip(x1, x2, y)]


# -- signals -----------------------------------------------------------------------

def residual_momentum(stock: Sequence[float], market: Sequence[float], industry: Sequence[float],
                      spec: Spec = V1) -> Optional[float]:
    """Monthly returns, oldest first, ENDING at the last complete month t-1.
    Uses the trailing ``regression_months`` (minimum ``min_regression_months``)
    and scores the residuals of months t-12..t-2 (the last month is skipped)."""
    n = min(len(stock), len(market), len(industry), spec.regression_months)
    if n < max(spec.min_regression_months, 12):
        return None
    y, x1, x2 = stock[-n:], market[-n:], industry[-n:]
    resid = ols_residuals(y, x1, x2)
    if resid is None:
        return None
    window = resid[-12:-1]  # t-12 .. t-2
    sd = stdev(window)
    if sd <= 0:
        return None
    return sum(window) / sd


def plain_momentum(month_end_closes: Sequence[float]) -> Optional[float]:
    """12-1 momentum: close at t-2 month-end over close at t-13 month-end.
    ``month_end_closes`` oldest first, ending at month-end t-1."""
    if len(month_end_closes) < 13:
        return None
    start, end = month_end_closes[-13], month_end_closes[-2]
    if start <= 0:
        return None
    return end / start - 1


def earnings_momentum(event_days: Iterable[int], stock_returns: Sequence[Optional[float]],
                      market_returns: Sequence[Optional[float]], today: int, lookback: int = 252) -> Optional[float]:
    """Sum of [-1,+1] excess returns around each event session index inside
    (today - lookback, today). Returns are daily, indexed by session; an
    event whose window is incomplete before ``today`` is skipped. None when
    no event qualifies."""
    total, used = 0.0, 0
    for day in sorted(set(event_days)):
        if not (today - lookback < day - 1 and day + 1 < today):
            continue
        window = 0.0
        ok = True
        for k in (day - 1, day, day + 1):
            r, m = stock_returns[k], market_returns[k]
            if r is None or m is None:
                ok = False
                break
            window += r - m
        if ok:
            total += window
            used += 1
    return total if used else None


def zscores(values: dict, clip: float = 3.0) -> dict:
    present = {k: v for k, v in values.items() if v is not None}
    if len(present) < 2:
        return {k: 0.0 for k in values}
    m, sd = mean(list(present.values())), stdev(list(present.values()))
    out = {}
    for k, v in values.items():
        if v is None or sd <= 0:
            out[k] = 0.0
        else:
            out[k] = max(-clip, min(clip, (v - m) / sd))
    return out


def excluded(daily_returns: Sequence[Optional[float]], closes: Sequence[float], pending_target: bool,
             spec: Spec = V1, exempt: Optional[Sequence[bool]] = None) -> Optional[str]:
    """Why a name may not be BOUGHT now, or None. Inputs end at the decision
    session. ``exempt`` (aligned with ``daily_returns``) marks days the gap
    filter ignores - earnings-day moves, for a spec with gap_exempt_earnings."""
    if pending_target:
        return "pending acquisition target"
    window = list(daily_returns[-spec.gap_lookback:])
    skip = list(exempt[-spec.gap_lookback:]) if (exempt is not None and spec.gap_exempt_earnings) else [False] * len(window)
    recent = [r for r, s in zip(window, skip) if r is not None and not s]
    if any(abs(r) > spec.gap_limit for r in recent):
        return f"a single-day move over {spec.gap_limit:.0%} in the past {spec.gap_lookback} sessions"
    if len(closes) < spec.trend_days:
        return "less than 100 sessions of history"
    if closes[-1] < mean(closes[-spec.trend_days:]):
        return "below its 100-day average"
    return None


def realized_vol(daily_returns: Sequence[Optional[float]], days: int) -> Optional[float]:
    xs = [r for r in daily_returns[-days:] if r is not None]
    if len(xs) < max(20, days // 3):
        return None
    return stdev(xs) * math.sqrt(252)


# -- the book ----------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    key: str
    score: float
    sector: int
    vol: Optional[float]
    exclusion: Optional[str]   # None = may be bought


@dataclass(frozen=True)
class Selection:
    holdings: tuple[str, ...]
    sold: tuple[tuple[str, str], ...]        # (key, why)
    bought: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]     # (key, why) - top names not bought
    ranks: dict


def select(candidates: Sequence[Candidate], held: Sequence[str], spec: Spec, *, buys_allowed: bool,
           vetoed: Optional[dict] = None) -> Selection:
    """Rank all scored names; keep holdings while rank <= sell_rank; fill to
    ``holdings`` from the top (rank <= holdings, extended down the list only
    to replace an exclusion, a veto or a sector-capped name - the vacated
    slot goes to the next rank), honouring the sector cap."""
    vetoed = vetoed or {}
    ranked = sorted(candidates, key=lambda c: (-c.score, c.key))
    ranks = {c.key: i + 1 for i, c in enumerate(ranked)}
    by_key = {c.key: c for c in ranked}
    keep, sold = [], []
    for key in held:
        rank = ranks.get(key)
        if rank is None:
            sold.append((key, "left the universe or lost its score"))
        elif rank > spec.sell_rank:
            sold.append((key, f"fell to rank {rank}, past {spec.sell_rank}"))
        else:
            keep.append(key)
    max_per_sector = max(1, math.floor(spec.sector_cap * spec.holdings + 1e-9))
    sectors: dict[int, int] = {}
    for key in keep:
        sector = by_key[key].sector
        sectors[sector] = sectors.get(sector, 0) + 1
    bought, skipped = [], []
    if buys_allowed:
        for cand in ranked:
            if len(keep) + len(bought) >= spec.holdings:
                break
            if cand.key in keep:
                continue
            # a name ranked past the buy line is reached only by replacing a
            # skipped name above it (the "next rank" rule)
            if ranks[cand.key] > spec.holdings + len(skipped):
                break
            if cand.exclusion:
                skipped.append((cand.key, cand.exclusion))
                continue
            if cand.key in vetoed:
                skipped.append((cand.key, f"vetoed: {vetoed[cand.key]}"))
                continue
            if sectors.get(cand.sector, 0) >= max_per_sector:
                skipped.append((cand.key, f"sector cap ({SECTOR_NAMES.get(cand.sector, cand.sector)})"))
                continue
            bought.append(cand.key)
            sectors[cand.sector] = sectors.get(cand.sector, 0) + 1
    return Selection(tuple(keep + bought), tuple(sold), tuple(bought), tuple(skipped), ranks)


def weights(holdings: Sequence[str], vols: dict, spec: Spec) -> dict:
    """Target weights summing to at most 1 BEFORE vol scaling: each name's
    slot is 1/holdings of the book (empty slots stay cash); inverse-vol
    redistributes inside the filled slots; no name above position_cap."""
    if not holdings:
        return {}
    filled = len(holdings) / spec.holdings
    if spec.weighting == "inverse_vol":
        inv = {k: 1 / vols[k] for k in holdings if vols.get(k)}
        missing = [k for k in holdings if k not in inv]
        if missing:
            fallback = mean(list(inv.values())) if inv else 1.0
            for k in missing:
                inv[k] = fallback
        total = sum(inv.values())
        raw = {k: filled * inv[k] / total for k in holdings}
    else:
        raw = {k: 1 / spec.holdings for k in holdings}
    # cap and redistribute the excess among uncapped names (never above cap)
    for _ in range(10):
        over = {k: w for k, w in raw.items() if w > spec.position_cap + 1e-12}
        if not over:
            break
        excess = sum(w - spec.position_cap for w in over.values())
        for k in over:
            raw[k] = spec.position_cap
        free = [k for k in raw if raw[k] < spec.position_cap - 1e-12]
        if not free:
            break
        share = sum(raw[k] for k in free)
        for k in free:
            raw[k] += excess * raw[k] / share if share > 0 else excess / len(free)
    return raw


def exposure(target_vol: Optional[float], current_vol: Optional[float]) -> float:
    """Vol scaling (B): min(1, target / current); 1 when either is unknown."""
    if not target_vol or not current_vol or current_vol <= 0:
        return 1.0
    return min(1.0, target_vol / current_vol)


def sma(closes: Sequence[float], days: int) -> Optional[float]:
    if len(closes) < days:
        return None
    return mean(closes[-days:])


def market_filter_blocks(spy_closes: Sequence[float], days: int = 200) -> bool:
    """True when SPY is below its 200-day average (no new buys)."""
    average = sma(spy_closes, days)
    return average is not None and spy_closes[-1] < average
