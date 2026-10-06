"""Government contract awards (human ruling 2026-10-06, amended the same day).

Claims: the production parser reads an award paragraph's awardee, amount,
ceiling, kind, multiple-award / IDIQ / sole-source language, offers and
completion from both the site HTML and the proxy's markdown; the contractor
map resolves point-in-time (renames, listings, delistings, known non-public,
small-business); the tiers are relative size and nothing else researches;
the DoD fetcher reads the RSS and the proxy, drains the evening pending file,
and stamps the determinants the funnel parses back; the FPDS fetcher excludes
DoD and stamps the structured facts; the funnel, scoring facet, prompt branch
and source family all see the new source; the forward engine's open split
measures from the next session's open and marks old rows for one recompute;
the weekly renders the subtype table.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from signals.contracts import (
    FEED_DOD,
    KIND_MODIFICATION,
    KIND_NEW,
    KIND_OPTION,
    TIER_BELOW_FLOOR,
    TIER_MEASURE,
    TIER_RESEARCH,
    AwardeeSize,
    AwardFacts,
    ContractorMap,
    DodDigestFetcher,
    FpdsCivilianFetcher,
    MapEntry,
    RatioRules,
    Resolution,
    build_item,
    digest_date_from,
    normalize_name,
    parse_digest,
    parse_fpds_atom,
    parse_rss,
    size_tier,
    term_months,
)

NOW = datetime(2026, 10, 6, 13, 35, tzinfo=timezone.utc)

DIGEST_HTML = """<html><body><div class="body">
<p><strong>NAVY</strong></p>
<p>Lockheed Martin Corp., Rotary and Missions Systems, Liverpool, New York (N00024-20-C-5503), was awarded a $208,962,712 firm-fixed-price modification to a previously-awarded contract for production of Surface Electronic Warfare Improvement Program AN/SLQ-32(V)6 systems. This contract modification includes options which, if exercised, would bring the cumulative value of this contract to $1,708,478,799. Work will be performed in Liverpool, New York (78%); and Lansdale, Pennsylvania (22%), and is expected to be completed by September 2029. Naval Sea Systems Command, Washington, D.C., is the contracting activity.</p>
<p>Kratos Defense &amp; Security Solutions Inc., San Diego, California, is awarded a $412,500,000 firm-fixed-price contract for hypersonic test vehicles. Work is expected to be completed by Dec. 31, 2028. This contract was not competitively procured in accordance with 10 U.S.C. 3204(a)(1). Naval Air Systems Command, Patuxent River, Maryland, is the contracting activity (N00019-26-C-0042).</p>
<p><strong>ARMY</strong></p>
<p>Napatree Technology LLC,* McLean, Virginia, was awarded a $499,000,000 firm-fixed-price contract for counter-drone systems. Bids were solicited via the internet with three received. Work will be performed in McLean, Virginia, with an estimated completion date of Sept. 27, 2031. Army Contracting Command, Aberdeen Proving Ground, Maryland, is the contracting activity (W91CRB-26-D-0019).</p>
<p>Alberici Constructors Inc., St. Louis, Missouri; Hensel Phelps Construction Co., Greeley, Colorado; and Fluor Federal Solutions LLC, Greenville, South Carolina, are awarded a $9,000,000,000 indefinite-delivery/indefinite-quantity, multiple award construction contract for dry dock work. Each awardee will be awarded a $5,000 minimum guarantee. Work is expected to be completed by September 2034. Naval Facilities Engineering Systems Command, Norfolk, Virginia, is the contracting activity.</p>
<p>Amentum Services Inc., Chantilly, Virginia, has been awarded a $67,648,024 modification (P00038) to a previously awarded contract (FA489022C0019) for Option Year Four of the Aerial Targets III Program. Work is expected to be completed by Sept. 30, 2027.</p>
</div></body></html>"""

DIGEST_MARKDOWN = """Title: Contracts for Oct. 5, 2026

URL Source: https://www.war.gov/News/Contracts/Contract/Article/4619270/contracts-for-oct-5-2026/

Markdown Content:
**NAVY**

Kratos Defense & Security Solutions Inc., San Diego, California, is awarded a $412,500,000 firm-fixed-price contract for hypersonic test vehicles. Work is expected to be completed by Dec. 31, 2028. Naval Air Systems Command, Patuxent River, Maryland, is the contracting activity (N00019-26-C-0042).

**AIR FORCE**

Amentum Services Inc., Chantilly, Virginia, has been awarded a $67,648,024 modification (P00038) to a previously awarded contract (FA489022C0019) for Option Year Four of the Aerial Targets III Program.
"""

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Contracts for Oct. 5, 2026</title><link>https://www.war.gov/News/Contracts/Contract/Article/4619270/contracts-for-oct-5-2026/</link><pubDate>Mon, 05 Oct 2026 21:00:16 GMT</pubDate><description>short</description></item>
<item><title>Contracts for Oct. 2, 2026</title><link>https://www.war.gov/News/Contracts/Contract/Article/4618190/contracts-for-oct-2-2026/</link><pubDate>Fri, 02 Oct 2026 21:00:25 GMT</pubDate><description>short</description></item>
</channel></rss>"""

FPDS_ATOM = """<feed><entry><title>x</title><modified>2026-10-01 14:41:43</modified><content><ns1:award xmlns:ns1="https://www.fpds.gov/FPDS">
<ns1:awardID><ns1:awardContractID><ns1:agencyID name="VETERANS AFFAIRS, DEPARTMENT OF">3600</ns1:agencyID><ns1:PIID>36C10B26D0001</ns1:PIID><ns1:modNumber>0</ns1:modNumber><ns1:transactionNumber>0</ns1:transactionNumber></ns1:awardContractID></ns1:awardID>
<ns1:relevantContractDates><ns1:signedDate>2026-10-01 00:00:00</ns1:signedDate><ns1:effectiveDate>2026-10-01 00:00:00</ns1:effectiveDate><ns1:ultimateCompletionDate>2031-09-30 00:00:00</ns1:ultimateCompletionDate></ns1:relevantContractDates>
<ns1:dollarValues><ns1:obligatedAmount>153001558.00</ns1:obligatedAmount><ns1:baseAndAllOptionsValue>400000000.00</ns1:baseAndAllOptionsValue></ns1:dollarValues>
<ns1:purchaserInformation><ns1:contractingOfficeAgencyID name="VETERANS AFFAIRS, DEPARTMENT OF" departmentID="3600" departmentName="VETERANS AFFAIRS, DEPARTMENT OF">3600</ns1:contractingOfficeAgencyID></ns1:purchaserInformation>
<ns1:contractData><ns1:contractActionType description="DEFINITIVE CONTRACT">D</ns1:contractActionType><ns1:descriptionOfContractRequirement>ENTERPRISE CLOUD</ns1:descriptionOfContractRequirement></ns1:contractData>
<ns1:competition><ns1:extentCompeted description="FULL AND OPEN COMPETITION">A</ns1:extentCompeted><ns1:numberOfOffersReceived>3</ns1:numberOfOffersReceived></ns1:competition>
<ns1:vendor><ns1:vendorHeader><ns1:vendorName>LEIDOS, INC.</ns1:vendorName></ns1:vendorHeader></ns1:vendor>
</ns1:award></content></entry>
<entry><title>y</title><modified>2026-10-01 15:00:00</modified><content><ns1:award xmlns:ns1="https://www.fpds.gov/FPDS">
<ns1:awardID><ns1:awardContractID><ns1:agencyID name="DEPT OF THE NAVY">1700</ns1:agencyID><ns1:PIID>N0002426C0001</ns1:PIID><ns1:modNumber>0</ns1:modNumber><ns1:transactionNumber>0</ns1:transactionNumber></ns1:awardContractID></ns1:awardID>
<ns1:relevantContractDates><ns1:signedDate>2026-07-01 00:00:00</ns1:signedDate></ns1:relevantContractDates>
<ns1:dollarValues><ns1:obligatedAmount>99000000.00</ns1:obligatedAmount></ns1:dollarValues>
<ns1:purchaserInformation><ns1:contractingOfficeAgencyID name="DEPT OF THE NAVY" departmentID="9700" departmentName="DEPT OF DEFENSE">1700</ns1:contractingOfficeAgencyID></ns1:purchaserInformation>
<ns1:contractData><ns1:contractActionType description="DEFINITIVE CONTRACT">D</ns1:contractActionType></ns1:contractData>
<ns1:vendor><ns1:vendorHeader><ns1:vendorName>LOCKHEED MARTIN CORPORATION</ns1:vendorName></ns1:vendorHeader></ns1:vendor>
</ns1:award></content></entry></feed>"""


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.fixture(scope="session")
def source(signals_config):
    return signals_config.source("class_1", "gov_contract_awards")


@pytest.fixture(scope="session")
def contractors():
    return ContractorMap.load()


# ---------------------------------------------------------------- parser

def test_the_parser_reads_html_and_proxy_markdown_the_same_way():
    html_awards = parse_digest(DIGEST_HTML)
    md_awards = parse_digest(DIGEST_MARKDOWN)
    by_name = {a.awardee: a for a in html_awards}
    lmt = by_name["Lockheed Martin Corp."]
    assert lmt.amount == Decimal("208962712")
    assert lmt.ceiling == Decimal("1708478799") and lmt.ceiling_stated
    assert lmt.kind == KIND_MODIFICATION and lmt.agency == "NAVY"
    assert lmt.contract_number == "N00024-20-C-5503"
    assert term_months(lmt.completion, date(2026, 10, 5)) == 35
    ktos = by_name["Kratos Defense & Security Solutions Inc."]
    assert ktos.amount == Decimal("412500000") and ktos.kind == KIND_NEW
    assert ktos.sole_source and not ktos.multiple_award and not ktos.idiq
    assert ktos.contract_number == "N00019-26-C-0042"
    napa = by_name["Napatree Technology LLC"]
    assert napa.small_business and napa.offers == 3 and napa.agency == "ARMY"
    # The three-awardee IDIQ pool yields one row per awardee, all flagged.
    pool = [a for a in html_awards if a.amount == Decimal("9000000000")]
    assert {a.awardee for a in pool} == {"Alberici Constructors Inc.", "Hensel Phelps Construction Co.", "Fluor Federal Solutions LLC"}
    assert all(a.multiple_award and a.idiq for a in pool)
    amentum = by_name["Amentum Services Inc."]
    assert amentum.kind == KIND_OPTION  # option year exercise, a modification by nature
    # Proxy markdown: same facts, agency header read through the bold markers.
    md = {a.awardee: a for a in md_awards}
    assert md["Kratos Defense & Security Solutions Inc."].amount == Decimal("412500000")
    assert md["Amentum Services Inc."].agency == "AIR FORCE"


def test_digest_dates_and_rss():
    assert digest_date_from("Contracts for Oct. 5, 2026") == date(2026, 10, 5)
    assert digest_date_from("https://www.war.gov/News/Contracts/Contract/Article/4615442/contracts-for-sept-30-2026/") == date(2026, 9, 30)
    refs = parse_rss(RSS)
    assert [r.article_id for r in refs] == ["4619270", "4618190"]
    assert refs[0].published_at == datetime(2026, 10, 5, 21, 0, 16, tzinfo=timezone.utc)
    assert refs[0].digest_date == date(2026, 10, 5)


# ---------------------------------------------------------------- map

def test_the_map_resolves_point_in_time(contractors):
    assert normalize_name("Lockheed Martin Corp., Rotary and Missions Systems") == "lockheed martin rotary and missions systems"
    assert contractors.resolve("Lockheed Martin Corp.", date(2026, 10, 5)).ticker == "LMT"
    assert contractors.resolve("Sikorsky Aircraft Corp.", date(2026, 10, 5)).ticker == "LMT"
    # Raytheon Co. was RTN until the 2020-04-03 merger, RTX after.
    before = contractors.resolve("Raytheon Co.", date(2019, 6, 1))
    after = contractors.resolve("Raytheon Co.", date(2021, 6, 1))
    assert (before.ticker, before.how) == ("RTN", "curated")
    assert (after.ticker, after.how) == ("RTX", "curated_successor")
    # Not yet listed, known non-public, small business, unresolved.
    assert contractors.resolve("Palantir Technologies Inc.", date(2019, 6, 1)).how == "not_yet_listed"
    assert contractors.resolve("General Atomics Aeronautical Systems Inc.", date(2026, 1, 1)).how == "known_nonpublic"
    assert contractors.resolve("Anything LLC", date(2026, 1, 1), small_business=True).how == "small_business"
    assert contractors.resolve("Totally Unknown Widgets Inc.", date(2026, 1, 1)).how == "unresolved"
    # Longest prefix wins: the subsidiary entry beats the parent entry.
    assert contractors.resolve("General Dynamics Electric Boat Corp.", date(2026, 1, 1)).ticker == "GD"
    # Exact EDGAR-name fallback when provided.
    with_edgar = ContractorMap([MapEntry("lockheed martin ", "LMT")], {"metallus": ("MTUS", 1598428)})
    assert with_edgar.resolve("Metallus Inc.", date(2026, 9, 25)).how == "edgar_exact"


# ---------------------------------------------------------------- tiers

def _facts(kind=KIND_NEW, multiple=False, amount="400000000"):
    return AwardFacts("X Corp.", Decimal(amount), None, kind, multiple, False, False, None, None, "NAVY", None, False, "text")


def test_tiers_are_relative_size_and_only_new_single_awards_research():
    rules = RatioRules()
    assert size_tier(_facts(), Decimal("0.02"), rules) == TIER_RESEARCH
    assert size_tier(_facts(), Decimal("0.005"), rules) == TIER_MEASURE
    assert size_tier(_facts(), Decimal("0.001"), rules) == TIER_BELOW_FLOOR
    # A modification or a multiple-award pool never researches, however large.
    assert size_tier(_facts(kind=KIND_MODIFICATION), Decimal("0.5"), rules) == TIER_MEASURE
    assert size_tier(_facts(multiple=True), Decimal("0.5"), rules) == TIER_MEASURE
    # Unknown market cap: a row to grade, never a pass to buy.
    assert size_tier(_facts(), None, rules) == TIER_MEASURE
    # An award larger than the awardee's whole market cap is a pool ceiling
    # reported per awardee (backtest ceiling audit): measured, never researched.
    assert size_tier(_facts(), Decimal("1.5"), rules) == TIER_MEASURE
    assert size_tier(_facts(), Decimal("0.99"), rules) == TIER_RESEARCH


def test_build_item_stamps_the_determinants_and_the_funnel_parses_them_back():
    from audit.records import SignalSnapshot, snapshot_gov_award

    facts = _facts()
    size = AwardeeSize(Decimal("8000000000"), date(2026, 10, 6), Decimal("1100000000"), "FY ending 2025-12-31", "test")
    item = build_item(
        feed=FEED_DOD, facts=facts, event_date=date(2026, 10, 5), published_at=datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc),
        resolution=Resolution("KTOS", "curated"), size=size, rules=RatioRules(), department="DEPT OF DEFENSE",
        external_id="dod:1:0", digest_title="Contracts for Oct. 5, 2026", article_id="1",
    )
    f = item.fields
    assert f["ticker"] == "KTOS" and f["size_tier"] == TIER_RESEARCH and f["measurement_only"] == "false"
    assert f["rel_mcap"] == "0.050000" and f["military"] == "true" and f["report_date"] == "2026-10-05"
    assert "award / market cap: 5.00%" in item.content and "ticker: KTOS" in item.content

    class _Snap:
        content = item.content
        tickers = ("KTOS",)

    facts_back = snapshot_gov_award(_Snap())
    assert facts_back is not None
    assert facts_back.tier == TIER_RESEARCH and facts_back.kind == KIND_NEW and facts_back.military
    assert facts_back.rel_mcap == Decimal("5.00") and facts_back.rel_band == ">=5%" and facts_back.mcap_band == "mid"
    assert facts_back.market_cap == Decimal("8000000000") and facts_back.event_date == date(2026, 10, 5)
    # The measure tier carries its own measurement code for the loop.
    small = build_item(
        feed=FEED_DOD, facts=_facts(amount="20000000"), event_date=date(2026, 10, 5), published_at=NOW,
        resolution=Resolution("LMT", "curated"), size=AwardeeSize(Decimal("116000000000"), date(2026, 10, 6), None, None, "t"),
        rules=RatioRules(), department="DEPT OF DEFENSE", external_id="dod:1:1",
    )
    assert small.fields["size_tier"] == TIER_BELOW_FLOOR and small.fields["measurement_only"] == "true"
    assert small.fields["measurement_code"] == "award_below_floor"


# ---------------------------------------------------------------- fetchers

class _Routes:
    def __init__(self, digest_text=DIGEST_MARKDOWN, proxy_status=200):
        self.requests = []
        self._digest = digest_text
        self._status = proxy_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "RSS.ashx" in url:
            return httpx.Response(200, text=RSS)
        if url.startswith("https://r.jina.ai/"):
            return httpx.Response(self._status, text=self._digest)
        if "fpds.gov" in url:
            start = request.url.params.get("start", "0")
            return httpx.Response(200, text=FPDS_ATOM if start == "0" else "<feed></feed>")
        return httpx.Response(404, text=f"unrouted {url}")


class _Sizer:
    def __init__(self, caps):
        self._caps = caps

    def size(self, ticker, cik):
        cap = self._caps.get(ticker)
        return AwardeeSize(cap, date(2026, 10, 6), None, None, "test")


def test_the_dod_fetcher_reads_rss_then_the_proxy_and_stamps_tiers(source, contractors, tmp_path):
    routes = _Routes()
    client = httpx.Client(transport=httpx.MockTransport(routes.handler))
    fetcher = DodDigestFetcher(
        client, contractors=contractors, sizer=_Sizer({"KTOS": Decimal("8000000000"), "AMTM": Decimal("5000000000")}),
        clock=lambda: NOW, sleeper=lambda s: None, pending_path=tmp_path / "pending.jsonl",
    )
    items = fetcher(source)
    by_ticker = {i.fields["ticker"]: i for i in items}
    assert by_ticker["KTOS"].fields["size_tier"] == TIER_RESEARCH
    assert by_ticker["AMTM"].fields["size_tier"] == TIER_MEASURE  # an option exercise, 0.35% of cap
    assert all(i.external_id.startswith("dod:4619270:") for i in items)
    assert items[0].published_at == datetime(2026, 10, 5, 21, 0, 16, tzinfo=timezone.utc)
    # The Oct 2 digest is 3.7 days old at a Tuesday-morning poll: outside the
    # three-day lookback, so it is not re-read (a Monday poll would take it).
    assert not any(i.external_id.startswith("dod:4618190:") for i in items)
    # A second poll inside the throttle returns nothing new.
    assert fetcher(source) == []
    # The evening stash is drained at the next poll and never re-fetched.
    stash_item = items[0]
    fetcher2 = DodDigestFetcher(
        httpx.Client(transport=httpx.MockTransport(_Routes(proxy_status=403).handler)),
        contractors=contractors, sizer=None, clock=lambda: NOW, sleeper=lambda s: None,
        pending_path=tmp_path / "pending2.jsonl",
    )
    assert fetcher2.stash_pending([stash_item]) == 1
    drained = fetcher2(source)
    assert [i.external_id for i in drained] == [stash_item.external_id]
    assert not (tmp_path / "pending2.jsonl").exists()


def test_the_dod_fetcher_survives_a_proxy_refusal(source, contractors):
    routes = _Routes(proxy_status=403)
    fetcher = DodDigestFetcher(httpx.Client(transport=httpx.MockTransport(routes.handler)), contractors=contractors, clock=lambda: NOW, sleeper=lambda s: None)
    assert fetcher(source) == []


def test_the_fpds_fetcher_excludes_dod_and_stamps_structured_facts(source, contractors):
    routes = _Routes()
    fetcher = FpdsCivilianFetcher(httpx.Client(transport=httpx.MockTransport(routes.handler)), contractors=contractors, sizer=_Sizer({"LDOS": Decimal("15000000000")}), clock=lambda: NOW)
    items = fetcher(source)
    assert len(items) == 1
    item = items[0]
    assert item.fields["ticker"] == "LDOS" and item.fields["feed"] == "fpds" and item.fields["military"] == "false"
    assert item.fields["award_kind"] == KIND_NEW and item.fields["offers"] == "3" and item.fields["ceiling_value"] == "400000000.00"
    assert item.fields["size_tier"] == TIER_RESEARCH  # 153M / 15B = 1.02%
    assert "competition: FULL AND OPEN COMPETITION" in item.content
    assert fetcher.last_tally["dod_excluded"] == 1
    actions = parse_fpds_atom(FPDS_ATOM)
    assert actions[0].external_id == "fpds:36C10B26D0001:0:0"


# ---------------------------------------------------------------- wiring

def test_the_source_is_configured_routed_scored_and_familied(signals_config, source):
    from orchestrator.registry import MEASUREMENT_CODES, family_of
    from orchestrator.scoring import DispatchPriors, facet_gov_award
    from signals import SignalClass

    assert source.probation and source.require_instrument and source.daily_research_cap == 6
    assert source.contract_awards is not None and source.contract_awards.research_min == Decimal("0.01")
    assert family_of("gov_contract_awards", SignalClass.CLASS_1_REALTIME) == "government_awards"
    assert {"award_measurement", "award_below_floor"} <= MEASUREMENT_CODES
    priors = DispatchPriors.load()
    prior, grounded, _ = priors.prior_for("gov_contract_awards", facet_gov_award("research", "new", "mid"))
    assert prior == 1.0 and grounded is False


def test_the_class_1_scanner_and_prefilter_treat_the_tiers_as_ruled(signals_config, source, contractors, tmp_path):
    from orchestrator.prefilter import ResearchPreFilter
    from signals import Class1RealtimeScanner, SignalQueue

    routes = _Routes(digest_text=DIGEST_HTML)  # the full digest: mapped, small-business and non-public awardees
    fetcher = DodDigestFetcher(httpx.Client(transport=httpx.MockTransport(routes.handler)), contractors=contractors, sizer=_Sizer({"KTOS": Decimal("8000000000"), "AMTM": Decimal("5000000000")}), clock=lambda: NOW, sleeper=lambda s: None)
    items = fetcher(source)
    scanner = Class1RealtimeScanner(signals_config.klass("class_1"), lambda s: items if s.id == "gov_contract_awards" else [], SignalQueue(), clock=lambda: NOW)
    signals = scanner.poll(force=True)
    by_ticker = {s.metadata.get("ticker"): s for s in signals if s.metadata.get("ticker")}
    prefilter = ResearchPreFilter.from_config(signals_config)
    research = by_ticker["KTOS"]
    assert research.metadata["tickers"] == "KTOS"
    assert prefilter.skip_verdict(research, now=NOW) is None  # researches
    measure = by_ticker["AMTM"]
    verdict = prefilter.skip_verdict(measure, now=NOW)
    assert verdict is not None and verdict[1] == "measurement"
    assert measure.metadata["measurement_code"] == "award_measurement"
    # Unmapped awardees carry no ticker and die as no_instrument.
    unmapped = [s for s in signals if not s.metadata.get("tickers")]
    assert unmapped and all(prefilter.missing_instrument(s) for s in unmapped)


def test_the_prompt_names_the_award_frame_and_demands_priced_in(signals_config, source, contractors):
    from research.prompts import build_user_prompt
    from signals import Class1RealtimeScanner, SignalQueue

    routes = _Routes()
    fetcher = DodDigestFetcher(httpx.Client(transport=httpx.MockTransport(routes.handler)), contractors=contractors, sizer=_Sizer({"KTOS": Decimal("8000000000")}), clock=lambda: NOW, sleeper=lambda s: None)
    items = [i for i in fetcher(source) if i.fields["ticker"] == "KTOS"]
    scanner = Class1RealtimeScanner(signals_config.klass("class_1"), lambda s: items if s.id == "gov_contract_awards" else [], SignalQueue(), clock=lambda: NOW)
    signal = scanner.poll(force=True)[0]
    prompt = build_user_prompt(signal)
    assert "GOVERNMENT CONTRACT AWARD" in prompt and "priced_in_analysis is MANDATORY" in prompt
    assert "17:00 ET" in prompt and "award / market cap: 5.16%" in prompt
    # The prompt never learns the source is measurement-first, on probation,
    # or how the book is doing: the frame is the award and the awardee only.
    lowered = prompt.lower()
    assert "probation" not in lowered and "measurement-only" not in lowered and "measurement-first" not in lowered
    assert "shortfall" not in lowered and "weekly target" not in lowered


# ---------------------------------------------------------------- forward open split

def _bars(symbol, start, end):
    # Four sessions: Fri 10-02 (t0), Mon 10-05, Tue 10-06, Wed 10-07.
    base = {"SPY": (500, 502, 504, 506), "KTOS": (100, 104, 103, 108)}[symbol]
    days = ("2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07")
    return [{"t": f"{d}T04:00:00Z", "o": str(c - 1), "c": str(c)} for d, c in zip(days, base)]


def test_the_forward_engine_splits_at_the_next_open(tmp_path):
    from forward.returns import ForwardReturns, ForwardRow

    engine = ForwardReturns(_bars, tmp_path / "fwd.jsonl", clock=lambda: datetime(2026, 10, 10, tzinfo=timezone.utc), pace_seconds=0)
    rows = engine.rows_for({("KTOS", date(2026, 10, 2))})
    row = rows[("KTOS", date(2026, 10, 2))]
    assert row.base_close == Decimal("100") and row.base_date == date(2026, 10, 2)
    assert row.open_date == date(2026, 10, 5) and row.base_open == Decimal("103")
    mark = row.marks[3]  # due 10-05
    assert mark.return_pct == Decimal("4.00")  # close-to-close includes the gap
    assert mark.open_return_pct == Decimal("0.97")  # 104/103 from the next open
    spy_cc = (Decimal("502") / Decimal("500") - 1) * 100
    assert mark.excess_pct == (Decimal("4.00") - spy_cc).quantize(Decimal("0.01"))
    spy_oc = (Decimal("502") / Decimal("501") - 1) * 100
    assert mark.open_excess_pct == (Decimal("0.97") - spy_oc).quantize(Decimal("0.01"))
    # Round trip through the cache keeps the open fields.
    reloaded = ForwardRow.from_json(row.to_json())
    assert reloaded.base_open == row.base_open and reloaded.marks[3].open_excess_pct == mark.open_excess_pct
    # A legacy row without the open base is not complete and recomputes once.
    legacy = ForwardRow(symbol="KTOS", observed=date(2026, 10, 2), base_date=date(2026, 10, 2), base_close=Decimal("100"), marks=row.marks, computed_at=row.computed_at)
    assert row.complete is False  # 20/60/120d not due yet
    assert legacy.has_open_base is False


def test_the_weekly_renders_the_award_subtype_table(signals_config, source, contractors, tmp_path):
    from forward.funnel import FunnelEntry
    from forward.report import gov_award_section
    from forward.returns import ForwardReturns
    from audit.records import snapshot_gov_award
    from signals import Class1RealtimeScanner, SignalClass, SignalQueue

    routes = _Routes()
    fetcher = DodDigestFetcher(httpx.Client(transport=httpx.MockTransport(routes.handler)), contractors=contractors, sizer=_Sizer({"KTOS": Decimal("8000000000")}), clock=lambda: NOW, sleeper=lambda s: None)
    items = [i for i in fetcher(source) if i.fields["ticker"] == "KTOS"]

    class _Snap:
        content = items[0].content
        tickers = ("KTOS",)

    facts = snapshot_gov_award(_Snap())
    entry = FunnelEntry(decision_id="d1", source_id="gov_contract_awards", credibility_key="gov_contract_awards", signal_class=SignalClass.CLASS_1_REALTIME,
                        observed_at=datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc), tickers=("KTOS",), bucket="declined", code="probation", confidence=66, lag_days=None, gov_award=facts)
    engine = ForwardReturns(_bars, tmp_path / "fwd.jsonl", clock=lambda: datetime(2026, 10, 10, tzinfo=timezone.utc), pace_seconds=0)
    rows = engine.rows_for({("KTOS", date(2026, 10, 2))})
    text = "\n".join(gov_award_section([entry], rows))
    assert "Government contract awards by subtype" in text
    assert "tier research: 1 rows" in text and "PRE-REGISTERED RULE" in text
    assert "next-open->close" in text and "close->close" in text
