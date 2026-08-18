"""Deterministic risk gate.

CONSTRAINT #3 (CLAUDE.md): this package is deterministic Python. No LLM calls may be
made from anywhere inside it. Every order — equity, option, or event contract —
passes through the gate before touching a broker, and no bypass path may exist.

Nothing reaches a broker without an ``ApprovedOrder``, and only ``RiskGate`` can
construct one.
"""

from risk_gate.gate import (
    ApprovedOrder,
    BuyingPowerBreached,
    GateDecision,
    RiskGate,
)
from risk_gate.limits import RiskLimits, default_limits_path
from risk_gate.rejections import Rejection, RejectionCode
from risk_gate.state import (
    AccountState,
    AccountType,
    Position,
    PositionKey,
    Sleeve,
    position_key,
    sleeve_of,
    units_of,
)
from risk_gate.schema import (
    OPTION_CONTRACT_MULTIPLIER,
    ORDER_ADAPTER,
    ORDER_KINDS,
    UNREPRESENTABLE_ACTIONS,
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    EventContractSellToCloseOrder,
    BuyExecution,
    Execution,
    LimitExecution,
    MarketBuyExecution,
    MarketSellExecution,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
    Order,
    SellExecution,
    parse_order,
)

__all__ = [
    "OPTION_CONTRACT_MULTIPLIER",
    "ORDER_ADAPTER",
    "ORDER_KINDS",
    "UNREPRESENTABLE_ACTIONS",
    "AccountState",
    "AccountType",
    "ApprovedOrder",
    "BuyExecution",
    "SellExecution",
    "BuyingPowerBreached",
    "GateDecision",
    "Position",
    "PositionKey",
    "Rejection",
    "RejectionCode",
    "RiskGate",
    "RiskLimits",
    "Sleeve",
    "default_limits_path",
    "position_key",
    "sleeve_of",
    "units_of",
    "EquityBuyOrder",
    "EquitySellToCloseOrder",
    "EventContractBuyOrder",
    "EventContractSellToCloseOrder",
    "Execution",
    "LimitExecution",
    "MarketBuyExecution",
    "MarketSellExecution",
    "OptionBuyToOpenOrder",
    "OptionSellToCloseOrder",
    "Order",
    "parse_order",
]
