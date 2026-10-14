"""Verify every credential in ~/Agentic/.env with one read-only call each
(audit 2026-10-14). Prints key names, PASS/FAIL and HTTP status - NEVER a
value. Discord webhooks also print their creation time (from the webhook ID),
which shows whether a webhook was recreated after a given date.

    ~/Agentic/.venv/bin/python ~/Agentic/ops/vps/verify_keys.py

Exit 0 when every verifiable key passes. The Gmail SMTP login cannot be
checked from the droplet (DigitalOcean blocks outbound SMTP); it is reported
UNVERIFIED and does not fail the run.
"""
import datetime as dt
import sys
import smtplib
from pathlib import Path

import httpx
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import datasafety  # noqa: E402,F401 - the data guard: this script reads only

path = Path.home() / "Agentic" / ".env"
env = dotenv_values(path)
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
print(".env modified:", dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
c = httpx.Client(timeout=20)


failures = []


def show(name, ok, note=""):
    if not ok:
        failures.append(name)
    print(f"{name:34s} {'PASS' if ok else 'FAIL'}  verified {now}  {note}")


for n, k, s in (("main", "ALPACA_API_KEY", "ALPACA_API_SECRET"),
                ("ladder", "ALPACA_LAB_LADDER_API_KEY", "ALPACA_LAB_LADDER_API_SECRET"),
                ("ai-trader", "ALPACA_LAB_AI_TRADER_API_KEY", "ALPACA_LAB_AI_TRADER_API_SECRET")):
    r = c.get("https://paper-api.alpaca.markets/v2/account",
              headers={"APCA-API-KEY-ID": env[k], "APCA-API-SECRET-KEY": env[s]})
    show(f"{k} + _SECRET", r.status_code == 200, f"HTTP {r.status_code} ({n})")

r = c.get("https://api.anthropic.com/v1/models", headers={"x-api-key": env["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
show("ANTHROPIC_API_KEY", r.status_code == 200, f"HTTP {r.status_code}")

for k in ("DISCORD_WEBHOOK_URGENT", "DISCORD_WEBHOOK_REPORTS"):
    r = c.get(env[k])
    note = f"HTTP {r.status_code}"
    if r.status_code == 200:
        wid = int(r.json()["id"])
        created = dt.datetime.fromtimestamp(((wid >> 22) + 1420070400000) / 1000, dt.timezone.utc)
        note += f"; webhook created {created:%Y-%m-%d %H:%M UTC}"
    show(k, r.status_code == 200, note)

r = c.get("https://finnhub.io/api/v1/quote", params={"symbol": "SPY", "token": env["FINNHUB_API_KEY"]})
show("FINNHUB_API_KEY", r.status_code == 200, f"HTTP {r.status_code}")

r = c.get("https://api.quiverquant.com/beta/live/congresstrading", headers={"Authorization": f"Bearer {env['QUIVER_API_KEY']}"})
show("QUIVER_API_KEY", r.status_code == 200, f"HTTP {r.status_code}")

r = c.get("https://api.x.com/2/users/by/username/X", headers={"Authorization": f"Bearer {env['X_BEARER_TOKEN']}"})
show("X_BEARER_TOKEN", r.status_code == 200, f"HTTP {r.status_code}")

r = c.get(f"https://ntfy.sh/{env['NTFY_TOPIC']}/json", params={"poll": "1", "since": "1m"})
show("NTFY_TOPIC", r.status_code == 200, f"HTTP {r.status_code} (the topic name is the secret: anyone holding it can post and read)")

try:
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as s:
        s.starttls()
        s.login(env["ALERT_SMTP_USER"], env["ALERT_SMTP_PASSWORD"])
    show("ALERT_SMTP_USER + _PASSWORD", True, "login accepted (smtp.gmail.com assumed)")
except Exception as e:  # noqa: BLE001
    print(f"{'ALERT_SMTP_USER + _PASSWORD':34s} UNVERIFIED  {type(e).__name__}: {str(e)[:80]}")
print(f"{'ALERT_TO':34s} not a secret (an address)")
print(f"{'PAPER_MODE':34s} not a secret; value check: {'paper' if env.get('PAPER_MODE', '').strip().lower() in ('true', '1', 'yes') else 'NOT true'}")
sys.exit(1 if failures else 0)
