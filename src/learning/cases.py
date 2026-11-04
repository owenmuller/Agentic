"""A resolved decision, as the post-mortem writer sees it (human ruling
2026-11-04: the resolved-case feed). Pure facts: the orchestrator - the only
importer of the audit log - and the lab-ledger reader build these and hand
them over; this package reads nothing on its own.

The facts are one decision's own: its thesis, its instrument, its entry and
exit, its return and SPY's over the same window. Never the account's P&L,
NAV or any target (CLAUDE.md § Standards; Constraint #6).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Case:
    decision_id: str
    account: str                    # "main", "ai-trader"
    strategy: str                   # judged, ai_trader, ...
    kind: str                       # trade | decline | veto
    symbol: str
    opened: str                     # ISO date of the entry (a decline: the decision)
    resolved: str                   # ISO date the outcome became known
    #: long | short (a put or an inverse ETF) | none (a decline with no direction)
    direction: str
    thesis: str                     # the decision's own words, model-written: DATA
    #: what the decision expected, in its own terms (target, stop, horizon, confidence)
    expected: str
    #: what happened, in plain facts (exit reason, days held, the moves)
    actual: str
    #: return on cost in percent (a decline: the forward return of the trade not made)
    return_pct: Optional[float]
    spy_pct: Optional[float]
    #: return_pct - spy_pct, percentage points: what the miner tallies
    excess: Optional[float]
    #: The result split for the luck question, both signed IN THE DECISION'S
    #: FAVOUR (for a decline: the trade not made): the underlying's own move
    #: beyond SPY, and SPY's move. None where the underlying's move is unknown
    #: or not comparable (an inverse fund).
    own_move: Optional[float] = None
    market_move: Optional[float] = None
    #: the computed tags (sector, regime, confidence_band, source, instrument, entry)
    tags: dict = field(default_factory=dict)
    #: anything else worth stating, one line each (catalyst text, invalidation, gap)
    notes: tuple[str, ...] = ()
