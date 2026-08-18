"""X Class 1 fetcher tests.

The claims that matter: since_id keeps a quiet minute at zero posts billed; long
posts arrive untruncated via note_tweet and reach research whole; the existing
forward/retrospective classification applies unchanged; dedup survives restarts via
the audit log; and the daily read counter trips loudly on the failure mode that is
billing-shaped (a since_id regression). Live smoke gated behind ``X_LIVE_TESTS=1`` —
it spends real cents.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
import pytest

from execution.environment import LIVE_CONFIRMATION_VARIABLE
from fixture_posts import PURE_FORWARD_CALL, PURE_RETROSPECTIVE
from signals import (
    SignalQueue,
    SignalsConfig,
    SourceRouter,
    XError,
    XRecentSearchFetcher,
)
from signals.scanners import Class1RealtimeScanner

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)

#: One sentence, forward-marked, longer than 280 chars — the classifier keeps it
#: whole and the truncation trap is real. The marker sits at the far end.
LONG_CALL = (
    "Buying $NVDA calls here at 142.50 with a stop at 138 because the setup has "
    "everything lining up at once and I want the whole thesis in one place: "
    + "capacity constraints stay bid through year end, " * 4
    + "and the level that confirms all of it is 160 MARKER_END_OF_LONG_POST"
)
assert len(LONG_CALL) > 280

TRUNCATED = LONG_CALL[:277] + "..."


def post(
    post_id: str,
    text: str,
    *,
    note_text: Optional[str] = None,
    created_at: str = "2026-08-18T14:29:30.000Z",
    cashtags: Optional[list[str]] = None,
    referenced: Optional[list[dict]] = None,
) -> dict:
    body: dict = {
        "id": post_id,
        "text": text,
        "created_at": created_at,
        "author_id": "999",
        "edit_history_tweet_ids": [post_id],
    }
    if note_text is not None:
        body["note_tweet"] = {"text": note_text}
    if cashtags:
        body["entities"] = {"cashtags": [{"tag": tag} for tag in cashtags]}
    if referenced:
        body["referenced_tweets"] = referenced
    return body


def search_response(*posts: dict) -> httpx.Response:
    payload: dict = {"meta": {"result_count": len(posts)}}
    if posts:
        payload["data"] = list(posts)
        payload["meta"]["newest_id"] = max(p["id"] for p in posts)
    return httpx.Response(200, json=payload)


class XRecorder:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = responses

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._responses) - 1)
        return self._responses[index]


def fetcher_with(
    responses: Optional[list[httpx.Response]] = None, **kwargs
) -> tuple[XRecentSearchFetcher, XRecorder]:
    recorder = XRecorder(
        responses or [search_response(post("100", PURE_FORWARD_CALL, cashtags=["NVDA"]))]
    )
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    kwargs.setdefault("bearer_token", "test-token")
    kwargs.setdefault("clock", lambda: NOW)
    kwargs.setdefault("sleeper", lambda seconds: None)
    return XRecentSearchFetcher(client, **kwargs), recorder


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def source(signals_config):
    return signals_config.source("class_1", "nolimitgains")


# ================================================================================
# The query and the fields
# ================================================================================


def test_the_query_names_the_handle_and_excludes_retweets(source):
    fetcher, recorder = fetcher_with()
    fetcher(source)

    request = recorder.requests[0]
    assert request.url.params["query"] == "from:nolimitgains -is:retweet"
    assert request.headers["Authorization"] == "Bearer test-token"
    fields = request.url.params["tweet.fields"]
    for field in ("created_at", "note_tweet", "entities", "referenced_tweets"):
        assert field in fields


def test_a_post_becomes_one_raw_item(source):
    fetcher, _ = fetcher_with()
    items = fetcher(source)

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "100"
    assert item.content == PURE_FORWARD_CALL
    assert item.published_at == datetime(2026, 8, 18, 14, 29, 30, tzinfo=timezone.utc)
    assert item.fields["handle"] == "@nolimitgains"
    assert item.fields["cashtags"] == "NVDA"
    assert item.fields["post_type"] == "original"
    assert item.fields["was_long_post"] == "false"


def test_a_long_post_arrives_whole_via_note_tweet(source):
    """The truncation trap: text clips past 280 chars; note_tweet carries it all."""
    fetcher, _ = fetcher_with(
        [search_response(post("101", TRUNCATED, note_text=LONG_CALL))]
    )
    item = fetcher(source)[0]

    assert item.content == LONG_CALL
    assert "MARKER_END_OF_LONG_POST" in item.content
    assert "..." not in item.content
    assert item.fields["was_long_post"] == "true"


def test_quotes_and_replies_are_labelled(source):
    fetcher, _ = fetcher_with(
        [
            search_response(
                post("102", "Quoting this. $AMD calls here now.", referenced=[{"type": "quoted", "id": "9"}]),
                post("103", "Replying: buying $SOFI here, entry: 14.20.", referenced=[{"type": "replied_to", "id": "8"}]),
            )
        ]
    )
    items = fetcher(source)
    assert {item.fields["post_type"] for item in items} == {"quoted", "reply"}


def test_a_routed_source_without_a_handle_is_a_config_error(source):
    fetcher, _ = fetcher_with()
    nameless = source.model_copy(update={"handle": None})
    with pytest.raises(XError, match="no handle"):
        fetcher(nameless)


# ================================================================================
# since_id: the cost discipline
# ================================================================================


def test_the_first_poll_uses_a_short_lookback_not_seven_days(source):
    fetcher, recorder = fetcher_with(first_poll_lookback_seconds=900)
    fetcher(source)

    request = recorder.requests[0]
    assert "since_id" not in request.url.params
    assert request.url.params["start_time"] == "2026-08-18T14:15:00Z"


def test_subsequent_polls_ask_only_for_newer_posts(source):
    fetcher, recorder = fetcher_with(
        [
            search_response(post("100", PURE_FORWARD_CALL), post("105", "gm")),
            search_response(),  # a quiet minute
        ]
    )
    fetcher(source)
    fetcher(source)

    second = recorder.requests[1]
    assert second.url.params["since_id"] == "105"  # meta.newest_id advanced
    assert "start_time" not in second.url.params


def test_a_quiet_minute_returns_nothing_and_advances_nothing(source):
    fetcher, recorder = fetcher_with(
        [
            search_response(post("100", PURE_FORWARD_CALL)),
            search_response(),
            search_response(),
        ]
    )
    fetcher(source)
    assert fetcher(source) == []
    assert fetcher(source) == []
    # since_id held steady across the quiet polls.
    assert recorder.requests[2].url.params["since_id"] == "100"
    assert fetcher.posts_read_today == 1  # only the real post was ever billed


# ================================================================================
# Dedup across restarts
# ================================================================================


def test_dedup_survives_a_restart_via_the_audit_log(source):
    first, _ = fetcher_with()
    emitted = first(source)
    post_id = emitted[0].external_id

    restarted, _ = fetcher_with(seen=[post_id])
    assert restarted(source) == []


def test_an_unresearched_post_reemits_after_restart(source):
    restarted, _ = fetcher_with(seen=[])
    assert len(restarted(source)) == 1


# ================================================================================
# The daily read counter — the billing tripwire
# ================================================================================


def test_reads_accumulate_per_day_and_reset_at_the_boundary(source):
    from test_orchestrator import FakeClock

    clock = FakeClock(NOW)
    fetcher, _ = fetcher_with(
        [
            search_response(*(post(str(200 + n), f"post {n}") for n in range(3))),
            search_response(post("300", "next day post")),
        ],
        clock=clock,
    )
    fetcher(source)
    assert fetcher.posts_read_today == 3

    clock.advance(days=1)
    fetcher(source)
    assert fetcher.posts_read_today == 1  # rolled at the UTC day boundary


def test_crossing_the_threshold_warns_once_and_hits_the_sink(source, caplog):
    import logging

    warnings: list[str] = []
    posts = [post(str(400 + n), f"repeat {n}") for n in range(6)]
    fetcher, _ = fetcher_with(
        [search_response(*posts[:3]), search_response(*posts[3:]), search_response(*posts[3:])],
        read_warning_threshold=4,
        warn_sink=warnings.append,
        seen=[],
    )
    # The source-level threshold (200 in signals.yaml) would override the constructor;
    # strip it to exercise the configured-threshold plumbing both ways.
    bare = source.model_copy(update={"daily_read_warning": None})

    with caplog.at_level(logging.WARNING, logger="signals.x"):
        fetcher(bare)  # 3 reads: under
        assert warnings == []
        fetcher(bare)  # 6 reads: past 4 — warn
        fetcher(bare)  # still past — but the day already warned

    assert len(warnings) == 1
    assert "since_id regression" in warnings[0]
    assert "6" in warnings[0]
    assert sum("since_id regression" in r.message for r in caplog.records) == 1


def test_the_source_configured_threshold_wins(source):
    warnings: list[str] = []
    posts = [post(str(500 + n), f"p{n}") for n in range(3)]
    fetcher, _ = fetcher_with(
        [search_response(*posts)],
        read_warning_threshold=1000,  # constructor says relax...
        warn_sink=warnings.append,
    )
    tight = source.model_copy(update={"daily_read_warning": 2})  # ...config says trip
    fetcher(tight)
    assert len(warnings) == 1


# ================================================================================
# Citizenship: auth, retry, failure
# ================================================================================


def test_a_missing_token_refuses_before_any_request(source, monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    recorder = XRecorder([search_response()])
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    fetcher = XRecentSearchFetcher(client, clock=lambda: NOW, sleeper=lambda s: None)

    with pytest.raises(XError, match="X_BEARER_TOKEN"):
        fetcher(source)
    assert recorder.requests == []


def test_a_refused_token_is_a_loud_distinct_error(source):
    fetcher, _ = fetcher_with([httpx.Response(401, json={"title": "Unauthorized"})])
    with pytest.raises(XError, match="X_BEARER_TOKEN"):
        fetcher(source)


def test_a_transient_blip_is_retried_once(source):
    fetcher, recorder = fetcher_with(
        [
            httpx.Response(503, text="down"),
            search_response(post("600", PURE_FORWARD_CALL)),
        ]
    )
    assert len(fetcher(source)) == 1
    assert len(recorder.requests) == 2


def test_a_persistent_outage_fails_the_poll(source):
    fetcher, _ = fetcher_with([httpx.Response(429, text="rate limited")])
    with pytest.raises(XError, match="429"):
        fetcher(source)


# ================================================================================
# Through the Class 1 scanner: classification unchanged
# ================================================================================


def scanner_for(fetcher, signals_config):
    queue = SignalQueue()
    return (
        Class1RealtimeScanner(
            signals_config.klass("class_1"), fetcher, queue, clock=lambda: NOW
        ),
        queue,
    )


def route_to_nolimitgains(fetcher):
    """The production shape: nolimitgains routed, the Trump leg still unbuilt."""
    return SourceRouter(routes={"nolimitgains": fetcher}, unbuilt={"trump_posts"})


def test_a_forward_call_reaches_the_queue_classified(signals_config):
    fetcher, _ = fetcher_with()
    scanner, _ = scanner_for(route_to_nolimitgains(fetcher), signals_config)

    emitted = scanner.poll(force=True)

    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.source_id == "nolimitgains"
    assert str(signal.classification) == "forward_call"
    assert signal.metadata["copy_trade"] == "false"
    assert signal.raw_content == PURE_FORWARD_CALL


def test_a_retrospective_is_discarded_and_logged_not_queued(signals_config):
    fetcher, _ = fetcher_with([search_response(post("700", PURE_RETROSPECTIVE))])
    scanner, queue = scanner_for(route_to_nolimitgains(fetcher), signals_config)

    emitted = scanner.poll(force=True)

    assert emitted == []
    assert len(queue) == 0
    assert len(scanner.credibility_log) == 1  # tracked, never traded


def test_the_router_leaves_the_trump_leg_explicitly_unbuilt(signals_config):
    fetcher, recorder = fetcher_with()
    router = route_to_nolimitgains(fetcher)

    assert router(signals_config.source("class_1", "trump_posts")) == []
    assert recorder.requests == []  # the X fetcher was never asked about Trump


# ================================================================================
# The full pipeline: a long post's full text reaches research and the audit trail
# ================================================================================


def test_a_long_post_traverses_the_pipeline_untruncated(
    tmp_path, signals_config
):
    from orchestrator import start
    from research.reports import REPORT_TOOL_NAME
    from risk_gate import RiskLimits
    from research.config import ResearchConfig
    from test_exits import MutablePrices, RoutingLLM
    from test_orchestrator import REPORT, FakeBroker, FakeClock, counter, orchestrator_config, structured

    x_fetcher, _ = fetcher_with(
        [search_response(post("800", TRUNCATED, note_text=LONG_CALL, cashtags=["NVDA"]))]
    )
    llm = RoutingLLM(**{REPORT_TOOL_NAME: structured({**REPORT, "tickers": ["NVDA"]})})
    started = start(
        fetcher=route_to_nolimitgains(x_fetcher),
        prices=MutablePrices(NVDA="142.50"),
        llm_client=llm,
        adapter=FakeBroker(),
        clock=FakeClock(NOW),
        data_dir=tmp_path,
        limits=RiskLimits.load(),
        signals_config=signals_config,
        research_config=ResearchConfig.load(),
        orchestrator_config=orchestrator_config(),
        id_factory=counter(),
    )
    report = started.loop.tick()

    assert report.processed and report.processed[0].traded
    # The research pass saw the WHOLE post, inside the fence.
    prompt = llm.calls[0]["user"]
    fence = prompt.index("BEGIN UNTRUSTED THIRD-PARTY CONTENT")
    assert "MARKER_END_OF_LONG_POST" in prompt[fence:]
    # And the audit record carries the untruncated original verbatim.
    trail = started.audit.trail(report.processed[0].decision_id)
    assert trail.decision.signal.raw_content == LONG_CALL
    assert "MARKER_END_OF_LONG_POST" in trail.decision.signal.raw_content
    started.loop.shutdown()


# ================================================================================
# Live smoke — opt-in: it spends real cents
# ================================================================================


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("X_LIVE_TESTS") != "1",
    reason="set X_LIVE_TESTS=1 to hit the real X API (pay-per-use: costs real cents)",
)
def test_live_smoke_against_the_real_account():
    """One real authenticated search over the last 6 days. Success proves the token,
    the query shape, and the field set; whether the account posted recently is its
    own business. Cost: at most max_results posts x $0.005."""
    from execution.environment import load_environment

    load_environment()
    source = SignalsConfig.load().source("class_1", "nolimitgains")
    fetcher = XRecentSearchFetcher(first_poll_lookback_seconds=6 * 86400)
    try:
        items = fetcher(source)
    finally:
        fetcher.close()

    assert isinstance(items, list)
    for item in items:
        assert item.external_id.isdigit()
        assert item.content
        assert item.fields["handle"] == "@nolimitgains"
        assert item.fields["created_at"]
    print(
        f"live smoke: {len(items)} posts from @nolimitgains in 6 days "
        f"({fetcher.posts_read_today} posts billed)"
    )


def test_a_not_enrolled_app_gets_the_actionable_message_not_token_advice(source):
    """The 403 seen in practice: a valid token whose App is outside a Project.
    "Check your token" would be the wrong advice; the error says the right one."""
    fetcher, _ = fetcher_with(
        [
            httpx.Response(
                403,
                json={
                    "client_id": "123",
                    "reason": "client-not-enrolled",
                    "detail": "…must use keys and tokens from a developer App that "
                    "is attached to a Project…",
                    "title": "Client Forbidden",
                },
            )
        ]
    )
    with pytest.raises(XError, match="attached to a Project"):
        fetcher(source)
