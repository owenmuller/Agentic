"""Research pre-filter tests — approved 2026-08-18 to solve the budget collision.

The claims: only trump_posts (and its mirror-delivered content) is filtered; a
ticker or a configured theme earns the pass; everything filtered writes a readable
pre_filter stage rejection WITHOUT spending budget; and the budget replay does not
count filtered ids as spent passes — otherwise the filter would eat the budget it
exists to protect.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from audit import AuditLog, RejectedStage
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from orchestrator import ResearchPreFilter, start
from research.reports import REPORT_TOOL_NAME
from signals import SignalQueue, SignalsConfig, SourceRouter
from signals.scanners import Class1RealtimeScanner
from test_exits import MutablePrices, RoutingLLM
from test_mirrors import TTOX_FORMAT
from test_orchestrator import (
    NOW,
    REPORT,
    FakeBroker,
    FakeClock,
    counter,
    feed,
    orchestrator_config,
    structured,
)

CHITCHAT = "Happy Birthday to the great Elvis Presley. Nobody sings like Elvis!"
THEMED = "The Fake News won't tell you: our Tariffs are bringing in BILLIONS!"
TICKERED = "$NVDA is the greatest American company. Powerful!"


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def prefilter(signals_config):
    return ResearchPreFilter.from_config(signals_config)


def trump_signal(signals_config, text):
    queue = SignalQueue()
    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"), feed(trump_posts=[text]), queue,
        clock=lambda: NOW,
    )
    return scanner.poll(force=True)[0]


def nolimitgains_signal(signals_config, text):
    queue = SignalQueue()
    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"), feed(nolimitgains=[text]), queue,
        clock=lambda: NOW,
    )
    return scanner.poll(force=True)[0]


# ================================================================================
# The verdicts
# ================================================================================


def test_only_the_configured_source_is_filtered(prefilter):
    assert prefilter.filtered_sources == ("trump_posts",)


def test_a_post_with_a_cashtag_earns_the_pass(signals_config, prefilter):
    assert prefilter.skip_reason(trump_signal(signals_config, TICKERED)) is None


def test_a_post_touching_a_theme_earns_the_pass(signals_config, prefilter):
    assert prefilter.skip_reason(trump_signal(signals_config, THEMED)) is None


def test_theme_matching_is_stem_based_and_case_blind(signals_config, prefilter):
    for text in (
        "TARIFFS ARE WORKING!",
        "We will be tariffing every foreign car.",
        "Interest Rates must come down NOW.",
    ):
        assert prefilter.skip_reason(trump_signal(signals_config, text)) is None, text


def test_chitchat_is_filtered_with_a_readable_reason(signals_config, prefilter):
    reason = prefilter.skip_reason(trump_signal(signals_config, CHITCHAT))
    assert reason is not None
    assert "no tickers" in reason
    assert "research_prefilter_themes" in reason


def test_mirror_delivered_content_is_filtered_like_the_principals(
    signals_config, prefilter
):
    """Mirror signals attribute to trump_posts, so the filter sees them the same."""
    queue = SignalQueue()
    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"),
        feed(trump_mirror_ttox=[CHITCHAT + " (TS: 18 Aug 14:31 ET)"]),
        queue,
        clock=lambda: NOW,
    )
    mirrored = scanner.poll(force=True)[0]
    assert mirrored.source_id == "trump_posts"
    assert prefilter.skip_reason(mirrored) is not None

    themed_queue = SignalQueue()
    themed_scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"),
        feed(trump_mirror_ttox=[TTOX_FORMAT]),  # the steel-tariff fixture
        themed_queue,
        clock=lambda: NOW,
    )
    assert prefilter.skip_reason(themed_scanner.poll(force=True)[0]) is None


def test_unconfigured_sources_are_never_filtered(signals_config, prefilter):
    """@nolimitgains chitchat still reaches research (and dies there on its own
    merits) — the filter is a trump_posts decision, not a general gate."""
    signal = nolimitgains_signal(
        signals_config, "Loading up here. Setup is live, entry: 14.20."
    )
    assert prefilter.skip_reason(signal) is None


# ================================================================================
# Through the loop: recorded, unspent, replayed correctly
# ================================================================================


def run_loop(tmp_path, signals_config, posts, clock=None):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    llm = RoutingLLM(
        **{REPORT_TOOL_NAME: structured({**REPORT, "confidence": 40})}
    )
    started = start(
        fetcher=SourceRouter(
            routes={
                "trump_mirror_ttox": feed(trump_mirror_ttox=posts),
                "trump_mirror_tdp": feed(),
                "nolimitgains": feed(),
            },
            unbuilt={"trump_posts"},
        ),
        prices=MutablePrices(),
        llm_client=llm,
        adapter=FakeBroker(),
        clock=clock or FakeClock(NOW),
        data_dir=tmp_path,
        limits=RiskLimits.load(),
        signals_config=signals_config,
        research_config=ResearchConfig.load(),
        orchestrator_config=orchestrator_config(),
        id_factory=counter(),
    )
    return started, llm


def test_a_filtered_post_writes_the_trail_and_spends_nothing(
    tmp_path, signals_config
):
    started, llm = run_loop(tmp_path, signals_config, [CHITCHAT])
    report = started.loop.tick()

    assert report.prefiltered == 1
    assert report.processed == []
    assert started.budget.spent == 0, "a filtered post must not cost a pass"
    assert llm.calls == []

    rejections = started.audit.stage_rejections()
    assert len(rejections) == 1
    rejection = rejections[0]
    assert rejection.stage is RejectedStage.PRE_FILTER
    assert rejection.code == "pre_filter"
    assert CHITCHAT in rejection.signal.raw_content  # readable, revisitable
    assert rejection.signal.source_id == "trump_posts"
    assert rejection.signal.delivered_by == "trump_mirror_ttox"
    started.loop.shutdown()


def test_a_themed_post_goes_to_research_as_before(tmp_path, signals_config):
    started, llm = run_loop(tmp_path, signals_config, [THEMED])
    report = started.loop.tick()

    assert report.prefiltered == 0
    assert len(report.processed) == 1
    assert started.budget.spent == 1
    assert len(llm.calls) == 1
    started.loop.shutdown()


def test_the_filter_triages_a_trump_flood_without_touching_the_budget(
    tmp_path, signals_config
):
    """The collision the filter was approved for: a 12-post day where 10 are noise
    leaves 38 passes for everything else, instead of zero."""
    posts = [f"{CHITCHAT} number {n}" for n in range(10)] + [THEMED, TICKERED]
    started, _ = run_loop(tmp_path, signals_config, posts)
    report = started.loop.tick()

    assert report.prefiltered == 10
    assert len(report.processed) == 2
    assert started.budget.spent == 2
    assert started.budget.remaining == 38
    started.loop.shutdown()


def test_budget_replay_does_not_count_filtered_ids_as_spent(
    tmp_path, signals_config
):
    """A pre_filter record creates a decision_id but no research call. If replay
    counted it, the filter would consume the budget it exists to protect."""
    clock = FakeClock(NOW)
    started, _ = run_loop(tmp_path, signals_config, [CHITCHAT, THEMED], clock=clock)
    started.loop.tick()
    assert started.budget.spent == 1  # only the themed post
    started.loop.shutdown()

    from orchestrator.bootstrap import preflight
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    restarted = preflight(
        adapter=FakeBroker(),
        limits=RiskLimits.load(),
        signals_config=signals_config,
        research_config=ResearchConfig.load(),
        orchestrator_config=orchestrator_config(),
        data_dir=tmp_path,
        clock=clock,
        id_factory=counter("b"),
    )
    assert restarted.budget.spent == 1, "replay counted a filtered post as a pass"


def test_filtered_posts_do_not_requeue_after_restart(tmp_path, signals_config):
    """The pre_filter record carries the signal's external id, so the seeded queue
    recognises the same Truth next session — filtered once, not re-filtered daily."""
    started, _ = run_loop(tmp_path, signals_config, [CHITCHAT])
    started.loop.tick()
    started.loop.shutdown()

    restarted, _ = run_loop(tmp_path, signals_config, [CHITCHAT])
    # Fresh data dir? No - same tmp_path, so the queue seeds from the same log.
    report = restarted.loop.tick()
    assert report.prefiltered == 0
    assert len(restarted.audit.stage_rejections()) == 1  # still just the one record
    restarted.loop.shutdown()
