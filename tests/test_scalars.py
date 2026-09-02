"""Post-table risk scalars (rulings 2026-09-01/02): drawdown ladder + regime.

The claims: boundaries are inclusive toward less risk; the two multipliers
compose at one point and only ever shrink; recovery restores statelessly;
missing/stale VIX runs at x1.0 loudly rather than silently halving the book;
the scaled proposal preserves the table's own dollars for the weekly
forgone-size line; and bootstrap arms the scalars against the gate's own
drawdown while the mechanical arm stays exempt by construction.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from audit import AuditLog
from audit.attribution import build_attribution
from orchestrator.config import OrchestratorConfig, RiskScalarsConfig
from execution.vix import CboeVixSource
from orchestrator.scalars import (
    SizingScalars,
    drawdown_multiplier,
    regime_multiplier,
)
from risk_gate.limits import RiskLimits
from risk_gate.state import Sleeve
from sizing.engine import SizingEngine
from test_audit import equity_order, gate_for, make_report, make_signal

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)
CONFIG = RiskScalarsConfig()
LIMITS = RiskLimits.load()


def scalars(drawdown="0", vix=None, vix_date=date(2026, 9, 1)):
    reading = None if vix is None else (vix_date, Decimal(vix))
    return SizingScalars(
        CONFIG,
        drawdown=lambda: Decimal(drawdown),
        vix_close=(lambda: reading) if vix is not None else None,
        clock=lambda: NOW,
    )


def report(confidence=72):
    return make_report(confidence=confidence)


# ================================================================================
# The rungs
# ================================================================================


def test_ladder_boundaries_are_inclusive_toward_less_risk():
    cases = {
        "0": "1", "0.0399": "1",
        "0.04": "0.75", "0.079": "0.75",
        "0.08": "0.5", "0.119": "0.5",
        # Beyond the last rung the ladder stays at its floor — the 12% kill
        # switch is the gate's own halt, deliberately not reimplemented here.
        "0.12": "0.5", "0.5": "0.5",
    }
    for drawdown, expected in cases.items():
        assert drawdown_multiplier(Decimal(drawdown), CONFIG) == Decimal(expected), drawdown


def test_regime_boundaries_are_inclusive_toward_less_risk():
    cases = {
        "14.43": "1", "24.99": "1",
        "25": "0.75", "34.99": "0.75",
        "35": "0.5", "80": "0.5",
    }
    for vix, expected in cases.items():
        assert regime_multiplier(Decimal(vix), CONFIG) == Decimal(expected), vix


def test_the_two_scalars_compose_multiplicatively():
    reading = scalars(drawdown="0.05", vix="27").current()
    assert reading.drawdown_multiplier == Decimal("0.75")
    assert reading.regime_multiplier == Decimal("0.75")
    assert reading.multiplier == Decimal("0.5625")


def test_recovery_restores_statelessly():
    state = {"drawdown": Decimal("0.09")}
    ladder = SizingScalars(
        CONFIG, drawdown=lambda: state["drawdown"], clock=lambda: NOW
    )
    assert ladder.current().multiplier == Decimal("0.5")
    state["drawdown"] = Decimal("0.01")
    assert ladder.current().multiplier == Decimal("1")


def test_stale_or_missing_vix_runs_at_one_and_says_so():
    stale = scalars(vix="40", vix_date=date(2026, 8, 20)).current()
    assert stale.regime_multiplier == Decimal("1")
    assert "stale" in stale.detail

    missing = SizingScalars(
        CONFIG, drawdown=lambda: Decimal("0"), vix_close=lambda: None,
        clock=lambda: NOW,
    ).current()
    assert missing.regime_multiplier == Decimal("1")
    assert "unavailable" in missing.detail


# ================================================================================
# The composition point
# ================================================================================


def test_scale_shrinks_capital_and_preserves_the_tables_dollars():
    engine = SizingEngine(LIMITS)
    proposal = engine.propose_equity(report(72), Decimal("75000"))
    scaled = scalars(drawdown="0.04").scale(proposal)

    assert scaled.table_capital == proposal.capital
    assert scaled.capital == (proposal.capital * Decimal("0.75")).quantize(
        Decimal("0.01")
    )
    assert scaled.fraction_of_sleeve_nav < proposal.fraction_of_sleeve_nav
    assert "risk scalars x0.75" in scaled.rationale
    assert scaled.confidence == proposal.confidence


def test_a_flat_book_scales_nothing_and_stamps_nothing():
    engine = SizingEngine(LIMITS)
    proposal = engine.propose_equity(report(72), Decimal("75000"))
    untouched = scalars(drawdown="0", vix="14").scale(proposal)
    assert untouched is proposal
    assert untouched.table_capital is None


def test_a_no_trade_proposal_passes_through_unscaled():
    engine = SizingEngine(LIMITS)
    proposal = engine.propose_equity(report(40), Decimal("75000"))
    assert not proposal.is_tradeable
    assert scalars(drawdown="0.09").scale(proposal) is proposal


# ================================================================================
# The VIX source
# ================================================================================


def test_cboe_source_parses_the_last_close_and_caches_per_day():
    csv = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "08/31/2026,15.240000,15.480000,14.860000,14.920000\n"
        "09/01/2026,14.950000,16.800000,14.950000,16.340000\n"
    )
    calls = []

    def get(url):
        calls.append(url)
        return httpx.Response(200, text=csv)

    source = CboeVixSource(get, clock=lambda: NOW)
    assert source() == (date(2026, 9, 1), Decimal("16.340000"))
    assert source() == (date(2026, 9, 1), Decimal("16.340000"))
    assert len(calls) == 1  # once per UTC day


def test_cboe_outage_returns_none_never_raises():
    def get(url):
        raise httpx.ConnectError("down")

    source = CboeVixSource(get, clock=lambda: NOW)
    assert source() is None


# ================================================================================
# Config guards and wiring
# ================================================================================


def test_config_rejects_a_multiplier_above_one():
    with pytest.raises(Exception, match="less than or equal to 1"):
        RiskScalarsConfig.model_validate(
            {"drawdown_steps": [{"at": "0.04", "multiplier": "1.5"}]}
        )


def test_config_rejects_a_ladder_that_rises_with_drawdown():
    with pytest.raises(Exception, match="never rise"):
        RiskScalarsConfig.model_validate(
            {
                "drawdown_steps": [
                    {"at": "0.04", "multiplier": "0.5"},
                    {"at": "0.08", "multiplier": "0.75"},
                ]
            }
        )


def test_the_shipped_yaml_arms_both_scalars():
    config = OrchestratorConfig.load().risk_scalars
    assert config.enabled and config.regime.enabled
    assert [(s.at, s.multiplier) for s in config.drawdown_steps] == [
        (Decimal("0.04"), Decimal("0.75")),
        (Decimal("0.08"), Decimal("0.5")),
    ]
    assert [(s.vix_at_or_above, s.multiplier) for s in config.regime.thresholds] == [
        (Decimal("25"), Decimal("0.75")),
        (Decimal("35"), Decimal("0.5")),
    ]


def test_the_weekly_line_prices_forgone_size(tmp_path):
    """A scaled proposal's table_capital - capital lands in the report line,
    written through the real record path — no snapshot surgery."""
    log = AuditLog(tmp_path / "audit.jsonl")
    gate = gate_for(LIMITS)
    research = report(72)
    table = SizingEngine(LIMITS).propose_equity(
        research, gate.sleeve_nav(Sleeve.EQUITY)
    )
    scaled = scalars(drawdown="0.04").scale(table)
    log.record_decision(make_signal(), research, scaled, gate.submit(equity_order()))

    attribution = build_attribution(
        log.trails(), generated_at=datetime.now(timezone.utc)
    )
    forgone = table.capital - scaled.capital
    assert forgone > Decimal("0")
    assert attribution.scalar_forgone == forgone
    assert attribution.scalar_scaled_entries == 1
    assert f"forgone ${forgone:.2f} across 1 scaled judged entries" in (
        attribution.render()
    )
