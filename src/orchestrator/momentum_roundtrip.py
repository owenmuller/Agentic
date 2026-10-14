"""The momentum sleeve's order-path round trip (human ruling 2026-10-13):
one share bought and sold in the MAIN paper account through the real engine,
the real gate's momentum path, the real broker adapter and the audit log.

    python -m orchestrator momentum-roundtrip [--symbol F]

Run in session (10:00-15:30 ET). Outside a production unit (always, for this
command) the audit log is a scratch snapshot, so the trade is recorded there,
not in the book's log. The production session does not re-read broker
positions mid-session; a restart during the few minutes the share is held
would report it as unmanaged.

What it proves, in order:
  1. a one-name plan for this month -> the engine buys ONE share through the
     gate (``sleeve="momentum"``, entries switched on IN THIS PROCESS ONLY -
     the config file is untouched), the fill settles, a lot exists;
  2. an empty plan for "next month" -> a fresh engine replays the lot from
     the audit log and sells it with a ``momentum_rebalance`` exit;
  3. the trail is complete: decision, buy fill, exit, sell fill, outcome;
     the sleeve is flat.
Funding: the buy spends the gate's settled buying power, not the sweep-based
``momentum_spendable`` - with entries off in the live session the sweep holds
no cash for the sleeve yet, and the round trip tests the order path.
"""

from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from audit.records import ExitReason
from risk_gate.state import Sleeve

from orchestrator.config import MomentumSleeveConfig
from orchestrator.momentum_sleeve import PLAN_VERSION, MomentumSleeve, plan_path

NY = ZoneInfo("America/New_York")


@dataclass
class Result:
    passed: bool
    steps: list = field(default_factory=list)

    def note(self, text: str) -> None:
        self.steps.append(text)


def _write_plan(directory: Path, month: str, day: str, targets: list) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"version": PLAN_VERSION, "spec": "roundtrip", "month": month, "rebalance_date": day,
               "data_through": day, "computed_at": datetime.now(timezone.utc).isoformat(), "buys_allowed": True,
               "exposure": "1", "targets": targets}
    plan_path(directory, month).write_text(json.dumps(payload), encoding="utf-8")


def _next_month_same_time(now: datetime) -> datetime:
    local = now.astimezone(NY)
    first = (local.replace(day=1) + timedelta(days=32)).replace(day=1)
    return first.replace(hour=local.hour, minute=local.minute, second=local.second)


def run(checks, prices, symbol: str, work: Path, *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = _time.sleep, timeout: float = 420, step: float = 5,
        id_factory: Optional[Callable[[], str]] = None) -> Result:
    import uuid

    result = Result(False)
    gate, audit, adapter = checks.gate, checks.audit, checks.adapter
    limits = gate.limits.momentum_sleeve
    if limits is None or not limits.entries_enabled:
        result.note("refused: the gate passed in does not have momentum entries on for this process")
        return result
    ids = id_factory or (lambda: uuid.uuid4().hex[:16])
    config = MomentumSleeveConfig()
    now = clock()
    local = now.astimezone(NY)
    quote = prices(symbol)
    if quote is None or quote <= 0:
        result.note(f"no live quote for {symbol}; nothing traded")
        return result
    nav = gate.sleeve_nav(Sleeve.MOMENTUM)
    # 1.5 shares' worth: the engine's floor() lands on exactly one share
    weight = min((quote * Decimal("1.5") / nav).quantize(Decimal("0.000001"), rounding=ROUND_DOWN),
                 limits.max_single_position)
    month, day = local.strftime("%Y-%m"), local.date().isoformat()
    _write_plan(work / "plans", month, day, [{"symbol": symbol, "weight": str(weight), "rank": 1}])
    result.note(f"{symbol} quote {quote}; momentum sleeve NAV {nav:.2f}; plan weight {weight}")

    def engine(state_name: str) -> MomentumSleeve:
        return MomentumSleeve(gate=gate, adapter=adapter, audit=audit, prices=prices,
                              bids=getattr(prices, "bid", None), limits=limits, config=config,
                              plan_dir=work / "plans", state_path=work / state_name,
                              spendable=lambda: gate.state.settled_buying_power, clock=clock,
                              id_factory=ids, note=result.note)

    buyer = engine("state-buy.json")
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        buyer.tick(clock())
        if buyer.lots and not buyer.working:
            break
        sleep(step)
    if not buyer.lots:
        result.note("FAILED: no buy filled inside the timeout")
        buyer.cancel_working()
        return result
    lot = buyer.lots[0]
    result.note(f"bought {lot.quantity} {symbol} for {lot.entry_cost:.2f} (decision {lot.decision_id})")

    # 2. "next month": an empty plan; a fresh engine replays the lot and sells
    shift = _next_month_same_time(clock()) - clock()
    later: Callable[[], datetime] = lambda: clock() + shift
    _write_plan(work / "plans", later().astimezone(NY).strftime("%Y-%m"),
                later().astimezone(NY).date().isoformat(), [])
    seller = MomentumSleeve(gate=gate, adapter=adapter, audit=audit, prices=prices,
                            bids=getattr(prices, "bid", None), limits=limits, config=config,
                            plan_dir=work / "plans", state_path=work / "state-sell.json",
                            spendable=lambda: gate.state.settled_buying_power, clock=later,
                            id_factory=ids, note=result.note)
    restored = seller.replay(audit.trails())
    result.note(f"sell engine replayed {restored} lot(s) from the audit log")
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        seller.tick(later())
        if not seller.held() and not seller.working:
            break
        sleep(step)
    if seller.held():
        result.note("FAILED: the share was not sold inside the timeout")
        seller.cancel_working()
        return result

    trail = audit.trail(lot.decision_id)
    buys = [f for f in trail.fills if f.side == "buy"]
    sells = [f for f in trail.fills if f.side == "sell"]
    reasons = [e.reason for e in trail.exits]
    checks_ok = {
        "decision is strategy=momentum, sleeve=momentum": trail.decision.sizing.strategy == "momentum"
        and trail.decision.sizing.sleeve == "momentum",
        "gate approved": bool(trail.decision.was_approved),
        "buy fill recorded": bool(buys),
        "momentum_rebalance exit recorded": ExitReason.MOMENTUM_REBALANCE in reasons,
        "sell fill recorded": bool(sells),
        "outcome recorded": trail.outcome is not None,
        "sleeve flat in the gate": gate.state.position((Sleeve.MOMENTUM.value, symbol)) is None,
    }
    for name, ok in checks_ok.items():
        result.note(f"{'PASS' if ok else 'FAIL'} {name}")
    if buys and sells:
        result.note(f"buy {buys[0].filled_quantity} @ {buys[0].fill_price}; sell {sells[0].filled_quantity} @ "
                    f"{sells[0].fill_price}; outcome {trail.outcome.realised_pnl if trail.outcome else None}")
    result.passed = all(checks_ok.values())
    return result
