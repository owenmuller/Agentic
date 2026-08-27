"""Alpaca adapter — equities and long options, paper first.

Every order goes out as a LIMIT order, including those the schema marked as market
executions. The limit is the execution's ``price_bound``:

  - ``MarketBuyExecution`` bounds with ``max_price``, a ceiling. Sent as the limit, it
    is marketable — fills immediately at or better — and a fill above the price the
    risk gate cash-secured against stops being something the broker can do to us.
  - ``MarketSellExecution`` bounds with ``min_price``, a floor. Sent as the limit, it
    caps how far proceeds can slip, at the cost of resting unfilled if the market is
    already below the floor. That trade-off is the point: a sell that will not print
    below your floor is what a floor means.

The gate's trip-and-raise on a bad fill remains as defence in depth for edge cases
(a venue ignoring the limit, corporate actions, adapter bugs) but should now be
unreachable in normal operation.

Talking to the REST API directly rather than through ``alpaca-py`` keeps the wire
format visible in this file: the exact payload sent to a broker is the thing you most
want to be able to read during an incident.
"""

from __future__ import annotations

import uuid
import warnings
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx

from execution.base import (
    BrokerAdapter,
    BrokerPermissions,
    BrokerPosition,
    BrokerRejected,
    OrderReceipt,
    OrderStatus,
    UnsupportedInstrument,
)
from execution.environment import (
    load_environment,
    require_env,
    require_paper_or_confirmed_live,
)
from risk_gate import ApprovedOrder
from risk_gate.schema import (
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    EventContractSellToCloseOrder,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"

_SIDES = {
    EquityBuyOrder: "buy",
    EquitySellToCloseOrder: "sell",
    OptionBuyToOpenOrder: "buy",
    OptionSellToCloseOrder: "sell",
}


class AlpacaAdapter(BrokerAdapter):
    """Equity and long-option execution against Alpaca."""

    #: Alpaca fractional trading, docs verified 2026-08-20: supported for market,
    #: limit, stop & stop-limit orders with time_in_force=day only; "both notional
    #: and qty fields can take up to 9 decimal point values"; asset must carry
    #: fractionable=true (a fractional order on a non-fractionable name is rejected
    #: broker-side, which the audit trail records like any other rejection).
    equity_quantity_step = Decimal("0.000000001")

    def tradeable_equity(self, symbol: str) -> bool:
        """Alpaca's asset record: active, tradable, and — because this venue
        trades fractional slices — fractionable. Used by the mechanical
        sleeve's qualification (ruling 2026-08-27). Fails CLOSED on any error
        (Constraint #6: the doubtful direction is the fewer trades), with the
        reason logged; the signal is not sealed and re-emits at a restart."""
        try:
            response = self._client.get(f"/v2/assets/{symbol}")
        except Exception as error:  # noqa: BLE001 - an outage is "not tradeable now"
            logger.warning("asset lookup for %s failed: %s", symbol, error)
            return False
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            logger.warning(
                "asset lookup for %s returned HTTP %d", symbol, response.status_code
            )
            return False
        try:
            asset = response.json()
        except ValueError:
            return False
        if not isinstance(asset, dict):
            return False
        return (
            asset.get("status") == "active"
            and bool(asset.get("tradable"))
            and bool(asset.get("fractionable"))
        )

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        time_in_force: str = "day",
        timeout: float = 15.0,
    ) -> None:
        load_environment()
        # Raises LiveModeMisconfigured if PAPER_MODE is off without the exact
        # confirmation phrase. It never falls back to paper — see Constraint #4.
        self.paper = require_paper_or_confirmed_live()
        if not self.paper:
            # Both keys were turned by a human, which is the only way to get here.
            # Say so loudly anyway. Note CLAUDE.md build order step 7: the full
            # pipeline should paper trade 2-4 weeks before live is even discussed.
            warnings.warn(
                "PAPER_MODE is disabled and live trading is confirmed — this adapter "
                "is pointed at LIVE Alpaca and will trade real money.",
                stacklevel=2,
            )
        self.base_url = base_url or (PAPER_BASE_URL if self.paper else LIVE_BASE_URL)
        self.time_in_force = time_in_force
        # Alpaca requires client_order_id to be unique per account ACROSS TIME, and
        # the gate's approval sequence restarts at 1 in every new process — so a bare
        # sequence collides with the previous run's orders and draws a 422. A launch
        # token makes ids unique across restarts while the sequence still links the
        # order to its approval in the audit trail.
        self._launch_token = uuid.uuid4().hex[:8]

        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=timeout,
                headers={
                    "APCA-API-KEY-ID": api_key or require_env("ALPACA_API_KEY"),
                    "APCA-API-SECRET-KEY": api_secret
                    or require_env("ALPACA_API_SECRET"),
                    "Content-Type": "application/json",
                },
            )

    # -- interface ---------------------------------------------------------------

    def submit_order(self, approved: ApprovedOrder) -> OrderReceipt:
        approved = self._require_approved(approved)
        payload = self.build_payload(approved)
        data = self._request("POST", "/v2/orders", json=payload)
        return OrderReceipt(
            broker_order_id=str(data["id"]),
            status=str(data.get("status", "unknown")),
            symbol=str(data.get("symbol", payload["symbol"])),
            quantity=Decimal(str(data.get("qty", payload["qty"]))),
            limit_price=Decimal(str(data.get("limit_price", payload["limit_price"]))),
            client_order_id=data.get("client_order_id"),
            submitted_at=_parse_timestamp(data.get("submitted_at")),
        )

    def get_positions(self) -> list[BrokerPosition]:
        data = self._request("GET", "/v2/positions")
        return [
            BrokerPosition(
                symbol=str(row["symbol"]),
                quantity=Decimal(str(row["qty"])),
                market_value=Decimal(str(row["market_value"])),
                cost_basis=Decimal(str(row["cost_basis"])),
                asset_class=str(row.get("asset_class", "us_equity")),
            )
            for row in data
        ]

    def get_buying_power(self) -> Decimal:
        """Settled cash, not Alpaca's ``buying_power`` field.

        ``buying_power`` includes margin in a margin account, and Constraint #1 forbids
        borrowed buying power outright. ``cash`` is the figure that cannot be inflated
        by leverage, so it is the only one this adapter will report.
        """
        data = self._request("GET", "/v2/account")
        return Decimal(str(data["cash"]))

    def permissions(self) -> BrokerPermissions:
        """The account's configured permissions, from ``GET /v2/account``.

        ``options_trading_level`` is the effective level (approved level capped by
        any account setting); it is the one that decides what an order can do.
        """
        data = self._request("GET", "/v2/account")
        level = data.get("options_trading_level", data.get("options_approved_level", 0))
        return BrokerPermissions(
            options_level=int(level or 0),
            shorting_enabled=bool(data.get("shorting_enabled", False)),
            margin_multiplier=Decimal(str(data.get("multiplier", "1"))),
        )

    def option_contracts(self, underlying: str, limit: int = 5) -> list[str]:
        """A few live option contract symbols for ``underlying``.

        Exists to answer "can this account actually see the options market" as a
        fact rather than an inference from the approval level — and it is the first
        sliver of the options-chain seam that short_via_puts will eventually need.
        Read-only; no order is involved.
        """
        data = self._request(
            "GET",
            "/v2/options/contracts",
            params={"underlying_symbols": underlying, "limit": limit},
        )
        contracts = (data or {}).get("option_contracts", []) or []
        return [str(row["symbol"]) for row in contracts]

    def open_orders(self) -> list[str]:
        data = self._request("GET", "/v2/orders", params={"status": "open"})
        return [str(row["id"]) for row in data or []]

    def get_order(self, broker_order_id: str) -> OrderStatus:
        data = self._request("GET", f"/v2/orders/{broker_order_id}")
        raw_price = data.get("filled_avg_price")
        return OrderStatus(
            broker_order_id=str(data.get("id", broker_order_id)),
            status=str(data.get("status", "unknown")),
            filled_quantity=Decimal(str(data.get("filled_qty", "0"))),
            filled_avg_price=(
                Decimal(str(raw_price)) if raw_price not in (None, "") else None
            ),
        )

    def cancel_order(self, broker_order_id: str) -> None:
        self._request("DELETE", f"/v2/orders/{broker_order_id}", expect_body=False)

    # -- translation ---------------------------------------------------------------

    def build_payload(self, approved: ApprovedOrder) -> dict[str, Any]:
        """Turn a gate-approved order into an Alpaca order payload.

        Public because the payload is worth asserting on in tests and worth logging
        verbatim into the audit record.
        """
        order = approved.order

        if isinstance(order, (EventContractBuyOrder, EventContractSellToCloseOrder)):
            raise UnsupportedInstrument(
                "Alpaca does not trade event contracts; the prediction-market sleeve "
                "routes to Kalshi's official API"
            )

        side = _SIDES.get(type(order))
        if side is None:  # pragma: no cover - unreachable while Order is closed
            raise UnsupportedInstrument(
                f"no Alpaca mapping for order kind {order.kind!r}"
            )

        # price_bound is limit_price for a limit execution and max_price for a market
        # execution. Using it for both is what makes an over-bound fill impossible.
        limit_price = order.execution.price_bound
        quantity = (
            order.quantity if hasattr(order, "quantity") else order.contracts
        )

        # Fractional quantities are only valid at Alpaca with time_in_force=day
        # (docs 2026-08-20). This adapter defaults to day; if it is ever configured
        # otherwise, a fractional order must fail loudly here rather than be
        # silently reshaped or rejected downstream with a confusing broker error.
        is_fractional = (
            isinstance(quantity, Decimal)
            and quantity != quantity.to_integral_value()
        )
        if is_fractional and self.time_in_force != "day":
            raise UnsupportedInstrument(
                f"fractional quantity {quantity} requires time_in_force='day' at "
                f"Alpaca; this adapter is configured {self.time_in_force!r}"
            )

        return {
            "symbol": order.symbol,
            "qty": _quantity_str(quantity),
            "side": side,
            "type": "limit",
            "time_in_force": self.time_in_force,
            "limit_price": str(limit_price),
            "client_order_id": f"agentic-{self._launch_token}-{approved.sequence}",
        }

    # -- plumbing ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        expect_body: bool = True,
    ) -> Any:
        response = self._client.request(method, path, json=json, params=params)
        if response.status_code >= 400:
            raise BrokerRejected(
                f"Alpaca {method} {path} failed with {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )
        if not expect_body or not response.content:
            return None
        return response.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AlpacaAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _quantity_str(quantity: object) -> str:
    """Decimal quantities without trailing zeros or scientific notation.

    ``str(Decimal("2.500000000"))`` keeps the zeros and ``normalize()`` alone can
    emit ``1E+2``; ``format(..., "f")`` after normalize gives the plain form the
    wire wants.
    """
    if isinstance(quantity, Decimal):
        return format(quantity.normalize(), "f")
    return str(quantity)


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - broker format drift
        return None
