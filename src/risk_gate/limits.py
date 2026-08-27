"""Typed view of ``config/risk_limits.yaml``.

The YAML is the source of truth and changing it needs human approval. This module
only parses it — it applies no defaults for a cap, because a silently-defaulted cap is
a cap nobody approved. Every limit the gate enforces must be present in the file.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Fraction = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1"))]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SleeveWeights(_Strict):
    equity: Fraction
    #: The mechanical disclosure follower (human ruling 2026-08-27).
    mechanical: Fraction
    prediction: Fraction

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "SleeveWeights":
        total = self.equity + self.mechanical + self.prediction
        if total != Decimal("1"):
            raise ValueError(f"sleeve weights must sum to 1, got {total}")
        return self


class PortfolioLimits(_Strict):
    sleeves: SleeveWeights
    drift_tolerance: Fraction
    rebalance: str


class AccountLimits(_Strict):
    cash_secured_only: bool
    margin_enabled: bool
    short_selling: str
    option_writing: str

    @model_validator(mode="after")
    def _constraints_are_not_negotiable(self) -> "AccountLimits":
        """Constraints #1 and #2 are not configuration.

        A config file that switches margin on, or permits writing, is not a
        configuration change — it is an attempt to edit an inviolable constraint, and
        the gate refuses to start rather than honour it.
        """
        if self.margin_enabled:
            raise ValueError("margin_enabled must be false (Constraint #1)")
        if not self.cash_secured_only:
            raise ValueError("cash_secured_only must be true (Constraint #1)")
        if self.short_selling != "forbidden":
            raise ValueError("short_selling must be 'forbidden' (Constraint #2)")
        if self.option_writing != "forbidden":
            raise ValueError("option_writing must be 'forbidden' (Constraint #2)")
        return self


class EquitySleeveLimits(_Strict):
    max_single_position: Fraction
    max_daily_deployment: Fraction
    max_options_premium_at_risk: Fraction
    #: Aggregate equity exposure per sector, of sleeve NAV (config/sectors.yaml
    #: defines membership; unmapped tickers are singleton sectors).
    max_sector_exposure: Fraction
    #: Smallest opening equity order the gate will pass, in dollars. Fractional
    #: shares (2026-08-20) can compute dust positions; below this floor the order is
    #: a typed rejection, not a trade. Scoped to opening equity orders only: closes
    #: are risk-reducing and must never be trapped by a floor, and the prediction
    #: sleeve's arb strategy is explicitly micro-unit.
    min_order_notional_usd: Annotated[Decimal, Field(ge=Decimal("0"))]


class MechanicalSleeveLimits(_Strict):
    """The mechanical disclosure follower's cap table (human ruling 2026-08-27).

    A controlled experiment: deterministic diversified copying of congressional
    purchase disclosures, no LLM in the path, long holds. The slot caps and the
    hold rule are the strategy; the fractions are the gate's backstops."""

    #: Gate backstop, of mechanical sleeve NAV (slices are ~1/max_positions).
    max_single_position: Fraction
    #: Of mechanical sleeve NAV, per trading day — its own budget, isolated
    #: from the judged sleeve's daily deployment cap.
    max_daily_deployment: Fraction
    #: Gate backstop, of mechanical sleeve NAV; the slot cap is the primary
    #: sector control. Same membership table (config/sectors.yaml), same
    #: unmapped-name-is-a-singleton convention.
    max_sector_exposure: Fraction
    #: Equal-weight slots: slice = sleeve NAV / max_positions.
    max_positions: Annotated[int, Field(gt=0)]
    max_per_filer: Annotated[int, Field(gt=0)]
    #: Slots per MAPPED sector; unmapped names are unconstrained singletons.
    max_per_sector_slots: Annotated[int, Field(gt=0)]
    #: Time exit: close each position this many days after fill. No price
    #: stop — the stop IS the slice size, and tight stops would amputate the
    #: winners the strategy exists to hold.
    hold_days: Annotated[int, Field(gt=0)]
    #: Sleeve circuit breaker: drawdown from the sleeve's OWN high-water mark
    #: beyond this halts new mechanical entries until a human resets
    #: (strictly greater; same discipline as the global kill switch).
    drawdown_halt_fraction: Fraction
    #: Stamped on every mechanical entry record so attribution can partition
    #: history across rule changes.
    ruleset_version: str


class KillSwitchLimits(_Strict):
    drawdown_from_high_water_mark: Fraction
    halts: str
    reset: str

    @model_validator(mode="after")
    def _reset_is_human_only(self) -> "KillSwitchLimits":
        if self.reset != "manual_human_only":
            raise ValueError("kill switch reset must be 'manual_human_only'")
        return self


class PdtLimits(_Strict):
    equity_threshold_usd: Decimal
    enforce_day_trade_count_in_margin_account: bool
    preferred_account_type: str
    #: FINRA: four or more day trades in five rolling business days.
    max_day_trades_per_window: int = 3
    window_business_days: int = 5


class SizingBand(_Strict):
    min: int
    max: int
    size: Fraction
    lower_inclusive: bool = False

    def contains(self, confidence: int) -> bool:
        """Bands are (min, max] unless explicitly lower-inclusive.

        Per Inviolable Constraint #6, a confidence landing exactly on a shared
        boundary takes the smaller size, which falls out of the half-open interval
        closing at the top.
        """
        lower_ok = confidence >= self.min if self.lower_inclusive else confidence > self.min
        return lower_ok and confidence <= self.max


class OptionSizing(_Strict):
    basis: str
    multiplier: Fraction


class SizingLimits(_Strict):
    no_trade_below: int
    bounds: str
    bands: tuple[SizingBand, ...]
    hard_cap: Fraction
    options: OptionSizing

    def size_for(self, confidence: int) -> Decimal:
        """Fraction of sleeve NAV for a confidence score. Zero means do not trade."""
        if confidence < self.no_trade_below:
            return Decimal("0")
        for band in self.bands:
            if band.contains(confidence):
                return min(band.size, self.hard_cap)
        return Decimal("0")

    @model_validator(mode="after")
    def _bands_never_exceed_hard_cap(self) -> "SizingLimits":
        for band in self.bands:
            if band.size > self.hard_cap:
                raise ValueError(
                    f"band {band.min}-{band.max} size {band.size} exceeds hard cap "
                    f"{self.hard_cap}"
                )
        return self


class ArbitrageLimits(_Strict):
    max_position: Fraction
    fee_clearance_multiplier: Decimal
    fees_source: str
    high_turnover_expected: bool


class DirectionalLimits(_Strict):
    min_divergence_points: int
    max_position: Fraction


class PredictionSleeveLimits(_Strict):
    arbitrage: ArbitrageLimits
    directional: DirectionalLimits


class DeltaBand(_Strict):
    """Confidence -> |delta| range for strike selection. Same boundary semantics
    as SizingBand: (min, max] unless lower_inclusive — a confidence landing on a
    shared boundary takes the DEEPER-ITM band (more intrinsic = less risk,
    Constraint #6)."""

    min: int
    max: int
    delta_min: Decimal
    delta_max: Decimal
    lower_inclusive: bool = False

    def contains(self, confidence: int) -> bool:
        lower_ok = (
            confidence >= self.min if self.lower_inclusive else confidence > self.min
        )
        return lower_ok and confidence <= self.max

    @model_validator(mode="after")
    def _range_is_sane(self) -> "DeltaBand":
        if not Decimal("0") < self.delta_min < self.delta_max <= Decimal("1"):
            raise ValueError(
                f"delta band must satisfy 0 < delta_min < delta_max <= 1, got "
                f"[{self.delta_min}, {self.delta_max}]"
            )
        return self


class MinExpiryDays(_Strict):
    """Per-horizon expiry floors: the thesis gets room to be slow."""

    days: int
    weeks: int
    months: int

    def for_horizon(self, horizon: str) -> int:
        table = {"days": self.days, "weeks": self.weeks, "months": self.months}
        if horizon not in table:
            raise ValueError(f"unknown time horizon {horizon!r}")
        return table[horizon]


class OptionsSelectionLimits(_Strict):
    """Deterministic option-selection thresholds (build ruling 2026-08-24).

    Loaded like every other cap: no defaults, human approval to change."""

    min_expiry_days: MinExpiryDays
    delta_bands: tuple[DeltaBand, ...]
    #: Absolute |delta| floor for ANY band — never OTM lottery strikes.
    min_delta_floor: Decimal
    min_open_interest: int
    max_spread_pct_of_mid: Fraction
    #: Ceiling on the pick's IV percentile within its own chain's IV population.
    max_iv_percentile: Fraction
    #: Close any long option this many days before expiry, thesis or no thesis.
    close_before_expiry_days: int

    def band_for(self, confidence: int) -> Optional[DeltaBand]:
        for band in self.delta_bands:
            if band.contains(confidence):
                return band
        return None

    @model_validator(mode="after")
    def _bands_respect_the_floor(self) -> "OptionsSelectionLimits":
        """A band below the floor is a config contradiction, not a preference."""
        for band in self.delta_bands:
            if band.delta_min < self.min_delta_floor:
                raise ValueError(
                    f"delta band {band.min}-{band.max} starts at {band.delta_min}, "
                    f"below the {self.min_delta_floor} floor"
                )
        if self.close_before_expiry_days < 1:
            raise ValueError("close_before_expiry_days must be at least 1")
        return self


class ExecutionLimits(_Strict):
    default_order_type: str
    market_orders_require_justification: bool


class RiskLimits(_Strict):
    """The whole of ``risk_limits.yaml``, validated."""

    version: int
    account: AccountLimits
    portfolio: PortfolioLimits
    equity_sleeve: EquitySleeveLimits
    kill_switch: KillSwitchLimits
    pdt: PdtLimits
    sizing: SizingLimits
    mechanical_sleeve: MechanicalSleeveLimits
    prediction_sleeve: PredictionSleeveLimits
    options_selection: OptionsSelectionLimits
    execution: ExecutionLimits

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "RiskLimits":
        """Load and validate the limits file.

        Raises rather than falling back to defaults: a gate running on assumed caps is
        worse than a gate that will not start.
        """
        path = path or default_limits_path()
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        return cls.model_validate(raw)


def default_limits_path() -> Path:
    """``config/risk_limits.yaml`` relative to the repository root."""
    return Path(__file__).resolve().parents[2] / "config" / "risk_limits.yaml"
