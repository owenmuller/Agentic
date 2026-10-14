"""Leverage factors for the AI trader's measurements (human ruling
2026-10-14, second, item 2). Leveraged and leveraged-inverse ETFs stay
ALLOWED in this account - it exists to test the AI's risk-taking - and are
MEASURED: gross exposure, the gross cap's figure and every report count each
position's notional times |its leverage factor|, and their trades are tagged
so the scoreboard (and the learning journal) show them apart from stock picks.

A factor comes from the configured table (``lab.yaml ai_trader.leverage_factors``)
first, then from the fund's own name at the broker ("Direxion Daily
Semiconductor Bear 3X Shares" -> -3; "ProShares UltraPro QQQ" -> 3); anything
else is 1. Options count their premium at face value (factor 1).
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Mapping, Optional

ONE = Decimal("1")
LEVERAGED, INVERSE = "leveraged_etf", "inverse_etf"

_MULTIPLE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[xX](?![a-wyzA-WYZ])")
_BEAR = re.compile(r"\b(bear|short|inverse)\b", re.I)


def from_name(name: str) -> Optional[Decimal]:
    """A factor read from a fund's name, or None when the name says nothing."""
    if not name:
        return None
    magnitude = None
    match = _MULTIPLE.search(name)
    if match:
        magnitude = Decimal(match.group(1))
    elif re.search(r"\bUltraPro\b", name, re.I):
        magnitude = Decimal("3")
    elif re.search(r"\bUltra(Short)?\b", name, re.I):
        magnitude = Decimal("2")
    elif _BEAR.search(name):
        magnitude = ONE
    if magnitude is None:
        return None
    return -magnitude if _BEAR.search(name) or re.search(r"UltraShort", name, re.I) else magnitude


def factor(symbol: str, instrument: str, table: Mapping[str, Decimal], name: Optional[str] = None) -> Decimal:
    """The signed leverage factor of one position. Options and stocks: 1."""
    if instrument not in ("etf", "stock"):
        return ONE
    if symbol.upper() in table:
        return Decimal(str(table[symbol.upper()]))
    if instrument == "etf" and name:
        parsed = from_name(name)
        if parsed is not None:
            return parsed
    return ONE


def tag(value: Decimal) -> Optional[str]:
    if abs(value) > ONE:
        return LEVERAGED
    if value < 0:
        return INVERSE
    return None
