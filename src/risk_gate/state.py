"""Account state the gate reasons over: cash, positions, drawdown, day trades.

The reserve-then-settle model
-----------------------------
Approving an order does not move cash or holdings. It *reserves* them:

  - An approved buy reserves cash (``reserved_cash``), so a second order cannot spend
    the same dollars. ``buying_power = cash - reserved_cash`` is the quantity the
    never-negative constraint is asserted against.
  - An approved sell reserves units of the position (``reserved_close``), so two
    close orders cannot each sell the same shares. Without this, two individually
    valid closes could together exceed the held quantity and synthesise a short.

Settlement happens in ``record_fill``. This split matters for the kill switch: NAV is
deliberately unchanged by an approval (cash is committed but not yet spent), so
queueing orders can never itself trip a drawdown halt.

Marks
-----
Positions carry a market value that starts at cost basis and is updated by
``mark_to_market``. Until a real mark arrives, cost basis is the assumption — stated
here rather than hidden, because every percentage-of-NAV cap depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

from risk_gate.schema import (
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    EventContractSellToCloseOrder,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
    Order,
)

ZERO = Decimal("0")


class Sleeve(StrEnum):
    """CLAUDE.md § Portfolio Structure: 90% equity (incl. long options) / 10% events."""

    EQUITY = "equity"
    PREDICTION = "prediction"


class AccountType(StrEnum):
    CASH = "cash"
    MARGIN = "margin"


#: Identifies an instrument. Options key on the OCC symbol, which already encodes
#: expiration/strike/right, so two different contracts never collide. Event contracts
#: key on (market, outcome): YES and NO are separate instruments.
PositionKey = tuple[str, ...]


def sleeve_of(order: Order) -> Sleeve:
    if isinstance(order, (EventContractBuyOrder, EventContractSellToCloseOrder)):
        return Sleeve.PREDICTION
    return Sleeve.EQUITY


def is_option(order: Order) -> bool:
    return isinstance(order, (OptionBuyToOpenOrder, OptionSellToCloseOrder))


def position_key(order: Order) -> PositionKey:
    if isinstance(order, (EquityBuyOrder, EquitySellToCloseOrder)):
        return ("equity", order.symbol)
    if isinstance(order, (OptionBuyToOpenOrder, OptionSellToCloseOrder)):
        return ("option", order.symbol)
    if isinstance(order, (EventContractBuyOrder, EventContractSellToCloseOrder)):
        return ("event", order.market_ticker, order.outcome)
    raise TypeError(f"unclassifiable order type: {type(order).__name__}")


def units_of(order: Order) -> Decimal:
    """Shares for equity (possibly fractional), contracts for the rest.

    Always Decimal so position arithmetic is one exact code path: contract counts
    are whole numbers, which Decimal represents exactly.
    """
    if isinstance(order, (EquityBuyOrder, EquitySellToCloseOrder)):
        return order.quantity
    return Decimal(order.contracts)


def unit_multiplier(order: Order) -> int:
    """Shares per contract. 1 for equity and event contracts."""
    if is_option(order):
        return order.multiplier
    return 1


@dataclass(slots=True)
class Position:
    """A held instrument. Quantity is settled units; reservations sit alongside."""

    key: PositionKey
    sleeve: Sleeve
    #: Decimal, not int: equity may hold fractional shares. Options and event
    #: contracts remain whole numbers, which Decimal carries exactly.
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO
    market_value: Decimal = ZERO
    unit_multiplier: int = 1
    is_option: bool = False
    #: Units promised to approved-but-unfilled close orders.
    reserved_close: Decimal = ZERO
    #: Units and cash promised to approved-but-unfilled open orders.
    pending_open_units: Decimal = ZERO
    pending_open_cost: Decimal = ZERO
    #: Approval date of the most recent opening order — drives day-trade detection.
    last_open_date: date | None = None

    @property
    def available_to_close(self) -> Decimal:
        """Units a new close order may claim without overselling."""
        return self.quantity - self.reserved_close

    @property
    def exposure(self) -> Decimal:
        """Current value plus cash already committed to unfilled opens."""
        return self.market_value + self.pending_open_cost

    @property
    def is_empty(self) -> bool:
        return (
            self.quantity == 0
            and self.reserved_close == 0
            and self.pending_open_units == 0
        )


@dataclass(slots=True)
class AccountState:
    """Everything the gate needs to decide, and nothing it doesn't."""

    cash: Decimal
    high_water_mark: Decimal
    account_type: AccountType = AccountType.CASH
    positions: dict[PositionKey, Position] = field(default_factory=dict)
    #: Cash committed to approved-but-unfilled buys.
    reserved_cash: Decimal = ZERO
    #: Worst-case capital deployed in the equity sleeve on ``deployment_date``.
    deployed_today: Decimal = ZERO
    deployment_date: date | None = None
    #: Dates on which a day trade completed (open and close on the same day).
    day_trades: list[date] = field(default_factory=list)
    kill_switch_tripped: bool = False

    # -- derived ---------------------------------------------------------------

    @property
    def buying_power(self) -> Decimal:
        """Spendable cash. The never-negative invariant is asserted on this."""
        return self.cash - self.reserved_cash

    @property
    def positions_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions.values()), ZERO)

    @property
    def nav(self) -> Decimal:
        """Net asset value. Unchanged by approvals; moves on fills and marks."""
        return self.cash + self.positions_value

    def sleeve_exposure(self, sleeve: Sleeve) -> Decimal:
        return sum(
            (p.exposure for p in self.positions.values() if p.sleeve is sleeve), ZERO
        )

    @property
    def options_premium_at_risk(self) -> Decimal:
        """Aggregate open long-option premium, including unfilled approvals."""
        return sum((p.exposure for p in self.positions.values() if p.is_option), ZERO)

    def drawdown(self) -> Decimal:
        """Fractional drawdown from the high-water mark. Zero if at or above it."""
        if self.high_water_mark <= ZERO:
            return ZERO
        loss = self.high_water_mark - self.nav
        if loss <= ZERO:
            return ZERO
        return loss / self.high_water_mark

    # -- mutation --------------------------------------------------------------

    def position(self, key: PositionKey) -> Position | None:
        return self.positions.get(key)

    def ensure_position(
        self, key: PositionKey, sleeve: Sleeve, multiplier: int, option: bool
    ) -> Position:
        existing = self.positions.get(key)
        if existing is not None:
            return existing
        created = Position(
            key=key, sleeve=sleeve, unit_multiplier=multiplier, is_option=option
        )
        self.positions[key] = created
        return created

    def drop_if_empty(self, key: PositionKey) -> None:
        position = self.positions.get(key)
        if position is not None and position.is_empty:
            del self.positions[key]

    def roll_deployment_window(self, today: date) -> None:
        """The daily deployment cap is per trading day; reset when the day changes."""
        if self.deployment_date != today:
            self.deployment_date = today
            self.deployed_today = ZERO

    def refresh_high_water_mark(self) -> None:
        current = self.nav
        if current > self.high_water_mark:
            self.high_water_mark = current

    def apply_marks(self, marks: Mapping[PositionKey, Decimal]) -> None:
        """Update market values from per-unit prices."""
        for key, price in marks.items():
            position = self.positions.get(key)
            if position is None:
                continue
            position.market_value = price * position.quantity * position.unit_multiplier

    def day_trades_in_window(self, today: date, business_days: int) -> int:
        cutoff = business_days_before(today, business_days - 1)
        return sum(1 for d in self.day_trades if d >= cutoff)


def business_days_before(day: date, count: int) -> date:
    """Walk back ``count`` weekdays.

    Weekends only — no exchange holiday calendar. That makes the window slightly
    longer than FINRA's in a holiday week, i.e. it counts marginally more day trades
    toward the limit than strictly required. Erring toward the tighter reading is
    deliberate (Constraint #6); a real holiday calendar is the proper fix.
    """
    remaining = count
    cursor = day
    while remaining > 0:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor
