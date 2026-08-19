"""Typed rejection reasons.

CLAUDE.md § Audit & Attribution: rejected orders are logged with their rejection
reason, and risk-gate rejections are signal, not noise. So a rejection is a typed
value carrying the limit and the observed figure that breached it — not a bare
boolean or a free-text string an analyst has to parse later.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Optional


class RejectionCode(StrEnum):
    """Every way the gate can say no."""

    #: Kill switch is tripped; nothing passes until a human resets it.
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    #: Order's worst-case cost exceeds available cash. Cash-secured means cash-secured.
    INSUFFICIENT_BUYING_POWER = "insufficient_buying_power"
    #: Close-only order against a position the account does not hold at all.
    POSITION_NOT_HELD = "position_not_held"
    #: Close-only order larger than the un-reserved held quantity. Allowing this would
    #: synthesise a short — the exact thing Constraint #2 forbids.
    CLOSE_EXCEEDS_HELD_QUANTITY = "close_exceeds_held_quantity"
    #: Resulting position exceeds max single position (% of sleeve NAV).
    MAX_SINGLE_POSITION_EXCEEDED = "max_single_position_exceeded"
    #: Today's cumulative deployment would exceed the daily cap.
    MAX_DAILY_DEPLOYMENT_EXCEEDED = "max_daily_deployment_exceeded"
    #: Aggregate open long-option premium would exceed the cap.
    MAX_OPTIONS_PREMIUM_EXCEEDED = "max_options_premium_exceeded"
    #: Aggregate equity exposure in one sector would exceed the per-sector cap.
    SECTOR_CONCENTRATION = "sector_concentration"
    #: Order would push a sleeve outside its target weight plus drift tolerance.
    SLEEVE_ALLOCATION_EXCEEDED = "sleeve_allocation_exceeded"
    #: Pattern-day-trader limit reached in a sub-threshold margin account.
    PDT_LIMIT_REACHED = "pdt_limit_reached"


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why an order was refused, with the numbers that justify it."""

    code: RejectionCode
    message: str
    limit: Optional[Decimal] = None
    observed: Optional[Decimal] = None

    @property
    def is_approved(self) -> bool:
        return False

    def __str__(self) -> str:  # pragma: no cover - convenience for logs
        detail = ""
        if self.limit is not None and self.observed is not None:
            detail = f" (limit {self.limit}, observed {self.observed})"
        return f"{self.code}: {self.message}{detail}"
