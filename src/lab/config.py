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


class LabConfig(_Strict):
    version: int
    accounts: dict[str, AccountConfig]
    ladder: LadderConfig

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
