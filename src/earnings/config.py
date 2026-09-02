"""Typed view of ``config/earnings.yaml``.

Same rule as every other config module: the YAML is the source of truth and
nothing here invents a default for a universe or a window. The universe in
particular is human-owned — this package cannot widen what it watches.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EarningsConfig(_Strict):
    """What the shadow logger watches, and when it looks."""

    version: int
    #: Whether the logger runs at all. False means a pass does nothing and says so.
    enabled: bool = True
    #: The names it watches. Human-owned: this package never adds to it, and an
    #: empty universe is a logger that observes nothing rather than everything.
    universe: tuple[str, ...] = ()
    #: How far ahead the calendar is scanned each pass.
    calendar_window_days: int = Field(default=14, gt=0)
    #: A print is armed when it is this many days out or nearer. Far enough
    #: ahead that the straddle is liquid, near enough that it is priced for the
    #: event rather than for the month.
    arm_within_days: int = Field(default=3, gt=0)
    #: Shortest expiry to consider, in days after the print. The straddle must
    #: outlive the event it is pricing.
    min_days_after_print: int = Field(default=1, ge=0)
    #: Longest expiry to consider, so the "implied move" is the event's and not
    #: a quarter's.
    max_days_after_print: int = Field(default=21, gt=0)
    #: Liquidity floors. A straddle nobody trades prices nothing, and recording
    #: its "implied move" would poison the series the whole exercise produces.
    min_open_interest: int = Field(default=100, ge=0)
    max_spread_pct_of_mid: Decimal = Field(default=Decimal("0.20"), gt=Decimal("0"))
    #: Sessions after the print at which the result is marked.
    settle_after_days: int = Field(default=1, gt=0)
    #: IV-watch widening (human ruling 2026-09-02): ceiling on the number of
    #: names snapshotted daily — the earnings universe plus whatever the trading
    #: loop's iv_watch.json hands over — so the daily chain fetches stay bounded.
    iv_watch_max_names: int = Field(default=60, gt=0)

    @model_validator(mode="after")
    def _expiry_window_is_ordered(self) -> "EarningsConfig":
        if self.min_days_after_print > self.max_days_after_print:
            raise ValueError(
                f"expiry window is inverted: {self.min_days_after_print} > "
                f"{self.max_days_after_print}"
            )
        if self.arm_within_days > self.calendar_window_days:
            raise ValueError(
                f"arm_within_days {self.arm_within_days} exceeds the calendar "
                f"window {self.calendar_window_days}; prints would be armed "
                f"before they are ever seen"
            )
        return self

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "EarningsConfig":
        path = path or default_earnings_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_earnings_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "earnings.yaml"
