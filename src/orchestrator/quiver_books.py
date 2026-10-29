"""QUIVER STRATEGY SHADOWS (human ruling 2026-10-29, item 2): software-only
books that follow two of Quiver's published strategies (quiverquant.com/
strategies, read 2026-10-29) on our own Quiver data and live prices. No order,
no sizing, no prompt: rows in the weekly shadow table beside SPY and QQQ.

Congress Buys (Quiver: "stocks that have been purchased by members of U.S.
Congress (or their family) ... weighted based on the reported size of the
purchases, with weekly rebalancing"):
  every disclosed PURCHASE of a stock whose disclosure (``ReportDate``) falls
  in the trailing ``CONGRESS_WINDOW_DAYS`` before the rebalance day, weighted
  by the reported size (the midpoint of the disclosed range), summed per
  ticker; rebalanced at the close of the first session of each week.
  UNSTATED by Quiver: how long a purchase stays in the book. 182 days is the
  window our House replication pre-registered (2026-10-09); stated, not tuned.

Lobbying Spending Growth (Quiver: "an equal-weighted position in 10
publicly-traded companies that disclosed the most Quarter over Quarter growth
in U.S. federal lobbying expenditures, rebalanced monthly"):
  the readings pre-registered 2026-10-29 for its backtest (SESSION_NOTES), the
  same code for the backtest and the live book.

Entry on disclosure/publication date only: a row counts only when its own
public date is BEFORE the rebalance day - never a transaction or period date.
Insider Purchases and Analyst Buys are not built: our Quiver plan does not
include either dataset, and both rest on proprietary scoring.

Pure: no network, no clock.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Callable, Iterable, Optional

CONGRESS_WINDOW_DAYS = 182
LOBBY_TOP = 10
LOBBY_MIN_BASE = 10_000.0
LOBBY_LAG_DAYS = 45
_EQUITY = frozenset({"st", "stock", "cs"})
_MONEY = re.compile(r"[\d,]+(?:\.\d+)?")


def _day(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# -- Congress Buys -------------------------------------------------------------------


def reported_size(range_text: object, amount: object = None) -> Optional[float]:
    """The midpoint of a disclosed range ("$15,001 - $50,000" -> 32,500.5);
    the lower bound alone for an open-ended "Over $X"; else ``amount``."""
    numbers = [float(n.replace(",", "")) for n in _MONEY.findall(str(range_text or ""))]
    numbers = [n for n in numbers if n > 0]
    if len(numbers) >= 2:
        return (numbers[0] + numbers[1]) / 2
    if len(numbers) == 1:
        return numbers[0]
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def congress_buys_weights(rows: Iterable[dict], day: str,
                          window_days: int = CONGRESS_WINDOW_DAYS) -> dict[str, float]:
    """Target weights on ``day``: purchases disclosed in [day - window, day),
    stock tickers only, weighted by reported size and summed per ticker."""
    end = _day(day)
    start = end - timedelta(days=window_days)
    size: dict[str, float] = {}
    for row in rows:
        if str(row.get("Transaction") or "").strip().lower() != "purchase":
            continue
        if str(row.get("TickerType") or "").strip().lower() not in _EQUITY:
            continue
        disclosed = _day(row.get("ReportDate"))
        ticker = str(row.get("Ticker") or "").strip().upper()
        if disclosed is None or not ticker or not (start <= disclosed < end):
            continue
        value = reported_size(row.get("Range"), row.get("Amount"))
        if value:
            size[ticker] = size.get(ticker, 0.0) + value
    total = sum(size.values())
    return {t: v / total for t, v in sorted(size.items())} if total > 0 else {}


# -- Lobbying Spending Growth ----------------------------------------------------------


def report_quarter(filed: date) -> tuple[int, int]:
    """The report quarter a filing is assigned to: the calendar quarter
    containing (filing date - 45 days) - on-time (the 20th after quarter end)
    and mildly late filings land on the quarter they report."""
    d = filed - timedelta(days=LOBBY_LAG_DAYS)
    return d.year, (d.month - 1) // 3 + 1


def quarter_deadline(year: int, quarter: int) -> date:
    """The Lobbying Disclosure Act deadline: the 20th of the month after the quarter."""
    month = quarter * 3 + 1
    return date(year + 1, 1, 20) if month > 12 else date(year, month, 20)


def previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def latest_reported_quarter(day: date) -> tuple[int, int]:
    """The latest quarter whose deadline has passed before ``day``."""
    year, quarter = day.year, (day.month - 1) // 3 + 1
    while quarter_deadline(year, quarter) >= day:
        year, quarter = previous_quarter(year, quarter)
    return year, quarter


def dedupe(rows: Iterable[dict]) -> list[dict]:
    """Exact duplicates on (Date, Registrant, Amount) dropped (pre-registered)."""
    seen, out = set(), []
    for row in rows:
        key = (str(row.get("Date"))[:10], str(row.get("Registrant") or "").strip().upper(), str(row.get("Amount")))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def lobbying_growth(rows_by_symbol: dict[str, list[dict]], day: str, *, min_base: float = LOBBY_MIN_BASE,
                    metric: str = "percent") -> list[tuple[str, float]]:
    """Every symbol's quarter-over-quarter growth in disclosed spend on ``day``,
    best first: P = the latest quarter whose deadline has passed, filings
    dated before ``day`` only (point in time); spend(P-1) >= ``min_base`` and
    spend(P) > 0. ``metric`` "percent" (pre-registered) or "dollars"."""
    when = _day(day)
    current = latest_reported_quarter(when)
    prior = previous_quarter(*current)
    out = []
    for symbol, rows in rows_by_symbol.items():
        spend = {current: 0.0, prior: 0.0}
        for row in rows:
            filed = _day(row.get("Date"))
            if filed is None or filed >= when:
                continue
            quarter = report_quarter(filed)
            if quarter in spend:
                try:
                    spend[quarter] += float(row.get("Amount") or 0)
                except (TypeError, ValueError):
                    continue
        if spend[prior] < min_base or spend[current] <= 0:
            continue
        growth = (spend[current] / spend[prior] - 1) if metric == "percent" else spend[current] - spend[prior]
        out.append((symbol, growth))
    out.sort(key=lambda item: (-item[1], item[0]))
    return out


def lobbying_weights(rows_by_symbol: dict[str, list[dict]], day: str, tradeable: Callable[[str], bool], *,
                     top: int = LOBBY_TOP, min_base: float = LOBBY_MIN_BASE,
                     metric: str = "percent") -> dict[str, float]:
    """Equal weight in the ``top`` best growers that ``tradeable`` admits."""
    picks = []
    for symbol, _growth in lobbying_growth(rows_by_symbol, day, min_base=min_base, metric=metric):
        if tradeable(symbol):
            picks.append(symbol)
        if len(picks) == top:
            break
    return {s: 1.0 / len(picks) for s in picks} if picks else {}
