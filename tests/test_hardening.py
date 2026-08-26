"""Hardening + funnel efficiency (rulings 2026-08-25).

1. Invisible-character sanitization at ingest: content entering any prompt
   contains only what a human reader sees; raw_content keeps the bytes.
2. no_instrument: a nolimitgains forward_call with no extracted ticker is
   commentary, recorded and never researched.
3. bare_link: a theme-matched trump_posts post that is a headline plus a URL
   is not a thesis, and Opus is not paid to read a t.co link.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from audit.records import RejectedStage, SignalSnapshot
from orchestrator.prefilter import ResearchPreFilter
from research.triage import build_triage_prompt
from signals import SignalsConfig
from signals.records import Classification, Priority, Signal, SignalClass, sanitize_invisible
from test_orchestrator import (
    NOW,
    FakeBroker,
    FakeLLM,
    build,
    feed,
    prices_of,
)
from test_prefilter import nolimitgains_signal, trump_signal

ZWSP = "​"
ZWJ = "‍"
ZWNJ = "‌"
WORD_JOINER = "⁠"
BOM = "﻿"
SOFT_HYPHEN = "­"
LRM = "‎"
VS16 = "️"


@pytest.fixture(scope="module")
def signals_config() -> SignalsConfig:
    return SignalsConfig.load()


@pytest.fixture(scope="module")
def prefilter(signals_config) -> ResearchPreFilter:
    return ResearchPreFilter.from_config(signals_config)


def signal(
    content: str,
    source_id: str = "trump_posts",
    classification=None,
    metadata=None,
) -> Signal:
    return Signal(
        signal_id="sig-1",
        source_id=source_id,
        signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc),
        content=content,
        raw_content=content,
        priority=Priority.ELEVATED,
        classification=classification,
        metadata=metadata or {},
    )


# ================================================================================
# 1. Sanitization
# ================================================================================


def test_visible_text_is_preserved_exactly_and_only_invisibles_go():
    laced = f"Tar{ZWSP}iffs{ZWSP}{ZWJ} on st{WORD_JOINER}eel are w{ZWNJ}orking!"
    clean, stripped = sanitize_invisible(laced)
    assert clean == "Tariffs on steel are working!"
    assert stripped == 5


def test_every_invisible_family_is_stripped():
    for char in (ZWSP, ZWJ, ZWNJ, WORD_JOINER, BOM, SOFT_HYPHEN, LRM, VS16):
        clean, stripped = sanitize_invisible(f"a{char}b")
        assert clean == "ab", hex(ord(char))
        assert stripped == 1


def test_clean_text_passes_through_untouched():
    text = "Tariffs are working! $NUE to the moon — 100% conviction."
    clean, stripped = sanitize_invisible(text)
    assert clean == text
    assert stripped == 0


def test_a_signal_sanitizes_its_content_and_keeps_the_raw_bytes():
    laced = f"Tariffs{ZWSP}{ZWSP} are{ZWJ} working!"
    laced_signal = signal(laced)
    assert laced_signal.content == "Tariffs are working!"
    assert laced_signal.raw_content == laced  # verbatim, payload included
    assert laced_signal.sanitized is True
    assert laced_signal.invisible_stripped == 3


def test_a_clean_signal_is_not_flagged():
    clean_signal = signal("Tariffs are working!")
    assert clean_signal.sanitized is False
    assert clean_signal.invisible_stripped == 0


def test_no_prompt_surface_carries_an_invisible_character():
    laced_signal = signal(f"Tar{ZWSP}iffs are wor{ZWJ}king!")
    for rendered in (
        laced_signal.for_research_prompt(),
        build_triage_prompt(laced_signal),
    ):
        assert ZWSP not in rendered and ZWJ not in rendered
        assert "Tariffs are working!" in rendered


def test_the_audit_snapshot_records_the_flag_and_count():
    snapshot = SignalSnapshot.of(signal(f"Tariffs{ZWSP} are working!{ZWSP}"))
    assert snapshot.sanitized is True
    assert snapshot.invisible_stripped == 2
    assert ZWSP in snapshot.raw_content
    assert ZWSP not in snapshot.content


def test_sanitization_holds_through_the_scanner_and_the_decision_record(
    tmp_path, signals_config
):
    """Ingest to audit, end to end: the model saw clean text; the trail kept
    the payload."""
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    laced = (
        f"Tariffs{ZWSP} on{ZWJ} all foreign steel imports are{ZWSP} DOUBLING to "
        "50% effective next week. American Steel will be stronger than ever, "
        "and the mills are coming back to Pennsylvania and Ohio!"
    )
    llm = FakeLLM()
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=llm,
        fetcher=feed(trump_posts=[laced]),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
    )
    result = started.loop.tick().processed[0]
    record = started.audit.trail(result.decision_id).decision
    assert record.signal.sanitized is True
    assert record.signal.invisible_stripped == 3
    assert ZWSP in record.signal.raw_content
    assert ZWSP not in record.signal.content
    for call in llm.calls:  # nothing invisible reached any prompt
        assert ZWSP not in call["user"] and ZWJ not in call["user"]


# ================================================================================
# 2. no_instrument (nolimitgains only)
# ================================================================================

#: Today's fixture: a correct no_position verdict that still cost a pass.
APHORISM = Signal(
    signal_id="nlg-wma",
    source_id="nolimitgains",
    signal_class=SignalClass.CLASS_1_REALTIME,
    observed_at=datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc),
    content="Respect the 200-WMA. It has never been wrong.",
    raw_content="Respect the 200-WMA. It has never been wrong.",
    priority=Priority.ELEVATED,
    classification=Classification.FORWARD_CALL,
    metadata={"tickers": ""},
)


def test_an_instrumentless_forward_call_is_blocked_with_the_reason(prefilter):
    reason = prefilter.missing_instrument(APHORISM)
    assert reason is not None
    assert "names no instrument" in reason


def test_a_tickered_call_passes_through_unchanged(signals_config, prefilter):
    tickered = nolimitgains_signal(
        signals_config, "Loading $NVDA here. Setup is live, entry: 182."
    )
    assert tickered.metadata["tickers"]
    assert prefilter.missing_instrument(tickered) is None
    assert prefilter.skip_reason(tickered) is None


def test_the_rule_is_scoped_to_nolimitgains_alone(signals_config, prefilter):
    """Trump theme posts legitimately trade without tickers via sector effects."""
    themed = trump_signal(
        signals_config, "Tariffs on all foreign steel are DOUBLING next week!"
    )
    assert not (themed.metadata.get("tickers") or "")
    assert prefilter.missing_instrument(themed) is None


def test_the_aphorism_never_reaches_research_through_the_loop(
    tmp_path, signals_config
):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    llm = FakeLLM()
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=llm,
        fetcher=feed(nolimitgains=["Loading up here. Setup is live, entry: 14.20."]),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
    )
    report = started.loop.tick()
    assert report.prefiltered == 1
    assert llm.calls == []  # no triage, no pass, no spend

    rejection = started.audit.stage_rejections()[0]
    assert rejection.stage is RejectedStage.PRE_FILTER
    assert rejection.code == "no_instrument"


# ================================================================================
# 3. bare_link (trump_posts)
# ================================================================================

#: Today's fixture: theme-matched, ticker-less, a headline and a t.co link.
BARE_LINK = (
    "Big progress on Oil with our Middle East and Gulf partners! "
    "https://t.co/AbCdEf1234"
)

SUBSTANTIVE = (
    "Tariffs on all foreign steel and aluminum are DOUBLING to 50% effective "
    "June 4th. American Steel and American Aluminum will be stronger than ever "
    "before, and the jobs are coming back to Pennsylvania and Ohio! "
    "https://t.co/AbCdEf1234"
)


def test_the_bare_link_fixture_is_filtered(signals_config, prefilter):
    bare = trump_signal(signals_config, BARE_LINK)
    reason = prefilter.bare_link(bare)
    assert reason is not None
    assert "not a thesis" in reason


def test_a_substantive_theme_post_over_the_threshold_still_researches(
    signals_config, prefilter
):
    substantive = trump_signal(signals_config, SUBSTANTIVE)
    assert prefilter.bare_link(substantive) is None
    assert prefilter.skip_reason(substantive) is None


def test_an_unthemed_short_link_post_belongs_to_the_theme_rule(
    signals_config, prefilter
):
    """bare_link names its reason precisely; it does not annex the theme rule."""
    unthemed = trump_signal(
        signals_config, "What a wonderful evening! https://t.co/AbCdEf1234"
    )
    assert prefilter.bare_link(unthemed) is None
    assert prefilter.skip_reason(unthemed) is not None


def test_a_tickered_short_link_post_is_not_bare(signals_config, prefilter):
    tickered = trump_signal(signals_config, "$NUE! https://t.co/AbCdEf1234")
    assert prefilter.bare_link(tickered) is None


def test_the_bare_link_rejection_is_coded_through_the_loop(
    tmp_path, signals_config
):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    llm = FakeLLM()
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=llm,
        fetcher=feed(trump_posts=[BARE_LINK]),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
    )
    report = started.loop.tick()
    assert report.prefiltered == 1
    assert llm.calls == []

    rejection = started.audit.stage_rejections()[0]
    assert rejection.code == "bare_link"
    assert "not a thesis" in rejection.message


# ================================================================================
# Breadth round 1 (2026-08-25): per-member credibility, source caps, UW config
# ================================================================================


def test_outcomes_credit_the_member_not_the_firehose(tmp_path):
    """A congressional win accrues to congressional_disclosures/<member>."""
    from datetime import datetime, timezone
    from decimal import Decimal

    from audit.log import AuditLog
    from risk_gate import (
        AccountState,
        EquityBuyOrder,
        LimitExecution,
        RiskGate,
        RiskLimits,
    )

    class RecordingCredibility:
        def __init__(self):
            self.outcomes = []

        def record_outcome(self, source_id, *, won):
            self.outcomes.append((source_id, won))

    laced = signal(
        "Purchase NUE $50,001 - $100,000",
        source_id="congressional_disclosures",
        metadata={"credibility_key": "congressional_disclosures/Nancy Pelosi"},
    )
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    gate = RiskGate(
        RiskLimits.load(),
        AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")),
    )
    from test_orchestrator import REPORT
    from research.reports import ResearchReport
    from sizing import SizingEngine

    report = ResearchReport.model_validate({**REPORT, "tickers": ["NUE"]})
    proposal = SizingEngine(RiskLimits.load()).propose_equity(
        report, Decimal("100000")
    )
    decision = gate.submit(
        EquityBuyOrder(
            symbol="NUE",
            quantity=10,
            execution=LimitExecution(limit_price=Decimal("140.00")),
        )
    )
    record = audit.record_decision(laced, report, proposal, decision)
    audit.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140"))

    credibility = RecordingCredibility()
    audit.record_outcome(
        record.decision_id, Decimal("50"), credibility=credibility
    )
    assert credibility.outcomes == [
        ("congressional_disclosures/Nancy Pelosi", True)
    ]


def test_research_passes_by_source_seed_the_caps_across_restarts(tmp_path):
    from datetime import datetime, timezone

    from audit.log import AuditLog

    audit = AuditLog(path=tmp_path / "audit.jsonl")
    from audit.records import RejectedStage

    uw = signal("UW callout", source_id="unusual_whales")
    audit.record_stage_rejection("d1", RejectedStage.RESEARCH, "no_position", "x", uw)
    audit.record_stage_rejection("d2", RejectedStage.SIZING, "below_floor", "x", uw)
    # These two spent nothing and must not count toward the cap.
    audit.record_stage_rejection("d3", RejectedStage.PRE_FILTER, "source_cap", "x", uw)
    audit.record_stage_rejection("d4", RejectedStage.TRIAGE, "triage", "x", uw)

    today = datetime.now(timezone.utc).date()
    counts = audit.research_passes_by_source_on(today)
    assert counts == {"unusual_whales": 2}


def test_the_source_cap_stops_the_fourth_pass_with_the_code(
    tmp_path, signals_config
):
    """unusual_whales caps at 3/day: passes 1-3 research, pass 4 is source_cap."""
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    calls = [
        f"Loading $NVDA here. Setup is live, entry: {180 + n}." for n in range(4)
    ]
    llm = FakeLLM()
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=llm,
        fetcher=feed(unusual_whales=calls),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
    )
    report = started.loop.tick()
    assert len(report.processed) == 3
    assert report.prefiltered == 1

    capped = [
        r for r in started.audit.stage_rejections() if r.code == "source_cap"
    ]
    assert len(capped) == 1
    assert "3-pass daily cap" in capped[0].message


def test_unusual_whales_ships_with_the_governance_it_was_ruled_in_with(
    signals_config,
):
    source = signals_config.source("class_1", "unusual_whales")
    assert source.require_instrument is True
    assert source.research_tier == "class_2"
    assert source.daily_research_cap == 3
    assert source.copy_trade is False
    assert source.treatment == "thesis_input_only"
    congressional = signals_config.source("class_2", "congressional_disclosures")
    assert congressional.daily_research_cap == 5
    assert congressional.watchlist == ()


# ================================================================================
# Breadth round 2 (2026-08-25): probation, citrini at class-2 cadence, 13F round 1
# ================================================================================


def test_probation_short_circuits_sizing_with_the_code(tmp_path, signals_config):
    """optionshawk researches and accrues credibility, but a would-be trade
    stops at sizing with code probation — zero dollars ride on the source."""
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    broker = FakeBroker()
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=FakeLLM(),  # confidence 71: this WOULD have traded
        fetcher=feed(
            optionshawk=["Loading $NUE here. Setup is live, entry: 140.20."]
        ),
        prices=prices_of(NUE="140.00"),
        broker=broker,
    )
    result = started.loop.tick().processed[0]
    assert result.traded is False
    rejection = result.rejection
    assert rejection is not None
    assert rejection.stage == RejectedStage.SIZING
    assert rejection.code == "probation"
    # The research and the proposed size ride on the record — that is the
    # would-have-traded evidence the promote-or-drop review counts.
    assert rejection.research is not None
    assert rejection.sizing is not None
    assert "would have deployed" in rejection.message.lower()
    assert broker.submitted == []


def test_probation_never_masks_an_honest_no(tmp_path, signals_config):
    """A below-floor verdict from a probation source keeps its own code —
    probation intercepts only what WOULD have traded."""
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    from test_orchestrator import REPORT, structured

    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=FakeLLM(structured({**REPORT, "confidence": 40})),
        fetcher=feed(
            optionshawk=["Loading $NUE here. Setup is live, entry: 140.20."]
        ),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
    )
    result = started.loop.tick().processed[0]
    assert result.rejection is not None
    assert result.rejection.code == "below_floor"


def test_citrini_classifies_at_class_2_cadence(signals_config):
    """The class-2 scanner routes classification-flagged sources through the
    trade-call path: retrospectives to the credibility log, forward calls
    enqueued as class-2 signals with the scanner's own ticker extraction."""
    from fixture_posts import PURE_RETROSPECTIVE
    from signals.records import SignalQueue
    from signals.scanners import Class2CongressionalScanner, RawItem

    posts = [
        PURE_RETROSPECTIVE,
        "Loading $NUE here. Setup is live, entry: 140.20.",
    ]

    def fetcher(source):
        if source.id != "citrini":
            return []
        return [
            RawItem(external_id=f"c-{index}", content=content, published_at=NOW)
            for index, content in enumerate(posts)
        ]

    queue = SignalQueue()
    scanner = Class2CongressionalScanner(
        signals_config.klass("class_2"), fetcher, queue
    )
    emitted = scanner.poll(force=True)

    assert len(emitted) == 1
    forward = emitted[0]
    assert forward.source_id == "citrini"
    assert forward.signal_class == SignalClass.CLASS_2_MOMENTUM
    assert forward.classification == Classification.FORWARD_CALL
    assert "NUE" in forward.metadata["tickers"]
    assert len(scanner.credibility_log) == 1  # the brag: tracked, never traded


def test_citrini_trades_end_to_end_at_class_2(tmp_path, signals_config):
    """Loop-level: a citrini forward call flows scanner -> classification ->
    research (priced_in mandatory at class 2) -> order, on the real config."""
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    from test_orchestrator import REPORT, structured

    broker = FakeBroker()
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=FakeLLM(
            structured(
                {
                    **REPORT,
                    "priced_in_analysis": "NUE up 2% since the post; thesis "
                    "not fully priced.",
                }
            )
        ),
        fetcher=feed(citrini=["Loading $NUE here. Setup is live, entry: 140.20."]),
        prices=prices_of(NUE="140.00"),
        broker=broker,
    )
    result = started.loop.tick().processed[0]
    assert result.traded is True
    assert len(broker.submitted) == 1


def test_a_class_2_trade_call_prompt_speaks_call_language_not_disclosure():
    """citrini prompts must not claim the post is a congressional disclosure;
    disclosures keep their guidance, and both demand priced_in_analysis."""
    from research.prompts import build_user_prompt

    def class_2_signal(classification):
        return Signal(
            signal_id="sig-c2",
            source_id="citrini" if classification else "congressional_disclosures",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=NOW,
            content="Loading $NUE here. Setup is live, entry: 140.20.",
            raw_content="raw",
            priority=Priority.ROUTINE,
            classification=classification,
            metadata={"tickers": "NUE"},
        )

    call_prompt = build_user_prompt(class_2_signal(Classification.FORWARD_CALL))
    assert "congressional disclosure" not in call_prompt
    assert "polled hourly" in call_prompt
    assert "priced_in_analysis is MANDATORY" in call_prompt

    disclosure_prompt = build_user_prompt(class_2_signal(None))
    assert "congressional disclosure" in disclosure_prompt
    assert "priced_in_analysis is MANDATORY" in disclosure_prompt


def test_round_two_sources_ship_with_their_ruled_governance(signals_config):
    citrini = signals_config.source("class_2", "citrini")
    assert citrini.require_instrument is True
    assert citrini.research_tier == "class_1"  # prose caller: Opus verification
    assert citrini.daily_research_cap == 3
    assert citrini.copy_trade is False
    assert citrini.treatment == "thesis_input_only"
    assert citrini.probation is False

    hawk = signals_config.source("class_1", "optionshawk")
    assert hawk.probation is True  # the first probation source
    assert hawk.require_instrument is True
    assert hawk.research_tier == "class_1"
    assert hawk.daily_research_cap == 3
    assert hawk.copy_trade is False

    # Sources never ruled onto probation must not drift onto it.
    assert signals_config.source("class_1", "nolimitgains").probation is False
    assert signals_config.source("class_1", "unusual_whales").probation is False


def test_13f_round_one_watchlist_is_exactly_the_ruling(signals_config):
    source = signals_config.source("class_3", "form_13f")
    funds = [entry["fund"] for entry in source.watchlist]
    assert funds == [
        "Situational Awareness",
        "Appaloosa",
        "Altimeter Capital Management",
        "Pershing Square Capital Management",
        "TCI Fund Management",
    ]
    # Duquesne deferred (theme-cluster mode pending); Scion deregistered.
    assert not any("Duquesne" in fund or "Scion" in fund for fund in funds)


# ================================================================================
# Day-one fixes (rulings 2026-08-26): cap unseal, staleness, lookback, CLASSIFY
# ================================================================================


def test_source_cap_rejections_do_not_seal_the_signal(tmp_path):
    """The cap must never permanently discard signals it didn't pay to
    evaluate: a source_cap rejection is excluded from dedup seeding, so the
    signal re-emits at the next startup. Paid-for verdicts still seal."""
    from audit.log import AuditLog

    def with_external_id(external_id, content):
        return Signal(
            signal_id=f"sig-{external_id}",
            source_id="congressional_disclosures",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=NOW,
            content=content,
            raw_content=content,
            priority=Priority.ROUTINE,
            external_id=external_id,
            metadata={},
        )

    audit = AuditLog(path=tmp_path / "audit.jsonl")
    audit.record_stage_rejection(
        "d1",
        RejectedStage.PRE_FILTER,
        "source_cap",
        "capped",
        with_external_id("capped-row", "Purchase NUE"),
    )
    audit.record_stage_rejection(
        "d2",
        RejectedStage.PRE_FILTER,
        "pre_filter",
        "stale",
        with_external_id("stale-row", "Purchase AMD"),
    )
    audit.record_stage_rejection(
        "d3",
        RejectedStage.RESEARCH,
        "no_position",
        "priced in",
        with_external_id("researched-row", "Purchase INTC"),
    )

    seen = audit.researched_external_ids()
    assert ("congressional_disclosures", "capped-row") not in seen
    assert ("congressional_disclosures", "stale-row") in seen
    assert ("congressional_disclosures", "researched-row") in seen


def test_stale_reported_disclosures_die_at_the_prefilter(prefilter):
    """Disclosure->today staleness (max_report_age_days: 14), distinct from
    the trade->disclosure lag rule. Fresh passes; missing date fails open."""
    from datetime import datetime as dt, timezone as tz

    now = dt(2026, 8, 26, 14, 0, tzinfo=tz.utc)

    def disclosure(report_date):
        metadata = {"amount_range": "$50,001 - $100,000"}
        if report_date:
            metadata["report_date"] = report_date
        return signal(
            "Purchase NUE $50,001 - $100,000",
            source_id="congressional_disclosures",
            metadata=metadata,
        )

    stale = prefilter.skip_reason(disclosure("2026-07-15"), now=now)  # 42d old
    assert stale is not None
    assert "report-staleness" in stale

    assert prefilter.skip_reason(disclosure("2026-08-20"), now=now) is None  # 6d
    assert prefilter.skip_reason(disclosure("2026-08-12"), now=now) is None  # 14d, boundary passes
    assert prefilter.skip_reason(disclosure(None), now=now) is None  # fails open


def test_the_lookback_is_the_session_gap_clamped():
    from datetime import datetime as dt, timedelta, timezone as tz

    from orchestrator.ops import first_poll_lookback_seconds

    now = dt(2026, 8, 26, 13, 15, tzinfo=tz.utc)
    # Fresh data directory: no earlier session to be continuous with -> cap.
    assert first_poll_lookback_seconds(None, now) == 86400
    # Mid-session bounce: floored at the old 15 minutes.
    assert first_poll_lookback_seconds(now - timedelta(minutes=5), now) == 900
    # Overnight: the actual gap.
    assert (
        first_poll_lookback_seconds(now - timedelta(hours=17, minutes=15), now)
        == 62100
    )
    # Weekend: capped at 24h — X bills per post returned.
    assert first_poll_lookback_seconds(now - timedelta(days=3), now) == 86400


def test_classification_outcomes_reach_the_sink_and_other_leaves_a_trace(
    signals_config,
):
    """One CLASSIFY line per source per poll, and an "other" post lands in the
    credibility log — fetched-but-discarded is reconstructable after the fact."""
    from fixture_posts import PURE_FORWARD_CALL, PURE_RETROSPECTIVE
    from signals.records import SignalQueue
    from signals.scanners import Class1RealtimeScanner, RawItem

    posts = [
        PURE_FORWARD_CALL,
        PURE_RETROSPECTIVE,
        "Good morning everyone. Coffee first, charts later.",
    ]

    def fetcher(source):
        if source.id != "nolimitgains":
            return []
        return [
            RawItem(external_id=f"p-{index}", content=content, published_at=NOW)
            for index, content in enumerate(posts)
        ]

    lines = []
    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"),
        fetcher,
        SignalQueue(),
        None,
        None,
        lines.append,
    )
    emitted = scanner.poll(force=True)

    assert len(emitted) == 1  # the forward call
    assert lines == ["nolimitgains forward_call=1 other=1 retrospective=1"]
    reasons = [entry.reason for entry in scanner.credibility_log.records]
    assert any(reason.startswith("classified other") for reason in reasons)


def test_the_credibility_log_persists_when_given_a_path(tmp_path):
    import json as jsonlib
    from datetime import datetime as dt, timezone as tz

    from signals.records import CredibilityLog, CredibilityRecord

    path = tmp_path / "credibility.jsonl"
    log = CredibilityLog(path=path)
    log.record(
        CredibilityRecord(
            source_id="unusual_whales",
            observed_at=dt(2026, 8, 26, 14, 0, tzinfo=tz.utc),
            content="a data callout",
            external_id="post-1",
            reason="classified other: no actionable or historical segments",
        )
    )
    log.record(
        CredibilityRecord(
            source_id="nolimitgains",
            observed_at=dt(2026, 8, 26, 14, 1, tzinfo=tz.utc),
            content="brag",
            external_id="post-2",
            reason="past_tense",
        )
    )

    rows = [jsonlib.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["source_id"] for row in rows] == ["unusual_whales", "nolimitgains"]
    assert rows[0]["content"] == "a data callout"
    assert rows[0]["reason"].startswith("classified other")


def test_the_staleness_threshold_ships_as_ruled(signals_config):
    source = signals_config.source("class_2", "congressional_disclosures")
    assert source.prefilter is not None
    assert source.prefilter.max_report_age_days == 14
