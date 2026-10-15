"""Fills vs plan for a momentum rebalance day (human ruling 2026-10-14: report
the first off-cycle trade against its plan). Read-only.

    python -m orchestrator momentum-fills [--date YYYY-MM-DD]

For every name in the day's plan: target weight and dollars (weight x the
sleeve NAV the engine sized with), the gate's verdict, filled quantity, fill
price and filled dollars, and the share of the target reached; then the sells,
and any momentum decision the gate refused. Exit 0 when every target name is
at least 90% filled, 1 otherwise (so a scheduled check goes red).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

FILLED_ENOUGH = Decimal("0.90")


def report(audit, plan_dir: Path, day: date) -> tuple[bool, str]:
    plans = []
    for path in sorted(plan_dir.glob("plan-*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if plan.get("rebalance_date", "") <= day.isoformat():
            plans.append(plan)
    if not plans:
        return False, f"no momentum plan on or before {day} in {plan_dir}"
    plan = plans[-1]
    bought: dict[str, dict] = {}
    refused = []
    sells = []
    sleeve_nav = None
    for trail in audit.trails():
        decision = trail.decision
        if decision.sizing.strategy != "momentum":
            continue
        symbol = str((decision.gate.order or {}).get("symbol", ""))
        if decision.recorded_at.date() == day:
            sleeve_nav = sleeve_nav or decision.sizing.sleeve_nav
            if not decision.was_approved:
                refused.append(f"{symbol}: {decision.gate.code} {str(decision.gate.message or '')[:120]}")
            entry = bought.setdefault(symbol, {"qty": Decimal(0), "value": Decimal(0), "orders": 0})
            entry["orders"] += 1
            for fill in trail.fills:
                if fill.side == "buy" and fill.recorded_at.date() == day:
                    entry["qty"] += fill.filled_quantity
                    entry["value"] += fill.filled_value
        for fill in trail.fills:
            if fill.side == "sell" and fill.recorded_at.date() == day:
                sells.append(f"{symbol}: sold {fill.filled_quantity} at {fill.fill_price}")

    lines = [f"MOMENTUM FILLS vs PLAN - {day} (plan {plan['rebalance_date']}, spec {plan.get('spec')}, "
             f"data through {plan.get('data_through')})",
             f"sleeve NAV the engine sized with: {sleeve_nav if sleeve_nav is not None else 'n/a (no momentum orders today)'}",
             "", f"{'symbol':8} {'weight':>7} {'target $':>10} {'filled $':>10} {'qty':>14} {'avg px':>10} {'of target':>9}"]
    complete = True
    for target in plan["targets"]:
        symbol, weight = target["symbol"], Decimal(str(target["weight"]))
        goal = weight * sleeve_nav if sleeve_nav else None
        got = bought.get(symbol, {"qty": Decimal(0), "value": Decimal(0), "orders": 0})
        share = (got["value"] / goal) if goal else None
        avg = (got["value"] / got["qty"]) if got["qty"] else None
        if share is None or share < FILLED_ENOUGH:
            complete = False
        lines.append(f"{symbol:8} {weight:>7.2%} {goal or 0:>10.2f} {got['value']:>10.2f} {got['qty']:>14.6f} "
                     f"{(f'{avg:.2f}' if avg else '-'):>10} {(f'{share:.0%}' if share is not None else '-'):>9}")
    total_goal = sum((Decimal(str(t["weight"])) for t in plan["targets"]), Decimal(0)) * (sleeve_nav or 0)
    total_got = sum((v["value"] for v in bought.values()), Decimal(0))
    lines += ["", f"total: {total_got:.2f} filled of {total_goal:.2f} planned"
              + (f" ({total_got / total_goal:.0%})" if total_goal else ""),
              "", "sells today:"] + ([f"  {s}" for s in sells] or ["  none"])
    lines += ["", "refused by the gate today:"] + ([f"  {r}" for r in refused] or ["  none"])
    lines += ["", "COMPLETE: every name at least 90% filled" if complete else
              "INCOMPLETE: a name is under 90% of its target (funds, T+1, the gate or not yet traded)"]
    return complete, "\n".join(lines)
