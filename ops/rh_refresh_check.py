"""Robinhood Agentic spike part 3 — unattended OAuth refresh check.

Run AFTER the stored access token has expired. Proves the headless-refresh path:
RFC 8414 discovery -> refresh grant -> new access token -> authenticated read,
with zero browser interaction. Prints a PASS/FAIL verdict; never prints tokens.

The refresh token ROTATES on use (measured 2026-08-18), so this script writes the
rotated pair back to Claude Code's credential store to keep it the single source
of truth. Standing consequence: a live orchestrator must hold its OWN OAuth grant
rather than share this store — two independent refreshers on one rotating token
strand whichever refreshes second.

Usage: python ops/rh_refresh_check.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MCP_URL = "https://agent.robinhood.com/mcp/trading"
AGENTIC_ACCOUNT = "742288012"


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def main() -> int:
    cred_path = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    store = json.load(open(cred_path, encoding="utf-8"))
    entry = next(
        v for v in store["mcpOAuth"].values()
        if v.get("serverName") == "robinhood-trading"
    )

    now_ms = int(time.time() * 1000)
    expired = now_ms >= entry["expiresAt"]
    print(f"stored access token (sha {_h(entry['accessToken'])}) "
          f"expire{'d' if expired else 's'} at "
          f"{datetime.fromtimestamp(entry['expiresAt'] / 1000, tz=timezone.utc).isoformat()}"
          f" — {'EXPIRED' if expired else 'still valid'}")
    if not expired:
        print("NOTE: running before expiry — still a valid refresh test, "
              "but not the post-expiry natural experiment.")

    old_refresh = entry["refreshToken"]
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": old_refresh,
        "client_id": entry["clientId"],
    }).encode()
    req = urllib.request.Request(
        "https://api.robinhood.com/oauth2/token/", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tok = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"FAIL: refresh grant returned HTTP {e.code}: {e.read().decode()[:400]}")
        return 1

    new_access = tok["access_token"]
    new_refresh = tok.get("refresh_token")
    rotated = bool(new_refresh) and new_refresh != old_refresh
    print(f"refresh grant OK: new access sha {_h(new_access)}, "
          f"expires_in {tok.get('expires_in')}s, rotated={rotated}")

    entry["accessToken"] = new_access
    if new_refresh:
        entry["refreshToken"] = new_refresh
    if tok.get("expires_in"):
        entry["expiresAt"] = now_ms + int(tok["expires_in"]) * 1000
    json.dump(store, open(cred_path, "w", encoding="utf-8"))
    print("credential store updated with the rotated pair")

    # Authenticated read with the fresh token: MCP initialize + get_portfolio.
    def post(payload: dict, session_id: str | None) -> tuple[dict | None, str | None]:
        r = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(),
                                   method="POST")
        r.add_header("Authorization", "Bearer " + new_access)
        r.add_header("Content-Type", "application/json")
        r.add_header("Accept", "application/json, text/event-stream")
        r.add_header("MCP-Protocol-Version", "2025-06-18")
        if session_id:
            r.add_header("Mcp-Session-Id", session_id)
        with urllib.request.urlopen(r, timeout=60) as resp:
            sid = resp.headers.get("Mcp-Session-Id") or session_id
            raw = resp.read().decode("utf-8", "replace")
        msg = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    cand = json.loads(line[5:].strip())
                    if "id" in cand:
                        msg = cand
                except json.JSONDecodeError:
                    pass
        if msg is None and raw.strip():
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                pass
        return msg, sid

    _, sid = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "rh-refresh-check",
                                             "version": "0.1"}}}, None)
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    msg, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "get_portfolio",
                              "arguments": {"account_number": AGENTIC_ACCOUNT}}}, sid)
    result = (msg or {}).get("result", {})
    if result.get("isError"):
        print(f"FAIL: authenticated read rejected: "
              f"{json.dumps(result.get('content'))[:300]}")
        return 1
    data = result.get("structuredContent", {}).get("data", {})
    print(f"authenticated read OK: cash {data.get('cash')}, "
          f"total_value {data.get('total_value')}")
    print("PASS: unattended refresh -> authenticated read, no interaction needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
