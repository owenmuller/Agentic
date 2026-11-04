"""What a quote is worth, and what a stop may trigger on (human rulings
2026-11-04). One reader for every engine that marks or stops an equity
position: the momentum sleeve, the exit engine (judged and aggressive) and
the mechanical arm.

  value     the MID of one quote (bid and ask together). A quote wider than
            ``max_spread`` of its mid is no price: no valuation, the last
            mark stands, and a yellow card says so (once a symbol a day).
            Origin: marks at the IEX ask (EPD 36.85 x 42.28, THC 265.06 x
            299.01) valued two holdings ~12% high and triggered trims that
            were not due.
  trigger   what a stop compares: the BID of a tight quote; on a wide quote,
            or one with no bid, the LAST TRADE; never the ask (a stale high
            ask delays a stop). Neither available: no trigger this tick.

A source without ``sides`` (the test doubles) values and triggers at its
single price, as before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import datahealth

#: The ruling's threshold (2026-11-04): wider than this, a quote is no price.
MAX_SPREAD = Decimal("0.02")
NY = ZoneInfo("America/New_York")

logger = logging.getLogger("orchestrator.quotes")


class QuoteReader:
    def __init__(
        self,
        prices: Callable[[str], Optional[Decimal]],
        *,
        sides: Optional[Callable[[str], Optional[tuple[Decimal, Decimal]]]] = None,
        last_trade: Optional[Callable[[str], Optional[Decimal]]] = None,
        max_spread: Decimal = MAX_SPREAD,
        engine: str = "",
        clock: Optional[Callable[[], datetime]] = None,
        report: Callable[..., None] = datahealth.report,
    ) -> None:
        self._prices = prices
        self._sides = sides
        self._last_trade = last_trade
        self._max_spread = max_spread
        self._engine = engine
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._report = report
        self._carded: set[tuple[str, str]] = set()

    def read(self, symbol: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """(value, trigger) from one quote; either may be None."""
        if self._sides is None:
            price = self._call(self._prices, symbol)
            return price, price
        sides = self._call(self._sides, symbol)
        if sides is None:
            return None, self._call(self._last_trade, symbol)
        bid, ask = sides
        mid = (bid + ask) / 2
        if mid > 0 and (ask - bid) / mid <= self._max_spread:
            return mid, bid
        self._card_wide(symbol, bid, ask, mid)
        return None, self._call(self._last_trade, symbol)

    def value(self, symbol: str) -> Optional[Decimal]:
        return self.read(symbol)[0]

    def _call(self, source, symbol: str):
        if source is None:
            return None
        try:
            return source(symbol)
        except Exception:  # noqa: BLE001 - a price bug must not kill the loop
            logger.exception("%s quote failed for %s", self._engine or "quote", symbol)
            return None

    def _card_wide(self, symbol: str, bid: Decimal, ask: Decimal, mid: Decimal) -> None:
        day = self._clock().astimezone(NY).date().isoformat()
        if (symbol, day) in self._carded:
            return
        self._carded.add((symbol, day))
        spread = (ask - bid) / mid if mid > 0 else None
        self._report("Quote quality", (
            f"{symbol} quoted {bid} x {ask}"
            + (f" ({spread:.1%} wide)" if spread is not None else "")
            + f", wider than {self._max_spread:.0%}: {self._engine + ' ' if self._engine else ''}valuation skipped, "
              f"the last mark stands; stops compare the last trade, never the ask"))
