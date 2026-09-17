"""Class 1 staleness (human ruling 2026-09-16).

The claims under test: every Class 1 prompt renders the post's own timestamp
and its age at observation; a Class 1 signal observed 30+ minutes after it was
posted carries the lagged-class measurement framing and a MANDATORY
priced_in_analysis (a report without one is rejected, never traded); under 30
minutes the fresh path is byte-identical to before; an unknown age reads as
fresh; the scanner stamps published_at on every signal; the golden set grades
the stale relay on measurement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.golden import GoldenCase, grade, load_cases
from research.prompts import (
    CLASS1_STALE_AFTER,
    build_user_prompt,
    class1_is_stale,
    signal_age,
)
from research.reports import ResearchReport, ResearchRejection, ResearchRejectionCode
from research.research_pass import ResearchPass
from signals.records import Priority, Signal, SignalClass, SignalQueue
from signals.scanners import Class1RealtimeScanner, RawItem
from test_orchestrator import REPORT, FakeLLM, structured

import pytest


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()

OBSERVED = datetime(2026, 9, 15, 1, 7, 2, tzinfo=timezone.utc)
POST = "Ukraine has agreed not to hit Russian Energy targets. ( TS: Sep 14 2026, 11:05 AM ET )"


def class1(age: timedelta | None, **metadata) -> Signal:
    meta = {"tickers": "", **metadata}
    if age is not None:
        meta["created_at"] = (OBSERVED - age).isoformat().replace("+00:00", "Z")
    return Signal(
        signal_id="s-1",
        source_id="trump_posts",
        signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=OBSERVED,
        content=POST,
        raw_content=POST,
        priority=Priority.ELEVATED,
        external_id="p-1",
        metadata=meta,
    )


def test_the_prompt_renders_when_the_post_was_said_and_how_old_it_is():
    prompt = build_user_prompt(class1(timedelta(hours=10)))
    assert "- posted at: 2026-09-14T15:07:02+00:00 (age at observation: 10h 00m" in prompt
    assert "STALE for a real-time signal" in prompt
    assert "priced_in_analysis is MANDATORY" in prompt
    assert "Decline for DEMONSTRATED priced-in movement, never for elapsed time" in prompt
    assert "priced_in_analysis may be null" not in prompt


def test_a_fresh_post_keeps_the_fresh_framing():
    fresh = build_user_prompt(class1(timedelta(minutes=2)))
    assert "- posted at: " in fresh and "age at observation: 2 min)" in fresh
    assert "STALE" not in fresh
    assert "priced_in_analysis may be null" in fresh
    assert not class1_is_stale(class1(timedelta(minutes=29, seconds=59)))
    assert class1_is_stale(class1(CLASS1_STALE_AFTER))  # inclusive: 30 min is stale


def test_an_unknown_age_is_stated_and_reads_as_fresh():
    signal = class1(None)
    assert signal_age(signal) is None and not class1_is_stale(signal)
    prompt = build_user_prompt(signal)
    assert "- posted at: not provided by the scanner (age unknown)" in prompt
    assert "priced_in_analysis may be null" in prompt


def test_lagged_classes_are_untouched():
    lagged = Signal(
        signal_id="s-2", source_id="congressional_disclosures",
        signal_class=SignalClass.CLASS_2_MOMENTUM, observed_at=OBSERVED,
        content="x", raw_content="x", priority=Priority.ROUTINE,
        metadata={"created_at": (OBSERVED - timedelta(days=3)).isoformat()},
    )
    assert not class1_is_stale(lagged)
    assert "- posted at:" not in build_user_prompt(lagged)


def test_a_stale_relay_without_the_analysis_is_rejected_not_traded():
    payload = {**REPORT, "priced_in_analysis": None}
    pass_ = ResearchPass(FakeLLM(structured(payload)))
    outcome = pass_.run(class1(timedelta(hours=10)))
    assert isinstance(outcome, ResearchRejection)
    assert outcome.code is ResearchRejectionCode.MISSING_PRICED_IN_ANALYSIS
    assert "stale past 0:30:00" in outcome.message


def test_a_stale_relay_with_the_analysis_and_a_fresh_one_without_are_accepted():
    with_analysis = {**REPORT, "priced_in_analysis": "XLE +1.8% since the post; diesel cracks unchanged."}
    assert isinstance(ResearchPass(FakeLLM(structured(with_analysis))).run(class1(timedelta(hours=10))), ResearchReport)
    without = {**REPORT, "priced_in_analysis": None}
    assert isinstance(ResearchPass(FakeLLM(structured(without))).run(class1(timedelta(minutes=5))), ResearchReport)


def test_the_scanner_stamps_the_items_own_timestamp(signals_config):
    posted = OBSERVED - timedelta(hours=10)
    item = RawItem(external_id="p-9", content="Tariffs on all steel imports start Monday. $NUE", published_at=posted)
    queue = SignalQueue()
    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"),
        lambda source: [item] if source.id == "trump_posts" else [],
        queue,
        clock=lambda: OBSERVED,
    )
    emitted = [s for s in scanner.poll(force=True) if s.source_id == "trump_posts"]
    assert len(emitted) == 1
    signal = emitted[0]
    assert signal.metadata["published_at"] == posted.isoformat()
    assert signal.observed_at == OBSERVED  # the poll time, as before
    assert signal_age(signal) == timedelta(hours=10) and class1_is_stale(signal)
    # A fetcher's own created_at (the X fetcher) is left alone.
    x_item = RawItem(external_id="p-10", content="Buying $NUE", published_at=posted, fields={"created_at": "2026-09-14T15:07:02.000Z"})
    scanner2 = Class1RealtimeScanner(
        signals_config.klass("class_1"),
        lambda source: [x_item] if source.id == "trump_posts" else [],
        SignalQueue(), clock=lambda: OBSERVED,
    )
    stamped = [s for s in scanner2.poll(force=True) if s.source_id == "trump_posts"][0]
    assert stamped.metadata["created_at"] == "2026-09-14T15:07:02.000Z"
    assert stamped.metadata["published_at"] == posted.isoformat()


def test_the_golden_set_grades_the_stale_relay_on_measurement():
    case = [c for c in load_cases() if c.name == "trump-energy-relay-read-10h-later"][0]
    assert case.expect_priced_in is True
    signal = case.signal(datetime(2026, 9, 16, tzinfo=timezone.utc))
    assert signal.observed_at == OBSERVED  # frozen observation moment, 10h after the post
    assert class1_is_stale(signal)
    prompt = build_user_prompt(signal)
    assert "age at observation: 10h 00m" in prompt and "theme -> ETF proposal" in prompt

    def report(analysis):
        return ResearchReport.model_validate({**REPORT, "tickers": ["XLE"], "direction": "no_position", "target_price": None, "priced_in_analysis": analysis})

    assert not grade(case, report(None), None).passed
    assert not grade(case, report("Probably already priced in by now."), None).passed
    assert grade(case, report("XLE +1.4% and Brent -2.1% since the 15:07Z post; the diesel claim is already in the tape."), None).passed
