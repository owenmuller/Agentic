"""Trump-leg mirror tests.

The claims that matter: a mirrored signal carries BOTH attributions — the principal's
words, the mirror's delivery — all the way into the audit record; the research prompt
states the provenance and demands verification before any confidence, with
no_position as the mandatory answer for an unverifiable post; two mirrors delivering
the same original post dedup to one signal; and a mirror that goes silent for too
many trading days is flagged for a human, because silence might mean the principal is
quiet or the bot is dead, and only a human can check which.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from audit import AuditLog, RejectedStage
from audit.records import SignalSnapshot
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from orchestrator import mirror_silence, start
from research.prompts import build_user_prompt
from research.reports import REPORT_TOOL_NAME
from signals import (
    SignalQueue,
    SignalsConfig,
    SourceRouter,
    mirror_content_key,
)
from signals.scanners import Class1RealtimeScanner, RawItem
from test_exits import MutablePrices, RoutingLLM
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

#: The same original Truth, as each mirror actually formats it (formats taken from
#: live posts: TrumpDailyPosts wraps a timestamp header; TrumpTruthOnX appends a
#: "(TS: ...)" stamp and pads with zero-width characters).
ORIGINAL_TEXT = (
    "I am announcing a 100% Tariff on all foreign steel entering the United "
    "States, effective immediately. American Steel will boom like never before!"
)
TDP_FORMAT = (
    "Donald J. Trump Truth Social Post 02:31 PM EST 08.18.26 " + ORIGINAL_TEXT
)
TTOX_FORMAT = ORIGINAL_TEXT + "​‍ (TS: 18 Aug 14:31 ET)"


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


def scanner_with(fetcher, signals_config, queue=None):
    queue = queue if queue is not None else SignalQueue()
    return (
        Class1RealtimeScanner(
            signals_config.klass("class_1"), fetcher, queue, clock=lambda: NOW
        ),
        queue,
    )


# ================================================================================
# Attribution: the principal's words, the mirror's delivery
# ================================================================================


def test_a_mirrored_signal_is_attributed_to_the_principal(signals_config):
    scanner, _ = scanner_with(
        feed(trump_mirror_ttox=[TTOX_FORMAT]), signals_config
    )
    emitted = scanner.poll(force=True)

    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.source_id == "trump_posts"  # whose words these are
    assert signal.metadata["delivered_by"] == "trump_mirror_ttox"  # who carried them
    assert signal.metadata["delivered_handle"] == "@TrumpTruthOnX"
    assert signal.metadata["provenance"] == "unofficial_mirror"
    assert signal.raw_content == TTOX_FORMAT  # verbatim, furniture included
    # The external id is the normalised content key, not the tweet id — the tweet
    # id is preserved separately for accountability.
    assert signal.external_id == mirror_content_key(TTOX_FORMAT)
    assert signal.metadata["delivered_post_id"] == "trump_mirror_ttox-0"


def test_the_same_truth_from_two_mirrors_is_one_signal(signals_config):
    """One research pass per original post, however many mirrors relay it."""
    scanner, queue = scanner_with(
        feed(trump_mirror_ttox=[TTOX_FORMAT], trump_mirror_tdp=[TDP_FORMAT]),
        signals_config,
    )
    scanner.poll(force=True)

    queued = queue.drain()
    assert len(queued) == 1
    assert queued[0].source_id == "trump_posts"


def test_different_truths_from_the_same_mirror_are_distinct(signals_config):
    scanner, queue = scanner_with(
        feed(trump_mirror_ttox=[TTOX_FORMAT, "A completely different post. (TS: 18 Aug 15:02 ET)"]),
        signals_config,
    )
    scanner.poll(force=True)
    assert len(queue.drain()) == 2


def test_a_direct_source_signal_carries_no_delivered_by(signals_config):
    scanner, _ = scanner_with(
        feed(trump_posts=["Tariffs on steel coming."]), signals_config
    )
    signal = scanner.poll(force=True)[0]
    assert signal.source_id == "trump_posts"
    assert "delivered_by" not in signal.metadata


def test_the_snapshot_preserves_both_attributions_and_old_records_still_parse(
    signals_config,
):
    scanner, _ = scanner_with(feed(trump_mirror_tdp=[TDP_FORMAT]), signals_config)
    snapshot = SignalSnapshot.of(scanner.poll(force=True)[0])
    assert snapshot.source_id == "trump_posts"
    assert snapshot.delivered_by == "trump_mirror_tdp"

    # A record written before mirrors existed has no delivered_by and must parse.
    old = snapshot.model_dump(mode="json")
    del old["delivered_by"]
    assert SignalSnapshot.model_validate(old).delivered_by is None


# ================================================================================
# The research framing
# ================================================================================


def mirrored_signal(signals_config, text=TTOX_FORMAT):
    scanner, _ = scanner_with(feed(trump_mirror_ttox=[text]), signals_config)
    return scanner.poll(force=True)[0]


def test_the_prompt_states_provenance_and_demands_verification(signals_config):
    prompt = build_user_prompt(mirrored_signal(signals_config))

    assert "MIRROR PROVENANCE" in prompt
    assert "@TrumpTruthOnX (trump_mirror_ttox)" in prompt
    assert "UNOFFICIAL automated mirror" in prompt
    assert "VERIFICATION IS PART OF THIS PASS" in prompt
    assert "before assigning any confidence" in prompt
    assert '"no_position"' in prompt
    assert "unverifiable post is not tradeable at any confidence" in prompt
    # The framing sits OUTSIDE the fence — system-established fact, not content.
    fence = prompt.index("BEGIN UNTRUSTED THIRD-PARTY CONTENT")
    assert prompt.index("MIRROR PROVENANCE") < fence


def test_a_direct_signal_gets_no_mirror_framing(signals_config):
    scanner, _ = scanner_with(feed(trump_posts=["Steel tariffs now."]), signals_config)
    prompt = build_user_prompt(scanner.poll(force=True)[0])
    assert "MIRROR PROVENANCE" not in prompt


# ================================================================================
# The pipeline, end to end: both attributions in the audit record
# ================================================================================


def run_pipeline(tmp_path, signals_config, llm):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    started = start(
        fetcher=SourceRouter(
            routes={
                "trump_mirror_ttox": feed(trump_mirror_ttox=[TTOX_FORMAT]),
                "trump_mirror_tdp": feed(),
                "nolimitgains": feed(),
            },
            unbuilt={"trump_posts"},
        ),
        prices=MutablePrices(X="25.00"),
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
    return started, report


def test_a_mirrored_signal_traverses_the_pipeline_with_both_attributions(
    tmp_path, signals_config
):
    llm = RoutingLLM(
        **{
            REPORT_TOOL_NAME: structured(
                {
                    **REPORT,
                    "tickers": ["X"],  # United States Steel, fittingly
                    "thesis": "Verified against the Truth Social archive; the post "
                    "exists. Steel tariffs benefit domestic producers.",
                }
            )
        }
    )
    started, report = run_pipeline(tmp_path, signals_config, llm)

    assert report.processed and report.processed[0].traded
    decision = started.audit.trail(report.processed[0].decision_id).decision
    # Research and attribution see the principal; the record keeps the deliverer.
    assert decision.signal.source_id == "trump_posts"
    assert decision.signal.delivered_by == "trump_mirror_ttox"
    assert decision.signal.raw_content == TTOX_FORMAT
    # And the research pass was shown the provenance block.
    assert "MIRROR PROVENANCE" in llm.calls[0]["user"]
    started.loop.shutdown()


def test_an_unverifiable_post_lands_as_no_position(tmp_path, signals_config):
    """The instructed path for a post the model cannot verify exists: no_position,
    which sizes to zero at any confidence and leaves a complete rejected trail."""
    llm = RoutingLLM(
        **{
            REPORT_TOOL_NAME: structured(
                {
                    **REPORT,
                    "direction": "no_position",
                    "confidence": 90,
                    "tickers": ["X"],
                    "thesis": "Could not verify this post exists on Truth Social or "
                    "in any coverage; an unverifiable mirrored post is not tradeable.",
                    "manipulation_assessment": "Possible fabricated mirror content.",
                }
            )
        }
    )
    started, report = run_pipeline(tmp_path, signals_config, llm)

    result = report.processed[0]
    assert not result.traded
    rejection = started.audit.rejections_for(result.decision_id)[0]
    assert rejection.stage is RejectedStage.SIZING
    assert rejection.code == "no_position"
    assert rejection.research.confidence == 90  # confident about the absence
    assert rejection.signal.delivered_by == "trump_mirror_ttox"
    assert rejection.research.flagged_manipulation
    started.loop.shutdown()


def test_restart_dedup_covers_mirrored_content_via_the_seeded_queue(
    tmp_path, signals_config
):
    """Mirror external ids are content keys, not any fetcher's native ids — the
    queue seeding from the audit log is the layer that stops a restarted process
    re-buying research on a Truth it already scored."""
    llm = RoutingLLM(
        **{REPORT_TOOL_NAME: structured({**REPORT, "tickers": ["X"]})}
    )
    first, report = run_pipeline(tmp_path, signals_config, llm)
    assert report.processed
    first.loop.shutdown()

    # Restart: the same Truth arrives again — this time via the OTHER mirror.
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    second_llm = RoutingLLM()
    restarted = start(
        fetcher=SourceRouter(
            routes={
                "trump_mirror_ttox": feed(),
                "trump_mirror_tdp": feed(trump_mirror_tdp=[TDP_FORMAT]),
                "nolimitgains": feed(),
            },
            unbuilt={"trump_posts"},
        ),
        prices=MutablePrices(),
        llm_client=second_llm,
        adapter=FakeBroker(),
        clock=FakeClock(NOW),
        data_dir=tmp_path,
        limits=RiskLimits.load(),
        signals_config=signals_config,
        research_config=ResearchConfig.load(),
        orchestrator_config=orchestrator_config(),
        id_factory=counter("b"),
    )
    report = restarted.loop.tick()

    assert report.polled == 0  # queued nothing: the seeded queue recognised it
    assert report.processed == []
    assert [c for c in second_llm.calls if c["tool"] == REPORT_TOOL_NAME] == []
    restarted.loop.shutdown()


# ================================================================================
# Mirror health: silence goes to a human
# ================================================================================


def silence_fixture(tmp_path, signals_config, *, delivered_days_ago):
    """An audit log whose only mirror delivery is N days in the past."""
    log = AuditLog(path=tmp_path / "audit.jsonl", clock=lambda: NOW)
    scanner, _ = scanner_with(feed(trump_mirror_ttox=[TTOX_FORMAT]), signals_config)
    signal = scanner.poll(force=True)[0]

    from research import ResearchReport
    from risk_gate import AccountState, EquityBuyOrder, LimitExecution, RiskGate, RiskLimits
    from sizing import SizingEngine

    limits = RiskLimits.load()
    gate = RiskGate(
        limits,
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
        lambda: NOW,
    )
    report = ResearchReport.model_validate({**REPORT, "tickers": ["X"]})
    proposal = SizingEngine(limits).propose_equity(report, Decimal("90000"))
    decision = gate.submit(
        EquityBuyOrder(
            symbol="X", quantity=10, execution=LimitExecution(limit_price=Decimal("25"))
        )
    )
    log._clock = lambda: NOW - timedelta(days=delivered_days_ago)
    log.record_decision(signal, report, proposal, decision)
    log._clock = lambda: NOW
    return log


def test_a_recent_delivery_is_not_silence(tmp_path, signals_config):
    log = silence_fixture(tmp_path, signals_config, delivered_days_ago=1)
    messages = mirror_silence(log, signals_config, NOW)
    # ttox delivered yesterday — quiet; tdp has never delivered, baselined at the
    # first record (also yesterday) — also under the 2-trading-day threshold.
    assert messages == []


def test_a_silent_mirror_is_flagged_for_a_human(tmp_path, signals_config):
    """NOW is Monday Aug 17; a delivery on Tue Aug 11 is 4 trading days of silence
    (Wed, Thu, Fri, Mon)."""
    log = silence_fixture(tmp_path, signals_config, delivered_days_ago=6)
    messages = mirror_silence(log, signals_config, NOW)

    silent = [m for m in messages if "trump_mirror_ttox" in m]
    assert len(silent) == 1
    assert "silent for 4 trading days" in silent[0]
    assert "a human should check which" in silent[0]
    # The never-delivered mirror is flagged too, against the same baseline.
    assert any("trump_mirror_tdp" in m and "NEVER delivered" in m for m in messages)


def test_a_weekend_gap_is_not_silence(tmp_path, signals_config):
    """NOW is Monday Aug 17; Friday's delivery is 3 calendar days but only 1
    trading day back — a weekend is not a dead bot."""
    log = silence_fixture(tmp_path, signals_config, delivered_days_ago=3)
    messages = mirror_silence(log, signals_config, NOW)
    assert [m for m in messages if "trump_mirror_ttox" in m] == []


def test_an_empty_log_measures_no_silence(tmp_path, signals_config):
    log = AuditLog(path=tmp_path / "empty.jsonl", clock=lambda: NOW)
    assert mirror_silence(log, signals_config, NOW) == []


# ================================================================================
# Content-key normalisation
# ================================================================================


def test_the_two_real_mirror_formats_normalise_to_one_key():
    assert mirror_content_key(TDP_FORMAT) == mirror_content_key(TTOX_FORMAT)


def test_links_and_invisible_padding_do_not_split_identities():
    with_link = ORIGINAL_TEXT + " https://t.co/AbC123"
    padded = "​" + ORIGINAL_TEXT + "⁠"
    assert mirror_content_key(with_link) == mirror_content_key(padded)


def test_genuinely_different_posts_keep_different_keys():
    assert mirror_content_key(ORIGINAL_TEXT) != mirror_content_key(
        "A 10% tariff on all foreign steel."
    )


# ================================================================================
# Mirror integrity (2026-08-20): no marker, no principal signal
# ================================================================================

#: Real formats, verified against live posts 2026-08-20. ttox's stamp has a space
#: after the paren and trailing zero-width padding; tdp's genuine relays carry the
#: header, and everything else it posts is its own commentary.
REAL_TTOX = (
    ORIGINAL_TEXT
    + " \n\n( TS: Aug 19 2026, 6:59 PM ET )\u200b\u200d\u200c"
)
TDP_COMMENTARY = (
    "Nike stock just hit a 12 year low. Nike made the decision 10 years ago to "
    "back Colin Kaepernick. Go woke, go broke."
)


def test_headerless_tdp_content_is_commentary_not_a_principal_signal(signals_config):
    """The finding this fix exists for: 24/24 recent tdp posts were its own
    commentary, two of which reached research at Opus prices. No header -> no
    signal, logged to the MIRROR's credibility record, free."""
    scanner, queue = scanner_with(
        feed(trump_mirror_tdp=[TDP_COMMENTARY]), signals_config
    )
    emitted = scanner.poll(force=True)

    assert emitted == []
    assert queue.drain() == []
    records = scanner.credibility_log.records
    assert len(records) == 1
    assert records[0].source_id == "trump_mirror_tdp"  # the channel, not Trump
    assert "required marker absent" in records[0].reason
    assert TDP_COMMENTARY in records[0].content


def test_headered_tdp_content_passes_through_as_the_principal(signals_config):
    scanner, queue = scanner_with(feed(trump_mirror_tdp=[TDP_FORMAT]), signals_config)
    emitted = scanner.poll(force=True)

    assert len(emitted) == 1
    assert emitted[0].source_id == "trump_posts"
    assert emitted[0].metadata["delivered_by"] == "trump_mirror_tdp"
    assert scanner.credibility_log.records == ()


def test_stampless_ttox_content_is_commentary_too(signals_config):
    scanner, queue = scanner_with(
        feed(trump_mirror_ttox=["My own hot take about the market today."]),
        signals_config,
    )
    assert scanner.poll(force=True) == []
    records = scanner.credibility_log.records
    assert len(records) == 1
    assert records[0].source_id == "trump_mirror_ttox"


def test_the_real_ttox_format_passes_the_marker_and_deduplicates(signals_config):
    """The live '( TS: ... )' spacing passes the marker AND normalises: the same
    Truth via real-format ttox and headered tdp is ONE signal (the spacing bug in
    the old suffix regex broke this on every real post)."""
    scanner, queue = scanner_with(
        feed(trump_mirror_ttox=[REAL_TTOX], trump_mirror_tdp=[TDP_FORMAT]),
        signals_config,
    )
    emitted = scanner.poll(force=True)
    assert len(emitted) == 1  # second delivery deduped against the first
    assert mirror_content_key(REAL_TTOX) == mirror_content_key(TDP_FORMAT)


def test_headerless_content_never_reaches_research(tmp_path, signals_config):
    """Loop-level: the mislabeled commentary that cost ~$4.24 on 2026-08-19 now
    costs nothing — no signal, no budget, no LLM call, no audit decision."""
    llm = RoutingLLM(**{REPORT_TOOL_NAME: structured(REPORT)})
    started = start(
        fetcher=SourceRouter(
            routes={
                "trump_mirror_ttox": feed(),
                "trump_mirror_tdp": feed(trump_mirror_tdp=[TDP_COMMENTARY]),
                "nolimitgains": feed(),
            },
            unbuilt={"trump_posts"},
        ),
        prices=MutablePrices(),
        llm_client=llm,
        adapter=FakeBroker(),
        clock=FakeClock(NOW),
        data_dir=tmp_path,
        limits=__import__("risk_gate").RiskLimits.load(),
        signals_config=signals_config,
        research_config=__import__("research.config", fromlist=["ResearchConfig"]).ResearchConfig.load(),
        orchestrator_config=orchestrator_config(),
        id_factory=counter(),
    )
    report = started.loop.tick()
    assert report.processed == []
    assert report.prefiltered == 0
    assert started.budget.spent == 0
    assert llm.calls == []
    assert list(started.audit.records()) == []
    started.loop.shutdown()


# ================================================================================
# Flag separation: the leaky channel accumulates the record, not the principal
# ================================================================================


def test_a_mirror_delivered_flag_lands_on_the_channel():
    from research.credibility import CredibilityTracker
    from research.reports import ResearchReport
    from test_orchestrator import REPORT as REPORT_PAYLOAD

    tracker = CredibilityTracker()
    flagged = ResearchReport.model_validate(
        {
            **REPORT_PAYLOAD,
            "manipulation_assessment": (
                "Content does not match any post by the principal; probable "
                "mirror commentary mislabeled as a Trump post."
            ),
        }
    )
    tracker.record_report(
        "trump_posts", flagged, delivered_by="trump_mirror_tdp"
    )

    principal = tracker.summary_for("trump_posts")
    channel = tracker.summary_for("trump_mirror_tdp")
    assert principal.manipulation_flags == 0  # clean principal, not penalized
    assert principal.reports_scored == 1
    assert channel.manipulation_flags == 1  # the leaky channel owns the record
    assert channel.reports_scored == 1
    assert channel.recent_manipulation_notes


def test_a_direct_flag_still_lands_on_the_principal():
    from research.credibility import CredibilityTracker
    from research.reports import ResearchReport
    from test_orchestrator import REPORT as REPORT_PAYLOAD

    tracker = CredibilityTracker()
    flagged = ResearchReport.model_validate(
        {
            **REPORT_PAYLOAD,
            "manipulation_assessment": "pump-shaped urgency in the original post",
        }
    )
    tracker.record_report("nolimitgains", flagged)

    summary = tracker.summary_for("nolimitgains")
    assert summary.manipulation_flags == 1
    assert summary.reports_scored == 1


def test_the_channel_record_reaches_the_prompt_context(signals_config):
    from research.credibility import CredibilityTracker
    from research.reports import ResearchReport
    from research.research_pass import ResearchPass
    from test_orchestrator import REPORT as REPORT_PAYLOAD, FakeLLM

    tracker = CredibilityTracker()
    flagged = ResearchReport.model_validate(
        {**REPORT_PAYLOAD, "manipulation_assessment": "mislabeled commentary"}
    )
    tracker.record_report("trump_posts", flagged, delivered_by="trump_mirror_tdp")

    scanner, _ = scanner_with(feed(trump_mirror_tdp=[TDP_FORMAT]), signals_config)
    signal = scanner.poll(force=True)[0]
    llm = FakeLLM(structured(REPORT))
    ResearchPass(llm, credibility=tracker).run(signal)

    prompt = llm.calls[0]["user"]
    assert "DELIVERY CHANNEL RECORD" in prompt
    assert "mislabeled commentary" in prompt
