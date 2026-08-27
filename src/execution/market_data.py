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
from datetime import date, datetime, timedelta, timezone
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
class AlpacaDailyBars:
    """Daily bars from the same data host — the deterministic raw material for
    market context and benchmark returns.

    Same failure philosophy as the quote source: every failure path returns an
    empty list, never a fabricated bar. Callers render "unavailable", not zero.
    """

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        base_url: str = DATA_BASE_URL,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        feed: str = "iex",
        timeout: float = 10.0,
    ) -> None:
        self._feed = feed
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

    def bars(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Daily bars, oldest first. Empty on any failure — missing, never zero."""
        try:
            response = self._client.get(
                f"/v2/stocks/{quote(symbol)}/bars",
                params={
                    "timeframe": "1Day",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "feed": self._feed,
                    "limit": 10_000,
                    "adjustment": "split",
                },
            )
        except Exception as error:  # noqa: BLE001 - an outage is missing data
            logger.warning("bars request for %s failed: %s", symbol, error)
            return []
        if response.status_code >= 400:
            logger.warning(
                "bars for %s returned HTTP %d", symbol, response.status_code
            )
            return []
        try:
            payload: Any = response.json()
        except ValueError:
            logger.warning("bars for %s were not JSON", symbol)
            return []
        bars = payload.get("bars") if isinstance(payload, dict) else None
        if not isinstance(bars, list):
            return []
        return [bar for bar in bars if isinstance(bar, dict)]

    def window_return_pct(
        self, symbol: str, start: datetime, end: datetime
    ) -> Optional[Decimal]:
        """Close-to-close total return over the window, percent. None on any gap."""
        bars = self.bars(symbol, start, end)
        closes: list[Decimal] = []
        for bar in bars:
            raw = bar.get("c")
            try:
                close = Decimal(str(raw))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if close > ZERO:
                closes.append(close)
        if len(closes) < 2:
            return None
        return ((closes[-1] / closes[0] - 1) * 100).quantize(Decimal("0.01"))

    def close(self) -> None:
        self._client.close()


def _date_or_none(raw: object) -> Optional[date]:
    """A calendar date from an ISO string (bare or timestamped), or None."""
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


class MarketContextBuilder:
    """Deterministic market context for research prompts. Zero LLM cost.

    Pure arithmetic over daily bars: recent price change, distance from the
    52-week high, distance from the 200-day moving average with the
    consecutive-sessions-below streak (2026-08-26), current volume against its
    20-day average, and — when a provider is configured — days until the next
    earnings date. Missing data degrades to a sentence saying so; the pass
    always proceeds, and nothing is ever fabricated to fill a gap.
    """

    #: At most this many tickers get context — a many-ticker signal gets the
    #: leaders, not an unbounded fetch loop.
    MAX_TICKERS = 3

    def __init__(
        self,
        bars: AlpacaDailyBars,
        clock: Optional[Callable[[], datetime]] = None,
        earnings_provider: Optional[Callable[[str], Optional[Any]]] = None,
    ) -> None:
        self._bars = bars
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._earnings = earnings_provider

    def context_for(self, signal: Any) -> str:
        """A context block for the signal's extracted tickers. Never raises."""
        raw = (signal.metadata.get("tickers") or "").strip()
        tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
        if not tickers:
            return (
                "No instrument was extracted from this signal; no market "
                "context is available. Proceed on the signal content alone."
            )
        # Lagged signals (congressional disclosures) carry the trade date as
        # structured metadata; the priced-in analysis's whole question is
        # "what has happened since the trade", so that change is computed
        # directly (2026-08-27) instead of being left to inference from
        # fixed 5d/20d windows.
        since = _date_or_none(signal.metadata.get("transaction_date"))
        sections = []
        for ticker in tickers[: self.MAX_TICKERS]:
            try:
                sections.append(self._section_for(ticker, since=since))
            except Exception as error:  # noqa: BLE001 - context must never block
                logger.warning("market context for %s failed: %s", ticker, error)
                sections.append(
                    f"{ticker}: market context unavailable (data fetch failed). "
                    f"Proceed without it; do not infer or invent these numbers."
                )
        return "\n\n".join(sections)

    def _section_for(self, ticker: str, since: Optional["date"] = None) -> str:
        now = self._clock()
        bars = self._bars.bars(ticker, now - timedelta(days=380), now)
        closes: list[Decimal] = []
        volumes: list[Decimal] = []
        dates: list[Optional["date"]] = []
        for bar in bars:
            try:
                close = Decimal(str(bar.get("c")))
                volume = Decimal(str(bar.get("v")))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if close > ZERO:
                closes.append(close)
                volumes.append(volume)
                dates.append(_date_or_none(bar.get("t")))
        if len(closes) < 2:
            return (
                f"{ticker}: market context unavailable (no usable price history). "
                f"Proceed without it; do not infer or invent these numbers."
            )

        last = closes[-1]
        lines = [f"{ticker}: last close {last}"]

        def pct_change(days: int) -> Optional[Decimal]:
            if len(closes) <= days:
                return None
            base = closes[-1 - days]
            if base <= ZERO:
                return None
            return ((last / base - 1) * 100).quantize(Decimal("0.01"))

        for days, label in ((5, "5-day"), (20, "20-day")):
            change = pct_change(days)
            lines.append(
                f"- {label} change: "
                + (f"{change:+.2f}%" if change is not None else "unavailable")
            )

        # Trade-date-anchored change (2026-08-27): the number the priced-in
        # analysis is actually about, stated instead of inferred. Only lagged
        # signals carry a transaction_date; the anchor is the first session
        # on or after it.
        if since is not None:
            base = base_date = None
            for bar_date, close in zip(dates, closes):
                if bar_date is not None and bar_date >= since:
                    base, base_date = close, bar_date
                    break
            if base is None or base <= ZERO:
                lines.append(
                    f"- change since the disclosed trade date "
                    f"({since.isoformat()}): unavailable"
                )
            else:
                since_change = ((last / base - 1) * 100).quantize(Decimal("0.01"))
                lines.append(
                    f"- change since the disclosed trade date "
                    f"({since.isoformat()}, first session "
                    f"{base_date.isoformat()} at {base}): {since_change:+.2f}%"
                )

        high = max(closes)
        from_high = ((last / high - 1) * 100).quantize(Decimal("0.01"))
        lines.append(f"- vs 52-week high ({high}): {from_high:+.2f}%")

        # 200-day moving average (2026-08-26): distance, and how long the
        # price has sat below it — one number for how far, one for how
        # persistent. Same discipline as every other line: pure arithmetic
        # over the bars already fetched, "unavailable" when history is short.
        if len(closes) >= 200:

            def average_at(index: int) -> Decimal:
                return sum(closes[index - 199 : index + 1], ZERO) / Decimal(200)

            dma = average_at(len(closes) - 1)
            from_dma = ((last / dma - 1) * 100).quantize(Decimal("0.01"))
            lines.append(
                f"- vs 200-day moving average ({dma.quantize(Decimal('0.01'))}): "
                f"{from_dma:+.2f}%"
            )
            below = 0
            index = len(closes) - 1
            while index >= 199 and closes[index] < average_at(index):
                below += 1
                index -= 1
            streak = str(below)
            if below > 0 and index < 199:
                # Every computable window was below: the true streak extends
                # past the fetched history, and the line must not understate
                # that as an exact count.
                streak = f"{below}+ (fetched-history limit)"
            lines.append(f"- consecutive sessions below the 200-DMA: {streak}")
        else:
            lines.append(
                "- vs 200-day moving average: unavailable (insufficient history)"
            )

        if len(volumes) >= 21:
            window = volumes[-21:-1]
            average = sum(window, ZERO) / Decimal(len(window))
            if average > ZERO:
                ratio = (volumes[-1] / average).quantize(Decimal("0.01"))
                lines.append(
                    f"- latest volume vs 20-day average: {ratio}x "
                    f"({volumes[-1]:.0f} vs {average:.0f})"
                )
            else:
                lines.append("- latest volume vs 20-day average: unavailable")
        else:
            lines.append("- latest volume vs 20-day average: unavailable")

        if self._earnings is None:
            lines.append(
                "- next earnings date: unavailable (no earnings data source "
                "configured)"
            )
        else:
            earnings_date = self._earnings(ticker)
            if earnings_date is None:
                lines.append("- next earnings date: unavailable")
            else:
                days_until = (earnings_date - now.date()).days
                lines.append(
                    f"- next earnings: {earnings_date.isoformat()} "
                    f"({days_until} days away)"
                )
        return "\n".join(lines)
