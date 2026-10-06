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
