"""Typed view of ``config/orchestrator.yaml``.

Same rule as the other config modules: the YAML is the source of truth and nothing
here invents a default. A loop running on an assumed research budget is a loop that
can spend one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from risk_gate.state import AccountType


class OrchestratorConfig(BaseModel):
    """Loop cadence and the daily research budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    #: Research passes per UTC day. Zero is a valid, if inert, configuration.
    max_research_passes_per_day: int = Field(ge=0)
    tick_interval_seconds: int = Field(gt=0)
    account_type: AccountType

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "OrchestratorConfig":
        path = path or default_orchestrator_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_orchestrator_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "orchestrator.yaml"
