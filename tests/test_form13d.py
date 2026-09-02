"""Activist 13D source tests (human ruling 2026-09-02).

The claims: only watchlist activists' filings become signals (a market-wide
listing is filtered client-side, filed-by matched the 13F way); the structured
primary_doc.xml supplies stake/date/amendment facts and an unparseable XML
degrades to hit-level facts rather than a dropped filing; the ticker comes from
EDGAR's own display name; the scanner anchors the priced-in question to the
filing (not 45-day) convention; the family is 13f_filings; and the prompt
speaks 13D, not congressional.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from orchestrator.registry import family_of
from research.prompts import build_user_prompt
from signals import Form13DFetcher, SignalClass, SignalQueue, SignalsConfig
from signals.form13d import tickers_from_display
from signals.scanners import Class2CongressionalScanner, RawItem

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)

ACCESSION = "0000899140-26-000972"
SUBJECT_CIK = "0000897448"

HIT = {
    "_source": {
        "adsh": ACCESSION,
        "form": "SCHEDULE 13D/A",
        "file_date": "2026-09-01",
        "ciks": [SUBJECT_CIK, "0001577524"],
        "display_names": [
            "AMARIN CORP PLC\\UK  (AMRN)  (CIK 0000897448)",
            "Sarissa Capital Management LP  (CIK 0001577524)",
        ],
    }
}

STRANGER_HIT = {
    "_source": {
        "adsh": "0000899140-26-000999",
        "form": "SCHEDULE 13D",
        "file_date": "2026-09-01",
        "ciks": ["0000111111", "0000222222"],
        "display_names": [
            "Unwatched Corp  (UNWA)  (CIK 0000111111)",
            "Some Family Office LLC  (CIK 0000222222)",
        ],
    }
}

PRIMARY_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D">
  <formData>
    <coverPageHeader>
      <amendmentNo>4</amendmentNo>
      <securitiesClassTitle>Common Stock</securitiesClassTitle>
      <dateOfEvent>08/28/2026</dateOfEvent>
      <issuerInfo>
        <issuerCIK>0000897448</issuerCIK>
        <issuerName>Amarin Corp plc</issuerName>
      </issuerInfo>
    </coverPageHeader>
    <reportingPersonDetails>
      <reportingPersonName>Sarissa Capital Management LP</reportingPersonName>
      <aggregateAmountOwned>25000000</aggregateAmountOwned>
      <percentOfClass>7.2</percentOfClass>
    </reportingPersonDetails>
    <reportingPersonDetails>
      <reportingPersonName>Sarissa GP</reportingPersonName>
      <aggregateAmountOwned>1000000</aggregateAmountOwned>
      <percentOfClass>0.3</percentOfClass>
    </reportingPersonDetails>
  </formData>
</edgarSubmission>"""


class Recorder:
    def __init__(self, xml_status=200):
        self.requests: list[httpx.Request] = []
        self._xml_status = xml_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "efts.sec.gov" in url:
            form = request.url.params.get("forms", "")
            hits = [HIT, STRANGER_HIT] if form == "SCHEDULE 13D/A" else [STRANGER_HIT]
            return httpx.Response(200, json={"hits": {"hits": hits}})
        if url.endswith("/primary_doc.xml"):
            return httpx.Response(self._xml_status, text=PRIMARY_DOC)
        return httpx.Response(404, text=f"unrouted: {url}")


def fetcher_with(xml_status=200, **kwargs):
    recorder = Recorder(xml_status)
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("sleeper", lambda seconds: None)
    return Form13DFetcher(client, **kwargs), recorder


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def source(signals_config):
    return signals_config.source("class_2", "form_13d")


def test_tickers_come_from_the_display_name_never_the_cik():
    assert tickers_from_display("AMARIN CORP PLC\\UK  (AMRN)  (CIK 0000897448)") == (
        "AMRN",
    )
    assert tickers_from_display(
        "Liberty Broadband Corp  (LBRDA, LBRDK)  (CIK 0001611983)"
    ) == ("LBRDA", "LBRDK")
    assert tickers_from_display("Sarissa Capital Management LP  (CIK 0001577524)") == ()


def test_only_watchlist_activists_become_signals(source):
    fetcher, recorder = fetcher_with()
    items = fetcher(source)
    assert [item.external_id for item in items] == [ACCESSION]
    # The stranger's filing was never even fetched.
    assert not any("0000111111" in str(r.url) for r in recorder.requests)

    fields = items[0].fields
    assert fields["form"] == "SCHEDULE 13D/A"
    assert fields["ticker"] == "AMRN"
    assert fields["filer"] == "Sarissa Capital"
    assert fields["credibility_key"] == "form_13d/Sarissa Capital"
    assert fields["percent_of_class"] == "7.2"  # the activist's own, not the GP's
    assert fields["aggregate_shares"] == "25000000"
    assert fields["date_of_event"] == "08/28/2026"
    assert fields["amendment_no"] == "4"
    content = items[0].content
    assert "Amarin Corp plc (AMRN)" in content
    assert "amendment no. 4" in content
    assert "7.2% of class" in content


def test_an_unparseable_xml_degrades_to_hit_facts_not_a_drop(source):
    fetcher, _ = fetcher_with(xml_status=404)
    items = fetcher(source)
    assert len(items) == 1
    fields = items[0].fields
    assert fields["ticker"] == "AMRN"
    assert fields["percent_of_class"] == ""
    assert "percent not stated" in items[0].content


def test_seen_accessions_do_not_re_emit(source):
    fetcher, _ = fetcher_with(seen=[ACCESSION])
    assert fetcher(source) == []


def scanner_signal(signals_config, item: RawItem):
    queue = SignalQueue()
    scanner = Class2CongressionalScanner(
        signals_config.klass("class_2"),
        lambda config: [item] if config.id == "form_13d" else [],
        queue,
        clock=lambda: NOW,
    )
    emitted = [
        signal for signal in scanner.poll(force=True)
        if signal.source_id == "form_13d"
    ]
    assert len(emitted) == 1
    return emitted[0]


def test_the_scanner_and_prompt_speak_13d(signals_config, source):
    fetcher, _ = fetcher_with()
    item = fetcher(source)[0]
    signal = scanner_signal(signals_config, item)
    assert signal.signal_class is SignalClass.CLASS_2_MOMENTUM
    assert signal.metadata["tickers"] == "AMRN"
    assert "5 business days" in signal.metadata["disclosure_lag_note"]

    prompt = build_user_prompt(signal)
    assert "This is a Schedule 13D beneficial-ownership filing" in prompt
    assert "This is a congressional disclosure" not in prompt


def test_13d_joins_the_13f_family():
    assert family_of("form_13d", SignalClass.CLASS_2_MOMENTUM) == "13f_filings"
