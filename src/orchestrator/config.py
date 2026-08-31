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


class LeashBounds(BaseModel):
    """Floor and ceiling on a leash, in days, for one horizon bucket.

    Both are measured FROM ENTRY, never from the review that asks for them. A
    ceiling measured from "now" is not a ceiling: thirty reviews each asking for
    thirty more days would walk the leash out forever while every individual
    request looked modest (ruling 2026-08-31).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor: int = Field(gt=0)
    ceiling: int = Field(gt=0)

    @model_validator(mode="after")
    def _floor_under_ceiling(self) -> "LeashBounds":
        if self.floor > self.ceiling:
            raise ValueError(f"leash floor {self.floor} exceeds ceiling {self.ceiling}")
        return self

    def clamp(self, days: int) -> int:
        return max(self.floor, min(self.ceiling, days))


class LeashBoundsTable(BaseModel):
    """Per-horizon leash bounds, keyed like ``TimeStopDays``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    days: LeashBounds
    weeks: LeashBounds
    months: LeashBounds

    def for_horizon(self, horizon: str) -> LeashBounds:
        value = getattr(self, horizon, None)
        if not isinstance(value, LeashBounds):
            raise KeyError(f"{horizon!r} is not a configured time horizon")
        return value


class ReviewTriggerConfig(BaseModel):
    """When a price move forces a review out of cadence (ruling 2026-08-31).

    A trigger sets a flag and nothing else. It never closes a position — its job
    is to force the question, not to answer it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Favourable move from the last review's price that forces a re-read.
    up_fraction: Decimal = Field(gt=Decimal("0"))
    #: Adverse move that does the same. Deliberately TIGHTER than
    #: ``max_loss_fraction``: a downside trigger at the stop distance fires in the
    #: same cycle the stop closes the position, which is no trigger at all.
    down_fraction: Decimal = Field(gt=Decimal("0"))
    #: Ceiling on out-of-cadence reviews per UTC day, so a volatile week cannot
    #: eat the research budget.
    max_per_day: int = Field(ge=0)


class RatchetConfig(BaseModel):
    """The trailing backstop under the review layer (ruling 2026-08-31).

    Not a profit target: it arms only after a gain that has already happened, it
    takes no view on where price is going, and it exists solely so a resolved
    winner cannot fully round-trip in the gap between reviews.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Unrealised gain over entry at which the trailing stop engages.
    arm_at_gain: Decimal = Field(gt=Decimal("0"))
    #: Distance below the position's high-water mark the stop then follows at.
    trail_fraction: Decimal = Field(gt=Decimal("0"), lt=Decimal("1"))


class ExitsConfig(BaseModel):
    """Deterministic guardrails plus the thesis-review cadence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Close at or below entry_price x (1 - fraction). Frozen per position at entry.
    max_loss_fraction: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    #: Fallback leash when a report states no expected_resolution_date. The
    #: primary source is the report's own date (ruling 2026-08-31); this is what
    #: a report that declines to date itself gets.
    time_stop_days: TimeStopDays
    #: Bounds every leash is clamped into, whatever its source.
    leash_bounds: LeashBoundsTable
    thesis_review_interval_hours: int = Field(gt=0)
    review_trigger: ReviewTriggerConfig
    ratchet: RatchetConfig

    @model_validator(mode="after")
    def _fallbacks_sit_inside_their_bounds(self) -> "ExitsConfig":
        """A fallback outside its own bounds would be silently clamped, which is a
        config that does not say what it does."""
        for horizon in ("days", "weeks", "months"):
            fallback = self.time_stop_days.for_horizon(horizon)
            bounds = self.leash_bounds.for_horizon(horizon)
            if not bounds.floor <= fallback <= bounds.ceiling:
                raise ValueError(
                    f"{horizon} fallback leash {fallback} sits outside its bounds "
                    f"{bounds.floor}-{bounds.ceiling}"
                )
        return self


class MarketDataConfig(BaseModel):
    """Settings for the production price source. Consumed at wiring time —
    ``AlpacaPriceSource(feed=..., max_quote_age_seconds=...)`` — because the
    execution package cannot import this one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feed: str
    max_quote_age_seconds: int = Field(gt=0)


class OrchestratorConfig(BaseModel):
    """Loop cadence and the daily research budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    #: Research passes per UTC day. Zero is a valid, if inert, configuration.
    max_research_passes_per_day: int = Field(ge=0)
    #: Share of the day's passes reserved for exit reviews (ruling 2026-08-31).
    #: Entries stop at (1 - this); reviews may spend the whole remainder. A floor
    #: under the exit layer, not a ceiling on it.
    review_budget_reserve_fraction: Decimal = Field(
        default=Decimal("0.25"), ge=Decimal("0"), lt=Decimal("1")
    )
    #: Dollars of estimated research spend per UTC day before ONE COST warning
    #: line goes to run.log. A warning, not a stop — the pass budget above is
    #: the hard ceiling; this makes the bill visible before the console does.
    daily_cost_warning_usd: Decimal = Field(default=Decimal("10"), ge=Decimal("0"))
    tick_interval_seconds: int = Field(gt=0)
    account_type: AccountType
    exits: ExitsConfig
    market_data: MarketDataConfig

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "OrchestratorConfig":
        path = path or default_orchestrator_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_orchestrator_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "orchestrator.yaml"
