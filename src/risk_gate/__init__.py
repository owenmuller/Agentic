"""Deterministic risk gate.

CONSTRAINT #3 (CLAUDE.md): this package is deterministic Python. No LLM calls may be
made from anywhere inside it. Every order — equity, option, or event contract —
passes through the gate before touching a broker, and no bypass path may exist.

Currently implemented: the order schema. The enforcing gate (caps, kill switch, PDT
counting, position-aware validation of close-only orders) is the next build step.
"""

from risk_gate.schema import (
    OPTION_CONTRACT_MULTIPLIER,
    ORDER_ADAPTER,
    ORDER_KINDS,
    UNREPRESENTABLE_ACTIONS,
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    EventContractSellToCloseOrder,
    Execution,
    LimitExecution,
    MarketExecution,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
    Order,
    parse_order,
)

__all__ = [
    "OPTION_CONTRACT_MULTIPLIER",
    "ORDER_ADAPTER",
    "ORDER_KINDS",
    "UNREPRESENTABLE_ACTIONS",
    "EquityBuyOrder",
    "EquitySellToCloseOrder",
    "EventContractBuyOrder",
    "EventContractSellToCloseOrder",
    "Execution",
    "LimitExecution",
    "MarketExecution",
    "OptionBuyToOpenOrder",
    "OptionSellToCloseOrder",
    "Order",
    "parse_order",
]
