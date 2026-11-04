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
from pathlib import Path
from typing import Any, Optional

import httpx

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
