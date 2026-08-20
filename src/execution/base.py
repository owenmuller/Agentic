"""Broker adapter interface — one interface, swappable backends.

The no-bypass property (CLAUDE.md Constraint #3) is enforced at this boundary. Every
method that can move money takes an ``ApprovedOrder``, never a bare ``Order``:

    def submit_order(self, approved: ApprovedOrder) -> OrderReceipt

``ApprovedOrder`` is constructible only by ``RiskGate``, so a caller holding an
unvalidated ``Order`` has nothing to pass. A type checker rejects the call before it
runs, and ``_require_approved`` catches the untyped case at runtime. "Forgot to call
the risk gate" is not a bug that can reach a broker — it is a call that does not
type-check and does not execute.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from risk_gate import ApprovedOrder


class BrokerError(RuntimeError):
    """Any failure originating at the broker boundary."""


class UnsupportedInstrument(BrokerError):
    """This backend cannot trade this instrument.

    Not every adapter serves every sleeve: Alpaca handles equities and options,
    Kalshi handles event contracts. Routing is the caller's job, and sending an
    instrument to the wrong venue is an error rather than a silent no-op.
    """


class BrokerRejected(BrokerError):
    """The broker refused the order. Carries the response body for the audit record."""

    def __init__(self, message: str, status_code: int, body: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True, slots=True)
class BrokerPermissions:
    """What the broker account is CONFIGURED to allow — as distinct from what this
    system's code can express.

    The system's enforcement is structural (the order schema cannot represent a
    write, a short, or a margin buy; the gate cash-secures everything), so an account
    that permits more than the code allows is not unsafe — but it is worth one loud
    line at every startup, because on a LIVE account the broker-side configuration is
    a defence-in-depth layer this report says is missing. A misconfigured live
    account should be flagged on day one, not discovered in an incident.
    """

    #: The broker's options approval level. This system needs exactly the level that
    #: permits BUYING calls and puts (level 2 at Alpaca); level 3 adds spreads.
    options_level: int
    shorting_enabled: bool
    #: Buying-power multiplier. 1 = cash-like; 2/4 = margin.
    margin_multiplier: Decimal

    #: The most a broker can be asked to permit while still matching this system:
    #: long options yes, spreads no, shorting no, margin no.
    SYSTEM_NEEDS_OPTIONS_LEVEL = 2

    def excess_permissions(self) -> list[str]:
        """Everything the account permits that the code forbids. Empty = matched."""
        findings: list[str] = []
        if self.options_level > self.SYSTEM_NEEDS_OPTIONS_LEVEL:
            findings.append(
                f"options level {self.options_level} permits spreads and other "
                f"multi-leg strategies; the system only ever buys calls and puts "
                f"(level {self.SYSTEM_NEEDS_OPTIONS_LEVEL} suffices) and its order "
                f"schema cannot represent anything else"
            )
        if self.shorting_enabled:
            findings.append(
                "shorting is enabled at the broker; short exposure is "
                "unrepresentable in the order schema (Constraint #2)"
            )
        if self.margin_multiplier > 1:
            findings.append(
                f"margin multiplier is {self.margin_multiplier}x; Constraint #1 "
                f"forbids borrowed buying power — the adapter reports cash only and "
                f"the gate cash-secures every order, but a live account should be a "
                f"cash account"
            )
        return findings

    @property
    def can_trade_options(self) -> bool:
        """Level 2+ is what long calls/puts need. Below it, option orders will be
        refused by the broker, not by this system."""
        return self.options_level >= self.SYSTEM_NEEDS_OPTIONS_LEVEL

    def describe(self) -> str:
        return (
            f"options level {self.options_level}, "
            f"shorting {'ENABLED' if self.shorting_enabled else 'disabled'}, "
            f"margin {self.margin_multiplier}x"
        )


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A holding as the broker reports it — the reconciliation source of truth."""

    symbol: str
    quantity: Decimal
    market_value: Decimal
    cost_basis: Decimal
    #: The broker's own instrument classification. Carried rather than inferred from
    #: the symbol: an OCC option symbol is distinguishable from a ticker by shape, but
    #: only by a heuristic, and a heuristic that mis-files an option as equity would
    #: seed it into the wrong sleeve with the wrong contract multiplier. Defaults to
    #: equity so a backend that does not report one still parses.
    asset_class: str = "us_equity"

    @property
    def is_option(self) -> bool:
        return "option" in self.asset_class


@dataclass(frozen=True, slots=True)
class OrderStatus:
    """A working order as the broker currently sees it.

    Settlement is driven off ``is_terminal`` rather than off ``is_filled``, because the
    two questions differ on the case that matters: an order that was cancelled or
    expired after filling part of its quantity is finished, and the position it left
    behind is real. Treating only ``filled`` as final would leave that position
    unrecorded and the cash reservation held forever.
    """

    broker_order_id: str
    #: The broker's own status string, recorded verbatim.
    status: str
    filled_quantity: Decimal
    #: Average fill price. None while nothing has filled.
    filled_avg_price: Optional[Decimal] = None

    #: States after which nothing further will happen to the order.
    TERMINAL = frozenset(
        {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day", "stopped"}
    )

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in self.TERMINAL

    @property
    def is_filled(self) -> bool:
        return self.filled_quantity > 0


@dataclass(frozen=True, slots=True)
class OrderReceipt:
    """Proof the broker accepted an order, for the audit trail."""

    broker_order_id: str
    status: str
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    client_order_id: Optional[str] = None
    submitted_at: Optional[datetime] = None


class BrokerAdapter(ABC):
    """What every execution backend must provide."""

    #: Smallest equity share increment this venue accepts. Whole shares unless a
    #: backend has verified finer support against its broker's CURRENT docs — order
    #: construction rounds DOWN to this step, so a coarser step can only shrink an
    #: order, never grow it. (Robinhood: fractional acceptance and precision are
    #: unverified by the spike — its adapter must keep 1 until proven.)
    equity_quantity_step: Decimal = Decimal("1")

    @abstractmethod
    def submit_order(self, approved: ApprovedOrder) -> OrderReceipt:
        """Send a gate-approved order. Only an ``ApprovedOrder`` is accepted."""

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        """Current holdings as the broker sees them."""

    @abstractmethod
    def get_buying_power(self) -> Decimal:
        """Spendable cash. Cash-secured accounts only — never margin buying power."""

    @abstractmethod
    def permissions(self) -> BrokerPermissions:
        """What the account is configured to allow. Logged and compared against what
        the code can express at every startup — see ``BrokerPermissions``."""

    @abstractmethod
    def open_orders(self) -> list[str]:
        """Broker order ids for every order still working at the broker.

        Exists for crash recovery: an order left resting by a dead process is
        exposure no restarted gate can account for — its reservation lived in an
        ``ApprovedOrder`` that died with the process — so startup sweeps these and
        cancels them (see ``orchestrator.bootstrap``).
        """

    @abstractmethod
    def get_order(self, broker_order_id: str) -> OrderStatus:
        """Current state of one submitted order.

        The settlement path depends on this: the risk gate reserves cash at approval
        and only releases it when told what actually happened, so an order nobody polls
        is a reservation that never comes back.
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> None:
        """Cancel a working order. Idempotent from the caller's point of view."""

    @staticmethod
    def _require_approved(candidate: object) -> ApprovedOrder:
        """Runtime backstop for the type signature.

        The annotation is the real guarantee; this catches callers that reached the
        adapter through untyped code — a dict from a queue, a JSON payload, a mock.
        """
        if not isinstance(candidate, ApprovedOrder):
            raise TypeError(
                f"submit_order requires an ApprovedOrder from RiskGate.submit(), got "
                f"{type(candidate).__name__}. An order that has not passed the risk "
                f"gate must never reach a broker."
            )
        return candidate
