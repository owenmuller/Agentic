"""Order schema — the structural half of the risk gate.

Two constraints from CLAUDE.md are enforced *here*, in the types, rather than in a
validator that a later refactor could weaken or a caller could skip.

Constraint #2 — options are long-only; writing an option must be unrepresentable:
  - No order class in this module expresses a sell-to-open, write, or short.
    `OptionSellToCloseOrder` exists only to exit a position already held long.
  - Every model sets ``extra="forbid"``, so a caller cannot smuggle an unmodelled
    field (``side="sell_to_open"``) past validation.
  - ``Order`` is a closed union discriminated on ``kind``. A payload naming any other
    action fails to parse — there is no permissive fallback branch.
  - Every model is frozen, so an order cannot be mutated after the gate approves it.

Constraint #1 — the account can never go negative:
  - Every order reports a finite, non-negative ``max_loss()`` known at submission
    time. For opening orders this is the full cash outlay, which is what
    cash-securing requires; a long position's worst case is that it goes to zero.
  - A market order must carry ``max_price``, so even an unpriced order has a bounded
    worst case. An order whose cost cannot be bounded cannot be constructed.

What this module does NOT do: it has no view of current positions or NAV. Two rules
therefore remain the enforcing gate's responsibility, and are the only places where
position state is load-bearing for a constraint:

  - A close-only order (``*_sell_to_close``) must be rejected unless the account
    already holds at least that quantity long. Without that check a sell-to-close
    would become a naked short — the exact thing Constraint #2 forbids.
  - Per-order and aggregate caps from ``config/risk_limits.yaml``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

#: Standard equity-option contract size. Adjusted contracts (post split/merger) can
#: differ, so it is a per-order field defaulting to this.
OPTION_CONTRACT_MULTIPLIER = 100

#: Actions deliberately absent from this module. Nothing here is a rejection list the
#: gate consults at runtime — these strings simply do not name a member of ``Order``,
#: so they cannot be parsed into one. The list exists to document intent and to give
#: the test suite something concrete to assert against.
UNREPRESENTABLE_ACTIONS = frozenset(
    {
        "option_sell_to_open",
        "option_write",
        "covered_call",
        "cash_secured_put",
        "credit_spread",
        "debit_spread",
        "equity_short_sale",
        "equity_sell_short",
        "buy_on_margin",
        "event_contract_sell_to_open",
    }
)

ZERO = Decimal("0")
ONE = Decimal("1")

Money = Annotated[Decimal, Field(gt=ZERO, max_digits=18, decimal_places=6)]
Quantity = Annotated[int, Field(gt=0)]
Confidence = Annotated[int, Field(ge=0, le=100)]
Ticker = Annotated[str, Field(min_length=1, max_length=32)]


class _Frozen(BaseModel):
    """Immutable, closed to unmodelled fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------------
# Execution style
# --------------------------------------------------------------------------------


class LimitExecution(_Frozen):
    """The default. CLAUDE.md § Execution: orders are limit orders by default."""

    style: Literal["limit"] = "limit"
    limit_price: Money

    @property
    def price_bound(self) -> Decimal:
        """Worst-case price per unit."""
        return self.limit_price


class MarketExecution(_Frozen):
    """A market order, which CLAUDE.md permits only with explicit justification.

    ``max_price`` is required and has no counterpart in a broker API — it is a local
    worst-case bound so ``max_loss()`` stays finite for an order with no limit price.
    The gate cash-secures against it; the broker never sees it.
    """

    style: Literal["market"] = "market"
    justification: Annotated[str, Field(min_length=20)]
    max_price: Money

    @property
    def price_bound(self) -> Decimal:
        return self.max_price


Execution = Annotated[
    Union[LimitExecution, MarketExecution], Field(discriminator="style")
]


# --------------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------------


class _OrderBase(_Frozen):
    execution: Execution
    #: Links the order back to the signal that produced it, for the audit record
    #: (CLAUDE.md § Audit & Attribution: signal -> thesis -> confidence -> size -> ...).
    signal_id: Optional[str] = None
    #: Research confidence that drove sizing, carried for attribution.
    confidence: Optional[Confidence] = None

    def max_loss(self) -> Decimal:
        """Worst-case cash loss if this order fills completely.

        Non-negative and finite for every order type in the union.
        """
        raise NotImplementedError  # pragma: no cover - abstract

    @property
    def is_opening(self) -> bool:
        """True if the order can increase exposure (and so consumes buying power)."""
        raise NotImplementedError  # pragma: no cover - abstract


class EquityBuyOrder(_OrderBase):
    """Cash-secured long equity purchase.

    Max loss is the full outlay: a long equity position's floor is zero, and the cash
    is committed at fill either way.
    """

    kind: Literal["equity_buy"] = "equity_buy"
    symbol: Ticker
    quantity: Quantity

    def max_loss(self) -> Decimal:
        return self.execution.price_bound * self.quantity

    @property
    def is_opening(self) -> bool:
        return True


class EquitySellToCloseOrder(_OrderBase):
    """Sell shares already held long.

    Named for what it is: there is no short-sale order type in this module. The gate
    must verify ``quantity`` does not exceed the held long position — an oversized
    sell is what a short sale would look like from here.
    """

    kind: Literal["equity_sell_to_close"] = "equity_sell_to_close"
    symbol: Ticker
    quantity: Quantity

    def max_loss(self) -> Decimal:
        return ZERO

    @property
    def is_opening(self) -> bool:
        return False


class _OptionLeg(_OrderBase):
    """Fields identifying a single option contract."""

    symbol: Ticker  # OCC option symbol
    underlying: Ticker
    right: Literal["call", "put"]
    expiration: date
    strike: Money
    contracts: Quantity
    multiplier: Annotated[int, Field(ge=1)] = OPTION_CONTRACT_MULTIPLIER


class OptionBuyToOpenOrder(_OptionLeg):
    """Buy a call or a put. The only way to open option exposure in this system.

    Max loss is the premium paid, which is the whole point of Constraint #2: a bought
    option cannot lose more than it cost, so it cannot drive the account negative.
    """

    kind: Literal["option_buy_to_open"] = "option_buy_to_open"

    def max_loss(self) -> Decimal:
        return self.execution.price_bound * self.contracts * self.multiplier

    @property
    def is_opening(self) -> bool:
        return True


class OptionSellToCloseOrder(_OptionLeg):
    """Exit a long option position.

    This is NOT writing an option. It closes contracts the account already owns, and
    the gate must reject it unless the held long quantity covers it. Without an exit
    path a long option could only be abandoned at expiry, which the spec's
    ``invalidation_condition`` exit logic rules out.
    """

    kind: Literal["option_sell_to_close"] = "option_sell_to_close"

    def max_loss(self) -> Decimal:
        return ZERO

    @property
    def is_opening(self) -> bool:
        return False


class _EventContract(_OrderBase):
    market_ticker: Ticker
    outcome: Literal["yes", "no"]
    contracts: Quantity
    #: Which permitted prediction-market strategy this order belongs to. Required, with
    #: no default: the two carry very different position caps (0.5% vs 2% of the
    #: prediction sleeve), and defaulting either way would silently mis-cap an order.
    strategy: Literal["arb", "directional"]

    @model_validator(mode="after")
    def _price_within_unit_interval(self) -> "_EventContract":
        """An event contract settles at 0 or 1, so its price lives strictly between.

        Enforcing it here keeps ``max_loss`` honest: contracts x price is only the true
        worst case while price < 1.
        """
        price = self.execution.price_bound
        if not ZERO < price < ONE:
            raise ValueError(
                f"event contract price must be strictly between 0 and 1, got {price}"
            )
        return self


class EventContractBuyOrder(_EventContract):
    """Buy YES or NO on an event contract.

    Both directions are buys — taking the other side of a market means buying the
    complementary outcome, never writing. Max loss = contracts x price paid, which is
    structurally consistent with the never-negative constraint.
    """

    kind: Literal["event_contract_buy"] = "event_contract_buy"

    def max_loss(self) -> Decimal:
        return self.execution.price_bound * self.contracts

    @property
    def is_opening(self) -> bool:
        return True


class EventContractSellToCloseOrder(_EventContract):
    """Exit event-contract positions already held.

    Required by the arbitrage strategy's expected high turnover. As with the other
    close-only types, the gate must verify the held quantity covers it.
    """

    kind: Literal["event_contract_sell_to_close"] = "event_contract_sell_to_close"

    def max_loss(self) -> Decimal:
        return ZERO

    @property
    def is_opening(self) -> bool:
        return False


# --------------------------------------------------------------------------------
# The closed union
# --------------------------------------------------------------------------------

Order = Annotated[
    Union[
        EquityBuyOrder,
        EquitySellToCloseOrder,
        OptionBuyToOpenOrder,
        OptionSellToCloseOrder,
        EventContractBuyOrder,
        EventContractSellToCloseOrder,
    ],
    Field(discriminator="kind"),
]

ORDER_ADAPTER: TypeAdapter = TypeAdapter(Order)

#: Every kind the system can express, derived from the union rather than restated.
ORDER_KINDS = frozenset(
    model.model_fields["kind"].default for model in Order.__origin__.__args__
)


def parse_order(payload: object) -> Order:
    """Validate an untrusted payload into an ``Order``.

    Raises ``pydantic.ValidationError`` for anything outside the union — including
    every action in ``UNREPRESENTABLE_ACTIONS``.
    """
    return ORDER_ADAPTER.validate_python(payload)
