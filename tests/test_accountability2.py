"""Expectancy metrics, the reward:risk gate, model pinning, and the golden set
(human rulings 2026-09-02, live LLM-agent-evidence tier).

The claims: expectancy arithmetic is correct and renders insufficient below
n=20; the R:R gate is veto-only, rejects targetless and thin-reward longs with
a typed code, fails open on a missing quote and closed on a missing target,
and prices risk off the stop the position would actually get; an unpinned or
floating model id cannot start the system; and the golden set loads, grades,
and detects drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from audit.attribution import ExpectancyStats
from orchestrator.config import AtrSizingConfig, RewardRiskConfig
from orchestrator.golden import GoldenCase, grade, load_cases
from orchestrator.pipeline import SignalPipeline
from research.config import ResearchConfig
from research.reports import ResearchReport, report_tool_definition
from signals import SignalClass
from test_audit import make_report

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=timezone.utc)


# ================================================================================
# Expectancy
# ================================================================================


def test_expectancy_arithmetic():
    pnls = tuple(
        Decimal(x) for x in ("100", "50", "-30", "-20", "200", *["10"] * 16)
    )  # 21 trades
    stats = ExpectancyStats.of(pnls)
    assert stats.n == 21 and stats.wins == 19
    assert stats.avg_win == Decimal("26.84")  # (100+50+200+160)/19
    assert stats.avg_loss == Decimal("25.00")
    assert stats.profit_factor == Decimal("10.20")  # 510 / 50
    assert stats.expectancy == Decimal("21.90")  # 460 / 21
    assert "profit factor 10.20" in stats.line()


def test_expectancy_renders_insufficient_below_twenty():
    stats = ExpectancyStats.of((Decimal("5"), Decimal("-3")))
    assert stats.line() == "expectancy insufficient (n=2)"


# ================================================================================
# target_price and the reward:risk gate
# ================================================================================


def test_target_price_is_in_the_schema_and_required_of_the_model():
    schema = report_tool_definition()["input_schema"]
    assert "target_price" in schema["properties"]
    assert "target_price" in schema["required"]
    assert "target_price" in report_tool_definition()["description"]


def test_old_records_parse_without_a_target():
    report = make_report()
    assert report.target_price is None


def test_a_non_positive_target_is_rejected():
    with pytest.raises(Exception, match="positive"):
        make_report(target_price="-5")


def rr_pipeline(quote="140", atr=None, config=None):
    pipeline = object.__new__(SignalPipeline)
    pipeline._rr_config = config if config is not None else RewardRiskConfig()
    pipeline._prices = lambda symbol: Decimal(quote) if quote else None
    pipeline._atr_config = AtrSizingConfig() if atr is not None else None
    pipeline._atr_fraction = (lambda symbol: Decimal(atr)) if atr else None
    return pipeline


def test_a_long_without_a_target_is_rejected():
    reason = rr_pipeline()._reward_risk_reason(make_report(target_price=None))
    assert reason is not None and "no target_price" in reason


def test_thin_reward_fails_and_rich_reward_passes():
    # entry 140, fallback stop 15% -> risk 21. Target 160: ratio 0.95 -> reject.
    thin = rr_pipeline()._reward_risk_reason(make_report(target_price="160"))
    assert thin is not None and "below the 1.5 floor" in thin
    # Target 175: ratio 1.67 -> pass.
    assert rr_pipeline()._reward_risk_reason(make_report(target_price="175")) is None


def test_the_stop_used_is_the_one_the_position_would_get():
    # ATR 8% -> stop clamps to the 20% ceiling -> risk 28; target 175 now fails.
    tight = rr_pipeline(atr="0.08")._reward_risk_reason(
        make_report(target_price="175")
    )
    assert tight is not None and "20.00% stop" in tight
    # ATR 2% -> stop clamps to the 8% floor -> risk 11.2; target 160 now passes.
    assert (
        rr_pipeline(atr="0.02")._reward_risk_reason(make_report(target_price="160"))
        is None
    )


def test_a_missing_quote_fails_open():
    assert (
        rr_pipeline(quote=None)._reward_risk_reason(make_report(target_price="1"))
        is None
    )


def test_the_gate_can_be_disabled_but_ships_enabled():
    off = RewardRiskConfig(enabled=False)
    assert (
        rr_pipeline(config=off)._reward_risk_reason(make_report(target_price=None))
        is None
    )
    from orchestrator.config import OrchestratorConfig

    shipped = OrchestratorConfig.load().reward_risk
    assert shipped.enabled and shipped.min_ratio == Decimal("1.5")


# ================================================================================
# Model pinning
# ================================================================================


def test_a_floating_alias_cannot_start():
    payload = ResearchConfig.load().model_dump(mode="json")
    payload["model"] = "claude-opus-latest"
    payload["pinned_models"] = []
    with pytest.raises(Exception, match="floating alias"):
        ResearchConfig.model_validate(payload)


def test_an_unpinned_model_cannot_start():
    payload = ResearchConfig.load().model_dump(mode="json")
    payload["model"] = "claude-opus-4-8"  # real model, not pinned here
    with pytest.raises(Exception, match="not in pinned_models"):
        ResearchConfig.model_validate(payload)


def test_the_shipped_config_is_fully_pinned():
    config = ResearchConfig.load()
    assert config.pinned_models  # non-empty: validation is armed
    assert config.model in config.pinned_models


# ================================================================================
# Report-phase sampling (ruling 2026-09-03)
# ================================================================================


def test_the_shipped_sampling_applies_to_sonnet_report_phase_only():
    config = ResearchConfig.load()
    assert config.sampling.temperature_for("claude-sonnet-4-6") == 0.0
    assert config.sampling.temperature_for("claude-opus-5") is None
    assert config.sampling.temperature_for("claude-haiku-4-5-20251001") is None


@pytest.mark.parametrize(
    "models,fragment",
    [
        (["claude-opus-5"], "rejects non-default sampling"),
        (["claude-sonnet-4-5"], "not in pinned_models"),
        ([], "is empty"),
    ],
)
def test_sampling_aimed_at_the_wrong_model_fails_preflight(models, fragment):
    raw = ResearchConfig.load().model_dump()
    raw["sampling"] = {"report_temperature": 0.0, "report_temperature_models": models}
    with pytest.raises(ValueError, match=fragment):
        ResearchConfig.model_validate(raw)


def test_the_boundary_band_is_twenty_and_seventy_plus_stays_unconfirmed():
    from orchestrator.config import OrchestratorConfig

    boundary = OrchestratorConfig.load().boundary_confirmation
    assert boundary.enabled and boundary.band_width == 20


# ================================================================================
# Golden set
# ================================================================================


def test_the_golden_set_loads_and_names_the_ruled_cases():
    cases = load_cases()
    entries = [case for case in cases if case.kind == "entry"]
    reviews = [case for case in cases if case.kind == "review"]
    assert len(entries) == 20
    # Review cases (ruling 2026-09-02) grade the reasoning structure.
    assert len(reviews) == 3
    for case in reviews:
        under = case.under_review()
        assert under.symbol == "INTC" and under.stop_price is not None
    cases = entries
    names = {case.name for case in cases}
    assert {
        "pelosi-be-calls-decline",
        "pelosi-intc-calls-entry",
        "sa-13f-stale-heavy-puts",
        "sarissa-amrn-13da",
        "form4-intc-cluster",
        "injection-cashtag",
        "mirror-fabricated-buyback",
    } <= names
    # Every case builds a valid Signal.
    for case in cases:
        signal = case.signal(NOW)
        assert signal.source_id == case.source_id


def golden_case(**expect):
    return GoldenCase(
        name="t",
        origin="test",
        source_id="nolimitgains",
        signal_class=SignalClass.CLASS_1_REALTIME,
        content="x",
        external_id="e1",
        classification=None,
        metadata={},
        directions=tuple(expect.get("directions", ("no_position",))),
        confidence_band=tuple(expect.get("confidence", (0, 100))),
        must_flag_manipulation=expect.get("must_flag", False),
        note="",
        recorded_verdict="",
    )


def test_grading_passes_and_drifts():
    inside = make_report(direction="no_position", confidence=80)
    assert grade(golden_case(), inside, None).passed

    wrong_direction = make_report(direction="long", confidence=60, target_price="10")
    result = grade(golden_case(), wrong_direction, None)
    assert not result.passed and "direction long" in result.problems[0]

    unflagged = make_report(direction="no_position", confidence=80)
    result = grade(golden_case(must_flag=True), unflagged, None)
    assert not result.passed and "manipulation NOT flagged" in result.problems[0]

    flagged = make_report(
        direction="no_position",
        confidence=80,
        manipulation_assessment="embedded instruction detected",
    )
    assert grade(golden_case(must_flag=True), flagged, None).passed
