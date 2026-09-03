"""Average dollar volume for the liquidity gate (human ruling 2026-09-02).

The gate itself lives in ``risk_gate`` and stays offline: it receives a callable
``adv(symbol) -> Optional[Decimal]`` and compares the resulting position's
notional against a fraction of it. This module is the production callable —
the network half — built on the same daily bars the market context and ATR
sizing read, with one difference that is load-bearing: **the SIP feed**. Probed
2026-09-02: Alpaca's IEX feed reports IEX-venue volume only (AAPL ~1M shares a
day against ~34M consolidated), so an ADV computed from IEX bars would
understate liquidity ~30x and the 1% gate would block nearly every name.
Historical SIP daily bars are served on the free plan; the caller wires
``AlpacaDailyBars(feed="sip")`` here and IEX everywhere else.

Failure philosophy differs from ATR on purpose: ATR falls back to a fixed stop,
but the gate FAILS CLOSED on a missing ADV — a name whose volume history cannot
be read is exactly the name the gate exists to keep out. This module therefore
never fabricates; it returns None and lets the gate say ``illiquid_position``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("execution.liquidity")

ZERO = Decimal("0")


def dollar_adv_from_bars(
    bars: Iterable[dict[str, Any]], days: int = 20
) -> Optional[Decimal]:
    """Mean of close x volume over the LAST ``days`` bars, oldest-first input.

    None when fewer than ``days`` usable bars exist: a name that has not
    traded twenty sessions has no twenty-day average, and inventing one from
    fewer would flatter exactly the newly-listed and thinly-traded names the
    gate is for.
    """
    rows: list[Decimal] = []
    for bar in bars:
        try:
            close = Decimal(str(bar.get("c")))
            volume = Decimal(str(bar.get("v")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if close <= ZERO or volume < ZERO:
            continue
        rows.append(close * volume)
    if days <= 0 or len(rows) < days:
        return None
    window = rows[-days:]
    return sum(window, ZERO) / Decimal(days)


class AdvSource:
    """``adv(symbol)`` over SIP daily bars, cached per symbol per UTC day.

    Any failure returns None — the gate fails closed on it and says so in the
    rejection; nothing here ever stands in for a missing number.
    """

    def __init__(
        self,
        bars,  # AlpacaDailyBars(feed="sip"), or anything with .bars(symbol, start, end)
        *,
        days: int = 20,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if days <= 0:
            raise ValueError(f"adv days must be positive, got {days}")
        self._bars = bars
        self._days = days
        # Calendar lookback generous enough to hold `days` trading sessions
        # through holidays and halts; the function takes the LAST `days`.
        self._lookback = timedelta(days=days * 2 + 15)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[str, date], Optional[Decimal]] = {}

    def __call__(self, symbol: str) -> Optional[Decimal]:
        key = (symbol.upper(), self._clock().date())
        if key in self._cache:
            return self._cache[key]
        try:
            now = self._clock()
            bars = self._bars.bars(symbol, now - self._lookback, now)
            value = dollar_adv_from_bars(bars, self._days)
        except Exception as error:  # noqa: BLE001 - missing, never fabricated
            logger.warning("ADV for %s unavailable: %s", symbol, error)
            value = None
        if value is None:
            logger.warning(
                "no %d-day dollar ADV for %s; the liquidity gate fails CLOSED on it",
                self._days,
                symbol,
            )
        self._cache[key] = value
        return value
