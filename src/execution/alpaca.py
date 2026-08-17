"""Alpaca adapter — equities and long options, paper first.

Every order goes out as a LIMIT order, including those the schema marked as market
executions. A ``MarketExecution`` carries ``max_price``, the worst case the risk gate
cash-secured against, and that value becomes the limit price: for a buy this is a
marketable limit that fills immediately at or better, and a fill above the reserved
bound stops being something the broker can do to us. The gate's trip-and-raise on a
bad fill remains as defence in depth for edge cases (partial fills at a venue that
ignores the limit, corporate actions, adapter bugs) but should now be unreachable in
normal operation.

Talking to the REST API directly rather than through ``alpaca-py`` keeps the wire
format visible in this file: the exact payload sent to a broker is the thing you most
want to be able to read during an incident.
"""

from __future__ import annotations

import warnings
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx

from execution.base import (
    BrokerAdapter,
    BrokerPosition,
    BrokerRejected,
    OrderReceipt,
    UnsupportedInstrument,
)
from execution.environment import load_environment, paper_mode, require_env
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
        self.paper = paper_mode()
        if not self.paper:
            # The human flipped PAPER_MODE, which is their call to make and the only
            # way live mode can be reached. Say so loudly rather than quietly routing
            # real money. Note CLAUDE.md build order step 7: the full pipeline should
            # paper trade for 2-4 weeks before live is even discussed.
            warnings.warn(
                "PAPER_MODE is disabled — this adapter is pointed at LIVE Alpaca and "
                "will trade real money.",
                stacklevel=2,
            )
        self.base_url = base_url or (PAPER_BASE_URL if self.paper else LIVE_BASE_URL)
        self.time_in_force = time_in_force

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

        return {
            "symbol": order.symbol,
            "qty": str(quantity),
            "side": side,
            "type": "limit",
            "time_in_force": self.time_in_force,
            "limit_price": str(limit_price),
            "client_order_id": f"agentic-{approved.sequence}",
        }

    # -- plumbing ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        expect_body: bool = True,
    ) -> Any:
        response = self._client.request(method, path, json=json)
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


def _parse_timestamp(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - broker format drift
        return None
