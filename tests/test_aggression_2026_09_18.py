"""AGGRESSION RULING 2026-09-18, step 1 (prefilters, horizons, class caps).

The claims under test: ambiguous tense admits behind the realized-P&L guard
and only where the source says so; the bare-link floor is 60; the widened
theme list admits policy posts the old one dropped; every Class 1 prompt (and
no other) carries the fast-class horizon guidance; a Class 1 position's leash
clamps into the fast-class bounds (weeks floor 7) while Class 2/3 keep 14; the
combined class caps stop the pool's next pass with a precise code and never
touch the per-source caps or the review reserve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from fixture_posts import AMBIGUOUS_PAST_TENSE, PURE_FORWARD_CALL
from orchestrator.config import OrchestratorConfig, ResearchClassCaps
from research.prompts import FAST_CLASS_HORIZON_GUIDANCE, build_user_prompt
from signals import Classification, classify_post
from signals.records import Priority, Signal, SignalClass, SignalQueue
from signals.scanners import Class1RealtimeScanner
from test_orchestrator import (
    FakeBroker,
    FakeLLM,
    build,
    feed,
    orchestrator_config,
    prices_of,
)

NOW = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def signals_config():
    from signals import SignalsConfig

    return SignalsConfig.load()


# ================================================================================
# Classification: ambiguous tense admits, the P&L guard does not
# ================================================================================

BARE_PAST_TENSE = "$NVDA was strong into the close, 180 is the level that matters here."


def test_bare_past_tense_is_a_forward_call_only_where_the_source_says_so():
    admitted = classify_post(BARE_PAST_TENSE, default_when_ambiguous=Classification.FORWARD_CALL)
    assert admitted.label is Classification.FORWARD_CALL and admitted.is_actionable
    assert admitted.tickers == ("NVDA",)
    assert "ambiguous tense, admitted by the source's default" in admitted.markers
    # The function-level default is unchanged (Constraint #6): a caller that
    # states nothing gets the 2026-08-18 behaviour.
    assert classify_post(BARE_PAST_TENSE).label is Classification.RETROSPECTIVE


@pytest.mark.parametrize(
    "post",
    [
        "Was in $SOFI calls, made 40% on them this morning",
        "$OKTA +22% on results, hope you were in",
        "Made $2,400 on $PLTR puts, easy money was there",
        "$TENB index add was the leak of the year lol, nice for a few K win",
        "$AMD from 118 to 131, that was the move",
        "Took profits on $NVDA here, was a great run",
        "$QQQ $20M of put selling into tech reports was a great tell!",
        "This was the $NVDA earnings preview and trade for members, solid move",
        "3x on the $TSLA weeklies, we were early",
        "P/L for the week attached, $META was the big one",
        "Caught the $SMCI bottom yesterday, told you",
        AMBIGUOUS_PAST_TENSE,  # "That $META trade was beautiful. Nice one."
    ],
)
def test_the_realized_pnl_guard_discards_results_whatever_the_tense(post):
    result = classify_post(post, default_when_ambiguous=Classification.FORWARD_CALL)
    assert result.label is Classification.RETROSPECTIVE, (post, result.markers)
    assert not result.is_actionable


def test_a_live_call_with_a_leverage_multiple_is_not_a_result():
    result = classify_post(
        "Buying $NVDA 3x leveraged here, entry: 180", default_when_ambiguous=Classification.FORWARD_CALL
    )
    assert result.label is Classification.FORWARD_CALL


def test_the_scanner_passes_the_sources_rule(signals_config):
    from signals.records import CredibilityLog

    log = CredibilityLog()
    scanner = Class1RealtimeScanner(
        signals_config.klass("class_1"),
        feed(nolimitgains=[BARE_PAST_TENSE, AMBIGUOUS_PAST_TENSE, PURE_FORWARD_CALL]),
        SignalQueue(),
        clock=lambda: NOW,
        credibility_log=log,
    )
    emitted = [s for s in scanner.poll(force=True) if s.source_id == "nolimitgains"]
    assert [s.content for s in emitted] == [BARE_PAST_TENSE, PURE_FORWARD_CALL]
    # The celebrating post went to the credibility log, not to research.
    assert any("META" in r.content for r in log.records)
    for source in signals_config.klass("class_1").sources:
        if source.classification is not None:
            assert source.classification.default_when_ambiguous == "forward_call"


def test_the_default_must_be_a_configured_label():
    from signals.config import ClassificationRules

    with pytest.raises(ValueError):
        ClassificationRules(
            required_before_research_pass=True,
            labels=("forward_call", "retrospective", "other"),
            default_when_ambiguous="maybe",
            when_in_doubt="x",
        )


# ================================================================================
# Prefilter: bare-link 60, the widened themes
# ================================================================================


def test_the_bare_link_floor_is_60_and_the_theme_list_is_wide(signals_config):
    from orchestrator.prefilter import ResearchPreFilter
    from test_prefilter import trump_signal

    trump = signals_config.klass("class_1").sources[0]
    assert trump.id == "trump_posts" and trump.bare_link_min_chars == 60
    assert len(trump.research_prefilter_themes) >= 120
    prefilter = ResearchPreFilter.from_config(signals_config)
    # A 61-char themed headline researches now; it was bare_link at 120.
    headline = "White House to Announce New Drug Pricing Deals With Biotechs: https://t.co/x"
    assert prefilter.skip_reason(trump_signal(signals_config, headline)) is None
    # Under 60 is still a bare link.
    short = "Big progress on Oil with our Middle East and Gulf partners! https://t.co/x"
    assert prefilter.bare_link(trump_signal(signals_config, short)) is not None
    # Themes the old list dropped, now researched.
    for post in (
        "Medicare drug pricing deals are coming, the insurers have been ripping off Americans for decades and it ends now!",
        "The Border is CLOSED. Deportations of criminal illegal migrants are at record levels and the Cartels are finished.",
        "Powell should have cut months ago. The Federal Reserve is the only thing holding back the greatest economy ever.",
    ):
        assert prefilter.skip_reason(trump_signal(signals_config, post)) is None, post
    # Chit-chat still dies.
    chit = "Happy Birthday to the great Elvis Presley. Nobody sings like Elvis! What a wonderful voice he had."
    assert prefilter.skip_reason(trump_signal(signals_config, chit)) is not None


# ================================================================================
# Prompt: fast-class horizon on Class 1 only
# ================================================================================


def signal_of(klass, source_id, content="Tariffs on all steel imports start Monday. $NUE"):
    return Signal(
        signal_id="s-1",
        source_id=source_id,
        signal_class=klass,
        observed_at=NOW,
        content=content,
        raw_content=content,
        priority=Priority.for_class(klass),
        external_id="p-1",
        metadata={"tickers": "NUE", "created_at": NOW.isoformat()},
    )


def test_every_class_1_prompt_carries_the_horizon_guidance_and_no_other_class_does():
    fresh = build_user_prompt(signal_of(SignalClass.CLASS_1_REALTIME, "trump_posts"))
    assert FAST_CLASS_HORIZON_GUIDANCE in fresh
    assert "months horizon on a Class 1 signal needs explicit justification" in fresh
    eight_k = signal_of(SignalClass.CLASS_1_REALTIME, "form_8k")
    eight_k.metadata["form"] = "8-K"
    assert FAST_CLASS_HORIZON_GUIDANCE in build_user_prompt(eight_k)
    for klass, source in (
        (SignalClass.CLASS_2_MOMENTUM, "congressional_disclosures"),
        (SignalClass.CLASS_3_THESIS, "form_13f"),
    ):
        assert "FAST-CLASS HORIZON" not in build_user_prompt(signal_of(klass, source))


# ================================================================================
# Leash: fast-class bounds for Class 1 positions only
# ================================================================================


def test_class_1_leashes_clamp_into_the_fast_class_bounds():
    config = OrchestratorConfig.load()
    exits = config.exits
    assert exits.fast_class_leash_bounds is not None
    assert exits.leash_bounds_for("weeks", "class_1").floor == 7
    assert exits.leash_bounds_for("weeks", "class_2").floor == 14
    assert exits.leash_bounds_for("weeks", "").floor == 14  # unstamped = pre-ruling
    assert exits.leash_bounds_for("days", "class_1").floor == 3
    assert exits.leash_bounds_for("months", "class_1").floor == 60
    # Without the table, every class uses the ordinary bounds.
    plain = orchestrator_config().exits
    assert plain.fast_class_leash_bounds is None
    assert plain.leash_bounds_for("weeks", "class_1").floor == 14


def test_the_fast_class_fallbacks_are_validated_like_the_others():
    base = orchestrator_config().model_dump(mode="json")
    base["exits"]["fast_class_leash_bounds"] = {
        "days": {"floor": 3, "ceiling": 21},
        "weeks": {"floor": 50, "ceiling": 90},  # the 45-day weeks fallback sits below
        "months": {"floor": 60, "ceiling": 367},
    }
    with pytest.raises(ValueError, match="fast-class"):
        OrchestratorConfig.model_validate(base)


def test_an_opened_class_1_position_is_stamped_and_leashed_fast(tmp_path, signals_config):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits
    from test_orchestrator import REPORT, structured

    # The harness clock is 2026-08-17: a weeks thesis dated two days out.
    report = {**REPORT, "time_horizon": "weeks", "expected_resolution_date": "2026-08-19"}
    config = orchestrator_config()
    base = config.model_dump(mode="json")
    base["exits"]["fast_class_leash_bounds"] = {
        "days": {"floor": 3, "ceiling": 21},
        "weeks": {"floor": 7, "ceiling": 90},
        "months": {"floor": 60, "ceiling": 367},
    }
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=FakeLLM(structured(report)),
        fetcher=feed(nolimitgains=[PURE_FORWARD_CALL]),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
        config=OrchestratorConfig.model_validate(base),
    )
    started.loop.tick()
    positions = list(started.exits.tracked)
    assert len(positions) == 1
    position = positions[0]
    assert position.signal_class == "class_1"
    # 4 days to the stated date clamps UP to the fast weeks floor 7, not 14.
    assert position.leash_days == 7


# ================================================================================
# Budget: the class pools
# ================================================================================


def test_class_caps_must_fit_inside_the_entry_ceiling():
    assert ResearchClassCaps(class_1=15, class_2_3=20).bucket("class_3") == "class_2_3"
    live = OrchestratorConfig.load()
    assert live.max_research_passes_per_day == 40
    assert live.review_budget_reserve_fraction == Decimal("0.125")
    assert live.research_class_caps == ResearchClassCaps(class_1=15, class_2_3=20)
    with pytest.raises(ValueError, match="exceed the entry ceiling"):
        orchestrator_config(
            max_research_passes_per_day=40,
            review_budget_reserve_fraction="0.125",
            research_class_caps={"class_1": 16, "class_2_3": 20},
        )


def test_the_class_pool_stops_the_next_pass_with_the_code(tmp_path, signals_config):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    calls = [f"Loading ${symbol} here. Setup is live, entry: {180 + n}." for n, symbol in enumerate(("NVDA", "AMD", "INTC"))]
    started = build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=FakeLLM(),
        fetcher=feed(nolimitgains=calls),
        prices=prices_of(NUE="140.00"),
        broker=FakeBroker(),
        config=orchestrator_config(research_class_caps={"class_1": 2, "class_2_3": 20}),
    )
    report = started.loop.tick()
    assert len(report.processed) == 2
    capped = [r for r in started.audit.stage_rejections() if r.code == "class_cap"]
    assert len(capped) == 1
    assert "class_1 sources have spent their combined 2-pass daily cap" in capped[0].message


def test_the_class_pools_are_seeded_from_the_log_by_source_class(signals_config):
    """A restart cannot reset a pool: the per-source counts the log already
    keeps are folded into the two buckets by each source's configured class;
    an unknown source counts against the Class 2/3 pool (Constraint #6)."""
    from types import SimpleNamespace

    from orchestrator.bootstrap import _class_passes_today

    audit = SimpleNamespace(
        research_passes_by_source_on=lambda day: {
            "trump_posts": 3, "form_8k": 6, "nolimitgains": 1,
            "congressional_disclosures": 5, "form_13f": 2, "mystery_source": 1,
        }
    )
    checks = SimpleNamespace(
        orchestrator_config=orchestrator_config(
            review_budget_reserve_fraction="0.125",
            research_class_caps={"class_1": 15, "class_2_3": 20},
        ),
        signals_config=signals_config,
        audit=audit,
    )
    assert _class_passes_today(checks, NOW.date()) == {"class_1": 10, "class_2_3": 8}
    checks.orchestrator_config = orchestrator_config()
    assert _class_passes_today(checks, NOW.date()) == {}
