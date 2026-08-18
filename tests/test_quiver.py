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


def test_a_watchlist_disclosure_becomes_one_raw_item(source):
    fetcher, recorder = fetcher_with()
    items = fetcher(source)

    assert len(items) == 1
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


def test_a_member_not_on_the_watchlist_is_not_a_signal(source):
    fetcher, _ = fetcher_with()
    items = fetcher(source)
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
    assert len(fetcher(source)) == 1
    assert fetcher(source) == []  # same rows in the next pull
    assert len(recorder.requests) == 2  # it re-polled; it just re-emitted nothing


def test_dedup_survives_a_restart_via_the_audit_log(source):
    """A new process seeds its seen-set from what the log says was researched —
    the same replay philosophy as the budget and the kill switch."""
    first, _ = fetcher_with()
    emitted = first(source)
    identity = emitted[0].external_id

    # ...the signal went through the pipeline and left an audit record carrying its
    # external_id; a restarted process reads those ids and seeds the new fetcher:
    restarted, _ = fetcher_with(seen=[identity])
    assert restarted(source) == []


def test_an_unresearched_signal_reemits_after_restart(source):
    """The right edge of the seeding rule: queued-but-never-researched left no audit
    record, so it comes back and finally gets its pass — deferred, not dropped."""
    restarted, _ = fetcher_with(seen=[])  # nothing in the log for it
    assert len(restarted(source)) == 1


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
    assert len(fetcher(source)) == 1
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
            "nolimitgains": fake("x"),
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
        "x:nolimitgains",
        "quiver:congressional_disclosures",
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
            "nolimitgains": lambda s: [],
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
