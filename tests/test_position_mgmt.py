"""Position management upgrades (human rulings 2026-09-02, third batch).

The claims: every review answers the re-underwrite question (would this
position be opened under TODAY's entry rules?) and a "no" on a position not
ahead of its own timeline closes it, executing normally — invalidation-adjacent,
never shadowed by the exit-authority probation; a HOLD verdict reporting
resolution=partial on a position in profit TRIMS the configured fraction once
per position under its own exit reason (the ADD half of scaling stays
deferred); and health renders the management state per position instead of
leaving it to be inferred from a stop price.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

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
#: today's rules the numbers no longer admit the position — and it is not ahead.
STALE_HOLD = {
    "assessment": "Thesis intact but the remaining move no longer clears the bar.",
    "invalidation_triggered": False,
    "action": "hold",
    "validity": "intact",
    "progress": "on_track",
    "would_open_today": False,
    "would_open_today_reason": (
        "reward:risk from the current price to the target against the current "
        "stop is ~0.4, far below the 1.5 minimum"
    ),
}

#: A failed re-underwrite on a position running AHEAD of its timeline: recorded,
#: never a close.
AHEAD_NO = {**STALE_HOLD, "progress": "ahead"}

#: A hold reporting partial resolution — the trim trigger.
PARTIAL_HOLD = {
    "assessment": "About half the expected move has printed; thesis still live.",
    "invalidation_triggered": False,
    "action": "hold",
    "validity": "intact",
    "progress": "on_track",
    "resolution": "partial",
    "would_open_today": True,
    "would_open_today_reason": "still clears the floor and the reward:risk bar",
}


# ================================================================================
# The re-underwrite contradiction (rule 4)
# ================================================================================


@pytest.mark.parametrize(
    "would_open,progress,closes",
    [
        (True, "on_track", False),
        (False, "ahead", False),  # ahead of its timeline: recorded, not closed
        (False, "on_track", True),
        (False, "stalled", True),
    ],
)
def test_the_reunderwrite_truth_table(would_open, progress, closes):
    review = ExitReview(
        assessment="x",
        invalidation_triggered=False,
        action="hold",
        would_open_today=would_open,
        progress=progress,
    )
    assert review.should_close is closes
    assert review.reunderwrite_close is closes
    if closes:
        assert "would_open_today" in review.close_contradiction


def test_the_reunderwrite_default_cannot_manufacture_a_close():
    """A model that omitted the field (or an old fixture) must read as YES:
    a rejected review is a HOLD, and the default must agree with that."""
    review = ExitReview(
        assessment="x", invalidation_triggered=False, action="hold"
    )
    assert review.would_open_today is True
    assert not review.should_close


def test_a_failed_reunderwrite_closes_and_is_never_shadowed(
    tmp_path, limits, signals_config, research_config
):
    """Profitable + intact + inside the probation window — the shadowed class —
    but the close came from the re-underwrite, which is invalidation-adjacent
    and executes normally (ruling 2026-09-02)."""
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(STALE_HOLD),
        clock=clock,
    )
    prices.set("NUE", "150.00")  # profitable over the 140 entry
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 1
    assert started.audit.shadow_closes() == []

    review = started.audit.trail("dec-1").reviews[-1]
    assert review.outcome is ReviewOutcome.CLOSE
    assert review.would_open_today is False
    assert "reward:risk" in review.would_open_today_reason
    assert "would_open_today" in review.close_contradiction


def test_a_failed_reunderwrite_on_an_ahead_position_holds(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(AHEAD_NO),
        clock=clock,
    )
    prices.set("NUE", "150.00")
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    position = started.exits.tracked[0]
    assert not position.close_verdict
    # The honest "no" is recorded and VISIBLE on the position for health.
    assert position.last_review_would_open is False
    assert started.audit.trail("dec-1").reviews[-1].would_open_today is False


# ================================================================================
# The trim half of scaling
# ================================================================================


def test_a_partial_resolution_in_profit_trims_once(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(PARTIAL_HOLD),
        clock=clock,
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

    report = started.loop.tick()  # the trim settles
    assert report.positions_closed == 0  # a trim never closes the position
    position = started.exits.tracked[0]
    assert position.quantity == entered - trimmed
    assert position.review_trimmed

    trail = started.audit.trail("dec-1")
    trim_exits = [e for e in trail.exits if e.reason is ExitReason.REVIEW_TRIM]
    assert len(trim_exits) == 1 and trim_exits[0].submitted
    sells = [f for f in trail.fills if f.side == "sell"]
    assert len(sells) == 1 and sells[0].filled_quantity == trimmed
    assert trail.outcome is None  # still open

    # A second partial verdict does NOT trim again: at most once per position.
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert started.exits.tracked[0].quantity == entered - trimmed
    # And nothing about a trim touches the probation shadow.
    assert started.audit.shadow_closes() == []


def test_a_partial_resolution_below_entry_does_not_trim(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(PARTIAL_HOLD),
        clock=clock,
    )
    prices.set("NUE", "135.00")  # below the 140 entry: nothing to take
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert not started.exits.tracked[0].review_trimmed


def test_the_trim_latch_and_management_state_survive_a_restart(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    first, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(PARTIAL_HOLD),
        clock=clock,
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
        llm_client=routing(PARTIAL_HOLD),
        adapter=FakeBroker(
            cash=Decimal("99020"),
            positions=[
                BrokerPosition(
                    "NUE",
                    remaining,
                    remaining * Decimal("140"),
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
    # The last verdict's management state is visible immediately, not
    # "unreviewed until the next cadence slot".
    assert position.last_review_validity == "intact"
    assert position.last_review_resolution == "partial"
    assert position.last_review_would_open is True

    # And it holds: another partial verdict on the restarted loop cannot re-trim.
    clock.advance(hours=25)
    report = restarted.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 0
    assert restarted.exits.tracked[0].quantity == remaining


# ================================================================================
# The prompt states today's rules; health shows the management state
# ================================================================================


def test_the_prompt_states_the_stop_and_todays_entry_rules():
    position = PositionUnderReview(
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
        stop_price=Decimal("20.40"),
        sizing_floor=50,
        min_reward_risk=Decimal("1.5"),
    )
    prompt = build_review_prompt(position)
    assert "current stop price: 20.40" in prompt
    assert "today's entry rules" in prompt
    assert "below 50 does not trade" in prompt
    assert "at least 1.5" in prompt
    # Absent facts degrade to a prompt that does not state them.
    bare = build_review_prompt(
        PositionUnderReview(
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
    )
    assert "today's entry rules" not in bare


def test_the_system_prompt_asks_the_reunderwrite_and_discloses_the_trim():
    assert "RE-UNDERWRITE" in EXIT_SYSTEM_PROMPT
    assert "would_open_today" in EXIT_SYSTEM_PROMPT
    assert "TRIM" in EXIT_SYSTEM_PROMPT


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
