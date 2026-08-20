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
    prediction: Fraction

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "SleeveWeights":
        total = self.equity + self.prediction
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
    prediction_sleeve: PredictionSleeveLimits
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
