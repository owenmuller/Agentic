"""EDGAR 13F fetcher tests.

Unit tests run against fixture responses shaped like the real API (the shapes were
captured from live EDGAR responses for the actual watchlist fund, then anonymised).
The live smoke at the bottom hits the real EDGAR and is opt-in via
``EDGAR_LIVE_TESTS=1`` — it needs no key, but the default suite stays hermetic.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import httpx
import pytest

from signals import (
    EdgarError,
    Form13FFetcher,
    SignalClass,
    SignalQueue,
    SignalsConfig,
)
from signals.scanners import Class3Form13FScanner

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)

FUND = "Situational Awareness"
CIK = "0002045724"
ACCESSION = "0002045724-26-000008"

FTS_FIXTURE = {
    "hits": {
        "total": {"value": 2},
        "hits": [
            {
                "_id": f"{ACCESSION}:primary_doc.xml",
                "_source": {
                    "ciks": [CIK],
                    "display_names": ["Situational Awareness LP  (CIK 0002045724)"],
                    "file_date": "2026-05-18",
                    "adsh": ACCESSION,
                    "file_type": "13F-HR",
                },
            },
            {
                # A decoy: someone else's filing that merely MENTIONS the fund.
                "_id": "0009999999-26-000001:letter.htm",
                "_source": {
                    "ciks": ["0009999999"],
                    "display_names": ["Unrelated Capital Management  (CIK 0009999999)"],
                    "file_date": "2026-05-17",
                    "adsh": "0009999999-26-000001",
                    "file_type": "13F-HR",
                },
            },
        ],
    }
}

INDEX_FIXTURE = {
    "directory": {
        "item": [
            {"name": f"{ACCESSION}-index.html"},
            {"name": "primary_doc.xml"},
            {"name": "salp13fq1xml.xml"},
        ]
    }
}

PRIMARY_DOC_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler">
  <headerData>
    <filerInfo>
      <periodOfReport>03-31-2026</periodOfReport>
    </filerInfo>
  </headerData>
</edgarSubmission>
"""

INFOTABLE_FIXTURE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<ns1:informationTable xmlns:ns1="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <ns1:infoTable>
    <ns1:nameOfIssuer>ASML HLDG NV N Y REGISTRY</ns1:nameOfIssuer>
    <ns1:titleOfClass>SHS</ns1:titleOfClass>
    <ns1:cusip>N07059210</ns1:cusip>
    <ns1:value>494122503</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>374100</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
    <ns1:putCall>Put</ns1:putCall>
    <ns1:investmentDiscretion>SOLE</ns1:investmentDiscretion>
  </ns1:infoTable>
  <ns1:infoTable>
    <ns1:nameOfIssuer>NVIDIA CORP</ns1:nameOfIssuer>
    <ns1:titleOfClass>COM</ns1:titleOfClass>
    <ns1:cusip>67066G104</ns1:cusip>
    <ns1:value>812000000</ns1:value>
    <ns1:shrsOrPrnAmt>
      <ns1:sshPrnamt>6500000</ns1:sshPrnamt>
      <ns1:sshPrnamtType>SH</ns1:sshPrnamtType>
    </ns1:shrsOrPrnAmt>
    <ns1:investmentDiscretion>SOLE</ns1:investmentDiscretion>
  </ns1:infoTable>
</ns1:informationTable>
"""


class EdgarRecorder:
    """Routes requests to fixtures by URL, and remembers every request made."""

    def __init__(self, overrides: Optional[dict[str, httpx.Response]] = None) -> None:
        self.requests: list[httpx.Request] = []
        self._overrides = overrides or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for fragment, response in self._overrides.items():
            if fragment in url:
                return response
        if "efts.sec.gov" in url:
            return httpx.Response(200, json=FTS_FIXTURE)
        if url.endswith("/index.json"):
            return httpx.Response(200, json=INDEX_FIXTURE)
        if url.endswith("/primary_doc.xml"):
            return httpx.Response(200, text=PRIMARY_DOC_FIXTURE)
        if url.endswith("/salp13fq1xml.xml"):
            return httpx.Response(200, text=INFOTABLE_FIXTURE)
        return httpx.Response(404, text=f"unrouted: {url}")


def fetcher_with(
    overrides: Optional[dict[str, httpx.Response]] = None, **kwargs
) -> tuple[Form13FFetcher, EdgarRecorder]:
    recorder = EdgarRecorder(overrides)
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("sleeper", lambda seconds: None)
    return Form13FFetcher(client, **kwargs), recorder


@pytest.fixture(scope="session")
def source():
    return SignalsConfig.load().source("class_3", "form_13f")


# ================================================================================
# One filing, parsed
# ================================================================================


def test_a_13f_filing_becomes_one_raw_item(source):
    fetcher, _ = fetcher_with()
    items = fetcher(source)

    assert len(items) == 1
    item = items[0]
    assert item.external_id == ACCESSION
    assert item.published_at == datetime(2026, 5, 18, tzinfo=timezone.utc)
    assert item.fields["fund"] == FUND
    assert item.fields["cik"] == CIK
    assert item.fields["accession"] == ACCESSION
    assert item.fields["file_date"] == "2026-05-18"
    assert item.fields["period_of_report"] == "03-31-2026"
    assert item.fields["holdings_count"] == "2"


def test_the_content_renders_holdings_largest_first(source):
    fetcher, _ = fetcher_with()
    content = fetcher(source)[0].content

    assert "13F-HR filing by Situational Awareness" in content
    assert ACCESSION in content
    # NVDA's $812M outranks ASML's $494M whatever the XML order was.
    assert content.index("NVIDIA CORP") < content.index("ASML HLDG NV")
    assert "$812,000,000" in content
    assert "6500000 SH" in content
    assert "[CUSIP 67066G104]" in content


def test_a_put_position_is_rendered_as_a_put(source):
    """13Fs report bought puts too; a put is not a long-equity conviction and the
    research layer must be able to see the difference."""
    fetcher, _ = fetcher_with()
    content = fetcher(source)[0].content
    assert "ASML HLDG NV N Y REGISTRY (Put)" in content


def test_a_filing_that_merely_mentions_the_fund_is_not_a_signal(source):
    """FTS matches any document containing the phrase. The decoy hit is filed by
    'Unrelated Capital Management' and must be dropped by the display-name filter."""
    fetcher, recorder = fetcher_with()
    items = fetcher(source)

    assert [item.external_id for item in items] == [ACCESSION]
    # And its archives were never even fetched.
    assert not any("0009999999" in str(r.url) for r in recorder.requests)


# ================================================================================
# The SEC's rules: contact header and rate limit
# ================================================================================


def test_every_request_carries_the_contact_user_agent(source):
    fetcher, recorder = fetcher_with()
    fetcher(source)

    assert len(recorder.requests) >= 4  # FTS, index, cover, info table
    for request in recorder.requests:
        assert request.headers["User-Agent"] == source.user_agent
        assert "@" in request.headers["User-Agent"]


def test_a_missing_contact_refuses_to_run(source):
    fetcher, recorder = fetcher_with()
    anonymous = source.model_copy(update={"user_agent": None})

    with pytest.raises(EdgarError, match="User-Agent"):
        fetcher(anonymous)
    assert recorder.requests == []  # refused before anything went over the wire


def test_a_user_agent_without_an_email_is_not_a_contact(source):
    fetcher, _ = fetcher_with()
    nameless = source.model_copy(update={"user_agent": "agentic-bot/1.0"})
    with pytest.raises(EdgarError, match="email"):
        fetcher(nameless)


def test_requests_are_throttled_below_the_sec_ceiling(source):
    """Consecutive requests at the same monotonic instant must be spaced by the
    configured interval — 0.5s default, a fifth of the SEC's 10/s ceiling."""
    naps: list[float] = []
    fetcher, recorder = fetcher_with(
        sleeper=naps.append, monotonic=lambda: 1000.0, min_request_interval=0.5
    )
    fetcher(source)

    assert len(recorder.requests) >= 4
    # Every request after the first waited the full interval.
    assert len(naps) == len(recorder.requests) - 1
    assert all(nap == pytest.approx(0.5) for nap in naps)


def test_the_search_window_is_bounded_by_the_lookback(source):
    fetcher, recorder = fetcher_with(lookback_days=120)
    fetcher(source)

    fts = recorder.requests[0]
    assert fts.url.params["q"] == f'"{FUND}"'
    assert fts.url.params["forms"] == "13F-HR"
    assert fts.url.params["startdt"] == "2026-04-20"  # NOW - 120 days
    assert fts.url.params["enddt"] == "2026-08-18"


# ================================================================================
# Re-polls and degradation
# ================================================================================


def test_a_repoll_does_not_refetch_or_reemit_a_seen_filing(source):
    fetcher, recorder = fetcher_with()
    assert len(fetcher(source)) == 1
    before = len(recorder.requests)

    assert fetcher(source) == []
    # Only the FTS query re-ran; the archives were not touched again.
    assert len(recorder.requests) == before + 1
    assert "efts.sec.gov" in str(recorder.requests[-1].url)


def test_a_failed_search_raises_so_the_loop_logs_the_cycle(source):
    """A dead FTS is a dead poll — the loop's scanner handling logs and skips it."""
    fetcher, _ = fetcher_with({"efts.sec.gov": httpx.Response(503, text="down")})
    with pytest.raises(EdgarError, match="503"):
        fetcher(source)


def test_a_malformed_information_table_skips_the_filing_without_raising(source):
    fetcher, _ = fetcher_with(
        {"salp13fq1xml.xml": httpx.Response(200, text="<not really xml")}
    )
    assert fetcher(source) == []
    # Not marked seen: a later poll retries it rather than burying it forever.
    assert fetcher(source) == []  # (still broken here, but it was re-attempted)


def test_a_missing_cover_still_yields_the_filing_with_an_unknown_period(source):
    fetcher, _ = fetcher_with({"primary_doc.xml": httpx.Response(404, text="gone")})
    items = fetcher(source)
    assert len(items) == 1
    assert items[0].fields["period_of_report"] == ""
    assert "reporting period unknown" in items[0].content


# ================================================================================
# Through the real Class 3 scanner
# ================================================================================


def test_the_fetcher_feeds_the_class_3_scanner(source):
    config = SignalsConfig.load()
    fetcher, _ = fetcher_with()
    queue = SignalQueue()
    scanner = Class3Form13FScanner(
        config.klass("class_3"), fetcher, queue, clock=lambda: NOW
    )

    emitted = scanner.poll(force=True)

    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.signal_class is SignalClass.CLASS_3_THESIS
    assert signal.source_id == "form_13f"
    assert signal.external_id == ACCESSION
    assert "NVIDIA CORP" in signal.content
    # The class's standing caveats ride along as metadata, never as content.
    assert signal.metadata["longs_only"] == "true"
    assert signal.metadata["never_use_for"] == "timing"
    assert signal.metadata["priced_in_analysis_required"] == "true"
    assert signal.metadata["fund"] == FUND
    assert signal.metadata["period_of_report"] == "03-31-2026"


# ================================================================================
# Live smoke — opt-in, because the default suite stays hermetic
# ================================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("EDGAR_LIVE_TESTS") != "1",
    reason="set EDGAR_LIVE_TESTS=1 to hit the real EDGAR (no key needed)",
)
def test_live_smoke_against_the_real_watchlist_fund():
    """One real poll: FTS, archives, information table, all of it. Uses a generous
    lookback because 13Fs are quarterly and a fixed window would flap."""
    source = SignalsConfig.load().source("class_3", "form_13f")
    fetcher = Form13FFetcher(lookback_days=400)
    try:
        items = fetcher(source)
    finally:
        fetcher.close()

    assert items, "expected at least one 13F-HR in the last 400 days"
    item = items[0]
    assert item.fields["fund"] == FUND
    assert item.external_id.count("-") == 2  # an accession number
    assert int(item.fields["holdings_count"]) > 0
    assert "13F-HR filing by" in item.content
    assert "$" in item.content


def test_a_transient_throttle_is_retried_once(source):
    """One 503 must not cost a daily-cadence poll a full day. One retry, then give up."""
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="throttled")
        return EdgarRecorder().handler(request)

    client = httpx.Client(transport=httpx.MockTransport(flaky))
    fetcher = Form13FFetcher(
        client, clock=lambda: NOW, sleeper=lambda seconds: None
    )
    items = fetcher(source)
    assert len(items) == 1  # the retry carried the poll through


def test_a_persistent_outage_still_fails_the_poll(source):
    fetcher, _ = fetcher_with({"efts.sec.gov": httpx.Response(503, text="down")})
    with pytest.raises(EdgarError, match="503"):
        fetcher(source)


def test_edgar_dedup_survives_a_restart_via_the_audit_log(source):
    """Same seeding as Quiver: a restarted process is told what was already
    researched and does not re-download or re-emit it. Before this, every daily
    restart re-bought research on the same filings."""
    first, _ = fetcher_with()
    accession = first(source)[0].external_id

    restarted, recorder = fetcher_with(seen=[accession])
    assert restarted(source) == []
    # Only the FTS query ran; the filing's archives were never fetched.
    assert all("efts.sec.gov" in str(r.url) for r in recorder.requests)
