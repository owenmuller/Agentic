"""Form 8-K item-filtered source tests (human ruling 2026-09-15).

The claims: only filings carrying a whitelisted item emit; filings with no
US-listed ticker are dropped; bearish items are measurement-only and a mixed
filing is measurement-only; the primary document excerpt rides the content and
its failure degrades rather than drops; seen accessions never re-emit; the
fetcher self-throttles between EDGAR sweeps; the Class 1 scanner carries the
fetcher's ticker; the prefilter passes researchable filings and measurement-
skips bearish ones; the prompt speaks 8-K; the family is issuer_filings; the
forward funnel reads the items and the report slices by them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from audit.records import SignalSnapshot, snapshot_8k_items
from forward.funnel import FunnelEntry
from forward.report import render_forward_report
from orchestrator import ResearchPreFilter
from orchestrator.registry import family_of
from research.prompts import build_user_prompt
from signals import Form8KFetcher, SignalClass, SignalQueue, SignalsConfig
from signals.form8k import strip_html
from signals.scanners import Class1RealtimeScanner, RawItem

NOW = datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)


def hit(adsh, items, display="FIRST FINANCIAL BANCORP /OH/  (FFBC)  (CIK 0000708955)", form="8-K",
        cik="0000708955", file_date="2026-09-15", period="2026-09-14"):
    return {
        "_source": {
            "adsh": adsh,
            "form": form,
            "file_date": file_date,
            "items": list(items),
            "display_names": [display],
            "ciks": [cik],
            "period_ending": period,
            "file_description": form,
        }
    }


CEO_CHANGE = hit("0000708955-26-000155", ["5.02", "7.01", "9.01"])
RESTATEMENT = hit("0001050441-26-000106", ["4.02", "9.01"],
                  display="EAGLE BANCORP INC  (EGBN)  (CIK 0001050441)", cik="0001050441")
MIXED = hit("0002035989-26-000001", ["1.01", "1.05"],
            display="Amrize Ltd  (AMRZ)  (CIK 0002035989)", cik="0002035989")
OFF_LIST = hit("0003333333-26-000001", ["7.01", "9.01"],
               display="Quiet Corp  (QUIE)  (CIK 0003333333)", cik="0003333333")
NO_TICKER = hit("0004444444-26-000001", ["5.02"],
                display="Private Holdings LLC  (CIK 0004444444)", cik="0004444444")
AMENDED = hit("0005555555-26-000001", ["2.05", "9.01"], form="8-K/A",
              display="Restructure Inc  (RSTR)  (CIK 0005555555)", cik="0005555555")

PRIMARY_HTML = """<html><body><p>Item 5.02 Departure of Directors or Certain Officers.</p>
<p>On September 14, 2026, the Board accepted the resignation of Jane Doe as
Chief Executive Officer, effective immediately. Mr. John Roe was appointed
interim CEO.</p></body></html>"""


class Recorder:
    def __init__(self, hits_by_item, index_status=200, doc_status=200):
        self.requests: list[httpx.Request] = []
        self._hits = hits_by_item
        self._index_status = index_status
        self._doc_status = doc_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "efts.sec.gov" in url:
            q = request.url.params.get("q", "")
            item = q.replace('"Item ', "").replace('"', "")
            return httpx.Response(200, json={"hits": {"hits": self._hits.get(item, [])}})
        if url.endswith("/index.json"):
            return httpx.Response(
                self._index_status,
                json={"directory": {"item": [{"name": "0000708955-26-000155-index.htm"},
                                              {"name": "ex99-1.htm"},
                                              {"name": "ffbc-8k_20260914.htm"}]}},
            )
        if url.endswith(".htm"):
            return httpx.Response(self._doc_status, text=PRIMARY_HTML)
        return httpx.Response(404, text=f"unrouted: {url}")


def fetcher_with(hits_by_item, **kwargs):
    recorder = Recorder(hits_by_item, kwargs.pop("index_status", 200), kwargs.pop("doc_status", 200))
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    clock = kwargs.pop("clock", None) or (lambda: NOW)
    kwargs.setdefault("sleeper", lambda seconds: None)
    return Form8KFetcher(client, clock=clock, **kwargs), recorder


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def source(signals_config):
    return signals_config.source("class_1", "form_8k")


def test_the_source_is_configured_as_ruled(source):
    assert tuple(source.items_whitelist) == ("5.02", "4.02", "2.05", "1.01", "1.05")
    assert tuple(source.bearish_items) == ("4.02", "1.05")
    assert source.daily_research_cap == 6
    assert source.prefilter is not None and source.prefilter.max_report_age_days == 3


def test_only_whitelisted_items_emit_and_the_facts_ride_the_content(source):
    # OFF_LIST comes back on the 5.02 phrase query (its body mentions the item)
    # but carries no whitelisted item: listed, then dropped.
    fetcher, recorder = fetcher_with({"5.02": [CEO_CHANGE, NO_TICKER, OFF_LIST]})
    items = fetcher(source)
    assert [i.external_id for i in items] == [CEO_CHANGE["_source"]["adsh"]]
    fields = items[0].fields
    assert (fields["form"], fields["ticker"], fields["issuer"]) == ("8-K", "FFBC", "FIRST FINANCIAL BANCORP /OH/")
    assert (fields["items"], fields["whitelisted_items"]) == ("5.02,7.01,9.01", "5.02")
    assert (fields["measurement_only"], fields["amendment"]) == ("false", "false")
    assert (fields["report_date"], fields["event_date"]) == ("2026-09-15", "2026-09-14")
    content = items[0].content
    assert "issuer: FIRST FINANCIAL BANCORP /OH/ (FFBC)" in content
    assert "items: 5.02, 7.01, 9.01" in content
    assert "Item 5.02: departure or appointment" in content and "[WHITELISTED]" in content
    assert "resignation of Jane Doe" in content  # the primary document excerpt
    assert "ex99" not in "".join(str(r.url) for r in recorder.requests if r.url.path.endswith(".htm"))
    # One listing per whitelisted item, nothing for items outside it.
    listed = [r.url.params.get("q") for r in recorder.requests if "efts" in str(r.url)]
    assert listed == [f'"Item {i}"' for i in source.items_whitelist]
    assert fetcher.last_tally == {"listed": 3, "off_whitelist": 1, "no_ticker": 1, "measurement": 0, "emitted": 1}


def test_bearish_items_are_measurement_only_and_a_mixed_filing_is_too(source):
    fetcher, _ = fetcher_with({"4.02": [RESTATEMENT], "1.01": [MIXED], "1.05": [MIXED]})
    items = {i.fields["ticker"]: i for i in fetcher(source)}
    assert set(items) == {"EGBN", "AMRZ"}
    assert items["EGBN"].fields["measurement_only"] == "true"
    assert "MEASUREMENT ONLY" in items["EGBN"].content
    assert "[BEARISH — measurement only]" in items["EGBN"].content
    # 1.01 alone would research; the 1.05 on the same filing makes it measurement.
    assert items["AMRZ"].fields["measurement_only"] == "true"
    assert items["AMRZ"].fields["whitelisted_items"] == "1.01,1.05"


def test_an_amendment_is_marked_and_an_excerpt_failure_degrades(source):
    fetcher, _ = fetcher_with({"2.05": [AMENDED]}, index_status=404)
    items = fetcher(source)
    assert len(items) == 1
    assert items[0].fields["amendment"] == "true"
    assert "AMENDMENT of an earlier 8-K" in items[0].content
    assert "primary document excerpt" not in items[0].content
    assert items[0].fields["filing_url"].endswith("/5555555/000555555526000001/")


def test_seen_accessions_and_the_poll_throttle_hold(source):
    fetcher, recorder = fetcher_with({"5.02": [CEO_CHANGE]}, seen=[CEO_CHANGE["_source"]["adsh"]])
    assert fetcher(source) == []
    # A second call inside the throttle window makes NO EDGAR request at all.
    before = len(recorder.requests)
    assert fetcher(source) == []
    assert len(recorder.requests) == before

    state = {"now": NOW}
    fetcher, recorder = fetcher_with({"5.02": [CEO_CHANGE]}, clock=lambda: state["now"])
    assert len(fetcher(source)) == 1
    state["now"] = NOW + timedelta(seconds=120)
    assert fetcher(source) == [] and len([r for r in recorder.requests if "efts" in str(r.url)]) == 5
    state["now"] = NOW + timedelta(seconds=301)
    fetcher(source)
    assert len([r for r in recorder.requests if "efts" in str(r.url)]) == 10
    assert fetcher.last_tally["emitted"] == 0  # already seen: never re-emitted


def test_strip_html_is_tolerant():
    assert strip_html("<p>a &amp; b</p>\n<br/>  c", 100) == "a & b c"
    assert strip_html(None, 10) == ""
    assert len(strip_html("x" * 50, 10)) == 10


def scanner_signal(signals_config, item: RawItem):
    queue = SignalQueue()

    def fetch(config):
        return [item] if config.id == "form_8k" else []

    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"), fetch, queue, clock=lambda: NOW
    )
    emitted = [s for s in scanner.poll(force=True) if s.source_id == "form_8k"]
    assert len(emitted) == 1
    return emitted[0]


def raw_item(measurement=False, items="5.02,9.01"):
    fields = {
        "form": "8-K",
        "accession": "0000708955-26-000155",
        "ticker": "FFBC",
        "issuer": "First Financial Bancorp",
        "items": items,
        "whitelisted_items": items.split(",")[0],
        "report_date": NOW.date().isoformat(),
        "event_date": "2026-09-14",
        "amendment": "false",
        "measurement_only": "true" if measurement else "false",
        "filing_url": "https://www.sec.gov/Archives/edgar/data/708955/000070895526000155/",
    }
    return RawItem(
        external_id=fields["accession"],
        content=f"Form 8-K current report (SEC EDGAR)\nissuer: First Financial Bancorp (FFBC)\nitems: {items.replace(',', ', ')}",
        published_at=NOW,
        fields=fields,
    )


def test_the_class_1_scanner_carries_the_fetchers_ticker(signals_config):
    signal = scanner_signal(signals_config, raw_item())
    assert signal.signal_class is SignalClass.CLASS_1_REALTIME
    assert signal.metadata["tickers"] == "FFBC"  # from fields, not a cashtag
    assert signal.metadata["form"] == "8-K"


def test_researchable_filings_pass_the_prefilter_and_bearish_ones_are_measured(signals_config):
    prefilter = ResearchPreFilter.from_config(signals_config)
    assert prefilter.skip_verdict(scanner_signal(signals_config, raw_item()), now=NOW) is None
    verdict = prefilter.skip_verdict(
        scanner_signal(signals_config, raw_item(measurement=True, items="4.02,9.01")), now=NOW
    )
    assert verdict is not None and verdict[1] == "measurement"


def test_the_prompt_speaks_8k(signals_config):
    prompt = build_user_prompt(scanner_signal(signals_config, raw_item()))
    assert "Form 8-K current report filed by the issuer" in prompt
    assert "filing-day reaction is NOT the trade" in prompt
    assert "5.02 cuts both ways" in prompt
    assert "congressional" not in prompt.split("BEGIN UNTRUSTED")[0].lower()


def test_8k_is_the_sixth_family():
    assert family_of("form_8k", SignalClass.CLASS_1_REALTIME) == "issuer_filings"
    assert family_of("trump_posts", SignalClass.CLASS_1_REALTIME) == "trump_posts"


def test_the_forward_funnel_reads_the_items_and_the_report_slices_them():
    snap = SignalSnapshot(
        signal_id="x", source_id="form_8k", signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=NOW, content=raw_item().content, raw_content=raw_item().content,
    )
    assert snapshot_8k_items(snap) == ("5.02", "9.01")
    entries = [
        FunnelEntry(
            decision_id=f"d-{i}", source_id="form_8k", credibility_key="form_8k",
            signal_class=SignalClass.CLASS_1_REALTIME, observed_at=NOW,
            tickers=("FFBC",), bucket=bucket, code=code, confidence=None, lag_days=None,
            form8k_items=items,
        )
        for i, (bucket, code, items) in enumerate([
            ("traded", "", ("5.02", "9.01")),
            ("prefiltered", "bearish_measurement", ("4.02",)),
        ])
    ]
    report = render_forward_report(entries, {})
    assert "8-K by item" in report
    assert "item 5.02" in report
    assert "item 4.02 (bearish, measurement only" in report
