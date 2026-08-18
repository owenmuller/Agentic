"""Typed view of ``config/orchestrator.yaml``.

Same rule as the other config modules: the YAML is the source of truth and nothing
here invents a default. A loop running on an assumed research budget is a loop that
can spend one.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_gate.state import AccountType


class TimeStopDays(BaseModel):
    """Maximum holding period per research time horizon, in days.

    Keyed by the ``TimeHorizon`` values so a report's own stated horizon picks its
    leash: a "days" thesis that is still open after a week has already been wrong on
    its own terms, whatever the price did.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    days: int = Field(gt=0)
    weeks: int = Field(gt=0)
    months: int = Field(gt=0)

    @model_validator(mode="after")
    def _leashes_lengthen_with_the_horizon(self) -> "TimeStopDays":
        if not self.days <= self.weeks <= self.months:
            raise ValueError(
                f"time stops must not shorten as the horizon lengthens: "
                f"{self.days}/{self.weeks}/{self.months}"
            )
        return self

    def for_horizon(self, horizon: str) -> int:
        value = getattr(self, horizon, None)
        if not isinstance(value, int):
            raise KeyError(f"{horizon!r} is not a configured time horizon")
        return value


class ExitsConfig(BaseModel):
    """Deterministic guardrails plus the thesis-review cadence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Close at or below entry_price x (1 - fraction). Frozen per position at entry.
    max_loss_fraction: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    time_stop_days: TimeStopDays
    thesis_review_interval_hours: int = Field(gt=0)


class OrchestratorConfig(BaseModel):
    """Loop cadence and the daily research budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    #: Research passes per UTC day. Zero is a valid, if inert, configuration.
    max_research_passes_per_day: int = Field(ge=0)
    tick_interval_seconds: int = Field(gt=0)
    account_type: AccountType
    exits: ExitsConfig

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "OrchestratorConfig":
        path = path or default_orchestrator_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_orchestrator_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "orchestrator.yaml"
