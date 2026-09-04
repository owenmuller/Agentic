"""Form 4 insider-cluster source tests (human ruling 2026-09-02).

The claims: the parser reads only the structured XML (code P, the aff10b5One
checkbox, roles); the recipe's floors and the 15-day cluster window behave as
ruled; routine same-month-3-years insiders are excluded while unknown history
defaults opportunistic; singles failing only the cluster test are emitted
marked ``cluster: false`` and prefilter to code ``no_cluster``; state survives
a restart; the per-poll budget carries a backlog instead of dropping it; and
the prompt anchors priced-in to the transaction date, never the congressional
framing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx
import pytest

from forward.funnel import FunnelEntry
from forward.report import render_forward_report
from orchestrator import ResearchPreFilter
from orchestrator.registry import family_of
from research.prompts import build_user_prompt
from signals import Form4InsiderFetcher, SignalClass, SignalQueue, SignalsConfig
from signals.form4 import is_routine_month, parse_ownership_document
from signals.scanners import Class2CongressionalScanner, RawItem

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)

ISSUER_CIK = "0009999001"
SYMBOL = "CLST"


def form4_xml(
    owner_cik: str,
    owner_name: str,
    *,
    code: str = "P",
    shares: float = 3_000,
    price: float = 30.0,
    txn_date: str = "2026-08-25",
    plan: bool = False,
    officer_title: str = "Chief Financial Officer",
    extra_owner: Optional[tuple[str, str]] = None,
    issuer_symbol: str = SYMBOL,
) -> str:
    owners = [
        f"""
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>{owner_cik}</rptOwnerCik>
            <rptOwnerName>{owner_name}</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>0</isDirector>
            <isOfficer>1</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <officerTitle>{officer_title}</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>"""
    ]
    if extra_owner is not None:
        owners.append(
            f"""
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>{extra_owner[0]}</rptOwnerCik>
            <rptOwnerName>{extra_owner[1]}</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>1</isDirector>
            <isOfficer>0</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
        </reportingOwnerRelationship>
    </reportingOwner>"""
        )
    acquired = "A" if code == "P" else "D"
    return f"""<?xml version="1.0"?>
<ownershipDocument>
    <documentType>4</documentType>
    <periodOfReport>{txn_date}</periodOfReport>
    <issuer>
        <issuerCik>{ISSUER_CIK}</issuerCik>
        <issuerName>Cluster Corp</issuerName>
        <issuerTradingSymbol>{issuer_symbol}</issuerTradingSymbol>
    </issuer>{''.join(owners)}
    <aff10b5One>{1 if plan else 0}</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionDate><value>{txn_date}</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>{code}</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>{acquired}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""


def submission_txt(xml: str) -> str:
    return f"<SEC-DOCUMENT>header noise\n<XML>\n{xml}\n</XML>\n</SEC-DOCUMENT>"


#: (accession, owner cik, file_date, xml)
FILING_A = (
    "0001111111-26-000001",
    "0001111111",
    "2026-08-27",
    form4_xml("0001111111", "Avery Cfo", shares=3_000, price=30.0),  # $90K
)
FILING_B = (
    "0002222222-26-000001",
    "0002222222",
    "2026-09-01",
    form4_xml(
        "0002222222",
        "Blair Director",
        shares=2_500,
        price=30.0,  # $75K; with A the window holds $165K across 2 insiders
        txn_date="2026-08-30",
        officer_title="Chief Executive Officer",
    ),
)
FILING_SALE = (
    "0006666666-26-000001",
    "0006666666",
    "2026-09-01",
    form4_xml("0006666666", "Sully Seller", code="S", shares=9_000, price=40.0),
)
FILING_PLAN = (
    "0007777777-26-000001",
    "0007777777",
    "2026-09-01",
    form4_xml("0007777777", "Plan Buyer", shares=9_000, price=40.0, plan=True),
)
FILING_SMALL = (
    "0008888888-26-000001",
    "0008888888",
    "2026-09-01",
    form4_xml("0008888888", "Small Fry", shares=100, price=30.0),  # $3K
)
FILING_ROUTINE = (
    "0003333333-26-000001",
    "0003333333",
    "2026-09-01",
    form4_xml("0003333333", "Rhys Routine", shares=4_000, price=30.0,
              txn_date="2026-09-01"),
)
FILING_MULTI = (
    "0004444444-26-000001",
    "0004444444",
    "2026-09-01",
    form4_xml(
        "0004444444",
        "Trust One",
        shares=7_000,
        price=30.0,  # $210K, but two co-filers are ONE identity
        extra_owner=("0005555555", "Trust Two"),
    ),
)

ROUTINE_SUBMISSIONS = {
    "filings": {
        "recent": {
            "form": ["4", "4", "4", "8-K"],
            "filingDate": ["2023-09-05", "2024-09-06", "2025-09-04", "2025-01-01"],
        }
    }
}


def fts_hit(accession: str, cik: str, file_date: str, form: str = "4") -> dict:
    return {
        "_source": {
            "adsh": accession,
            "ciks": [cik],
            "file_date": file_date,
            "form": form,
        }
    }


class Form4Recorder:
    def __init__(self, filings, extra_hits=()):
        self.requests: list[httpx.Request] = []
        self._filings = {f[0]: f for f in filings}
        self._hits = [fts_hit(f[0], f[1], f[2]) for f in filings] + list(extra_hits)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "efts.sec.gov" in url:
            offset = int(request.url.params.get("from", "0"))
            return httpx.Response(
                200, json={"hits": {"hits": [] if offset else self._hits}}
            )
        if "data.sec.gov/submissions" in url:
            if "0003333333" in url:
                return httpx.Response(200, json=ROUTINE_SUBMISSIONS)
            return httpx.Response(404, text="no such owner")
        for accession, _, _, xml in self._filings.values():
            if url.endswith(f"/{accession}.txt"):
                return httpx.Response(200, text=submission_txt(xml))
        return httpx.Response(404, text=f"unrouted: {url}")


def fetcher_with(filings, extra_hits=(), **kwargs):
    recorder = Form4Recorder(filings, extra_hits)
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("sleeper", lambda seconds: None)
    return Form4InsiderFetcher(client, **kwargs), recorder


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def source(signals_config):
    return signals_config.source("class_2", "form4_insiders")


# ================================================================================
# The parser reads structure, not prose
# ================================================================================


def test_parser_reads_a_code_p_purchase():
    parsed = parse_ownership_document(FILING_A[3])
    assert parsed.symbol == SYMBOL
    assert parsed.owner_name == "Avery Cfo"
    assert "officer (Chief Financial Officer)" in parsed.roles
    assert parsed.amount == pytest.approx(90_000)
    assert parsed.purchase_date == "2026-08-25"
    assert parsed.plan is False


def test_parser_yields_no_purchase_for_a_sale():
    parsed = parse_ownership_document(FILING_SALE[3])
    assert parsed.purchase_date is None and parsed.shares == 0


def test_parser_reads_the_10b5_1_checkbox():
    assert parse_ownership_document(FILING_PLAN[3]).plan is True


def test_a_multi_owner_filing_is_one_identity():
    parsed = parse_ownership_document(FILING_MULTI[3])
    assert parsed.owner_cik == "0004444444"
    assert parsed.owner_name == "Trust One; Trust Two"


def test_routine_needs_three_consecutive_prior_years():
    months = {"2023-09", "2024-09", "2025-09"}
    assert is_routine_month(months, "2026-09") is True
    assert is_routine_month(months, "2026-08") is False
    assert is_routine_month({"2024-09", "2025-09"}, "2026-09") is False
    assert is_routine_month(set(), "2026-09") is False


# ================================================================================
# The recipe: floors, window, exclusions
# ================================================================================


def test_a_single_purchase_emits_marked_no_cluster(source):
    fetcher, _ = fetcher_with([FILING_A])
    items = fetcher(source)
    assert len(items) == 1
    fields = items[0].fields
    assert fields["form"] == "4"
    assert fields["ticker"] == SYMBOL
    assert fields["cluster"] == "false"
    assert "requires 2" in fields["cluster_detail"]
    assert fields["amount_range"] == "$90,000"
    assert fields["transaction_date"] == "2026-08-25"


def test_a_second_insider_completes_the_cluster(source):
    fetcher, _ = fetcher_with([FILING_A, FILING_B])
    items = fetcher(source)
    by_cluster = {item.fields["cluster"]: item for item in items}
    assert set(by_cluster) == {"true", "false"}
    cluster = by_cluster["true"]
    assert cluster.fields["cluster_insiders"] == "2"
    assert cluster.fields["amount_range"] == "$165,000"
    assert "Avery Cfo" in cluster.fields["filer"]
    assert "Blair Director" in cluster.fields["filer"]
    assert "CLUSTER: 2 distinct insiders" in cluster.content
    # The earliest transaction anchors the priced-in question.
    assert cluster.fields["transaction_date"] == "2026-08-25"


def test_two_insiders_below_the_aggregate_floor_stay_singles(source):
    small_a = (
        "0001111111-26-000009",
        "0001111111",
        "2026-08-28",
        form4_xml("0001111111", "Avery Cfo", shares=2_000, price=30.0),  # $60K
    )
    small_b = (
        "0002222222-26-000009",
        "0002222222",
        "2026-09-01",
        form4_xml("0002222222", "Blair Director", shares=2_000, price=30.0,
                  txn_date="2026-08-30"),  # $60K; aggregate $120K < $150K
    )
    fetcher, _ = fetcher_with([small_a, small_b])
    items = fetcher(source)
    assert [item.fields["cluster"] for item in items] == ["false", "false"]
    details = " ".join(item.fields.get("cluster_detail", "") for item in items)
    assert "below the $150,000 cluster floor" in details


def test_sales_plans_small_buys_and_routine_insiders_never_emit(source):
    fetcher, recorder = fetcher_with(
        [FILING_SALE, FILING_PLAN, FILING_SMALL, FILING_ROUTINE]
    )
    assert fetcher(source) == []
    # The routine check consulted the owner's real filing history.
    assert any(
        "data.sec.gov/submissions/CIK0003333333" in str(r.url)
        for r in recorder.requests
    )


def test_amendments_are_never_fetched(source):
    amendment = fts_hit("0009090909-26-000001", "0009090909", "2026-09-01", "4/A")
    fetcher, recorder = fetcher_with([FILING_A], extra_hits=[amendment])
    fetcher(source)
    assert not any("0009090909" in str(r.url) for r in recorder.requests[1:])


def test_a_multi_owner_filing_cannot_manufacture_a_cluster(source):
    fetcher, _ = fetcher_with([FILING_MULTI])
    items = fetcher(source)
    assert len(items) == 1
    assert items[0].fields["cluster"] == "false"  # $210K but ONE identity


# ================================================================================
# Budget, backlog, state
# ================================================================================


def test_the_poll_budget_carries_a_backlog_newest_first(source):
    fetcher, recorder = fetcher_with(
        [FILING_A, FILING_B], fetch_budget_per_poll=1
    )
    first = fetcher(source)
    assert len(first) == 1
    assert first[0].external_id == FILING_B[0]  # newest first under pressure
    second = fetcher(source)
    assert [item.external_id for item in second] == [FILING_A[0]]
    # And the second processing saw the restored window: A+B is a cluster.
    assert second[0].fields["cluster"] == "true"


def test_state_survives_a_restart(source, tmp_path):
    # Lookback 7 so FILING_A (filed 2026-08-27) stays inside the seen-retention
    # horizon — in production the seen set only needs to cover what the FTS
    # window can re-list, and the prune matches that.
    state = tmp_path / "form4_state.json"
    fetcher, _ = fetcher_with(
        [FILING_A], state_path=state, first_poll_lookback_days=7
    )
    assert len(fetcher(source)) == 1

    reborn, recorder = fetcher_with(
        [FILING_A, FILING_B], state_path=state, first_poll_lookback_days=7
    )
    items = reborn(source)
    # A is seen: not refetched, not re-emitted; B completes the cluster from
    # the restored window.
    assert [item.external_id for item in items] == [FILING_B[0]]
    assert items[0].fields["cluster"] == "true"
    assert not any(
        str(r.url).endswith(f"/{FILING_A[0]}.txt") for r in recorder.requests
    )


# ================================================================================
# Scanner, prefilter, family, prompt, report
# ================================================================================


def scanner_signal(signals_config, item: RawItem):
    queue = SignalQueue()
    source = signals_config.source("class_2", "form4_insiders")

    def fetch(config):
        return [item] if config.id == "form4_insiders" else []

    scanner = Class2CongressionalScanner(
        signals_config.klass("class_2"), fetch, queue, clock=lambda: NOW
    )
    emitted = [
        signal for signal in scanner.poll(force=True)
        if signal.source_id == "form4_insiders"
    ]
    assert len(emitted) == 1
    return emitted[0]


def form4_item(cluster: bool) -> RawItem:
    fields = {
        "form": "4",
        "accession": "0001111111-26-000001",
        "ticker": SYMBOL,
        "issuer": "Cluster Corp",
        "transaction": "Purchase",
        "report_date": "2026-09-01",
        "transaction_date": "2026-08-25",
        "amount_range": "$165,000",
        "cluster": "true" if cluster else "false",
        "cluster_insiders": "2" if cluster else "1",
        "filer": "Avery Cfo; Blair Director",
    }
    if not cluster:
        fields["cluster_detail"] = "1 insider(s) in the 15-day window; the cluster rule requires 2"
    return RawItem(
        external_id=fields["accession"],
        content="Form 4 insider filing (SEC EDGAR, structured XML)",
        published_at=NOW,
        fields=fields,
    )


def test_the_scanner_anchors_form4_to_the_transaction_date(signals_config):
    signal = scanner_signal(signals_config, form4_item(cluster=True))
    assert signal.signal_class is SignalClass.CLASS_2_MOMENTUM
    assert signal.metadata["tickers"] == SYMBOL
    assert "transaction date" in signal.metadata["disclosure_lag_note"]
    assert "45" not in signal.metadata["disclosure_lag_note"]
    assert signal.dispatch_weight > 4  # log10(165000) fresh


def test_singles_prefilter_to_no_cluster_and_clusters_pass(signals_config):
    prefilter = ResearchPreFilter.from_config(signals_config)
    single = scanner_signal(signals_config, form4_item(cluster=False))
    verdict = prefilter.skip_verdict(single, now=NOW)
    assert verdict is not None
    reason, rule = verdict
    assert rule == "cluster"
    assert "control group" in reason

    clustered = scanner_signal(signals_config, form4_item(cluster=True))
    assert prefilter.skip_verdict(clustered, now=NOW) is None


def test_form4_family_is_insider_filings():
    assert family_of("form4_insiders", SignalClass.CLASS_2_MOMENTUM) == (
        "insider_filings"
    )
    # And the class-2 default is untouched.
    assert family_of("congressional_disclosures", SignalClass.CLASS_2_MOMENTUM) == (
        "congressional_filings"
    )


def test_the_prompt_speaks_form4_not_congressional(signals_config):
    signal = scanner_signal(signals_config, form4_item(cluster=True))
    prompt = build_user_prompt(signal)
    assert "This is a Form 4 insider filing" in prompt
    assert "TWO BUSINESS DAYS" in prompt
    assert "This is a congressional disclosure" not in prompt


def test_the_forward_report_slices_the_cluster_rule():
    def entry(decision_id: str, code: str) -> FunnelEntry:
        return FunnelEntry(
            decision_id=decision_id,
            source_id="form4_insiders",
            credibility_key="form4_insiders",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=NOW,
            tickers=(SYMBOL,),
            bucket="prefiltered" if code else "declined",
            code=code,
            confidence=None,
            lag_days=None,
        )

    report = render_forward_report(
        [entry("d1", ""), entry("d2", "no_cluster")], rows={}
    )
    assert "Form 4 cluster rule" in report
    assert "clustered (researched)" in report
    assert "singles (prefiltered control)" in report
