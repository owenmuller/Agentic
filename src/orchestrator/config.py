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


class ConvergenceConfig(BaseModel):
    """Signal convergence: registry window and dispatch bonuses (ruling 2026-09-01).

    ORDERING AND CONTEXT ONLY. The bonuses join the dispatch sort key — which
    admitted signals spend limited research slots first — and the registry's
    summary joins the research prompt as fenced data. Nothing here can touch a
    cap, a size, or the gate, by the same rule as the 2026-08-26 dispatch weight.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: How long a signal counts as "active" on its symbol.
    window_days: int = Field(default=14, gt=0)
    #: 4a cross-filer clustering: bonus per OTHER congressional filer disclosing
    #: a purchase of the same name inside the window, and its cap. Sized against
    #: the base weight's log10(amount) scale (~4-7): two clustered filers move a
    #: signal several days of freshness up the queue, never across a class.
    cluster_bonus_per_filer: Decimal = Field(default=Decimal("1.0"), ge=Decimal("0"))
    cluster_bonus_cap: Decimal = Field(default=Decimal("3.0"), ge=Decimal("0"))
    #: Source-diversity bonus: per OTHER independent source active on the name.
    #: DIVERSITY, never count — ten posts from one account are one source.
    diversity_bonus_per_source: Decimal = Field(
        default=Decimal("0.5"), ge=Decimal("0")
    )
    diversity_bonus_cap: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0"))
    #: Most recent prior research verdicts on the name shown to the research pass.
    max_prior_verdicts: int = Field(default=3, gt=0)


class DrawdownStep(BaseModel):
    """One rung of the drawdown ladder: at or beyond this drawdown, this multiplier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: Decimal = Field(gt=Decimal("0"), lt=Decimal("1"))
    multiplier: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))


class RegimeStep(BaseModel):
    """One rung of the regime scalar: at or above this VIX close, this multiplier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vix_at_or_above: Decimal = Field(gt=Decimal("0"))
    multiplier: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))


class RegimeScalarConfig(BaseModel):
    """The volatility-regime scalar (approved 2026-09-01, built 2026-09-02)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    thresholds: tuple[RegimeStep, ...] = (
        RegimeStep(vix_at_or_above=Decimal("25"), multiplier=Decimal("0.75")),
        RegimeStep(vix_at_or_above=Decimal("35"), multiplier=Decimal("0.5")),
    )
    #: A VIX close older than this many calendar days is treated as MISSING.
    max_age_days: int = Field(default=7, gt=0)

    @model_validator(mode="after")
    def _rungs_descend(self) -> "RegimeScalarConfig":
        rungs = self.thresholds
        for earlier, later in zip(rungs, rungs[1:]):
            if not (
                earlier.vix_at_or_above < later.vix_at_or_above
                and earlier.multiplier >= later.multiplier
            ):
                raise ValueError(
                    "regime rungs must rise in VIX and never rise in multiplier"
                )
        return self


class AtrSizingConfig(BaseModel):
    """Volatility-adjusted sizing and stops (human ruling 2026-09-02).

    NEW judged EQUITY entries only: the stop moves from the fixed 15% to
    ``k x ATR(14)/price`` clamped into [stop_floor, stop_ceiling], and the size
    equalizes dollar risk — ``min(band capital, band capital x
    risk_budget_fraction / stop distance)`` — which can only SHRINK below the
    band (the band caps stay ceilings; with the shipped numbers the min binds
    at any stop tighter than 15%). Options excluded (premium is the stop);
    held positions keep the stops they were opened with; the mechanical arm
    has no price stop at all. Missing ATR data falls back to the fixed-15%
    regime — status quo ante, never a fabricated volatility.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    k: Decimal = Field(default=Decimal("2.5"), gt=Decimal("0"))
    stop_floor: Decimal = Field(default=Decimal("0.08"), gt=Decimal("0"))
    stop_ceiling: Decimal = Field(default=Decimal("0.20"), gt=Decimal("0"))
    #: Dollar risk per position as a fraction of the band's capital — 0.15
    #: matches the old fixed stop, so the worst-case loss per position is
    #: unchanged by this ruling.
    risk_budget_fraction: Decimal = Field(default=Decimal("0.15"), gt=Decimal("0"))
    #: The adverse review trigger as a fraction of the position's own stop
    #: distance — the trigger must fire while there is still a decision to
    #: make, whatever the stop is.
    trigger_down_of_stop: Decimal = Field(
        default=Decimal("0.66"), gt=Decimal("0"), lt=Decimal("1")
    )

    @model_validator(mode="after")
    def _floor_under_ceiling(self) -> "AtrSizingConfig":
        if self.stop_floor > self.stop_ceiling:
            raise ValueError(
                f"ATR stop floor {self.stop_floor} exceeds ceiling "
                f"{self.stop_ceiling}"
            )
        return self


class RiskScalarsConfig(BaseModel):
    """Post-table sizing multipliers (human rulings 2026-09-01/02): the graduated
    drawdown ladder and the volatility-regime scalar, composed at ONE point.

    Both are ≤1.0 BY VALIDATION — this section can only ever shrink a size, so
    the file's "nothing here can widen a risk limit" contract holds. Applied to
    NEW judged entries only, after the confidence table, LLM-unreachable; the
    mechanical arm never passes through the pipeline's sizing and is exempt by
    construction. The kill switch at 12% is UNTOUCHED — the ladder's last rung
    deliberately stops below it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    drawdown_steps: tuple[DrawdownStep, ...] = (
        DrawdownStep(at=Decimal("0.04"), multiplier=Decimal("0.75")),
        DrawdownStep(at=Decimal("0.08"), multiplier=Decimal("0.5")),
    )
    regime: RegimeScalarConfig = Field(default_factory=RegimeScalarConfig)

    @model_validator(mode="after")
    def _ladder_descends(self) -> "RiskScalarsConfig":
        steps = self.drawdown_steps
        for earlier, later in zip(steps, steps[1:]):
            if not (earlier.at < later.at and earlier.multiplier >= later.multiplier):
                raise ValueError(
                    "ladder steps must deepen in drawdown and never rise in "
                    "multiplier"
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
    #: Signal convergence (ruling 2026-09-01). Defaults apply when the yaml has
    #: no section — the registry is context and ordering, not a risk control, so
    #: an absent section means the shipped defaults rather than a startup error.
    convergence: ConvergenceConfig = Field(default_factory=ConvergenceConfig)
    #: Post-table sizing scalars (rulings 2026-09-01/02). Validation-bounded to
    #: ≤1.0, so the defaults applying on an absent section can only shrink.
    risk_scalars: RiskScalarsConfig = Field(default_factory=RiskScalarsConfig)
    #: Volatility-adjusted sizing and stops (ruling 2026-09-02). The resize can
    #: only shrink below the band, so absent-section defaults are safe here too.
    atr_sizing: AtrSizingConfig = Field(default_factory=AtrSizingConfig)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "OrchestratorConfig":
        path = path or default_orchestrator_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_orchestrator_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "orchestrator.yaml"
