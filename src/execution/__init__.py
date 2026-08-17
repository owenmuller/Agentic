"""Broker adapters — one interface, swappable backends.

Alpaca paper trading first; the paper/live flip is one environment variable, which
only a human may set (CLAUDE.md Constraint #4). Nothing in this package writes it.

Robinhood is not an execution backend: no official public equities API exists, and
community wrappers are reverse-engineered and violate RH ToS. Kalshi's official API
will serve the prediction-market sleeve, after the equity leg proves itself in paper.

Everything that can move money takes an ``ApprovedOrder``, so an order that has not
passed the risk gate cannot be submitted.
"""

from execution.alpaca import LIVE_BASE_URL, PAPER_BASE_URL, AlpacaAdapter
from execution.base import (
    BrokerAdapter,
    BrokerError,
    BrokerPosition,
    BrokerRejected,
    OrderReceipt,
    UnsupportedInstrument,
)
from execution.environment import load_environment, paper_mode, require_env

__all__ = [
    "LIVE_BASE_URL",
    "PAPER_BASE_URL",
    "AlpacaAdapter",
    "BrokerAdapter",
    "BrokerError",
    "BrokerPosition",
    "BrokerRejected",
    "OrderReceipt",
    "UnsupportedInstrument",
    "load_environment",
    "paper_mode",
    "require_env",
]
