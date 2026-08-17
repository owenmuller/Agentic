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
class BrokerPosition:
    """A holding as the broker reports it — the reconciliation source of truth."""

    symbol: str
    quantity: Decimal
    market_value: Decimal
    cost_basis: Decimal


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
