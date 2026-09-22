"""Daily-bars queries end at the last COMPLETED session (incident 2026-09-22).

Alpaca's free data plan returns HTTP 403 for a SIP query whose window reaches
into the last 15 minutes — every symbol, SPY included — so a forward-return
refresh run mid-session appended nothing while the after-close Friday run had
always worked. And an intraday daily bar is not a close: a mark taken from it
would be a number no later run recomputes. One clamp answers both.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from execution.market_data import AlpacaDailyBars, completed_bars_end

NY = ZoneInfo("America/New_York")


def ny(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=NY).astimezone(timezone.utc)


def test_mid_session_the_query_ends_before_todays_bar():
    now = ny(2026, 9, 22, 13, 16)  # Tuesday, market open
    end = completed_bars_end(now, now)
    # Strictly before midnight New York on the 22nd: the 22nd's bar (stamped
    # 04:00Z) is excluded, the 21st's close is the last bar served.
    assert end < ny(2026, 9, 22, 0, 0)
    assert end >= ny(2026, 9, 21, 23, 59)


def test_after_the_close_plus_the_sip_margin_todays_bar_is_final():
    now = ny(2026, 9, 22, 16, 30)
    end = completed_bars_end(now, now)
    assert end == now - timedelta(minutes=16)
    assert end > ny(2026, 9, 22, 0, 0)  # today's bar is inside the window


def test_inside_the_sixteen_minutes_after_the_close_still_waits():
    now = ny(2026, 9, 22, 16, 10)
    assert completed_bars_end(now, now) < ny(2026, 9, 22, 0, 0)


def test_an_earlier_end_is_kept():
    now = ny(2026, 9, 22, 16, 30)
    earlier = ny(2026, 9, 1, 12, 0)
    assert completed_bars_end(earlier, now) == earlier


def test_the_client_sends_the_clamped_end():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"bars": [{"t": "2026-09-21T04:00:00Z", "c": 1}]})

    now = ny(2026, 9, 22, 13, 16)
    bars = AlpacaDailyBars(
        httpx.Client(base_url="https://data.test", transport=httpx.MockTransport(handler)),
        feed="sip",
        clock=lambda: now,
    )
    rows = bars.bars("SPY", now - timedelta(days=12), now)
    assert rows and rows[0]["c"] == 1
    sent_end = datetime.fromisoformat(seen["end"])
    assert sent_end < ny(2026, 9, 22, 0, 0)
    assert seen["feed"] == "sip"
