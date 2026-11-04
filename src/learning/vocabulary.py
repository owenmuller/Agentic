"""The fixed vocabulary of the learning journal (ruling 2026-10-14, item 1).

Every tag a journal entry carries comes from these lists; anything else is
refused, so patterns tally over a closed set and a model cannot invent a
category. Six tags are COMPUTED by code from the decision record and market
data (sector, regime, confidence band, source, instrument, entry); the model
chooses only ``setup`` and ``catalyst`` from their lists, and the process
verdict.
"""
from __future__ import annotations

from typing import Mapping, Optional

SECTORS = (
    "consumer non-durables", "consumer durables", "manufacturing", "energy", "chemicals",
    "business equipment", "telecom", "utilities", "shops", "health", "money", "other",
    "broad etf", "sector etf", "leveraged etf", "unknown",
)
SETUPS = (
    "price_momentum", "earnings_drift", "insider_cluster", "congressional_trade", "issuer_filing",
    "contract_award", "activist_stake", "fund_holding", "policy_post", "trade_call", "theme_etf",
    "intraday_rules", "ai_idea", "rebalance", "other",
)
CATALYSTS = (
    "earnings", "guidance", "fda_regulatory", "m_and_a", "policy", "macro_rates", "government_contract",
    "insider_buying", "congressional_trade", "management_change", "restructuring", "product",
    "legal", "analyst", "squeeze_or_flow", "none", "other",
)
REGIMES = ("calm_uptrend", "calm_downtrend", "volatile_uptrend", "volatile_downtrend", "unknown")
CONFIDENCE_BANDS = ("<50", "50-70", "70-85", "85+", "n/a")
#: What was (or, for a decline, would have been) bought: an option counts as
#: one whatever its underlying.
INSTRUMENTS = ("stock", "etf", "option", "n/a")
#: Direction and the entry day's opening gap (human ruling 2026-11-04: "buy
#: after an earnings gap-up" must be visible as a pattern). Short = a put or
#: an inverse ETF, never a short sale. A gap is the open against the prior
#: close beyond GAP_THRESHOLD either way.
ENTRIES = (
    "long_after_gap_up", "long_after_gap_down", "long_no_gap",
    "short_after_gap_up", "short_after_gap_down", "short_no_gap", "unknown",
)
#: 2% - my reading, flagged: E1 measured the news gap at a mean of +2.0%.
GAP_THRESHOLD = 0.02
KINDS = ("trade", "decline", "veto")
#: Process vs outcome (item 5). *_luck entries are logged and teach nothing:
#: the miner and the playbook use only the *_reason verdicts.
PROCESS = ("right_reason", "right_luck", "wrong_reason", "wrong_luck", "unclear")
TEACHES = ("right_reason", "wrong_reason")

TAG_NAMES = ("sector", "setup", "catalyst", "regime", "confidence_band", "source", "instrument", "entry")
_ALLOWED = {"sector": SECTORS, "setup": SETUPS, "catalyst": CATALYSTS, "regime": REGIMES,
            "confidence_band": CONFIDENCE_BANDS, "instrument": INSTRUMENTS, "entry": ENTRIES}


def confidence_band(confidence: Optional[float]) -> str:
    """The sizing table's bands (CLAUDE.md § Sizing Engine)."""
    if confidence is None:
        return "n/a"
    if confidence < 50:
        return "<50"
    if confidence < 70:
        return "50-70"
    if confidence < 85:
        return "70-85"
    return "85+"


def regime(vix: Optional[float], spy_above_200d: Optional[bool]) -> str:
    """Volatile = VIX at or above 25 (the regime scalar's first rung)."""
    if vix is None or spy_above_200d is None:
        return "unknown"
    calm = vix < 25
    return f"{'calm' if calm else 'volatile'}_{'uptrend' if spy_above_200d else 'downtrend'}"


def entry(direction: Optional[str], gap: Optional[float]) -> str:
    """``direction`` long/short and the entry day's open over the prior close."""
    if direction not in ("long", "short") or gap is None:
        return "unknown"
    if gap >= GAP_THRESHOLD:
        return f"{direction}_after_gap_up"
    if gap <= -GAP_THRESHOLD:
        return f"{direction}_after_gap_down"
    return f"{direction}_no_gap"


def check(tags: Mapping[str, str], sources: tuple[str, ...]) -> None:
    """Refuse any tag outside the vocabulary (sources: the configured source
    ids plus the strategy names, passed in)."""
    missing = [name for name in TAG_NAMES if name not in tags]
    if missing:
        raise ValueError(f"missing tags: {missing}")
    for name, allowed in _ALLOWED.items():
        if tags[name] not in allowed:
            raise ValueError(f"tag {name}={tags[name]!r} is not in the vocabulary")
    if tags["source"] not in sources:
        raise ValueError(f"tag source={tags['source']!r} is not a known source")
