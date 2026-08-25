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
