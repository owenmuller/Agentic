"""Alpaca market data — the production ``PriceSource``.

Fills the seam declared in ``orchestrator.pipeline.PriceSource``: a callable taking a
symbol and returning the per-unit price an order should be bounded at, or ``None``
when no usable quote exists. Paper API keys grant the free IEX feed, which is what
this defaults to.

The one rule that matters here: **an outage must never read as a price.** The exit
engine compares quotes against a max-loss stop, so a failure mode that surfaced as
``Decimal("0")`` would fire every stop in the book on a data hiccup. Every failure
path in this module — HTTP error, timeout, malformed body, missing quote, a quote
with no priced side, a quote too old to trust — returns ``None``, which callers
already treat as "skip this symbol this tick". There is no code path that returns
zero: a non-positive price from the feed is a missing price.

Staleness: a quote older than ``max_quote_age_seconds`` is treated as missing.
"Older than" is strict — a quote exactly at the threshold still counts — and a quote
whose timestamp is absent or unparseable is treated as stale, because a freshness
claim that cannot be checked is not a freshness claim.

Which side: the ask when one is priced, else the bid. The ask is the correct bound
for a buy (it is what the gate cash-secures against); for the exit engine's sell
limits it is a marginally conservative floor on tight-spread names. A one-sided
quote falls back to the bid, which for a buy can only under-fill, never over-spend —
the limit is the bound either way.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional
from urllib.parse import quote

import httpx

from execution.environment import load_environment, require_env

DATA_BASE_URL = "https://data.alpaca.markets"

ZERO = Decimal("0")

logger = logging.getLogger("execution.market_data")

#: RFC3339 fractional seconds beyond microseconds (Alpaca sends nanoseconds), which
#: ``datetime.fromisoformat`` refuses. Trimmed, not rounded — a nanosecond never
#: changes a staleness verdict.
_EXCESS_FRACTION = re.compile(r"\.(\d{6})\d+")


class AlpacaPriceSource:
    """Latest-quote prices from Alpaca's market data API.

    Uses the same key pair as the trading adapter; market data is a different host
    (``data.alpaca.markets``) and works with paper keys.
    """

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        base_url: str = DATA_BASE_URL,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        feed: str = "iex",
        max_quote_age_seconds: int = 300,
        timeout: float = 5.0,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if max_quote_age_seconds <= 0:
            raise ValueError(
                f"max_quote_age_seconds must be positive, got {max_quote_age_seconds}"
            )
        self._feed = feed
        self._max_age = max_quote_age_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if client is not None:
            self._client = client
        else:
            load_environment()
            self._client = httpx.Client(
                base_url=base_url,
                timeout=timeout,
                headers={
                    "APCA-API-KEY-ID": api_key or require_env("ALPACA_API_KEY"),
                    "APCA-API-SECRET-KEY": api_secret
                    or require_env("ALPACA_API_SECRET"),
                },
            )

    def __call__(self, symbol: str) -> Optional[Decimal]:
        """The latest usable quote for ``symbol``, or None. Never raises, never zero."""
        try:
            response = self._client.get(
                f"/v2/stocks/{quote(symbol)}/quotes/latest",
                params={"feed": self._feed},
            )
        except Exception as error:  # noqa: BLE001 - an outage is a missing price
            logger.warning("quote request for %s failed: %s", symbol, error)
            return None

        if response.status_code >= 400:
            logger.warning(
                "quote for %s returned HTTP %d", symbol, response.status_code
            )
            return None

        try:
            payload: Any = response.json()
        except ValueError:
            logger.warning("quote for %s was not JSON", symbol)
            return None

        quote_body = payload.get("quote") if isinstance(payload, dict) else None
        if not isinstance(quote_body, dict):
            logger.warning("no quote in the response for %s", symbol)
            return None

        age = self._age_of(quote_body.get("t"))
        if age is None:
            logger.warning(
                "quote for %s has no verifiable timestamp; treating as missing", symbol
            )
            return None
        if age > self._max_age:
            logger.warning(
                "quote for %s is %.0fs old, past the %ds staleness threshold; "
                "treating as missing",
                symbol,
                age,
                self._max_age,
            )
            return None

        price = self._usable_side(quote_body)
        if price is None:
            logger.warning("quote for %s has no priced side; treating as missing", symbol)
        return price

    # -- internals -------------------------------------------------------------------

    def _age_of(self, raw: object) -> Optional[float]:
        """Seconds since the quote's timestamp, or None if it cannot be established."""
        if not isinstance(raw, str) or not raw:
            return None
        text = _EXCESS_FRACTION.sub(r".\1", raw.replace("Z", "+00:00"))
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (self._clock() - stamp).total_seconds()

    @staticmethod
    def _usable_side(quote_body: dict) -> Optional[Decimal]:
        """Ask if priced, else bid, else None. A non-positive side is not a price."""
        for field in ("ap", "bp"):
            raw = quote_body.get(field)
            if raw is None:
                continue
            try:
                price = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                continue
            if price > ZERO:
                return price
        return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AlpacaPriceSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
