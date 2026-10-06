"""The horizon-adjusted, opportunity-cost-aware reward hurdle (ADAPTIVE STANDARDS
ruling 2026-10-06).

Claims: a 2% move in five days clears where the same move in nine months does
not; the absolute reward:risk floor still vetoes a target too close to the stop;
the hurdle rises with judged deployment and queue pressure and never above
base × (1 + k); days come from the report's resolution date clamped into the
leash bounds, or the horizon's fallback; the pipeline applies the two-part test
with the live config and keeps the flat test when no annualized hurdle is
configured; the loop's opportunity read is deployment and queue only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from orchestrator.hurdle import (
    HurdleReading,
    Opportunity,
    annualized_return,
    days_to_resolution,
    effective_hurdle,
    judge,
)

BASE = Decimal("0.40")
K = Decimal("0.5")
FLOOR = Decimal("0.8")


def reading(opportunity=None) -> HurdleReading:
    return effective_hurdle(BASE, K, opportunity, FLOOR)


def test_a_fast_two_percent_clears_and_a_slow_one_does_not():
    entry = Decimal("100")
    target = Decimal("102")
    stop = Decimal("0.02")  # 2% stop: reward:risk exactly 1.0, above the 0.8 floor
    fast = judge(target=target, entry=entry, stop_fraction=stop, days=5, reading=reading())
    slow = judge(target=target, entry=entry, stop_fraction=stop, days=270, reading=reading())
    assert fast.passed and fast.annualized == annualized_return(target, entry, 5)
    assert fast.annualized > Decimal("1.4")  # 2% x 73 = 146% annualized
    assert not slow.passed and "annualized expected return" in slow.reason
    assert slow.annualized < BASE


def test_the_absolute_floor_still_vetoes_a_target_hugging_the_stop():
    # 0.5% target over a 15% stop: reward:risk 0.03 — annualizing a 3-day horizon
    # would make it look like 60%, and the floor is there to say no.
    verdict = judge(target=Decimal("100.5"), entry=Decimal("100"), stop_fraction=Decimal("0.15"), days=3, reading=reading())
    assert not verdict.passed and "absolute floor" in verdict.reason
    assert verdict.reward_risk < FLOOR


def test_the_hurdle_scales_with_the_opportunity_set_and_caps_at_base_times_one_plus_k():
    idle = Opportunity(deployed_fraction=Decimal("0.07"), positive_candidates=0, open_positions=2)
    assert idle.u == Decimal("0.07") and reading(idle).effective == BASE * (1 + K * Decimal("0.07"))
    queued = Opportunity(deployed_fraction=Decimal("0.07"), positive_candidates=9, open_positions=2, target_positions=20)
    assert queued.queue_pressure == Decimal(9) / Decimal(18)
    assert queued.u == Decimal(9) / Decimal(18)
    full = Opportunity(deployed_fraction=Decimal("1.3"), positive_candidates=500, open_positions=25)
    assert full.u == Decimal("1") and reading(full).effective == BASE * (1 + K)
    # A move that clears the idle hurdle fails the full one: the bar moved, not the trade.
    entry, target, stop = Decimal("100"), Decimal("104"), Decimal("0.04")
    assert judge(target=target, entry=entry, stop_fraction=stop, days=30, reading=reading(idle)).passed
    assert not judge(target=target, entry=entry, stop_fraction=stop, days=30, reading=reading(full)).passed


def test_days_come_from_the_resolution_date_clamped_or_the_horizon_fallback():
    observed = date(2026, 10, 6)
    assert days_to_resolution(date(2026, 10, 20), observed, "weeks", 7, 90, 45) == 14
    assert days_to_resolution(date(2026, 10, 8), observed, "weeks", 7, 90, 45) == 7  # floor
    assert days_to_resolution(date(2027, 10, 8), observed, "weeks", 7, 90, 45) == 90  # ceiling
    assert days_to_resolution(None, observed, "months", 60, 367, 120) == 120  # fallback
    assert days_to_resolution(date(2026, 10, 1), observed, "days", 3, 21, 7) == 7  # past date -> fallback


@pytest.fixture(scope="session")
def live_config():
    from orchestrator.config import OrchestratorConfig

    return OrchestratorConfig.load()


def test_the_live_config_carries_the_ruled_numbers(live_config):
    rr = live_config.reward_risk
    assert rr.min_ratio == Decimal("0.8")
    assert rr.annualized_hurdle == Decimal("0.40")
    assert rr.opportunity_cost_k == Decimal("0.5")
    assert rr.target_positions == 20


class _Report:
    def __init__(self, target, horizon="weeks", resolution=None):
        self.tickers = ("NUE",)
        self.target_price = Decimal(str(target))
        self.time_horizon = horizon
        self.expected_resolution_date = resolution


def _pipeline(live_config, opportunity=None, config=None):
    from orchestrator.pipeline import SignalPipeline

    pipeline = SignalPipeline.__new__(SignalPipeline)
    pipeline._rr_config = config or live_config.reward_risk
    pipeline._prices = lambda symbol: Decimal("100")
    pipeline._atr_config = None
    pipeline._atr_fraction = None
    pipeline._exits_config = live_config.exits
    pipeline._opportunity = opportunity
    pipeline._clock = lambda: __import__("datetime").datetime(2026, 10, 6, 14, tzinfo=__import__("datetime").timezone.utc)
    return pipeline


def test_the_pipeline_applies_the_two_part_test_with_the_live_config(live_config):
    pipeline = _pipeline(live_config)
    # +6% in three weeks against the 15% fallback stop: reward:risk 0.4 fails the floor.
    assert "absolute floor" in pipeline._reward_risk_reason(_Report(106, "weeks", date(2026, 10, 27)))
    # +15% in three weeks: reward:risk 1.0, annualized ~260% — clears.
    assert pipeline._reward_risk_reason(_Report(115, "weeks", date(2026, 10, 27))) is None
    # +15% in a year (months, resolution 300 days): reward:risk 1.0 but ~18% annualized — fails the hurdle.
    reason = pipeline._reward_risk_reason(_Report(115, "months", date(2027, 8, 6)))
    assert reason is not None and "below the" in reason and "hurdle" in reason
    # No target: still fails closed.
    assert "no target_price" in pipeline._reward_risk_reason(_Report(None) if False else type("R", (), {"tickers": ("NUE",), "target_price": None})())


def test_without_an_annualized_hurdle_the_flat_test_is_unchanged(live_config):
    from orchestrator.config import RewardRiskConfig

    flat = RewardRiskConfig(enabled=True, min_ratio=Decimal("1.3"), fallback_stop_fraction=Decimal("0.15"))
    pipeline = _pipeline(live_config, config=flat)
    assert "reward:risk" in pipeline._reward_risk_reason(_Report(110))  # 0.67 < 1.3
    assert pipeline._reward_risk_reason(_Report(125)) is None  # 1.67 >= 1.3


def test_the_opportunity_set_moves_the_bar_in_the_pipeline(live_config):
    idle = lambda: Opportunity(Decimal("0.05"), 0, 2)  # noqa: E731
    full = lambda: Opportunity(Decimal("0.95"), 40, 20)  # noqa: E731
    report = _Report(107, "weeks", date(2026, 11, 20))  # +7% in 45d: ~57% annualized, rr 0.47 -> floor fails
    report_ok = _Report(113, "weeks", date(2026, 11, 20))  # +13% in 45d: rr 0.87, ~105% annualized
    assert _pipeline(live_config, idle)._reward_risk_reason(report_ok) is None
    # At full pressure the hurdle is 60%: 105% still clears; a slower one would not.
    slow = _Report(113, "months", date(2027, 3, 1))  # 146d: ~32% annualized
    assert _pipeline(live_config, idle)._reward_risk_reason(slow) is not None
    assert "absolute floor" in _pipeline(live_config, full)._reward_risk_reason(report)
