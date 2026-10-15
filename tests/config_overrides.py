"""Test-only config variants. The REWORK ruling (2026-10-06) demoted the
congressional cap 5 -> 1 and Form 4 5 -> 2 in the shipped config; tests that
exercise dispatch MECHANICS with several same-source candidates (slot order,
pooled windows, aged-out-capped) keep a wider cap here so they test the
mechanism and not the ruling. Tests of the shipped governance read the live
file and pin the ruled values."""

from __future__ import annotations

from pathlib import Path

import yaml

from signals.config import SignalsConfig, default_signals_path


def signals_config_with_caps(**caps: int) -> SignalsConfig:
    """The shipped signals.yaml with ``daily_research_cap`` overridden per source id."""
    with open(default_signals_path(), "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    for klass in payload.get("classes", {}).values():
        for source in klass.get("sources", []):
            if source.get("id") in caps:
                source["daily_research_cap"] = caps[source["id"]]
    return SignalsConfig.model_validate(payload)


def pre_demotion_signals_config() -> SignalsConfig:
    """Caps as they were before 2026-10-06: congressional 5, Form 4 5."""
    return signals_config_with_caps(congressional_disclosures=5, form4_insiders=5)


def pre_redirect_limits():
    """The shipped risk_limits.yaml with the sleeve weights as they were before
    the risk-on redirect (2026-10-08): judged 55, aggressive 0. Mechanism tests
    pinned to judged-sizing arithmetic (bands off a 55% sleeve, sweep buffers,
    sector and single-position caps) test the mechanism, not the ruling; the
    shipped 30/25/15/30 weights are pinned in tests/test_aggressive.py."""
    from risk_gate.limits import RiskLimits, default_limits_path

    with open(default_limits_path(), "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["portfolio"]["sleeves"]["equity"] = 0.55
    payload["portfolio"]["sleeves"]["aggressive"] = 0.0
    payload["portfolio"]["sleeves"]["momentum"] = 0.0  # the momentum step (2026-10-13) is pinned elsewhere
    return RiskLimits.model_validate(payload)


def step_one_limits():
    """The shipped risk_limits.yaml with the momentum step-1 weights (2026-10-13
    to 2026-10-14): judged 15, momentum 15, aggressive 25, mechanical 15,
    baseline 30. Mechanism tests sized on a 15% momentum / 25% aggressive
    sleeve keep testing the mechanism; the shipped step-2 weights (momentum
    30, aggressive 10; applied 2026-10-14) are pinned in test_aggressive.py and
    test_momentum_gate.py."""
    from risk_gate.limits import RiskLimits, default_limits_path

    with open(default_limits_path(), "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["portfolio"]["sleeves"].update(
        {"equity": 0.15, "momentum": 0.15, "aggressive": 0.25, "mechanical": 0.15, "baseline": 0.30, "prediction": 0.0})
    return RiskLimits.model_validate(payload)


def pre_measurement_only_signals_config() -> SignalsConfig:
    """Caps as they were before 2026-10-14 (congressional 1, Form 4 2, 8-K 6).
    The human ruling of 2026-10-14 made those three sources measurement-only
    (cap 0); tests that exercise the JUDGED research machinery on their
    signals (adds, exits, convergence, context, the scan loop) keep research on
    here. The shipped caps are pinned in test_hardening and test_form8k."""
    return signals_config_with_caps(congressional_disclosures=1, form4_insiders=2, form_8k=6)


#: The retired citrini source (human ruling 2026-10-14), verbatim minus comments:
#: the class-2 trade-caller machinery it exercised is still live code, so its
#: tests keep running on this test-only copy.
RETIRED_CLASS2_CALLER = {
    "id": "citrini", "require_instrument": True, "research_tier": "class_1", "daily_research_cap": 3,
    "platforms": ["x"], "monthly_cost": 5, "start_date": "2026-09-30", "daily_read_warning": 200,
    "handle": "@citrini", "treatment": "thesis_input_only", "copy_trade": False,
    "classification": {
        "required_before_research_pass": True, "labels": ["forward_call", "retrospective", "other"],
        "default_when_ambiguous": "forward_call", "when_in_doubt": "research_behind_the_pnl_guard",
        "ambiguity_markers": ["past_tense", "missing_entry_price", "missing_timeframe", "celebrating_an_exit"],
    },
}


def signals_config_with_class2_caller() -> SignalsConfig:
    """The shipped signals.yaml plus the retired class-2 caller (test-only)."""
    with open(default_signals_path(), "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["classes"]["class_2"]["sources"].append(dict(RETIRED_CLASS2_CALLER))
    return SignalsConfig.model_validate(payload)
