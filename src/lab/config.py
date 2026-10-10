"""``config/lab.yaml``, validated (PAPER PUSH, human ruling 2026-10-09 late)."""
from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_gate.limits import RiskLimits, default_limits_path

ZERO = Decimal("0")
ONE = Decimal("1")


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AccountConfig(_Strict):
    enabled: bool
    label: str
    sleeve: str
    live_eligible: bool = False
    status: Optional[str] = None
    limits_overrides: dict[str, Any] = Field(default_factory=dict)
    #: The account's drawdown kill switch (12% from its own high-water mark,
    #: the main risk_limits value). OFF only for the leverage ladder (ruling
    #: 2026-10-10, item 3a: the benchmark must keep rebalancing through
    #: drawdowns); every other lab account keeps it - LabConfig enforces that.
    kill_switch: bool = True
    #: Drawdown from peak that raises an urgent alert (once per peak).
    drawdown_alert: Optional[Decimal] = Field(default=None, gt=ZERO, lt=ONE)


class Rung(_Strict):
    name: str
    label: str
    weights: dict[str, Decimal]

    @model_validator(mode="after")
    def _weights(self) -> "Rung":
        if not self.weights or any(w <= ZERO for w in self.weights.values()):
            raise ValueError(f"rung {self.name}: weights must be positive")
        if sum(self.weights.values()) != ONE:
            raise ValueError(f"rung {self.name}: weights must sum to 1")
        return self


class LadderConfig(_Strict):
    rungs: tuple[Rung, ...]
    cash_reserve_fraction: Decimal = Field(ge=ZERO, lt=Decimal("0.1"))
    min_trade_usd: Decimal = Field(gt=ZERO)
    limit_buffer: Decimal = Field(ge=ZERO, lt=Decimal("0.02"))
    reprice_seconds: int = Field(gt=0)
    fill_wait_seconds: int = Field(gt=0)
    trade_after: str
    trade_before: str

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({s for rung in self.rungs for s in rung.weights}))


class AiOptionsConfig(_Strict):
    """How the AI trader's rules engine picks a contract for a call/put idea:
    deterministic, never the model's choice of strike."""

    #: Days to expiry allowed by horizon: [min, max]. intraday may be 0DTE.
    dte: dict[str, tuple[int, int]]
    delta_min: Decimal = Field(gt=ZERO, lt=ONE)
    delta_max: Decimal = Field(gt=ZERO, le=ONE)
    target_delta: Decimal = Field(gt=ZERO, lt=ONE)
    min_open_interest: int = Field(ge=0)
    max_spread_pct_of_mid: Decimal = Field(gt=ZERO, le=ONE)
    max_iv_percentile: Decimal = Field(gt=ZERO, le=ONE)
    #: Close time (ET) on a contract's expiry day; a multi-day contract is
    #: also closed at this time on the session BEFORE expiry.
    expiry_close: str

    @model_validator(mode="after")
    def _bands(self) -> "AiOptionsConfig":
        if not self.delta_min <= self.target_delta <= self.delta_max:
            raise ValueError("target_delta must sit inside [delta_min, delta_max]")
        for horizon in ("intraday", "days", "weeks"):
            low, high = self.dte.get(horizon, (-1, -1))
            if low < 0 or high < low:
                raise ValueError(f"options dte for {horizon!r} must be [min, max] with 0 <= min <= max")
        return self


class AiTraderConfig(_Strict):
    """2b, the AUTONOMOUS AI TRADER (PAPER PUSH 2026-10-09; built per ruling
    2026-10-10, item 5). The LLM decides what and why; these rules decide
    how much, when out, and whether at all."""

    # -- the model (must be in research.yaml pinned_models) -------------------
    model: str
    effort: str
    max_searches: int = Field(ge=1, le=5)
    #: The search tool version (see research.config.WebSearchConfig.tool_type).
    search_tool: str
    #: Per-request timeout; one retry. A pass that cannot finish is a fault
    #: (no trade), never a frozen session.
    request_timeout_seconds: int = Field(gt=0, le=600)
    # -- spend: the daily cap is hard; a pass starts only with reserve left ---
    daily_spend_cap_usd: Decimal = Field(gt=ZERO)
    pass_cost_reserve_usd: Decimal = Field(gt=ZERO)
    search_fee_usd: Decimal = Field(ge=ZERO)
    # -- cadence ---------------------------------------------------------------
    scan_times: tuple[str, ...]
    max_ideas_per_scan: int = Field(gt=0, le=5)
    tick_seconds: int = Field(gt=0)
    # -- the rules engine --------------------------------------------------------
    risk_fraction: Decimal = Field(gt=ZERO, le=Decimal("0.05"))
    max_positions: int = Field(gt=0)
    min_confidence: int = Field(ge=0, le=100)
    min_reward_risk: Decimal = Field(ge=ZERO)
    min_stop_fraction: Decimal = Field(gt=ZERO, lt=ONE)
    max_stop_fraction: Decimal = Field(gt=ZERO, lt=ONE)
    no_entries_after: str
    no_intraday_entries_after: str
    intraday_close: str
    horizon_sessions: dict[str, int]
    limit_buffer: Decimal = Field(ge=ZERO, lt=Decimal("0.05"))
    fill_wait_seconds: int = Field(gt=0)
    options: AiOptionsConfig

    @model_validator(mode="after")
    def _sane(self) -> "AiTraderConfig":
        if self.min_stop_fraction >= self.max_stop_fraction:
            raise ValueError("min_stop_fraction must be below max_stop_fraction")
        if self.pass_cost_reserve_usd > self.daily_spend_cap_usd:
            raise ValueError("pass_cost_reserve_usd cannot exceed the daily cap")
        if set(self.horizon_sessions) != {"days", "weeks"}:
            raise ValueError("horizon_sessions needs exactly 'days' and 'weeks'")
        return self


class LabConfig(_Strict):
    version: int
    accounts: dict[str, AccountConfig]
    ladder: LadderConfig
    ai_trader: Optional[AiTraderConfig] = None

    @model_validator(mode="after")
    def _pinned_model(self) -> "LabConfig":
        """The AI trader's model obeys the main model-pinning rule (ruling
        2026-09-02): it must be on research.yaml's closed pinned_models list,
        and never a moving ``-latest`` alias."""
        if self.ai_trader is None:
            return self
        model = self.ai_trader.model
        research = Path(__file__).resolve().parents[2] / "config" / "research.yaml"
        with open(research, "r", encoding="utf-8") as handle:
            pinned = (yaml.safe_load(handle) or {}).get("pinned_models") or []
        if model.endswith("-latest") or model not in pinned:
            raise ValueError(f"ai_trader.model {model!r} is not on research.yaml pinned_models {pinned}")
        return self

    @model_validator(mode="after")
    def _kill_switch_off_only_for_the_ladder(self) -> "LabConfig":
        for name, account in self.accounts.items():
            if not account.kill_switch and account.sleeve != "leverage_ladder":
                raise ValueError(
                    f"lab account {name!r}: only the leverage ladder may run without its kill switch "
                    f"(ruling 2026-10-10, item 3a); the 12% switch stays for every other lab account"
                )
        return self

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "LabConfig":
        path = path or Path(__file__).resolve().parents[2] / "config" / "lab.yaml"
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))

    def account(self, name: str) -> AccountConfig:
        if name not in self.accounts:
            raise KeyError(f"no lab account named {name!r} (config/lab.yaml has {sorted(self.accounts)})")
        return self.accounts[name]


def _merge(base: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def account_limits(account: AccountConfig, base_path: Optional[Path] = None) -> RiskLimits:
    """The account's cap table: the main ``risk_limits.yaml`` with the
    account's overrides on top, validated by the SAME ``RiskLimits`` model -
    so an override that switches margin on or permits shorting fails to load
    (Constraints #1 and #2 are not configuration)."""
    with open(base_path or default_limits_path(), "r", encoding="utf-8") as handle:
        base = yaml.safe_load(handle)
    merged = _merge(base, account.limits_overrides)
    if not account.kill_switch:
        # The drawdown trip at 100% of the high-water mark: unreachable in a
        # cash account (NAV cannot go below zero). The operator halt marker
        # still freezes the account by hand.
        merged["kill_switch"]["drawdown_from_high_water_mark"] = "1"
    return RiskLimits.model_validate(merged)
