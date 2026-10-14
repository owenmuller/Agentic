"""Golden replay for the momentum veto prompt (human ruling 2026-10-13):
``config/golden/momentum_veto.yaml`` through the production request path.
Required before the prompt ships and after any change to it (CLAUDE.md §
LLM Request-Path Changes); spends real API dollars; writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from orchestrator.momentum_veto import Review

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "config" / "golden" / "momentum_veto.yaml"


@dataclass(frozen=True)
class Outcome:
    name: str
    passed: bool
    review: Review
    why: str


def load_cases(path: Path = GOLDEN_PATH) -> list[dict[str, Any]]:
    return list(yaml.safe_load(path.read_text(encoding="utf-8"))["cases"])


def facts(closes: list[float]) -> dict[str, Optional[float]]:
    """last close, 12-1 return (close ~21 sessions ago over ~252 ago), and
    the largest absolute daily move in the last 63 sessions."""
    if len(closes) < 2:
        return {"last_close": closes[-1] if closes else None, "return_12_1": None, "max_daily_move_3m": None}
    r12 = closes[-22] / closes[-253] - 1 if len(closes) >= 253 and closes[-253] > 0 else None
    tail = closes[-64:]
    moves = [abs(b / a - 1) for a, b in zip(tail, tail[1:]) if a > 0]
    return {"last_close": closes[-1], "return_12_1": r12, "max_daily_move_3m": max(moves) if moves else None}


def context(case: dict[str, Any], closes: list[float], data_through: str) -> dict[str, Any]:
    return dict(
        symbol=case["symbol"],
        name=case.get("company"),
        sector=case.get("sector"),
        rank=case.get("rank"),
        new_entrant=bool(case["new_entrant"]),
        data_through=data_through,
        residual_z=None,
        earnings_z=None,
        **facts(closes),
    )


def grade(case: dict[str, Any], review: Review) -> tuple[bool, str]:
    expect = case["expect"]
    if review.fault:
        return False, f"fault: {review.fault}"
    if expect["veto"]:
        if not review.veto:
            return False, "expected a veto, got none"
        if review.category not in expect.get("categories", []):
            return False, f"vetoed under {review.category}, expected {expect.get('categories')}"
        return True, f"vetoed: {review.category}"
    if review.veto:
        return False, f"unexpected veto ({review.category})"
    return True, "no veto"


def run(cases: list[dict[str, Any]], review: Callable[[dict], Review],
        closes_of: Callable[..., tuple[list[float], str]]) -> list[Outcome]:
    outcomes = []
    for case in cases:
        closes, through = closes_of(case["symbol"], case.get("as_of"))
        result = review(context(case, closes, through))
        passed, why = grade(case, result)
        outcomes.append(Outcome(case["name"], passed, result, why))
    return outcomes


def summary(outcomes: list[Outcome], spent: Any) -> str:
    lines = [f"momentum veto golden: {sum(o.passed for o in outcomes)}/{len(outcomes)} passed; spend ${spent}"]
    for o in outcomes:
        r = o.review
        lines.append(
            f"  {'PASS' if o.passed else 'FAIL'} {o.name}: {o.why}; sentiment {r.sentiment_6m}; "
            f"model {r.model}; transcript {r.transcript_hash[:12]}"
        )
        if r.evidence:
            lines.append(f"      evidence: {r.evidence[:300]}")
    return "\n".join(lines)


def alpaca_closes(alpaca) -> Callable[..., tuple[list[float], str]]:
    """Adjusted daily closes for the ~400 days before ``as_of`` (default:
    today), through the session before it. A historical case (``as_of`` set)
    sees only the prices it would have seen on that morning."""

    def closes_of(symbol: str, as_of: Optional[str] = None) -> tuple[list[float], str]:
        end = (date.fromisoformat(as_of) if as_of else date.today()) - timedelta(days=1)
        start = end - timedelta(days=400)
        rows = alpaca.bars([symbol], start.isoformat() + "T00:00:00Z", end.isoformat() + "T23:59:00Z", "all")
        series = rows.get(symbol, [])
        return [c for _, c in series], (series[-1][0] if series else end.isoformat())

    return closes_of
