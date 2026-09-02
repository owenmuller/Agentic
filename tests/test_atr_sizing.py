"""ATR sizing and stops (human ruling 2026-09-02).

The claims: ATR(14) is a stated simple mean of true ranges and returns None on
short/missing history; the stop clamps into [8%, 20%]; the resize equalizes
dollar risk and can only shrink below the band; options and missing data run
the fixed-15% regime untouched; the exit engine arms the position's OWN stop at
fill and restores it on replay while pre-ruling positions keep their 15%; and
the adverse review trigger scales to 0.66 x the position's own stop.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.atr import atr_fraction_from_bars
from orchestrator.config import AtrSizingConfig, OrchestratorConfig

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)
CONFIG = AtrSizingConfig()


def bars_with_range(daily_range: float, close: float = 100.0, days: int = 20):
    half = daily_range / 2
    return [
        {"h": close + half, "l": close - half, "c": close} for _ in range(days)
    ]


# ================================================================================
# The ATR arithmetic
# ================================================================================


def test_atr_is_the_mean_true_range_over_price():
    # Constant 4-point range on a $100 stock: ATR fraction = 4%.
    value = atr_fraction_from_bars(bars_with_range(4.0))
    assert value == Decimal("0.04")


def test_gaps_count_through_the_previous_close():
    # A flat bar after a close far below it: true range is the gap, not h-l.
    bars = bars_with_range(2.0, close=100.0)
    bars.append({"h": 111.0, "l": 110.0, "c": 110.0})
    value = atr_fraction_from_bars(bars)
    # 13 ranges of 2 + one gap range of 11 (111 - prev close 100), over 110.
    expected = (Decimal("2") * 13 + Decimal("11")) / 14 / Decimal("110")
    assert value == expected


def test_short_or_junk_history_is_none_never_zero():
    assert atr_fraction_from_bars(bars_with_range(4.0, days=10)) is None
    assert atr_fraction_from_bars([]) is None
    assert atr_fraction_from_bars([{"h": "x"}] * 20) is None


# ================================================================================
# The resize (through the pipeline's helper, wired the way bootstrap wires it)
# ================================================================================


def sized(atr: str | None, confidence: int = 72):
    """A real proposal through SizingEngine + the pipeline's _apply_atr."""
    from risk_gate.limits import RiskLimits
    from sizing.engine import SizingEngine
    from test_audit import make_report

    from orchestrator.pipeline import SignalPipeline

    report = make_report(confidence=confidence)
    proposal = SizingEngine(RiskLimits.load()).propose_equity(
        report, Decimal("75000")
    )
    pipeline = object.__new__(SignalPipeline)  # only the ATR seam is exercised
    pipeline._atr_config = CONFIG
    pipeline._atr_fraction = (
        (lambda symbol: Decimal(atr)) if atr is not None else None
    )
    return proposal, SignalPipeline._apply_atr(pipeline, proposal, report)


def test_a_volatile_name_shades_down_and_stamps_the_counterfactual():
    table, adjusted = sized("0.08")  # k=2.5 x 8% = 20%, ceiling binds
    assert adjusted.stop_fraction == Decimal("0.20")
    assert adjusted.atr_fraction == Decimal("0.08")
    assert adjusted.counterfactual_fixed_capital == table.capital
    # risk budget 15% of band / 20% stop = 0.75 x band.
    assert adjusted.capital == (table.capital * Decimal("0.75")).quantize(
        Decimal("0.01")
    )
    assert "ATR stop 20.00%" in adjusted.rationale


def test_a_quiet_name_sizes_at_the_band_cap_with_a_tighter_stop():
    table, adjusted = sized("0.032")  # 2.5 x 3.2% = 8%, the floor
    assert adjusted.stop_fraction == Decimal("0.08")
    assert adjusted.capital == table.capital  # min(band, band x 1.875) = band
    assert adjusted.counterfactual_fixed_capital == table.capital


def test_the_band_cap_is_never_exceeded():
    for atr in ("0.01", "0.04", "0.06", "0.08", "0.2"):
        table, adjusted = sized(atr)
        assert adjusted.capital <= table.capital


def test_missing_atr_runs_the_fixed_regime_untouched():
    table, adjusted = sized(None)
    assert adjusted is table
    assert adjusted.stop_fraction is None


# ================================================================================
# Exit engine: the position's own stop, frozen at entry, restored on replay
# ================================================================================


def test_stop_and_trigger_follow_the_positions_own_fraction():
    # Direct arithmetic on the seams, no full harness needed: _stop_for and
    # the trigger's down threshold.
    from orchestrator.config import OrchestratorConfig as OC
    from orchestrator.exits import ExitEngine, TrackedPosition

    config = OC.load().exits
    engine = object.__new__(ExitEngine)
    engine._config = config
    engine._trigger_down_of_stop = Decimal("0.66")

    # ATR position: 20% stop.
    stop = ExitEngine._stop_for(engine, Decimal("100"), Decimal("0.20"))
    assert stop == Decimal("80.00")
    # Fixed-regime position (None): the config's 15%.
    assert ExitEngine._stop_for(engine, Decimal("100"), None) == Decimal("85.00")

    def position(stop_fraction):
        return TrackedPosition(
            decision_id="d1",
            symbol="NUE",
            quantity=Decimal("10"),
            entry_quantity=Decimal("10"),
            entry_price=Decimal("100"),
            entry_cost=Decimal("1000"),
            opened_at=NOW,
            signal_id="s1",
            source_id="congressional_disclosures",
            content="",
            thesis="t",
            invalidation_condition="i",
            time_horizon="weeks",
            confidence=70,
            stop_price=Decimal("80"),
            leash_days=45,
            stop_fraction=stop_fraction,
        )

    atr_position = position(Decimal("0.20"))
    fixed_position = position(None)
    # ATR trigger sits at 0.66 x 20% = 13.2%: -13.5% fires, -10.5% does not.
    assert (
        ExitEngine._trigger_reason_for(engine, atr_position, Decimal("86.5"))
        is not None
    )
    assert (
        ExitEngine._trigger_reason_for(engine, atr_position, Decimal("89.5"))
        is None
    )
    # The fixed position keeps the config's 10%: -10.5% fires.
    assert (
        ExitEngine._trigger_reason_for(engine, fixed_position, Decimal("89.5"))
        is not None
    )


def test_the_shipped_yaml_arms_atr_sizing():
    config = OrchestratorConfig.load().atr_sizing
    assert config.enabled
    assert config.k == Decimal("2.5")
    assert (config.stop_floor, config.stop_ceiling) == (
        Decimal("0.08"),
        Decimal("0.20"),
    )
    assert config.risk_budget_fraction == Decimal("0.15")
    assert config.trigger_down_of_stop == Decimal("0.66")


def test_config_rejects_an_inverted_clamp():
    with pytest.raises(Exception, match="exceeds ceiling"):
        AtrSizingConfig.model_validate(
            {"stop_floor": "0.3", "stop_ceiling": "0.2"}
        )
