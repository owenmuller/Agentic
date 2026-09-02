"""Quiver Class 2 fetcher tests, plus the source router.

Fixtures mirror the live API's row shape. The claims that matter: both dates and the
lag between them reach the research prompt verbatim (the gap is what priced-in
analysis evaluates); a disclosure that reappears across pulls or across restarts
never re-emits; and the router dispatches every configured source somewhere
deliberate. Live smoke gated behind ``QUIVER_LIVE_TESTS=1``, like EDGAR's.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import httpx
import pytest

from research.prompts import build_user_prompt
from signals import (
    FeedNotConfigured,
    QuiverCongressFetcher,
    QuiverError,
    SignalClass,
    SignalQueue,
    SignalsConfig,
    SourceRouter,
)
from signals.scanners import Class2CongressionalScanner, RawItem

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)

PELOSI_ROW = {
    "Representative": "Nancy Pelosi",
    "BioGuideID": "P000197",
    "ReportDate": "2026-08-15",
    "TransactionDate": "2026-07-01",
    "Ticker": "NVDA",
    "Transaction": "Purchase",
    "Range": "$1,000,001 - $5,000,000",
    "House": "Representative",
    "Party": "D",
}

OTHER_ROW = {
    "Representative": "Some Other Member",
    "ReportDate": "2026-08-14",
    "TransactionDate": "2026-08-01",
    "Ticker": "XOM",
    "Transaction": "Sale",
    "Range": "$15,001 - $50,000",
    "House": "Representative",
}

FEED = [PELOSI_ROW, OTHER_ROW]


class QuiverRecorder:
    def __init__(self, responses: Optional[list[httpx.Response]] = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses or [httpx.Response(200, json=FEED)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return self._responses[index]


def fetcher_with(
    responses: Optional[list[httpx.Response]] = None, **kwargs
) -> tuple[QuiverCongressFetcher, QuiverRecorder]:
    recorder = QuiverRecorder(responses)
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("sleeper", lambda seconds: None)
    return QuiverCongressFetcher(client, **kwargs), recorder


@pytest.fixture(scope="session")
def source():
    return SignalsConfig.load().source("class_2", "congressional_disclosures")


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


# ================================================================================
# One disclosure, parsed — both dates are the point
# ================================================================================


def test_a_disclosure_becomes_one_raw_item(source):
    # Full roster (2026-08-25): the shipped config admits every filer.
    fetcher, recorder = fetcher_with()
    items = fetcher(source)

    assert len(items) == len(FEED)
    item = items[0]
    assert item.fields["representative"] == "Nancy Pelosi"
    assert item.fields["ticker"] == "NVDA"
    assert item.fields["transaction"] == "Purchase"
    assert item.fields["amount_range"] == "$1,000,001 - $5,000,000"
    assert item.fields["transaction_date"] == "2026-07-01"
    assert item.fields["report_date"] == "2026-08-15"
    assert item.fields["disclosure_lag_days"] == "45"
    # Published when it became PUBLIC — the report date, not the trade date.
    assert item.published_at == datetime(2026, 8, 15, tzinfo=timezone.utc)
    # One request served the whole watchlist.
    assert len(recorder.requests) == 1
    assert recorder.requests[0].headers["Authorization"] == "Bearer test-key"


def test_the_content_states_both_dates_and_the_gap(source):
    fetcher, _ = fetcher_with()
    content = fetcher(source)[0].content

    assert "transaction date: 2026-07-01 (when the trade was executed)" in content
    assert "report date: 2026-08-15 (when it became public)" in content
    assert "disclosure lag: 45 days" in content
    assert "Nancy Pelosi (Representative)" in content
    assert "$1,000,001 - $5,000,000" in content


def test_a_member_not_on_a_configured_watchlist_is_not_a_signal():
    # The narrowing mechanism survives the full-roster default (2026-08-25).
    from signals.config import SourceConfig

    narrowed = SourceConfig(
        id="congressional_disclosures",
        watchlist=({"name": "Nancy Pelosi", "chamber": "house"},),
    )
    fetcher, _ = fetcher_with()
    items = fetcher(narrowed)
    assert all("Other" not in item.fields["representative"] for item in items)


def test_name_matching_survives_the_apis_name_order():
    """"Pelosi, Nancy" and "Nancy Pelosi" are the same person, not two formats."""
    from signals.quiver import _matches_name

    assert _matches_name("Pelosi, Nancy", "Nancy Pelosi")
    assert _matches_name("Nancy Pelosi", "Nancy Pelosi")
    assert _matches_name("Hon. Nancy Pelosi", "Nancy Pelosi")
    assert not _matches_name("Nancy Mace", "Nancy Pelosi")
    assert not _matches_name("", "Nancy Pelosi")


def test_a_row_missing_core_fields_is_skipped_not_fatal(source):
    broken = {**PELOSI_ROW, "Ticker": ""}
    fetcher, _ = fetcher_with([httpx.Response(200, json=[broken, PELOSI_ROW])])
    items = fetcher(source)
    assert len(items) == 1  # the good row still came through


# ================================================================================
# Both dates reach the research prompt
# ================================================================================


def test_both_dates_and_the_lag_reach_the_research_prompt(source, signals_config):
    """The whole reason the dates are in the content: the model must see the
    staleness inside the fenced data block, not infer it."""
    fetcher, _ = fetcher_with()
    queue = SignalQueue()
    scanner = Class2CongressionalScanner(
        signals_config.klass("class_2"), fetcher, queue, clock=lambda: NOW
    )
    signal = scanner.poll(force=True)[0]
    prompt = build_user_prompt(signal)

    fence_start = prompt.index("BEGIN UNTRUSTED THIRD-PARTY CONTENT")
    assert "transaction date: 2026-07-01" in prompt[fence_start:]
    assert "report date: 2026-08-15" in prompt[fence_start:]
    assert "disclosure lag: 45 days" in prompt[fence_start:]
    # And the class guidance outside the fence demands the priced-in reasoning.
    assert "priced_in_analysis is MANDATORY" in prompt


def test_the_class_2_scanner_stamps_its_standing_metadata(source, signals_config):
    fetcher, _ = fetcher_with()
    queue = SignalQueue()
    scanner = Class2CongressionalScanner(
        signals_config.klass("class_2"), fetcher, queue, clock=lambda: NOW
    )
    signal = scanner.poll(force=True)[0]

    assert signal.signal_class is SignalClass.CLASS_2_MOMENTUM
    assert signal.metadata["priced_in_analysis_required"] == "true"
    assert signal.metadata["copy_trade"] == "false"
    assert "trade date" in signal.metadata["disclosure_lag_note"]
    assert signal.metadata["ticker"] == "NVDA"


# ================================================================================
# Dedup: across pulls, and across restarts
# ================================================================================


def test_a_reappearing_disclosure_does_not_reemit(source):
    fetcher, recorder = fetcher_with()
    assert len(fetcher(source)) == len(FEED)
    assert fetcher(source) == []  # same rows in the next pull
    assert len(recorder.requests) == 2  # it re-polled; it just re-emitted nothing


def test_dedup_survives_a_restart_via_the_audit_log(source):
    """A new process seeds its seen-set from what the log says was researched —
    the same replay philosophy as the budget and the kill switch."""
    first, _ = fetcher_with()
    emitted = first(source)
    identities = [item.external_id for item in emitted]

    # ...the signals went through the pipeline and left audit records carrying
    # their external_ids; a restarted process reads those and seeds the fetcher:
    restarted, _ = fetcher_with(seen=identities)
    assert restarted(source) == []


def test_an_unresearched_signal_reemits_after_restart(source):
    """The right edge of the seeding rule: queued-but-never-researched left no audit
    record, so it comes back and finally gets its pass — deferred, not dropped."""
    restarted, _ = fetcher_with(seen=[])  # nothing in the log for it
    assert len(restarted(source)) == len(FEED)


def test_the_identity_distinguishes_real_differences():
    from signals.quiver import _identity
    from datetime import date

    base = ("Nancy Pelosi", "NVDA", "Purchase", date(2026, 7, 1), date(2026, 8, 15), "$1M")
    assert _identity(*base) == _identity(*base)
    different_day = ("Nancy Pelosi", "NVDA", "Purchase", date(2026, 7, 2), date(2026, 8, 15), "$1M")
    assert _identity(*base) != _identity(*different_day)
    different_side = ("Nancy Pelosi", "NVDA", "Sale", date(2026, 7, 1), date(2026, 8, 15), "$1M")
    assert _identity(*base) != _identity(*different_side)


# ================================================================================
# Citizenship: auth, throttle, retry, failure
# ================================================================================


def test_a_missing_api_key_refuses_before_any_request(source, monkeypatch):
    monkeypatch.delenv("QUIVER_API_KEY", raising=False)
    recorder = QuiverRecorder()
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    fetcher = QuiverCongressFetcher(client, clock=lambda: NOW, sleeper=lambda s: None)

    with pytest.raises(QuiverError, match="QUIVER_API_KEY"):
        fetcher(source)
    assert recorder.requests == []


def test_a_refused_key_is_a_loud_distinct_error(source):
    fetcher, _ = fetcher_with([httpx.Response(401, json={"detail": "bad token"})])
    with pytest.raises(QuiverError, match="refused the API key"):
        fetcher(source)


def test_a_transient_blip_is_retried_once(source):
    fetcher, recorder = fetcher_with(
        [httpx.Response(503, text="down"), httpx.Response(200, json=FEED)]
    )
    assert len(fetcher(source)) == len(FEED)
    assert len(recorder.requests) == 2


def test_a_persistent_outage_fails_the_poll(source):
    fetcher, _ = fetcher_with([httpx.Response(503, text="down")])
    with pytest.raises(QuiverError, match="503"):
        fetcher(source)


def test_requests_are_throttled(source):
    naps: list[float] = []
    fetcher, _ = fetcher_with(
        [httpx.Response(503, text="x"), httpx.Response(200, json=FEED)],
        sleeper=naps.append,
        monotonic=lambda: 500.0,
    )
    fetcher(source)
    # The retry pause plus the min-interval gap before the second request.
    assert any(nap == pytest.approx(2.0) for nap in naps)
    assert any(nap == pytest.approx(0.5) for nap in naps)


def test_a_non_list_body_is_an_error_not_signals(source):
    fetcher, _ = fetcher_with([httpx.Response(200, json={"detail": "quota"})])
    with pytest.raises(QuiverError, match="not a list"):
        fetcher(source)


# ================================================================================
# The source router
# ================================================================================


def router_sources(signals_config):
    return {
        "class_1": signals_config.klass("class_1").sources,
        "class_2": signals_config.klass("class_2").sources,
        "class_3": signals_config.klass("class_3").sources,
    }


def test_the_router_dispatches_each_class_to_its_fetcher(signals_config):
    calls: list[str] = []

    def fake(name):
        def fetch(source):
            calls.append(f"{name}:{source.id}")
            return []

        return fetch

    router = SourceRouter(
        routes={
            "form_13f": fake("edgar"),
            "congressional_disclosures": fake("quiver"),
            "form4_insiders": fake("form4"),
            "form_13d": fake("form13d"),
            "nolimitgains": fake("x"),
            "unusual_whales": fake("x"),
            "optionshawk": fake("x"),
            "citrini": fake("x"),
            "trump_mirror_ttox": fake("x"),
            "trump_mirror_tdp": fake("x"),
        },
        unbuilt={"trump_posts"},
    )
    for sources in router_sources(signals_config).values():
        for source in sources:
            router(source)

    assert calls == [
        "x:trump_mirror_ttox",
        "x:trump_mirror_tdp",
        "x:unusual_whales",
        "x:optionshawk",
        "x:nolimitgains",
        "quiver:congressional_disclosures",
        "form4:form4_insiders",
        "form13d:form_13d",
        "x:citrini",
        "edgar:form_13f",
    ]


def test_unbuilt_sources_poll_nothing_and_warn_once(signals_config, caplog):
    import logging

    router = SourceRouter(routes={}, unbuilt={"trump_posts", "nolimitgains"})
    trump = signals_config.source("class_1", "trump_posts")

    with caplog.at_level(logging.WARNING, logger="signals.routing"):
        assert router(trump) == []
        assert router(trump) == []
        assert len(caplog.records) == 1
        assert "no fetcher built" in caplog.records[0].message


def test_an_undeclared_source_is_config_drift_and_raises(signals_config):
    router = SourceRouter(routes={}, unbuilt=set())
    with pytest.raises(FeedNotConfigured, match="trump_posts"):
        router(signals_config.source("class_1", "trump_posts"))


def test_a_source_cannot_be_both_routed_and_unbuilt():
    with pytest.raises(ValueError, match="both"):
        SourceRouter(routes={"x": lambda s: []}, unbuilt={"x"})


def test_every_configured_source_has_a_wiring_decision(signals_config):
    """The production router covers signals.yaml exactly — a source added to the
    config without a routing decision fails here before it fails at 9:30."""
    production = SourceRouter(
        routes={
            "form_13f": lambda s: [],
            "congressional_disclosures": lambda s: [],
            "form4_insiders": lambda s: [],
            "form_13d": lambda s: [],
            "nolimitgains": lambda s: [],
            "unusual_whales": lambda s: [],
            "optionshawk": lambda s: [],
            "citrini": lambda s: [],
            "trump_mirror_ttox": lambda s: [],
            "trump_mirror_tdp": lambda s: [],
        },
        unbuilt={"trump_posts"},
    )
    for sources in router_sources(signals_config).values():
        for source in sources:
            production(source)  # FeedNotConfigured would fail the test


# ================================================================================
# Live smoke — opt-in, needs the real key
# ================================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("QUIVER_LIVE_TESTS") != "1",
    reason="set QUIVER_LIVE_TESTS=1 to hit the real Quiver API (uses the paid key)",
)
def test_live_smoke_against_the_real_congress_feed():
    """One real authenticated pull. Success proves auth and the row shape; whether
    the watchlist member traded recently is the market's business, not the test's."""
    from execution.environment import load_environment

    load_environment()
    source = SignalsConfig.load().source("class_2", "congressional_disclosures")
    fetcher = QuiverCongressFetcher()
    try:
        items = fetcher(source)
    finally:
        fetcher.close()

    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, RawItem)
        assert item.fields["transaction_date"]
        assert "transaction date:" in item.content
        assert "report date:" in item.content
    print(f"live smoke: {len(items)} watchlist disclosures in the current feed")


# ================================================================================
# Full roster (2026-08-25): an empty watchlist means every filer
# ================================================================================


def test_an_empty_watchlist_admits_the_full_roster(source, signals_config):
    """The shipped config: every filer becomes a raw item; the pre-filters and
    the per-source daily cap do the triage downstream."""
    assert source.watchlist == ()  # the ruling, as shipped
    fetcher, _ = fetcher_with()
    items = fetcher(source)
    assert len(items) == len(FEED)  # Pelosi AND the other member


def test_a_nonempty_watchlist_still_narrows(signals_config):
    """The mechanism is kept: a configured watchlist filters exactly as before."""
    from signals.config import SourceConfig

    narrowed = SourceConfig(
        id="congressional_disclosures",
        watchlist=({"name": "Nancy Pelosi", "chamber": "house"},),
    )
    fetcher, _ = fetcher_with()
    items = fetcher(narrowed)
    assert len(items) == 1
    assert "Pelosi" in items[0].fields["representative"]


def test_every_item_carries_its_member_credibility_key(source):
    fetcher, _ = fetcher_with()
    for item in fetcher(source):
        key = item.fields["credibility_key"]
        assert key.startswith("congressional_disclosures/")
        assert item.fields["representative"] in key


# ================================================================================
# Instrument type (human ruling 2026-08-27): a purchase of calls is not a
# purchase of stock, and the feed knew the difference all along
# ================================================================================

PELOSI_CALLS_ROW = {
    "Representative": "Nancy Pelosi",
    "BioGuideID": "P000197",
    "ReportDate": "2026-08-21",
    "TransactionDate": "2026-07-24",
    "Ticker": "BE",
    "Transaction": "Purchase",
    "Range": "$1,000,001 - $5,000,000",
    "House": "Representatives",
    "Party": "D",
    "TickerType": "OP",
    "Description": (
        "PURCHASED 100 CALL OPTIONS WITH A STRIKE PRICE OF $100 AND AN "
        "EXPIRATION DATE OF 6/17/27."
    ),
}

#: The same filer, the same day, the same amount band — and a different trade.
PELOSI_SHARES_ROW = {
    **PELOSI_CALLS_ROW,
    "TickerType": "ST",
    "Description": "PURCHASED 10,000 SHARES.",
}

#: The row that makes TickerType alone insufficient: options by description,
#: ordinary stock by type (live feed, 2026-07-19).
BAC_MISTYPED_ROW = {
    "Representative": "Laurel Lee",
    "ReportDate": "2026-07-19",
    "TransactionDate": "2026-06-02",
    "Ticker": "BAC",
    "Transaction": "Sale",
    "Range": "$1,001 - $15,000",
    "House": "Representatives",
    "TickerType": "ST",
    "Description": "CALL OPTION CONTRACTS.",
}


def one_item(row, source):
    fetcher, _ = fetcher_with([httpx.Response(200, json=[row])])
    items = fetcher(source)
    assert len(items) == 1
    return items[0]


def test_an_option_purchase_carries_its_terms_as_structured_fields(source):
    item = one_item(PELOSI_CALLS_ROW, source)
    assert item.fields["instrument"] == "option"
    assert item.fields["option_side"] == "call"
    assert item.fields["option_strike"] == "100"
    assert item.fields["option_expiry"] == "2027-06-17"  # 6/17/27, this century
    assert item.fields["option_contracts"] == "100"
    assert item.fields["ticker_type"] == "OP"


def test_the_content_says_calls_where_it_used_to_say_purchase(source):
    """The defect, in one assertion: the research pass must be able to tell a
    million dollars of calls from a million dollars of stock."""
    content = one_item(PELOSI_CALLS_ROW, source).content
    assert "instrument: option" in content
    assert "option side: call" in content
    assert "option strike: $100" in content
    assert "option expiry: 2027-06-17" in content
    assert "contracts: 100" in content
    assert "the disclosed amount range is the PREMIUM paid" in content
    # And the filer's own words survive verbatim, inside the fenced content.
    assert "PURCHASED 100 CALL OPTIONS" in content
    # Nothing else regressed.
    assert "amount range: $1,000,001 - $5,000,000" in content
    assert "disclosure lag: 28 days" in content


def test_a_stock_purchase_says_stock(source):
    item = one_item(PELOSI_SHARES_ROW, source)
    assert item.fields["instrument"] == "stock"
    assert item.fields["option_side"] == ""
    assert "instrument: stock" in item.content
    assert "option side" not in item.content  # no empty options block


def test_options_by_description_beat_a_stock_ticker_type(source):
    """The feed mistypes: BAC reads "CALL OPTION CONTRACTS." under TickerType ST.
    Detection is the OR of both fields, so the type alone cannot hide an option."""
    item = one_item(BAC_MISTYPED_ROW, source)
    assert item.fields["instrument"] == "option"
    assert item.fields["option_side"] == "call"


def test_a_row_the_feed_does_not_type_is_unstated_not_stock(source):
    """Never inferred: an unknown type with no description is a fact about the
    feed, and reporting it as equity would be an invention."""
    untyped = {k: v for k, v in PELOSI_CALLS_ROW.items()
               if k not in ("TickerType", "Description")}
    item = one_item(untyped, source)
    assert item.fields["instrument"] == ""
    assert "instrument: not stated by the filing" in item.content


def test_terms_the_filing_withheld_say_so_and_are_never_invented(source):
    """"CALL OPTION" states a side and nothing else. The strike does not become
    zero, blank, or the stock price."""
    bare = {**PELOSI_CALLS_ROW, "Description": "CALL OPTION"}
    item = one_item(bare, source)
    assert item.fields["instrument"] == "option"
    assert item.fields["option_side"] == "call"
    assert item.fields["option_strike"] == ""
    assert item.fields["option_expiry"] == ""
    assert "option strike: not stated by the filing" in item.content
    assert "option expiry: not stated by the filing" in item.content
    assert "contracts: not stated by the filing" in item.content


def test_the_three_description_formats_the_feed_actually_uses(source):
    """Sampled from the live feed 2026-08-27: Pelosi's, Gottheimer's, and the
    Senate e-filing form's. All three parse; a fourth shape degrades to "not
    stated" rather than to a wrong number."""
    from signals.quiver import _terms_of

    pelosi = _terms_of("OP", "PURCHASED 200 CALL OPTIONS WITH A STRIKE PRICE OF "
                             "$50 AND AN EXPIRATION DATE OF 3/19/27.")
    assert (pelosi.side, pelosi.strike, pelosi.expiry, pelosi.contracts) == (
        "call", "50", "2027-03-19", "200"
    )
    gottheimer = _terms_of("OP", "CALL OPTIONS; STRIKE PRICE $325; EXPIRES 06/18/2026")
    assert (gottheimer.side, gottheimer.strike, gottheimer.expiry) == (
        "call", "325", "2026-06-18"
    )
    senate = _terms_of("Stock Option",
                       "Option Type: Call Strike price: $75.00  Expires: 2026-08-21")
    assert (senate.side, senate.strike, senate.expiry) == ("call", "75", "2026-08-21")
    puts = _terms_of("OP", "PUT OPTION")
    assert (puts.side, puts.strike) == ("put", None)


def test_prose_about_shares_is_not_an_option(source):
    """The description detector is the word "option", not a loose call/put match:
    "SHARES PUT INTO TRUST" is not a put."""
    from signals.quiver import _terms_of

    assert _terms_of("ST", "PURCHASED 10,000 SHARES.").instrument == "stock"
    assert _terms_of("ST", "SHARES PUT INTO TRUST").instrument == "stock"
    assert _terms_of("Stock", "AUTOMATIC REINVESTMENT OF DIVIDENDS EARNED.").instrument == "stock"


# ================================================================================
# The dedup collision the description fixes — and the re-emission it must not cause
# ================================================================================


def test_shares_and_calls_the_same_day_are_two_signals_not_one(source):
    """The proof the ruling rests on: before the description entered the hash,
    Pelosi's 10,000 BE shares and her 100 BE calls — same filer, same day, same
    amount band — collided, and whichever the API returned second was silently
    discarded as a duplicate."""
    fetcher, _ = fetcher_with(
        [httpx.Response(200, json=[PELOSI_CALLS_ROW, PELOSI_SHARES_ROW])]
    )
    items = fetcher(source)

    assert len(items) == 2
    assert {item.fields["instrument"] for item in items} == {"option", "stock"}
    assert items[0].external_id != items[1].external_id


def test_a_row_without_a_description_keeps_the_identity_it_always_had(source):
    """The 95% of the feed that carries no description must NOT re-emit: an empty
    component still changes a digest, and re-researching the whole feed is not a
    dedup fix, it is an outage with a bill attached."""
    from datetime import date

    from signals.quiver import _identity

    args = ("Nancy Pelosi", "NVDA", "Purchase", date(2026, 7, 1), date(2026, 8, 15),
            "$1M")
    # The pre-2026-08-27 digest, pinned: six components, joined, no trailing sep.
    import hashlib

    legacy = hashlib.sha256(
        "\x00".join(
            ["nancy pelosi", "NVDA", "purchase", "2026-07-01", "2026-08-15", "$1M"]
        ).encode("utf-8")
    ).hexdigest()[:32]
    assert _identity(*args) == legacy
    assert _identity(*args, "") == legacy
    assert _identity(*args, "PURCHASED 100 CALL OPTIONS.") != legacy


def test_a_described_row_reemits_exactly_once(source):
    """Described rows DO change identity — that is the authorized re-evaluation —
    but the new identity is stable, so it does not re-emit every poll."""
    fetcher, _ = fetcher_with(
        [httpx.Response(200, json=[PELOSI_CALLS_ROW])] * 3
    )
    assert len(fetcher(source)) == 1
    assert fetcher(source) == []
    assert fetcher(source) == []


# ================================================================================
# ...and it reaches the model
# ================================================================================


def option_signal(row, signals_config):
    fetcher, _ = fetcher_with([httpx.Response(200, json=[row])])
    scanner = Class2CongressionalScanner(
        signals_config.klass("class_2"), fetcher, SignalQueue(), clock=lambda: NOW
    )
    return scanner.poll(force=True)[0]


def test_the_disclosed_instrument_reaches_the_research_prompt(signals_config):
    signal = option_signal(PELOSI_CALLS_ROW, signals_config)
    prompt = build_user_prompt(signal)

    fence_start = prompt.index("BEGIN UNTRUSTED THIRD-PARTY CONTENT")
    outside = prompt[:fence_start]
    # Normalised extractions are stated as system facts, outside the fence...
    assert "DISCLOSED INSTRUMENT" in outside
    assert "- side: call" in outside
    assert "- strike: $100" in outside
    assert "- expiry: 2027-06-17" in outside
    # ...and the guidance the ruling asked for is with them.
    assert "stock replacement" in outside
    assert "CONVICTION AND SIZE" in outside
    assert "An expiry is a deadline; a catalyst is an event." in outside
    assert "PREMIUM paid" in outside
    # The filer's prose stays inside the fence, where third-party text lives.
    assert "PURCHASED 100 CALL OPTIONS" in prompt[fence_start:]
    assert "PURCHASED 100 CALL OPTIONS" not in outside


def test_a_stock_disclosure_gets_no_options_guidance(signals_config):
    """Prompt tokens are not spent on instructions about an instrument the
    signal does not name."""
    prompt = build_user_prompt(option_signal(PELOSI_SHARES_ROW, signals_config))
    assert "DISCLOSED INSTRUMENT" not in prompt
    assert "stock replacement" not in prompt


def test_unstated_terms_reach_the_model_as_unstated(signals_config):
    bare = {**PELOSI_CALLS_ROW, "Description": "CALL OPTION"}
    prompt = build_user_prompt(option_signal(bare, signals_config))
    assert "- strike: not stated by the filing" in prompt
    assert "never infer a strike or an expiry" in prompt
