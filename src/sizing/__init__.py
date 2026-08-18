"""Confidence-weighted sizing engine.

Implements the deterministic confidence -> size table in ``config/risk_limits.yaml``:
bands are (lower, upper] so a score on a boundary takes the smaller size, and anything
below the floor is no trade at all. Option sizes use the same table against premium at
risk, then halved. Event contracts are capped by their strategy tag.

Output is a ``SizedProposal``, which is not an order. Nothing reaches a broker without
passing ``RiskGate.submit`` and becoming an ``ApprovedOrder`` — sizing proposes against
a NAV figure it was handed, and the gate disposes against the real account.

Deterministic by construction: no LLM, no network, no clock. The only thing this
package takes from a research report is an integer.
"""

from sizing.engine import (
    EventStrategy,
    InstrumentKind,
    SizedProposal,
    SizingEngine,
)

__all__ = [
    "EventStrategy",
    "InstrumentKind",
    "SizedProposal",
    "SizingEngine",
]
