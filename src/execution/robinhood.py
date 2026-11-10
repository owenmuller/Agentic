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
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import httpx

from execution.base import (
    BrokerAdapter, BrokerError, BrokerPermissions, BrokerPosition, OrderOutcomeUnknown, OrderReceipt, OrderStatus,
)
from risk_gate import ApprovedOrder

MCP_URL = "https://agent.robinhood.com/mcp/trading"
TOKEN_URL = "https://api.robinhood.com/oauth2/token/"
PROTOCOL = "2025-06-18"
#: Refresh this long before the access token's expiry.
REFRESH_MARGIN_SECONDS = 24 * 3600
DEFAULT_STORE = Path.home() / ".config" / "agentic" / "rh_oauth.json"

logger = logging.getLogger("execution.robinhood")


#: A 5xx or a dropped/timed-out request says nothing about whether the server
#: acted. A READ is retried this many times, with these pauses (seconds); an
#: order is never re-sent blind - it is found by its reference instead.
READ_RETRY_PAUSES = (1.0, 3.0)
TRANSIENT_STATUS = frozenset({500, 502, 503, 504})
#: After a transient failure on the place call, look the order up by its
#: reference after these pauses (seconds) before handing back "unknown".
RECONCILE_PAUSES = (1.0, 2.0, 4.0)
#: A tool result whose error text is the server's own upstream failing: the
#: MCP answered HTTP 200 but the call behind it did not complete.
UPSTREAM_FAILURE = re.compile(r"API error 5\d\d|upstream connect error", re.IGNORECASE)
#: Namespace for the deterministic idempotency key of a caller's reference.
REF_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "live-mirror.agentic")


class RobinhoodError(RuntimeError):
    """A refused call: the server's category and message (never a token)."""


class RobinhoodTransient(RobinhoodError):
    """A 5xx, a timeout or a dropped connection: the server may or may not
    have acted. Origin: an HTTP 502 on ``initialize`` failed a live-mirror
    tick on 2026-11-05 13:40 ET."""


class RobinhoodUpstream(RobinhoodTransient):
    """The MCP answered, but its tool result says the service behind it
    failed (``API error 503: upstream connect error``). Transient: a read is
    retried, an order is looked up, never re-sent. Origin: a 503 inside a
    ``get_equity_positions`` result failed a live-mirror tick on 2026-11-10
    09:55 ET."""


def idempotency_key(client_reference: Optional[str]) -> str:
    """Robinhood's ``ref_id`` must be a UUID: the server refuses anything else
    (every live order on 2026-11-10 was refused for ``mirror-<pid>``). A
    caller's reference maps to ONE deterministic UUID, so the same logical
    order always sends the same key; no reference gets a fresh one. The key
    is shaped as a version-4 UUID, the form the server accepted on 2026-11-05."""
    if not client_reference:
        return str(uuid.uuid4())
    try:
        return str(uuid.UUID(client_reference))
    except ValueError:
        digest = uuid.uuid5(REF_NAMESPACE, client_reference).bytes
        return str(uuid.UUID(bytes=digest, version=4))


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
                 http: Optional[httpx.Client] = None, timeout: float = 60.0, sleep=time.sleep) -> None:
        if not account_number:
            raise ValueError("the Agentic account number must be pinned")
        self._store = store
        self.account_number = str(account_number)
        self._allow_orders = allow_orders
        self._http = http or httpx.Client(timeout=timeout)
        self._session: Optional[str] = None
        self._next_id = 1
        self.sleep = sleep

    # -- transport --------------------------------------------------------------------

    @staticmethod
    def _is_read(payload: dict) -> bool:
        """Safe to send twice: the handshake and every ``get_*`` tool. An order
        tool (place, cancel) is never retried here."""
        method = payload.get("method")
        if method in ("initialize", "notifications/initialized"):
            return True
        return method == "tools/call" and str((payload.get("params") or {}).get("name", "")).startswith("get_")

    def _post(self, payload: dict) -> Optional[dict]:
        pauses = READ_RETRY_PAUSES if self._is_read(payload) else ()
        for attempt in range(len(pauses) + 1):
            try:
                return self._post_once(payload)
            except RobinhoodTransient as error:
                if attempt >= len(pauses):
                    raise
                logger.warning("Robinhood %s; retrying in %.0fs (a read: safe to send again)", error,
                               pauses[attempt])
                self.sleep(pauses[attempt])
        raise AssertionError("unreachable")

    def _post_once(self, payload: dict) -> Optional[dict]:
        headers = {"Authorization": "Bearer " + self._store.access_token(), "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        try:
            response = self._http.post(MCP_URL, content=json.dumps(payload), headers=headers)
        except httpx.TransportError as error:   # a timeout or a dropped connection
            raise RobinhoodTransient(f"MCP {type(error).__name__} on {payload.get('method')}") from error
        if response.status_code in TRANSIENT_STATUS:
            raise RobinhoodTransient(f"MCP HTTP {response.status_code} on {payload.get('method')}")
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
        try:
            self._open()
        except RobinhoodTransient as error:   # the call itself was never sent: a definite "no"
            raise RobinhoodError(f"{tool} not sent: the MCP session could not be opened ({error})") from error
        # the transport retries an HTTP 5xx on a read; this retries the server's
        # upstream failing inside a 200 (a read only: an order is never re-sent)
        pauses = READ_RETRY_PAUSES if tool.startswith("get_") else ()
        for attempt in range(len(pauses) + 1):
            try:
                return self._call_once(tool, arguments)
            except RobinhoodUpstream as error:
                if attempt >= len(pauses):
                    raise
                logger.warning("Robinhood %s; retrying in %.0fs (a read: safe to send again)", error,
                               pauses[attempt])
                self.sleep(pauses[attempt])
        raise AssertionError("unreachable")

    def _call_once(self, tool: str, arguments: dict) -> Any:
        self._next_id += 1
        message = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                              "params": {"name": tool, "arguments": arguments}}) or {}
        result = message.get("result") or {}
        if message.get("error") or result.get("isError"):
            detail = json.dumps(_strip_guides(message.get("error") or result.get("content")))[:300]
            if UPSTREAM_FAILURE.search(detail):
                raise RobinhoodUpstream(f"{tool}: the server's upstream failed: {detail}")
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

    # -- option reads (temporary option ruling 2026-11-09) ------------------------------

    def option_instruments(self, underlying: str, expiration: date, right: str,
                           strike: Optional[Decimal] = None) -> list[dict]:
        """Every tradable contract on one expiry and side (all pages), or the
        one at ``strike``."""
        args: dict[str, Any] = {"chain_symbol": underlying.upper(), "expiration_dates": expiration.isoformat(),
                                "type": right, "state": "active"}
        if strike is not None:
            args["strike_price"] = f"{Decimal(strike):.4f}"
        rows: list[dict] = []
        while True:
            data = self.call("get_option_instruments", args)
            data = data.get("data", data) if isinstance(data, dict) else {}
            rows += [r for r in data.get("instruments") or [] if isinstance(r, dict)]
            cursor = data.get("next")
            if not cursor:
                return [r for r in rows if r.get("tradability", "tradable") == "tradable"]
            args["cursor"] = cursor

    def option_quotes(self, instrument_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for start in range(0, len(instrument_ids), 20):
            chunk = instrument_ids[start:start + 20]
            for row in _rows(self.call("get_option_quotes", {"instrument_ids": chunk}), "results"):
                quote = row.get("quote") if isinstance(row, dict) else None
                if isinstance(quote, dict) and quote.get("instrument_id"):
                    out[str(quote["instrument_id"])] = quote
        return out

    def option_positions(self) -> list[dict]:
        return _rows(self.call("get_option_positions", {"account_number": self.account_number, "nonzero": True}),
                     "positions")

    def option_orders(self, **filters: str) -> list[dict]:
        return _rows(self.call("get_option_orders", {"account_number": self.account_number, **filters}), "orders")


def occ_parts(occ: str) -> tuple[str, date, str, Decimal]:
    """(underlying, expiration, 'call'|'put', strike) from an OCC symbol such
    as ``MRNA261113C00260000``."""
    text = occ.strip().upper()
    root, stamp, flag, strike = text[:-15], text[-15:-9], text[-9], text[-8:]
    expiration = date(2000 + int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6]))
    return root, expiration, "call" if flag == "C" else "put", Decimal(strike) / 1000


def option_tick(instrument: dict, price: Decimal) -> Decimal:
    """The contract's price increment at ``price`` (its min_ticks)."""
    ticks = instrument.get("min_ticks") or {}
    cutoff = _d(ticks.get("cutoff_price"))
    above, below = _d(ticks.get("above_tick")) or Decimal("0.05"), _d(ticks.get("below_tick")) or Decimal("0.01")
    return above if cutoff > 0 and price >= cutoff else below


def option_position_id(row: dict) -> str:
    """The instrument UUID of an option position row, whichever way the
    server spells it (an id field, or the instrument URL)."""
    for key in ("option_id", "instrument_id"):
        if row.get(key):
            return str(row[key])
    url = str(row.get("option") or row.get("option_instrument") or "")
    return url.rstrip("/").rsplit("/", 1)[-1]


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
    """The live mirror's broker (GO LIVE ruling 2026-11-04). Equities, and
    since the temporary option ruling of 2026-11-09 single-leg LONG options:
    buy to open and sell to close, nothing else (the schema has no writing
    order). The option leg is resolved to Robinhood's instrument id from the
    order's own contract fields.

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

    def _option_payload(self, order) -> tuple[dict, str]:
        """A single long leg: buy to open, or sell to close a held long. Limits
        only (the mirror prices them on the contract's own tick)."""
        from risk_gate.schema import OptionBuyToOpenOrder

        rows = self._mcp.option_instruments(order.underlying, order.expiration, order.right, order.strike)
        if len(rows) != 1:
            raise BrokerError(f"{order.symbol}: {len(rows)} tradable contracts match at Robinhood, not one", 404, "")
        if order.execution.style != "limit":
            raise RobinhoodError("the live mirror sends option orders as limits only")
        option_id = str(rows[0]["id"])
        buy = isinstance(order, OptionBuyToOpenOrder)
        payload = {"account_number": self._mcp.account_number, "type": "limit", "time_in_force": "gfd",
                   "market_hours": "regular_hours", "quantity": str(int(order.contracts)),
                   "price": format(order.execution.limit_price.normalize(), "f"),
                   "legs": [{"option_id": option_id, "side": "buy" if buy else "sell",
                             "position_effect": "open" if buy else "close", "ratio_quantity": 1}]}
        return payload, option_id

    def submit_order(self, approved: ApprovedOrder, client_reference: Optional[str] = None) -> OrderReceipt:
        approved = self._require_approved(approved)
        option = _is_option_order(approved.order)
        try:
            # nothing before the place call can place an order: any failure here is a definite "not placed"
            if option:
                payload, option_id = self._option_payload(approved.order)
                review_tool, place_tool = "review_option_order", "place_option_order"
            else:
                payload, option_id = self._payload(approved.order), None
                review_tool, place_tool = "review_equity_order", "place_equity_order"
            if self._mcp.approvals_required():
                raise BrokerError("trade approvals are ON for the Agentic account (the human's app setting): an "
                                  "order would wait for a tap, so none is placed", 409, "human_must_approve_trades")
            review = self._mcp.call(review_tool, payload)
        except RobinhoodError as error:
            raise BrokerError(f"not placed (before the place call): {error}", 400, str(error)) from error
        review = review.get("data", review) if isinstance(review, dict) else {}
        self.last_review = review
        checks = review.get("order_checks") or {}
        if checks:
            raise BrokerError(f"review alert {checks.get('alert_type', 'UNKNOWN')}: not placed", 422,
                              json.dumps(checks)[:500])
        if review.get("customer_approval_required"):
            raise BrokerError("the review says the customer must approve this order: not placed", 409,
                              "customer_approval_required")
        ref_id = idempotency_key(client_reference)
        # Robinhood places every order under its own approval id, so the placed
        # order never carries ref_id (the tool's own schema says so; the Nov 5
        # round trip sent 42fa85ca-... and the order shows 01a1ac21-...): the
        # durable reference is the instrument, side, size and the time it was sent.
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        if option:
            leg = payload["legs"][0]
            lookup = f"opt:{option_id}:{leg['side']}:{payload['quantity']}:{since}"
        else:
            lookup = f"eq:{payload['symbol']}:{payload['side']}:{payload['quantity']}:{since}"
        try:
            placed = self._mcp.call(place_tool, {**payload, "ref_id": ref_id})
        except RobinhoodTransient as error:
            # the server may have acted: never re-send blind - find it by its reference
            found = self._reconcile(lookup)
            if found is None:
                raise OrderOutcomeUnknown(lookup, f"{place_tool}: {error}; no order matches {lookup} "
                                                  f"yet") from error
            order = found
        except RobinhoodError as error:
            if "409" in str(error) or "unique" in str(error).lower():
                found = self._by_ref(lookup)     # the first attempt registered (spike part 2)
                if found is None:
                    raise BrokerError(f"place refused and no order matches {lookup}", 409, str(error)) from error
                order = found
            else:
                raise BrokerError(str(error), 400, str(error)) from error
        else:
            order = ((placed.get("data") or {}).get("order") if isinstance(placed, dict) else None) or {}
        if not order.get("id"):
            raise BrokerError("place returned no order id", 502, json.dumps(order)[:300])
        if option:
            self._option_ids.add(str(order["id"]))
        return OrderReceipt(broker_order_id=str(order["id"]), status=str(order.get("state", "")),
                            symbol=approved.order.symbol,
                            quantity=approved.order.contracts if option else approved.order.quantity,
                            limit_price=approved.order.execution.price_bound, client_order_id=lookup)

    @property
    def _option_ids(self) -> set:
        if not hasattr(self, "_known_option_ids"):
            self._known_option_ids: set = set()
        return self._known_option_ids

    def _reconcile(self, ref_id: str) -> Optional[dict]:
        """After a transient failure on the place call: the order by its
        reference, looked up after each pause; None while it is not there."""
        for pause in RECONCILE_PAUSES:
            self._mcp.sleep(pause)
            try:
                found = self._by_ref(ref_id)
            except RobinhoodError:
                continue
            if found is not None:
                return found
        return None

    def _by_ref(self, ref_id: str) -> Optional[dict]:
        if ref_id.startswith("opt:"):
            return self._option_match(ref_id)
        if ref_id.startswith("eq:"):
            return self._equity_match(ref_id)
        for order in self._mcp.equity_orders():   # a reference from before 2026-11-10
            if order.get("ref_id") == ref_id:
                return order
        return None

    def _equity_match(self, lookup: str) -> Optional[dict]:
        """The equity order sent under ``lookup`` (eq:<symbol>:<side>:<qty>:<since>):
        the newest on that symbol, side and size created since then. The mirror
        keeps one working order, so a match is that order."""
        _, symbol, side, quantity, since = lookup.split(":", 4)
        for order in self._mcp.equity_orders(symbol=symbol, created_at_gte=since):
            if order.get("side") == side and _d(order.get("quantity")) == _d(quantity) and \
                    str(order.get("symbol", symbol)).upper() == symbol.upper():
                return order
        return None

    def _option_match(self, lookup: str) -> Optional[dict]:
        """The option order sent under ``lookup`` (opt:<id>:<side>:<qty>:<since>):
        the newest on that contract, side and size created since then. The
        mirror keeps one working order, so a match is that order."""
        _, option_id, side, quantity, since = lookup.split(":", 4)
        for order in self._mcp.option_orders(created_at_gte=since):
            legs = order.get("legs") or []
            if len(legs) != 1 or _d(order.get("quantity")) != _d(quantity):
                continue
            if option_position_id(legs[0]) == option_id and legs[0].get("side") == side:
                self._option_ids.add(str(order.get("id")))
                return order
        return None

    def _is_option_id(self, broker_order_id: str) -> bool:
        if broker_order_id in self._option_ids:
            return True
        if self._mcp.option_orders(order_id=broker_order_id):
            self._option_ids.add(broker_order_id)
            return True
        return False

    def get_order(self, broker_order_id: str) -> OrderStatus:
        if broker_order_id in self._option_ids:
            orders = self._mcp.option_orders(order_id=broker_order_id)
            if orders:
                return _option_status(orders[0])
        orders = self._mcp.equity_orders(order_id=broker_order_id)
        if orders:
            return _status(orders[0])
        orders = self._mcp.option_orders(order_id=broker_order_id)
        if orders:
            self._option_ids.add(broker_order_id)
            return _option_status(orders[0])
        raise BrokerError(f"no order {broker_order_id} on the Agentic account", 404, "")

    def get_order_by_client_reference(self, client_reference: str) -> Optional[OrderStatus]:
        found = self._by_ref(client_reference)
        if not found:
            return None
        return _option_status(found) if client_reference.startswith("opt:") else _status(found)

    def cancel_order(self, broker_order_id: str) -> None:
        tool = "cancel_option_order" if self._is_option_id(broker_order_id) else "cancel_equity_order"
        try:
            self._mcp.call(tool, {"account_number": self._mcp.account_number, "order_id": broker_order_id})
        except RobinhoodError as error:
            if "cannot be cancelled" not in str(error).lower():   # terminal already: idempotent
                raise BrokerError(str(error), 400, str(error)) from error

    def open_orders(self) -> list[str]:
        return ([str(o["id"]) for o in self._mcp.equity_orders() if o.get("id") and not _status(o).is_terminal]
                + [str(o["id"]) for o in self._mcp.option_orders()
                   if o.get("id") and not _option_status(o).is_terminal])

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


def _is_option_order(order) -> bool:
    from risk_gate.schema import OptionBuyToOpenOrder, OptionSellToCloseOrder

    return isinstance(order, (OptionBuyToOpenOrder, OptionSellToCloseOrder))


def _option_status(order: dict) -> OrderStatus:
    """An option order's state, contracts filled and average price PER SHARE
    (the quote's unit; a contract is 100 of it). The fill price comes from the
    leg executions, else the order's average, else its processed premium; with
    none of them a fill has no price and is reported as unknown, never the
    limit."""
    state = str(order.get("state", ""))
    legs = order.get("legs") or []
    executions = [x for leg in legs for x in (leg.get("executions") or []) if isinstance(x, dict)]
    filled = _d(order.get("processed_quantity")) or _d(order.get("cumulative_quantity")) \
        or sum((_d(x.get("quantity")) for x in executions), Decimal("0"))
    price: Optional[Decimal] = None
    if executions:
        units = sum((_d(x.get("quantity")) for x in executions), Decimal("0"))
        if units > 0:
            price = sum((_d(x.get("price")) * _d(x.get("quantity")) for x in executions), Decimal("0")) / units
    if price is None and _d(order.get("average_price")) > 0:
        price = _d(order.get("average_price"))
    if price is None and filled > 0 and _d(order.get("processed_premium")) > 0:
        price = _d(order.get("processed_premium")) / (filled * 100)
    return OrderStatus(broker_order_id=str(order.get("id", "")), status=_TERMINAL.get(state, state),
                       filled_quantity=filled, filled_avg_price=price if filled > 0 else None)
