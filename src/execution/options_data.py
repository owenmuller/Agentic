"""Alpaca options chain source — quotes, greeks, IV, and open interest.

Two endpoints, one merge (docs verified 2026-08-24):

  - Trading API ``GET /v2/options/contracts`` carries the contract universe:
    expirations, strikes, and — critically — ``open_interest``, which the data
    API's snapshots do NOT include.
  - Data API ``GET /v1beta1/options/snapshots/{underlying}`` carries
    ``latestQuote`` (bid/ask), ``impliedVolatility``, and Black-Scholes
    ``greeks`` per contract.

Joined on OCC symbol into the deterministic layer's ``OptionQuote``. Same
discipline as the equity price source: this module never raises out of a fetch
— any failure returns None (which the pipeline routes to the equity fallback
with reason ``chain_unavailable``) — and a contract with missing fields is
carried with them None so the selector's gates judge the absence. Nothing here
invents a number.

Feed note: without an OPRA subscription the data API serves the ``indicative``
feed (delayed trades, modified quotes). Acceptable for paper; the live-mode
checklist requires confirming feed=opra before options trade real money.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from dataclasses import dataclass

from execution.environment import load_environment, require_env

logger = logging.getLogger("execution.options_data")

ZERO = Decimal("0")
TWO = Decimal("2")


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One contract, as fetched. Satisfies ``sizing.selection.OptionQuote``
    structurally — the deterministic layer types against the shape, never this
    module (which keeps the topology a DAG). Missing data stays None."""

    occ_symbol: str
    underlying: str
    right: str  # "call" | "put"
    expiration: date
    strike: Decimal
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    delta: Optional[Decimal] = None
    implied_volatility: Optional[Decimal] = None
    open_interest: int = 0
    multiplier: int = 100

    @property
    def mid(self) -> Optional[Decimal]:
        if self.bid is None or self.ask is None:
            return None
        if self.bid <= ZERO or self.ask <= ZERO:
            return None
        return (self.bid + self.ask) / TWO

    @property
    def spread_pct(self) -> Optional[Decimal]:
        """Bid-ask spread as a fraction of mid. None when unquotable."""
        mid = self.mid
        if mid is None or mid <= ZERO:
            return None
        return (self.ask - self.bid) / mid  # type: ignore[operator]

TRADING_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"

#: Widest chain window ever fetched: LEAPS floor (180d) plus a year of headroom.
MAX_EXPIRY_WINDOW_DAYS = 550


class AlpacaOptionsChain:
    """Fetches and joins the chain for one underlying. Read-only, never raises."""

    def __init__(
        self,
        *,
        trading_client: Optional[httpx.Client] = None,
        data_client: Optional[httpx.Client] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        trading_base_url: str = TRADING_PAPER_BASE_URL,
        data_base_url: str = DATA_BASE_URL,
        timeout: float = 15.0,
    ) -> None:
        if trading_client is None or data_client is None:
            load_environment()
            headers = {
                "APCA-API-KEY-ID": api_key or require_env("ALPACA_API_KEY"),
                "APCA-API-SECRET-KEY": api_secret or require_env("ALPACA_API_SECRET"),
            }
            if trading_client is None:
                trading_client = httpx.Client(
                    base_url=trading_base_url, timeout=timeout, headers=headers
                )
            if data_client is None:
                data_client = httpx.Client(
                    base_url=data_base_url, timeout=timeout, headers=headers
                )
        self._trading = trading_client
        self._data = data_client

    # -- the chain -------------------------------------------------------------------

    def chain_for(
        self, underlying: str, *, min_expiry: date, max_expiry: Optional[date] = None
    ) -> Optional[list[OptionQuote]]:
        """Every contract for ``underlying`` expiring in the window, joined.

        None means "no usable chain" — degradation, not emptiness; an empty
        list means the venue really lists nothing in the window.
        """
        if max_expiry is None:
            max_expiry = min_expiry + timedelta(days=MAX_EXPIRY_WINDOW_DAYS)
        try:
            contracts = self._contracts(underlying, min_expiry, max_expiry)
            snapshots = self._snapshots(underlying, min_expiry, max_expiry)
        except Exception as error:  # noqa: BLE001 - degradation, not crash
            logger.warning(
                "options chain for %s unavailable (%s: %s)",
                underlying,
                type(error).__name__,
                error,
            )
            return None
        if contracts is None:
            return None

        quotes: list[OptionQuote] = []
        for occ_symbol, row in contracts.items():
            snapshot = (snapshots or {}).get(occ_symbol, {})
            latest_quote = snapshot.get("latestQuote") or {}
            greeks = snapshot.get("greeks") or {}
            quotes.append(
                OptionQuote(
                    occ_symbol=occ_symbol,
                    underlying=underlying.upper(),
                    right=row["right"],
                    expiration=row["expiration"],
                    strike=row["strike"],
                    bid=_decimal_or_none(latest_quote.get("bp")),
                    ask=_decimal_or_none(latest_quote.get("ap")),
                    delta=_decimal_or_none(greeks.get("delta")),
                    implied_volatility=_decimal_or_none(
                        snapshot.get("impliedVolatility")
                    ),
                    open_interest=row["open_interest"],
                    multiplier=row["multiplier"],
                )
            )
        return quotes

    def option_mid(self, occ_symbol: str) -> Optional[Decimal]:
        """Latest mid premium for one contract — the exits engine's mark source."""
        try:
            data = self._get_json(
                self._data,
                "/v1beta1/options/quotes/latest",
                params={"symbols": occ_symbol},
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "option quote for %s unavailable (%s: %s)",
                occ_symbol,
                type(error).__name__,
                error,
            )
            return None
        quote = ((data or {}).get("quotes") or {}).get(occ_symbol) or {}
        bid = _decimal_or_none(quote.get("bp"))
        ask = _decimal_or_none(quote.get("ap"))
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        return (bid + ask) / Decimal("2")

    # -- endpoints -------------------------------------------------------------------

    def _contracts(
        self, underlying: str, min_expiry: date, max_expiry: date
    ) -> Optional[dict[str, dict[str, Any]]]:
        """Trading API contract universe, keyed by OCC symbol."""
        rows: dict[str, dict[str, Any]] = {}
        token: Optional[str] = None
        for _ in range(20):  # pagination backstop, never an infinite loop
            params: dict[str, Any] = {
                "underlying_symbols": underlying,
                "expiration_date_gte": min_expiry.isoformat(),
                "expiration_date_lte": max_expiry.isoformat(),
                "limit": 1000,
            }
            if token:
                params["page_token"] = token
            data = self._get_json(self._trading, "/v2/options/contracts", params=params)
            for row in (data or {}).get("option_contracts") or []:
                try:
                    rows[str(row["symbol"])] = {
                        "right": str(row["type"]).lower(),
                        "expiration": date.fromisoformat(row["expiration_date"]),
                        "strike": Decimal(str(row["strike_price"])),
                        "open_interest": int(row.get("open_interest") or 0),
                        "multiplier": int(Decimal(str(row.get("size") or "100"))),
                    }
                except (KeyError, ValueError, InvalidOperation):
                    # One malformed row is not a reason to lose the chain.
                    logger.warning("skipping malformed contract row: %r", row)
            token = (data or {}).get("next_page_token")
            if not token:
                break
        return rows or None

    def _snapshots(
        self, underlying: str, min_expiry: date, max_expiry: date
    ) -> Optional[dict[str, dict[str, Any]]]:
        """Data API snapshots, keyed by OCC symbol. None = quotes degraded."""
        snapshots: dict[str, dict[str, Any]] = {}
        token: Optional[str] = None
        try:
            for _ in range(20):
                params: dict[str, Any] = {
                    "expiration_date_gte": min_expiry.isoformat(),
                    "expiration_date_lte": max_expiry.isoformat(),
                    "limit": 1000,
                }
                if token:
                    params["page_token"] = token
                data = self._get_json(
                    self._data,
                    f"/v1beta1/options/snapshots/{underlying}",
                    params=params,
                )
                snapshots.update((data or {}).get("snapshots") or {})
                token = (data or {}).get("next_page_token")
                if not token:
                    break
        except Exception as error:  # noqa: BLE001
            # Contracts without quotes still describe the universe; the
            # selector's liquidity gate will refuse them all, which routes to
            # equity with the honest reason rather than losing the fetch.
            logger.warning(
                "options snapshots for %s unavailable (%s: %s)",
                underlying,
                type(error).__name__,
                error,
            )
            return None
        return snapshots

    @staticmethod
    def _get_json(
        client: httpx.Client, path: str, params: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        response = client.get(path, params=params)
        if response.status_code >= 400:
            raise RuntimeError(
                f"GET {path} failed with {response.status_code}: "
                f"{response.text[:200]}"
            )
        return response.json() if response.content else None

    def close(self) -> None:
        self._trading.close()
        self._data.close()


def _decimal_or_none(raw: Any) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
