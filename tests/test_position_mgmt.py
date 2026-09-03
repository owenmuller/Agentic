"""Position management (human rulings 2026-09-02, third batch; review layer
revised the same day to the dialectic).

The claims: every review answers the re-underwrite question and records it as
EVIDENCE — a "no" never closes a position by itself; every review must argue
case_for_holding AND case_for_selling (a review missing either is a schema
rejection, i.e. a HOLD); the review's own TRIM verdict sells the configured
fraction once per position on a position in profit and stands as a hold
otherwise; a close argued on a profitable, intact position is still shadowed
by probation; and health renders the management state per position.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from execution.environment import LIVE_CONFIRMATION_VARIABLE
from audit.records import ExitReason, ReviewOutcome
from orchestrator.exits import TrackedPosition
from orchestrator.ops import _fmt_position
from research.exit_review import (
    EXIT_SYSTEM_PROMPT,
    ExitReview,
    PositionUnderReview,
    build_review_prompt,
)
from test_exits import (
    CASES,
    BrokerPosition,
    FakeBroker,
    MutablePrices,  # noqa: F401 - harness re-export
    counter,
    enter_position,
    feed,
    restart_kwargs,
    routing,
    start,
)
from test_orchestrator import FakeClock

PROBATION_NOW = datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def limits():
    from risk_gate import RiskLimits

    return RiskLimits.load()


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    from research.config import ResearchConfig

    return ResearchConfig.load()


#: An honest hold whose re-underwrite fails: the thesis is fine but under
#: today's rules the numbers no longer admit the position — evidence, not a
#: trigger.
STALE_HOLD = {
    "assessment": "Thesis intact but the remaining move no longer clears the entry bar.",
    "invalidation_triggered": False,
    "action": "hold",
    "validity": "intact",
    "progress": "on_track",
    "would_open_today": False,
    "would_open_today_reason": (
        "reward:risk from the current price to the target against the current "
        "stop is ~0.4, far below the 1.5 minimum"
    ),
    **CASES,
    "verdict_reason": (
        "holding wins: the failed re-underwrite is a selection-threshold "
        "difference, not a thesis problem"
    ),
}

#: The review's own trim verdict.
TRIM_VERDICT = {
    "assessment": "About half the expected move has printed; thesis still live.",
    "invalidation_triggered": False,
    "action": "trim",
    "validity": "intact",
    "progress": "on_track",
    "resolution": "partial",
    "would_open_today": True,
    "would_open_today_reason": "still clears the floor and the reward:risk bar",
    **CASES,
    "verdict_reason": "trim: bank half of a partial resolution and let the rest run",
}


# ================================================================================
# The dialectic is enforced by the schema
# ================================================================================


def test_a_review_without_both_cases_is_rejected():
    base = {"assessment": "x", "invalidation_triggered": False, "action": "hold"}
    with pytest.raises(ValidationError):
        ExitReview.model_validate(base)
    with pytest.raises(ValidationError):
        ExitReview.model_validate({**base, **CASES, "case_for_selling": "sell."})
    with pytest.raises(ValidationError):
        ExitReview.model_validate({**base, **CASES, "verdict_reason": "  "})
    assert ExitReview.model_validate({**base, **CASES}).action == "hold"


@pytest.mark.parametrize("would_open,progress", [
    (True, "on_track"), (False, "ahead"), (False, "on_track"), (False, "stalled"),
])
def test_would_open_today_is_evidence_never_a_trigger(would_open, progress):
    review = ExitReview(
        assessment="x",
        invalidation_triggered=False,
        action="hold",
        would_open_today=would_open,
        progress=progress,
        **CASES,
    )
    assert not review.should_close
    assert review.close_contradiction is None


def test_the_reunderwrite_default_cannot_manufacture_a_close():
    review = ExitReview(
        assessment="x", invalidation_triggered=False, action="hold", **CASES
    )
    assert review.would_open_today is True and not review.should_close


def test_a_failed_reunderwrite_is_recorded_and_the_position_holds(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config,
        llm=routing(STALE_HOLD), clock=clock,
    )
    prices.set("NUE", "150.00")
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert started.audit.shadow_closes() == []
    position = started.exits.tracked[0]
    assert not position.close_verdict
    assert position.last_review_would_open is False
    review = started.audit.trail("dec-1").reviews[-1]
    assert review.outcome is ReviewOutcome.HOLD
    assert review.would_open_today is False
    assert "reward:risk" in review.would_open_today_reason
    # Both cases and the winner are on the record.
    assert review.case_for_holding and review.case_for_selling
    assert "selection-threshold" in review.verdict_reason


def test_an_argued_close_on_a_profitable_intact_position_is_still_shadowed(
    tmp_path, limits, signals_config, research_config
):
    """The dialectical structure is what the shadow period evaluates."""
    argued_close = {
        **STALE_HOLD,
        "action": "close",
        "verdict_reason": "selling wins: a fresher candidate deserves the slot",
    }
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config,
        llm=routing(argued_close), clock=clock,
    )
    prices.set("NUE", "150.00")  # profitable + intact: the shadowed class
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1 and report.exits_started == 0
    shadows = started.audit.shadow_closes()
    assert len(shadows) == 1 and shadows[0].symbol == "NUE"
    assert started.audit.trail("dec-1").reviews[-1].outcome is ReviewOutcome.CLOSE


# ================================================================================
# The trim verdict
# ================================================================================


def test_a_trim_verdict_in_profit_trims_once(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config,
        llm=routing(TRIM_VERDICT), clock=clock,
    )
    position = started.exits.tracked[0]
    entered = position.quantity
    trimmed = Decimal(int(entered * Decimal("0.5")))  # whole units, rounded down
    assert trimmed >= 1

    prices.set("NUE", "150.00")  # in profit
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 1  # the trim order went out
    assert started.audit.trail("dec-1").reviews[-1].outcome is ReviewOutcome.TRIM

    report = started.loop.tick()  # the trim settles
    assert report.positions_closed == 0  # a trim never closes the position
    position = started.exits.tracked[0]
    assert position.quantity == entered - trimmed
    assert position.review_trimmed

    trail = started.audit.trail("dec-1")
    trim_exits = [e for e in trail.exits if e.reason is ExitReason.REVIEW_TRIM]
    assert len(trim_exits) == 1 and trim_exits[0].submitted
    assert "trim:" in trim_exits[0].detail  # the verdict reason rides the record
    sells = [f for f in trail.fills if f.side == "sell"]
    assert len(sells) == 1 and sells[0].filled_quantity == trimmed
    assert trail.outcome is None  # still open

    # A second trim verdict does NOT trim again: it stands as a hold.
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert started.exits.tracked[0].quantity == entered - trimmed
    assert started.audit.shadow_closes() == []  # trims never touch the shadow


def test_a_trim_verdict_below_entry_stands_as_a_hold(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config,
        llm=routing(TRIM_VERDICT), clock=clock,
    )
    prices.set("NUE", "135.00")  # below the 140 entry: nothing to take
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert not started.exits.tracked[0].review_trimmed
    # Recorded as the verdict the model gave; the engine simply did not act.
    assert started.audit.trail("dec-1").reviews[-1].outcome is ReviewOutcome.TRIM


def test_the_trim_latch_and_management_state_survive_a_restart(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    first, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config,
        llm=routing(TRIM_VERDICT), clock=clock,
    )
    entered = first.exits.tracked[0].quantity
    trimmed = Decimal(int(entered * Decimal("0.5")))
    remaining = entered - trimmed
    prices.set("NUE", "150.00")
    clock.advance(hours=25)
    first.loop.tick()  # trim goes out
    first.loop.tick()  # trim settles
    first.loop.shutdown()

    restarted = start(
        fetcher=feed(),
        prices=MutablePrices(NUE="150.00"),
        llm_client=routing(TRIM_VERDICT),
        adapter=FakeBroker(
            cash=Decimal("99020"),
            positions=[
                BrokerPosition(
                    "NUE", remaining, remaining * Decimal("140"),
                    remaining * Decimal("150"),
                )
            ],
        ),
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    position = restarted.exits.tracked[0]
    assert position.quantity == remaining
    assert position.review_trimmed  # the latch came back from the trail
    assert position.last_review_validity == "intact"
    assert position.last_review_resolution == "partial"
    assert position.last_review_would_open is True

    clock.advance(hours=25)
    report = restarted.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert restarted.exits.tracked[0].quantity == remaining


# ================================================================================
# The prompt states the rules and the context; health shows the state
# ================================================================================


def _under_review(**overrides) -> PositionUnderReview:
    base = dict(
        symbol="INTC",
        entry_price=Decimal("24.00"),
        current_price=Decimal("25.10"),
        opened_at=PROBATION_NOW,
        days_held=8,
        time_horizon="months",
        confidence_at_entry=54,
        source_id="congressional_disclosures",
        thesis="Foundry momentum.",
        invalidation_condition="Guidance cut.",
        original_content="filing text",
    )
    base.update(overrides)
    return PositionUnderReview(**base)


def test_the_prompt_states_the_stop_rules_costs_and_opportunity_context():
    prompt = build_review_prompt(
        _under_review(
            stop_price=Decimal("20.40"),
            sizing_floor=50,
            min_reward_risk=Decimal("1.5"),
            spread_pct=Decimal("0.07"),
            opportunity_context="2 name(s) carry active signals: AMRN, BE",
        )
    )
    assert "current stop price: 20.40" in prompt
    assert "today's entry rules" in prompt
    assert "below 50 does not trade" in prompt
    assert "at least 1.5" in prompt
    assert "quoted spread at review: 0.07%" in prompt
    assert "opportunity context" in prompt and "AMRN, BE" in prompt
    bare = build_review_prompt(_under_review())
    assert "today's entry rules" not in bare
    assert "no information about other candidates" in bare


def test_the_prompt_states_a_taken_trim_and_asks_for_hold_or_close():
    assert "already trimmed: YES" in build_review_prompt(
        _under_review(already_trimmed=True)
    )
    assert "already trimmed" not in build_review_prompt(_under_review())


def test_the_system_prompt_frames_the_dialectic():
    assert "THE TWO CASES" in EXIT_SYSTEM_PROMPT
    assert "case_for_holding" in EXIT_SYSTEM_PROMPT
    assert "case_for_selling" in EXIT_SYSTEM_PROMPT
    assert "EVIDENCE" in EXIT_SYSTEM_PROMPT  # would_open_today is not a trigger
    assert "hold, trim, or close" in EXIT_SYSTEM_PROMPT
    assert "backstop, not the decision" in EXIT_SYSTEM_PROMPT


def _tracked(**overrides) -> TrackedPosition:
    base = dict(
        decision_id="dec-9",
        symbol="INTC",
        quantity=Decimal("10"),
        entry_quantity=Decimal("10"),
        entry_price=Decimal("24.00"),
        entry_cost=Decimal("240.00"),
        opened_at=datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc),
        signal_id="s",
        source_id="congressional_disclosures",
        content="filing",
        thesis="Foundry momentum.",
        invalidation_condition="Guidance cut.",
        time_horizon="months",
        confidence=54,
        stop_price=Decimal("20.40"),
        leash_days=120,
    )
    base.update(overrides)
    return TrackedPosition(**base)


def test_health_renders_the_management_state():
    position = _tracked(
        last_review_at=datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc),
        last_review_validity="intact",
        last_review_progress="on_track",
        last_review_resolution="partial",
        last_review_would_open=False,
        resolution_date=date(2026, 11, 15),
        review_trimmed=True,
    )
    text = _fmt_position(position, PROBATION_NOW)
    assert "reviewed 2026-09-02" in text
    assert "trimmed" in text
    assert "managed: intact/on_track/partial" in text
    assert "would_open_today NO" in text
    assert "resolution 2026-11-15" in text
    assert "111d to leash" in text  # 120 - 9 days held


def test_health_says_unreviewed_before_the_first_review():
    text = _fmt_position(_tracked(), PROBATION_NOW)
    assert "managed: unreviewed" in text
