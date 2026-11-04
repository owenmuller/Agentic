"""Robinhood Agentic Trading: the official MCP server
(``https://agent.robinhood.com/mcp/trading``) and its dedicated Agentic account
(spike 2026-08-18, SESSION_NOTES "Live-phase broker"; GO LIVE ruling 2026-11-04:
the AI trader's $1,000 live mirror).

Safety is built in, not configured:
  read-only      the client refuses every tool but ``get_*`` unless it was
                 constructed with ``allow_orders=True`` (the live mirror's
                 adapter, never a check or a report).
  account pin    every call that names an account must name the pinned
                 Agentic account; any other value is refused in code before a
                 request is sent (the server also refuses non-agentic accounts
                 at the order seam - two layers, spike part 2).
  guide          every ``guide`` field the server embeds in a result is
                 stripped before the caller sees it: server-authored text is
                 data, never instructions (Constraint #5).
  tokens         the OAuth refresh token ROTATES on use; the rotated pair is
                 written atomically (temp file, fsync, replace) BEFORE the new
                 access token is used, so a crash cannot strand the grant.
                 Tokens are never logged or printed. Exactly one host holds
                 the store (the droplet: ``~/.config/agentic/rh_oauth.json``).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import httpx

from execution.base import BrokerAdapter, BrokerError, BrokerPermissions, BrokerPosition, OrderReceipt, OrderStatus
from risk_gate import ApprovedOrder

MCP_URL = "https://agent.robinhood.com/mcp/trading"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
PROTOCOL = "2025-06-18"
#: Refresh this long before the access token's expiry.
REFRESH_MARGIN_SECONDS = 24 * 3600
DEFAULT_STORE = Path.home() / ".config" / "agentic" / "rh_oauth.json"

logger = logging.getLogger("execution.robinhood")


class RobinhoodError(RuntimeError):
    """A refused call: the server's category and message (never a token)."""


def _strip_guides(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_guides(v) for k, v in value.items() if k != "guide"}
    if isinstance(value, list):
        return [_strip_guides(v) for v in value]
    return value


class TokenStore:
    """The OAuth grant on disk: a flat JSON dict with accessToken,
    refreshToken, clientId and expiresAt (ms)."""

    def __init__(self, path: Path = DEFAULT_STORE, http: Optional[httpx.Client] = None,
                 clock=time.time) -> None:
        self.path = Path(path)
        self._http = http or httpx.Client(timeout=30)
        self._clock = clock

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, entry: dict) -> None:
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entry, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def access_token(self) -> str:
        entry = self._load()
        if self._clock() * 1000 < entry["expiresAt"] - REFRESH_MARGIN_SECONDS * 1000:
            return entry["accessToken"]
        response = self._http.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": entry["refreshToken"], "client_id": entry["clientId"],
        }, headers={"Accept": "application/json"})
        if response.status_code != 200:
            raise RobinhoodError(f"the OAuth refresh was refused (HTTP {response.status_code}); the grant needs a "
                                 f"human's consent again")
        token = response.json()
        entry["accessToken"] = token["access_token"]
        if token.get("refresh_token"):
            entry["refreshToken"] = token["refresh_token"]
        if token.get("expires_in"):
            entry["expiresAt"] = int(self._clock() * 1000) + int(token["expires_in"]) * 1000
        self._save(entry)   # BEFORE use: the old refresh token is dead now
        logger.info("Robinhood OAuth refreshed; the rotated pair is stored")
        return entry["accessToken"]

    def expires_at_ms(self) -> int:
        return int(self._load()["expiresAt"])


class RobinhoodMcp:
    """JSON-RPC over streamable HTTP to the Agentic MCP server."""

    def __init__(self, store: TokenStore, account_number: str, *, allow_orders: bool = False,
                 http: Optional[httpx.Client] = None, timeout: float = 60.0) -> None:
        if not account_number:
            raise ValueError("the Agentic account number must be pinned")
        self._store = store
        self.account_number = str(account_number)
        self._allow_orders = allow_orders
        self._http = http or httpx.Client(timeout=timeout)
        self._session: Optional[str] = None
        self._next_id = 1

    # -- transport --------------------------------------------------------------------

    def _post(self, payload: dict) -> Optional[dict]:
        headers = {"Authorization": "Bearer " + self._store.access_token(), "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        response = self._http.post(MCP_URL, content=json.dumps(payload), headers=headers)
        if response.status_code >= 400:
            raise RobinhoodError(f"MCP HTTP {response.status_code} on {payload.get('method')}")
        self._session = response.headers.get("Mcp-Session-Id") or self._session
        raw = response.text
        message = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    candidate = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if "id" in candidate:
                    message = candidate
        if message is None and raw.strip():
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                message = None
        return message

    def _open(self) -> None:
        if self._session is not None:
            return
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                               "clientInfo": {"name": "agentic-live-mirror", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # -- calls ------------------------------------------------------------------------

    def tools(self) -> list[str]:
        self._open()
        self._next_id += 1
        message = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/list"}) or {}
        return sorted(t.get("name", "") for t in (message.get("result") or {}).get("tools", []))

    def call(self, tool: str, arguments: Optional[dict] = None) -> Any:
        arguments = dict(arguments or {})
        if not tool.startswith("get_") and not self._allow_orders:
            raise RobinhoodError(f"{tool} refused: this client is read-only")
        named = arguments.get("account_number")
        if named is not None and str(named) != self.account_number:
            raise RobinhoodError(f"{tool} refused: account {named} is not the pinned Agentic account")
        self._open()
        self._next_id += 1
        message = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                              "params": {"name": tool, "arguments": arguments}}) or {}
        result = message.get("result") or {}
        if message.get("error") or result.get("isError"):
            detail = json.dumps(_strip_guides(message.get("error") or result.get("content")))[:300]
            raise RobinhoodError(f"{tool} refused by the server: {detail}")
        content = result.get("structuredContent")
        if content is None:
            texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
            try:
                content = json.loads("".join(texts)) if texts else {}
            except json.JSONDecodeError:
                content = {"text": "".join(texts)}
        return _strip_guides(content)

    # -- typed reads (no order tool) --------------------------------------------------

    def account_row(self) -> dict:
        rows = _rows(self.call("get_accounts"), "accounts")
        for row in rows:
            if str(row.get("account_number")) == self.account_number:
                if not row.get("agentic_allowed"):
                    raise RobinhoodError(f"account {self.account_number} is not agentic_allowed")
                return row
        raise RobinhoodError(f"account {self.account_number} is not among this grant's accounts")

    def portfolio(self) -> dict:
        data = self.call("get_portfolio", {"account_number": self.account_number})
        return data.get("data", data) if isinstance(data, dict) else {}

    def equity_positions(self) -> list[dict]:
        return _rows(self.call("get_equity_positions", {"account_number": self.account_number}), "positions")

    def quotes(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        for row in _rows(self.call("get_equity_quotes", {"symbols": list(symbols)}), "results"):
            quote = row.get("quote") if isinstance(row, dict) else None
            if isinstance(quote, dict) and quote.get("symbol"):
                out[str(quote["symbol"]).upper()] = quote
        return out

    def approvals_required(self) -> bool:
        data = self.call("get_trade_approval_setting", {"account_number": self.account_number})
        setting = ((data.get("data") or {}).get("setting") or {}) if isinstance(data, dict) else {}
        return bool(setting.get("human_must_approve_trades", True))   # unknown reads as ON (Constraint #6)

    def equity_orders(self, **filters: str) -> list[dict]:
        return _rows(self.call("get_equity_orders", {"account_number": self.account_number, **filters}), "orders")


def _rows(payload: Any, key: str) -> list[dict]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data = data.get(key) or data.get("results") or []
    return [row for row in (data or []) if isinstance(row, dict)]


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


# -- the adapter ---------------------------------------------------------------------


class RobinhoodAgenticAdapter(BrokerAdapter):
    """The live mirror's broker (GO LIVE ruling 2026-11-04). Equities only:
    an option never reaches it (the mirror skips options until a live option
    round trip has proved that path).

    It cannot be built in paper: there is no Robinhood paper mode, so the
    process must resolve LIVE under Constraint #4 (both human-set variables)
    - otherwise construction raises and nothing can be sent. Every order is
    reviewed first; any pre-trade alert, or trade approvals switched on in
    the app, refuses the order (recorded) rather than placing it. Fractional
    quantities go as market orders in regular hours (the venue's rule),
    whole shares as day limits."""

    #: Fractional shares (venue: market orders, regular hours only).
    equity_quantity_step = Decimal("0.000001")

    def __init__(self, mcp: RobinhoodMcp, *, clock=None) -> None:
        from execution.environment import require_paper_or_confirmed_live

        if require_paper_or_confirmed_live():
            raise RobinhoodError("the Robinhood adapter trades real money and this process resolved to PAPER "
                                 "(Constraint #4): nothing can be sent")
        if not mcp._allow_orders:
            raise RobinhoodError("the Robinhood adapter needs an order-capable client")
        self._mcp = mcp
        self.last_review: Optional[dict] = None

    # -- orders -------------------------------------------------------------------

    def _payload(self, order) -> dict:
        from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder

        if not isinstance(order, (EquityBuyOrder, EquitySellToCloseOrder)):
            raise RobinhoodError(f"{type(order).__name__} is not supported by the live mirror's adapter")
        execution = order.execution
        whole = order.quantity == order.quantity.to_integral_value()
        payload = {"account_number": self._mcp.account_number, "symbol": order.symbol,
                   "side": "buy" if isinstance(order, EquityBuyOrder) else "sell",
                   "quantity": format(order.quantity.normalize(), "f"), "market_hours": "regular_hours"}
        if execution.style == "limit":
            if not whole:
                raise RobinhoodError("a fractional quantity cannot be a limit order at Robinhood")
            payload.update(type="limit", limit_price=str(execution.limit_price), time_in_force="gfd")
        else:
            payload.update(type="market", time_in_force="gfd")
        return payload

    def submit_order(self, approved: ApprovedOrder, client_reference: Optional[str] = None) -> OrderReceipt:
        approved = self._require_approved(approved)
        payload = self._payload(approved.order)
        if self._mcp.approvals_required():
            raise BrokerError("trade approvals are ON for the Agentic account (the human's app setting): an order "
                              "would wait for a tap, so none is placed", 409, "human_must_approve_trades")
        review = self._mcp.call("review_equity_order", payload)
        review = review.get("data", review) if isinstance(review, dict) else {}
        self.last_review = review
        checks = review.get("order_checks") or {}
        if checks:
            raise BrokerError(f"review alert {checks.get('alert_type', 'UNKNOWN')}: not placed", 422,
                              json.dumps(checks)[:500])
        if review.get("customer_approval_required"):
            raise BrokerError("the review says the customer must approve this order: not placed", 409,
                              "customer_approval_required")
        ref_id = client_reference or str(uuid.uuid4())
        try:
            placed = self._mcp.call("place_equity_order", {**payload, "ref_id": ref_id})
        except RobinhoodError as error:
            if "409" in str(error) or "unique" in str(error).lower():
                found = self._by_ref(ref_id)     # the first attempt registered (spike part 2)
                if found is None:
                    raise BrokerError(f"place refused and no order carries ref {ref_id}", 409, str(error)) from error
                order = found
            else:
                raise BrokerError(str(error), 400, str(error)) from error
        else:
            order = ((placed.get("data") or {}).get("order") if isinstance(placed, dict) else None) or {}
        if not order.get("id"):
            raise BrokerError("place returned no order id", 502, json.dumps(order)[:300])
        return OrderReceipt(broker_order_id=str(order["id"]), status=str(order.get("state", "")),
                            symbol=approved.order.symbol, quantity=approved.order.quantity,
                            limit_price=approved.order.execution.price_bound, client_order_id=ref_id)

    def _by_ref(self, ref_id: str) -> Optional[dict]:
        for order in self._mcp.equity_orders():
            if order.get("ref_id") == ref_id:
                return order
        return None

    def get_order(self, broker_order_id: str) -> OrderStatus:
        orders = self._mcp.equity_orders(order_id=broker_order_id)
        if not orders:
            raise BrokerError(f"no order {broker_order_id} on the Agentic account", 404, "")
        return _status(orders[0])

    def get_order_by_client_reference(self, client_reference: str) -> Optional[OrderStatus]:
        found = self._by_ref(client_reference)
        return _status(found) if found else None

    def cancel_order(self, broker_order_id: str) -> None:
        try:
            self._mcp.call("cancel_equity_order", {"account_number": self._mcp.account_number,
                                                   "order_id": broker_order_id})
        except RobinhoodError as error:
            if "cannot be cancelled" not in str(error).lower():   # terminal already: idempotent
                raise BrokerError(str(error), 400, str(error)) from error

    def open_orders(self) -> list[str]:
        return [str(o["id"]) for o in self._mcp.equity_orders()
                if o.get("id") and not _status(o).is_terminal]

    # -- the account ----------------------------------------------------------------

    def get_positions(self) -> list[BrokerPosition]:
        out = []
        for row in self._mcp.equity_positions():
            quantity = _d(row.get("quantity"))
            if quantity <= 0:
                continue
            cost = _d(row.get("average_buy_price")) * quantity
            out.append(BrokerPosition(symbol=str(row.get("symbol", "")).upper(), quantity=quantity,
                                      market_value=cost, cost_basis=cost))
        return out

    def get_buying_power(self) -> Decimal:
        """SETTLED cash only (ruling 2026-11-04: settled funds only, no
        good-faith violations): cash less the account's unsettled funds."""
        return settled_cash(self._mcp.portfolio(), self._mcp.account_row())

    def permissions(self) -> BrokerPermissions:
        row = self._mcp.account_row()
        level = int(str(row.get("option_level") or "option_level_0").rsplit("_", 1)[-1] or 0)
        return BrokerPermissions(options_level=level, shorting_enabled=False,
                                 margin_multiplier=Decimal("1") if row.get("type") == "cash" else Decimal("2"))


def settled_cash(portfolio: dict, account: dict) -> Decimal:
    """Cash the account may spend without a good-faith violation: cash less
    unsettled funds and less pending deposits (Robinhood's cash includes
    early access to deposits in transit)."""
    cash = _d(portfolio.get("cash"))
    held_back = _d(account.get("unsettled_funds")) + _d(portfolio.get("pending_deposits"))
    return max(Decimal("0"), cash - held_back)


_TERMINAL = {"filled": "filled", "cancelled": "cancelled", "rejected": "rejected", "failed": "rejected",
             "voided": "rejected", "partially_filled_rest_cancelled": "cancelled"}


def _status(order: dict) -> OrderStatus:
    state = str(order.get("state", ""))
    filled = _d(order.get("cumulative_quantity"))
    price = order.get("average_price")   # never "price": that is a limit order's limit
    return OrderStatus(broker_order_id=str(order.get("id", "")), status=_TERMINAL.get(state, state),
                       filled_quantity=filled, filled_avg_price=_d(price) if filled > 0 and price else None)
