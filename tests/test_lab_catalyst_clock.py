"""Move since public (human ruling 2026-11-12, item 4): the catalyst's first
matching headline, the price when it went public (the prior close for an
off-session release, the minute bar inside a session), the move in the idea's
direction, ATR(14) and the shadow's "would skip"; never a guess, never a raise."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from lab.catalyst_clock import CatalystClock, words

D = Decimal
NY = ZoneInfo("America/New_York")
CFG = SimpleNamespace(news_lookback_hours=96, min_word_overlap=2, skip_above_atr=D("1.0"))
NOW = datetime(2026, 11, 12, 12, 1, tzinfo=NY)


def daily(last_day=date(2026, 11, 11), close=12.0, today_close=14.0):
    """20 sessions of 1.00-wide bars closing at ``close``, then today's bar."""
    rows, day = [], last_day
    while len(rows) < 20:
        if day.weekday() < 5:
            rows.append({"t": f"{day.isoformat()}T05:00:00Z", "o": close, "h": close + 0.5, "l": close - 0.5,
                         "c": close})
        day -= timedelta(days=1)
    rows.reverse()
    rows.append({"t": "2026-11-12T05:00:00Z", "o": 13.5, "h": 15.0, "l": 13.0, "c": today_close})
    return rows


class Host:
    def __init__(self, news, bars=None, minute=None, fail=None):
        self.news, self.bars, self.minute, self.fail = news, bars or daily(), minute or [], fail
        self.paths: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if self.fail and self.fail in request.url.path:
            return httpx.Response(500, json={})
        if request.url.path == "/v1beta1/news":
            return httpx.Response(200, json={"news": self.news})
        if request.url.params.get("timeframe") == "1Min":
            return httpx.Response(200, json={"bars": self.minute})
        return httpx.Response(200, json={"bars": self.bars})


def clock_for(host, reported=None):
    client = httpx.Client(base_url="https://data.example", transport=httpx.MockTransport(host.handler))
    return CatalystClock(CFG, client, report=(lambda *a: reported.append(a)) if reported is not None else (lambda *a: 0))


LUNR_NEWS = [
    {"created_at": "2026-11-12T12:44:00Z", "headline": "Intuitive Machines Q3 EPS Beats, Reports Record Backlog"},
    {"created_at": "2026-11-12T12:31:00Z", "headline": "Intuitive Machines Narrows FY2026 Sales Guidance"},
    {"created_at": "2026-11-11T20:32:00Z", "headline": "Cisco Could Swing After Earnings"},
]


def test_an_overnight_release_is_measured_from_the_prior_close():
    host = Host(LUNR_NEWS)
    out = clock_for(host).measure("LUNR", "Q3 2026 earnings beat with record revenue and record backlog", 1,
                                  NOW, D("14.14"))
    assert out["status"] == "ok" and out["public_at"].startswith("2026-11-12T12:44")
    assert out["reference_kind"] == "prior_close" and D(out["reference"]) == D("12.0")
    assert D(out["move"]) == D("0.17833")                         # 14.14 / 12.00 - 1
    assert D(out["atr_fraction"]) == D("1") / D("12")              # today's bar is not in the ATR
    assert out["would_skip"] is True and D(out["move_in_atr"]) > 2


def test_a_release_inside_the_session_is_measured_from_its_minute_bar():
    news = [{"created_at": "2026-11-12T16:00:00Z", "headline": "Acme wins record defense contract award"}]
    host = Host(news, minute=[{"t": "2026-11-12T16:00:00Z", "o": 13.0, "h": 13.1, "l": 12.9, "c": 13.05}])
    out = clock_for(host).measure("ACME", "defense contract award, record size", 1, NOW, D("13.26"))
    assert out["reference_kind"] == "minute_at_release" and D(out["reference"]) == D("13.0")
    assert D(out["move"]) == D("0.02") and out["would_skip"] is False


def test_a_put_counts_a_fall_as_move_already_gone():
    out = clock_for(Host(LUNR_NEWS)).measure("LUNR", "record backlog and earnings beat", -1, NOW, D("10.80"))
    assert D(out["move"]) == D("0.1")                              # down 10% from 12.00: gone, for a put


def test_no_matching_headline_records_no_time_rather_than_a_guess():
    out = clock_for(Host(LUNR_NEWS)).measure("SOXS", "semis selling off on the NVDA CEO departure", 1, NOW,
                                             D("46.83"))
    assert out["status"] == "no_matching_headline" and "public_at" not in out and len(out["first_headlines"]) == 3


def test_a_failed_feed_is_a_logged_gap_and_a_card():
    reported: list = []
    out = clock_for(Host(LUNR_NEWS, fail="/v1beta1/news"), reported).measure("LUNR", "record backlog beat", 1,
                                                                              NOW, D("14.14"))
    assert out["status"] == "news_failed" and reported and "HTTP 500" in reported[0][1]


def test_words_ignore_short_words_and_stopwords():
    assert words("The stock reports Q3 EPS beat; record backlog") == {"beat", "record", "backlog"}
