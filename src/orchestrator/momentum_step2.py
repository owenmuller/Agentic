"""Momentum step 2 (human ruling 2026-10-13): after execution-test session 20
(~2026-11-06), +15% of NAV to the momentum sleeve from the aggressive sleeve
(aggressive 25 -> 10, momentum 15 -> 30). APPLIED 2026-10-14 by human ruling
under the standing no-waiting rule (the added capital goes in at the next
session; what settled cash cannot fund that day, the session after). Since
then this module only REPORTS: the execution test's completion and each
sleeve's allocation against its holdings. Before step 2 it proposed the
change and applied nothing; it never applies anything.

    python -m orchestrator momentum-step2-report

Exit 0 when the precondition holds (the execution test has run all its
sessions and holds nothing), 1 otherwise - so the scheduled check's card is
green only when step 2 is ready to be decided.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from risk_gate.state import Sleeve

TARGET = {"aggressive": Decimal("0.10"), "momentum": Decimal("0.30")}


@dataclass
class Step2:
    ready: bool
    text: str


def first_session_of_next_month(today: date, sessions: Optional[list[str]] = None) -> str:
    first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    if sessions:
        later = [s for s in sessions if s >= first.isoformat()]
        if later:
            return later[0]
    while first.weekday() >= 5:
        first += timedelta(days=1)
    return first.isoformat()


def report(checks, exec_state_path: Path, today: date, sessions: Optional[list[str]] = None) -> Step2:
    gate, limits = checks.gate, checks.gate.limits
    weights = limits.portfolio.sleeves
    max_sessions = checks.orchestrator_config.execution_test.max_sessions
    try:
        run = len(json.loads(exec_state_path.read_text(encoding="utf-8")).get("sessions", []))
    except (OSError, ValueError):
        run = None
    open_exec = {s: q for s, (q, _) in checks.audit.strategy_open_positions("execution_test").items() if q > 0}
    nav = gate.state.nav
    aggressive_held = gate.state.sleeve_exposure(Sleeve.AGGRESSIVE)
    momentum_held = gate.state.sleeve_exposure(Sleeve.MOMENTUM)
    drift = limits.portfolio.drift_tolerance
    new_aggressive_ceiling = nav * (TARGET["aggressive"] + drift)
    ready = run is not None and run >= max_sessions and not open_exec
    rebalance = first_session_of_next_month(today, sessions)

    def money(x: Decimal) -> str:
        return f"${x:,.0f}"

    if weights.aggressive == TARGET["aggressive"] and weights.momentum == TARGET["momentum"]:
        # Applied 2026-10-14 under the no-waiting rule: this run only reports.
        lines = [
            f"MOMENTUM STEP 2 - APPLIED 2026-10-14 (human ruling, standing no-waiting rule); report only ({today}).",
            "",
            f"Execution test: {run if run is not None else 'unknown'} of {max_sessions} sessions run; open "
            f"execution-test positions: {', '.join(open_exec) or 'none'} -> "
            + ("COMPLETE." if ready else "still running."),
            "",
            f"Book NAV {money(nav)}.",
            f"  aggressive {weights.aggressive:.0%}: allocation {money(nav * weights.aggressive)}; held {money(aggressive_held)}"
            + (f" - ABOVE its ceiling {money(new_aggressive_ceiling)}: new opens refused until exits bring it under"
               if aggressive_held > new_aggressive_ceiling else f" (ceiling {money(new_aggressive_ceiling)})"),
            f"  momentum   {weights.momentum:.0%}: allocation {money(nav * weights.momentum)}; held {money(momentum_held)}",
            "",
            f"Nothing changes. The next momentum rebalance is {rebalance}.",
        ]
        return Step2(ready, "\n".join(lines))

    lines = [
        f"MOMENTUM STEP 2 - REPORT ONLY ({today}); nothing has been changed.",
        "",
        f"Precondition: execution test {run if run is not None else 'unknown'} of {max_sessions} sessions run; "
        f"open execution-test positions: {', '.join(open_exec) or 'none'} -> "
        + ("READY to decide." if ready else "NOT READY."),
        "",
        f"Book NAV {money(nav)}.",
        f"  aggressive  {weights.aggressive:.0%} -> {TARGET['aggressive']:.0%}: allocation {money(nav * weights.aggressive)} -> "
        f"{money(nav * TARGET['aggressive'])}; held now {money(aggressive_held)}",
        f"  momentum    {weights.momentum:.0%} -> {TARGET['momentum']:.0%}: allocation {money(nav * weights.momentum)} -> "
        f"{money(nav * TARGET['momentum'])}; held now {money(momentum_held)}",
        "",
        ("Aggressive holdings are above the new ceiling "
         f"({money(new_aggressive_ceiling)} = {TARGET['aggressive'] + drift:.0%} of NAV): nothing is sold; its new "
         "opens are refused until exits bring it under (the mechanical arm's 2026-09-21 precedent)."
         if aggressive_held > new_aggressive_ceiling else
         f"Aggressive holdings are inside the new ceiling ({money(new_aggressive_ceiling)})."),
        f"The added momentum capital goes in at the next monthly rebalance, {rebalance} (no mid-month trade). "
        "With momentum entries on, the sweep holds the sleeve's undeployed allotment in cash, so SGOV is "
        "unswept for it ahead of that day.",
        "",
        "The change, as a reviewed commit after the human confirms (config/risk_limits.yaml portfolio.sleeves):",
        f"  aggressive: {weights.aggressive} -> {TARGET['aggressive']}",
        f"  momentum:   {weights.momentum} -> {TARGET['momentum']}",
        "plus CLAUDE.md § Portfolio Structure (the current-allocation line) and the tests that pin the weights.",
    ]
    return Step2(ready, "\n".join(lines))
