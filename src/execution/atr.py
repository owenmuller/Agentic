"""ATR(14) as a fraction of price — the volatility input to sizing and stops.

Human ruling 2026-09-02 (methodology build 4): stops move from a fixed 15% to
``k x ATR`` with a floor and ceiling, and per-position dollar risk is equalized
inside the band caps. This module supplies the one market fact that needs:
``ATR(14) / last close`` from the same Alpaca daily bars everything else reads.

Deterministic and stated: the true range is the classic
``max(high - low, |high - prev close|, |low - prev close|)`` and the average is
a SIMPLE mean of the last 14 true ranges — not Wilder's recursive smoothing,
whose value depends on where the recursion started. Missing or short history
returns None, and the caller falls back to the fixed-15% regime that predates
this ruling — absence restores the status quo ante, it never fabricates a
volatility.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

ATR_PERIODS = 14

logger = logging.getLogger("execution.atr")


def atr_fraction_from_bars(bars: list[dict]) -> Optional[Decimal]:
    """``ATR(14) / last close`` from daily bars (oldest first), or None."""
    rows: list[tuple[Decimal, Decimal, Decimal]] = []
    for bar in bars:
        try:
            high = Decimal(str(bar["h"]))
            low = Decimal(str(bar["l"]))
            close = Decimal(str(bar["c"]))
        except (InvalidOperation, ValueError, TypeError, KeyError):
            continue
        if high >= low > 0 and close > 0:
            rows.append((high, low, close))
    if len(rows) < ATR_PERIODS + 1:
        return None
    rows = rows[-(ATR_PERIODS + 1) :]
    ranges = []
    for previous, current in zip(rows, rows[1:]):
        previous_close = previous[2]
        high, low, _ = current
        ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    average = sum(ranges) / Decimal(len(ranges))
    last_close = rows[-1][2]
    return average / last_close


class AtrSource:
    """``atr_fraction(symbol)`` over AlpacaDailyBars, cached per symbol per UTC
    day. Any failure returns None — the sizing path falls back to fixed-15%."""

    def __init__(
        self,
        bars,  # AlpacaDailyBars, or anything with .bars(symbol, start, end)
        *,
        lookback_days: int = 45,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._bars = bars
        self._lookback = timedelta(days=lookback_days)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[str, date], Optional[Decimal]] = {}

    def __call__(self, symbol: str) -> Optional[Decimal]:
        key = (symbol.upper(), self._clock().date())
        if key in self._cache:
            return self._cache[key]
        try:
            now = self._clock()
            bars = self._bars.bars(symbol, now - self._lookback, now)
            value = atr_fraction_from_bars(bars)
        except Exception as error:  # noqa: BLE001 - missing, never fabricated
            logger.warning("ATR for %s unavailable: %s", symbol, error)
            value = None
        self._cache[key] = value
        return value
