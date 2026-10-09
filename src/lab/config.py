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
    return RiskLimits.model_validate(_merge(base, account.limits_overrides))
