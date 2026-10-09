"""The AI trader's golden replay (``python -m lab golden ai-trader``).

Every case runs the PRODUCTION prompt through the PRODUCTION request path
(``lab.llm`` -> the main book's research client: search, then the forced
report), so the replay is also the live search->report round trip CLAUDE.md
requires before a prompt ships. No broker is touched: ideas are graded
against the model's own ``entry_reference`` and the rules engine's verdicts
are computed with no order placed. Held positions and today's trades are
written to a throwaway ledger, so the prompt is built by the same code that
builds it live.

Graded HARD (the prompt states these as rules): the report tool is called;
no more ideas than the case allows; no held or traded symbol; no forbidden
horizon; every idea's stop and target on the right sides of its own
entry_reference. REPORTED, not graded: the rules engine's acceptance of each
idea (reward:risk, stop distance, confidence floor) and the pass cost.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import yaml

from lab.account import LabState, Ledger
from lab.ai_trader import NY, AiTrader, _reference, at, bullish
from lab.config import AccountConfig, AiTraderConfig


@dataclass
class CaseResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    ideas: list[dict] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    cost_usd: Decimal = Decimal("0")
    reported: bool = True


class _GoldenAccount:
    """Just enough of a LabAccount for a dry scan: a throwaway ledger."""

    def __init__(self, directory: Path, clock) -> None:
        self.name = "ai-trader-golden"
        self.config = AccountConfig(enabled=True, label="golden", sleeve="ai_trader")
        self.ledger = Ledger(directory / "ledger.jsonl", clock)
        self.state = LabState()
        self.state_path = directory / "state.json"
        self.data_dir = directory

    def halted(self) -> Optional[str]:
        return None


def next_session(today: date) -> date:
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def load_cases(path: Optional[Path] = None) -> list[dict]:
    path = path or Path(__file__).resolve().parents[2] / "config" / "golden" / "lab_ai_trader.yaml"
    with open(path, "r", encoding="utf-8") as handle:
        return list(yaml.safe_load(handle)["cases"])


def run_case(case: dict, config: AiTraderConfig, llm: Any, day: date) -> CaseResult:
    now = at(day, case["time"])
    with tempfile.TemporaryDirectory(prefix="lab-golden-") as tmp:
        clock_value = [at(day, "09:31")]
        account = _GoldenAccount(Path(tmp), lambda: clock_value[0])
        for i, held in enumerate(case.get("held") or []):
            pid = f"held{i}"
            account.ledger.append("position_opened", pid=pid, symbol=held["symbol"], instrument=held["instrument"],
                                  horizon=held["horizon"], stop=held["stop"], target=held["target"],
                                  thesis=held.get("thesis", ""))
            account.ledger.append("settled", order_id=f"g{i}", pid=pid, side="buy", filled_qty="1", avg_price="1")
        for i, symbol in enumerate(case.get("traded_today") or []):
            pid = f"traded{i}"
            account.ledger.append("position_opened", pid=pid, symbol=symbol, instrument="stock", horizon="intraday",
                                  stop="1", target="2", thesis="")
            account.ledger.append("settled", order_id=f"t{i}a", pid=pid, side="buy", filled_qty="1", avg_price="1")
            account.ledger.append("settled", order_id=f"t{i}b", pid=pid, side="sell", filled_qty="1", avg_price="1")
        trader = AiTrader(account, config, llm=llm, quotes=lambda s: None, chain=None)  # type: ignore[arg-type]
        outcome = trader.scan(now, label=f"golden:{case['name']}", dry_run=True)
    expect = case.get("expect") or {}
    result = CaseResult(case["name"], True, ideas=outcome.ideas, accepted=outcome.opened,
                        rejected=outcome.rejected, cost_usd=outcome.cost_usd)
    failures = result.failures
    if trader.notes and any("returned no report" in n for n in trader.notes):
        result.reported = False
        failures.append("the model did not call the report tool")
    if len(outcome.ideas) > int(expect.get("max_ideas", 3)):
        failures.append(f"{len(outcome.ideas)} ideas, the case allows {expect.get('max_ideas', 3)}")
    forbidden = {s.upper() for s in expect.get("forbidden_symbols") or []}
    forbidden_horizons = set(expect.get("forbidden_horizons") or [])
    for idea in outcome.ideas:
        symbol = str(idea.get("symbol", "")).upper()
        if symbol in forbidden:
            failures.append(f"proposed {symbol}, which is held or traded today")
        if idea.get("horizon") in forbidden_horizons:
            failures.append(f"proposed a {idea.get('horizon')} idea on {symbol} after the cutoff")
        price = _reference(idea)
        try:
            stop, target = Decimal(str(idea.get("stop"))), Decimal(str(idea.get("target")))
        except (ArithmeticError, ValueError, TypeError):
            failures.append(f"{symbol}: stop/target not numeric")
            continue
        sides_ok = (stop < price < target) if bullish(str(idea.get("instrument"))) else (target < price < stop)
        if not sides_ok:
            failures.append(f"{symbol} {idea.get('instrument')}: stop {stop} / reference {price} / target {target} "
                            f"on the wrong sides")
    result.passed = not failures
    return result


def run(config: AiTraderConfig, llm: Any, today: date) -> list[CaseResult]:
    day = next_session(today)
    return [run_case(case, config, llm, day) for case in load_cases()]


def render(results: list[CaseResult], day: date) -> str:
    lines = [f"AI TRADER GOLDEN REPLAY (cases dated {day}, production request path)"]
    total = sum((r.cost_usd for r in results), Decimal("0"))
    for r in results:
        lines.append(f"  {'PASS' if r.passed else 'FAIL'}  {r.name}: {len(r.ideas)} idea(s), engine accepted "
                     f"{r.accepted or 'none'}, cost {r.cost_usd:.3f}")
        for failure in r.failures:
            lines.append(f"        FAIL: {failure}")
        for symbol, reason in r.rejected:
            lines.append(f"        engine rejected {symbol}: {reason}")
        for idea in r.ideas:
            lines.append(f"        idea {idea.get('symbol')} {idea.get('instrument')} {idea.get('horizon')} "
                         f"ref {idea.get('entry_reference')} stop {idea.get('stop')} target {idea.get('target')} "
                         f"conf {idea.get('confidence')}: {str(idea.get('thesis', ''))[:140]}")
    passed = sum(1 for r in results if r.passed)
    lines.append(f"  {passed}/{len(results)} cases passed; total cost {total:.3f} USD")
    return "\n".join(lines)
