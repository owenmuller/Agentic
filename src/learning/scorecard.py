"""The intelligence scorecard (human ruling 2026-11-13, item 3): four plain
lines on the Friday learning card, each with its trend by month, for the AI
trader - whose ideas are graded at their own stated horizon whether taken or
not (``orchestrator.learning_feed.IdeaGrade``):

  calibration  how often ideas beat SPY, and by how much, at each confidence
               band - does a higher stated confidence mean better ideas?
  process      the share of its post-mortems judged right for the right reason
  selection    ideas taken against ideas not taken (refused, unfilled or
               never placed), on the same measure, with a 95% interval
  timing       the median move already made in a trade's favour when it was
               entered: since the prior close (every trade), and since the
               catalyst went public (logged from 2026-11-13)

Measured record only, aggregated from decisions: nothing here reads an
account's P&L, NAV or a target (CLAUDE.md § Standards; Constraint #6), and
none of it reaches a prompt. Pure: the caller passes the grades (dicts read
from ``idea_grades.jsonl``), the journal entries and the ledger's
move-since-public rows. One idea per name, day and direction, under its most
consequential fate.
"""
from __future__ import annotations

import math
from datetime import date
from statistics import mean, median, stdev
from typing import Any, Iterable, Mapping, Optional, Sequence

from learning import vocabulary
from learning.journal import Entry

MONTHS = 3          # months of trend on a line
_FATE = {"taken": 0, "unfilled": 1, "refused": 2, "unplaced": 3}


def unique(grades: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    best: dict[tuple, Mapping[str, Any]] = {}
    for g in grades:
        key = (g["day"], g["symbol"], g["direction"])
        if key not in best or _FATE[g["status"]] < _FATE[best[key]["status"]]:
            best[key] = g
    return list(best.values())


def _months(days: Iterable[str], today: date) -> list[str]:
    """The last MONTHS months that hold data, oldest first, up to today's."""
    present = sorted({d[:7] for d in days if d[:7] <= today.isoformat()[:7]})
    return present[-MONTHS:]


def _label(month: str) -> str:
    return date.fromisoformat(month + "-01").strftime("%b")


def _pts(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.1f}"


def _hit(rows: Sequence[Mapping[str, Any]]) -> str:
    wins = sum(1 for g in rows if g["excess"] > 0)
    return f"{wins / len(rows):.0%} (n {len(rows)})"


def calibration(grades: Sequence[Mapping[str, Any]], today: date) -> tuple[str, list[str]]:
    graded = [g for g in unique(grades) if g.get("excess") is not None]
    if not graded:
        return "Calibration: no idea has reached the end of its horizon yet.", []
    months = _months((g["day"] for g in graded), today)
    parts, detail = [], ["CALIBRATION - ideas taken or not, graded at their own horizon: share that beat SPY, "
                         "mean points against SPY"]
    for band in ("50-70", "70-85", "85+"):
        rows = [g for g in graded if vocabulary.confidence_band(g.get("confidence")) == band]
        if not rows:
            parts.append(f"{band} none yet")
            continue
        trend = " -> ".join(f"{_label(m)} {_hit(sub)}" for m in months
                            if (sub := [g for g in rows if g["day"][:7] == m]))
        parts.append(f"{band} beat SPY {_hit(rows)}, {_pts(mean(g['excess'] for g in rows))} points a case"
                     + (f" ({trend})" if len(months) > 1 else ""))
        detail += [f"  {band}: " + "; ".join(
            f"{_label(m)} {_hit(sub)} mean {_pts(mean(g['excess'] for g in sub))}" for m in months
            if (sub := [g for g in rows if g["day"][:7] == m]))]
    return "Calibration (ideas graded at their own horizon): " + "; ".join(parts) + ".", detail


def process(entries: Sequence[Entry], today: date) -> tuple[str, list[str]]:
    rows = [e for e in entries if e.account == "ai-trader" and e.resolved_at]
    if not rows:
        return "Process: no AI-trader post-mortem yet.", []
    months = _months((e.resolved_at for e in rows), today)

    def share(sub: Sequence[Entry]) -> str:
        right = sum(1 for e in sub if e.process == "right_reason")
        return f"{right} of {len(sub)} ({right / len(sub):.0%})"

    trend = " -> ".join(f"{_label(m)} {share(sub)}" for m in months
                        if (sub := [e for e in rows if e.resolved_at[:7] == m]))
    detail = ["PROCESS - AI-trader post-mortems judged right for the right reason, by month resolved"]
    for kind in sorted({e.kind for e in rows}):
        detail.append(f"  {kind}s: " + "; ".join(
            f"{_label(m)} {share(sub)}" for m in months
            if (sub := [e for e in rows if e.kind == kind and e.resolved_at[:7] == m])))
    return f"Process: right for the right reason in {trend} of the AI trader's post-mortems.", detail


def _interval(a: Sequence[float], b: Sequence[float]) -> Optional[tuple[float, float, float]]:
    """mean(a) - mean(b) and a normal 95% interval (Welch's standard error)."""
    if len(a) < 2 or len(b) < 2:
        return None
    diff = mean(a) - mean(b)
    half = 1.96 * math.sqrt(stdev(a) ** 2 / len(a) + stdev(b) ** 2 / len(b))
    return diff, diff - half, diff + half


def selection(grades: Sequence[Mapping[str, Any]], today: date) -> tuple[str, list[str]]:
    graded = [g for g in unique(grades) if g.get("excess") is not None]
    taken = [g["excess"] for g in graded if g["status"] == "taken"]
    other = [g["excess"] for g in graded if g["status"] != "taken"]
    if not taken or not other:
        return (f"Selection: not enough graded ideas on both sides yet (taken {len(taken)}, not taken "
                f"{len(other)})."), []
    months = _months((g["day"] for g in graded), today)
    trend = " -> ".join(
        f"{_label(m)} {_pts(mean(t))} vs {_pts(mean(o))}" for m in months
        if (t := [g["excess"] for g in graded if g["status"] == "taken" and g["day"][:7] == m])
        and (o := [g["excess"] for g in graded if g["status"] != "taken" and g["day"][:7] == m]))
    gap = _interval(taken, other)
    line = (f"Selection: ideas taken {_pts(mean(taken))} points a case against SPY at their horizon (n {len(taken)}), "
            f"ideas not taken {_pts(mean(other))} (n {len(other)})")
    if gap is not None:
        diff, low, high = gap
        line += (f"; the difference {_pts(diff)} [{_pts(low)}, {_pts(high)}] is "
                 + ("clear of zero" if low > 0 or high < 0 else "not yet clear of zero"))
    line += (f" (by month, taken vs not: {trend})" if len(months) > 1 and trend else "") + "."
    detail = ["SELECTION - ideas not taken, by why (mean points against SPY at their horizon)"]
    for status in ("refused", "unfilled", "unplaced"):
        sub = [g for g in graded if g["status"] == status]
        if sub:
            detail.append(f"  {status}: n {len(sub)}, mean {_pts(mean(g['excess'] for g in sub))}")
    return line, detail


def timing(grades: Sequence[Mapping[str, Any]], moves: Sequence[Mapping[str, Any]], today: date) -> tuple[str, list[str]]:
    taken = [g for g in unique(grades) if g["status"] == "taken" and g.get("moved_since_close") is not None]
    if not taken:
        return "Timing: no entry measured yet.", []
    months = _months((g["day"] for g in taken), today)
    trend = " -> ".join(f"{_label(m)} {median(sub):+.1f}% (n {len(sub)})" for m in months
                        if (sub := [g["moved_since_close"] for g in taken if g["day"][:7] == m]))
    line = (f"Timing: the median trade was entered after a {median(g['moved_since_close'] for g in taken):+.1f}% move "
            f"in its favour since the prior close (n {len(taken)}; {trend})")
    public = [float(r["move"]) * 100 for r in moves
              if r.get("pid") and r.get("status") == "ok" and r.get("move") is not None]
    line += (f"; since its catalyst went public, {median(public):+.1f}% (n {len(public)}, logged from Nov 13)"
             if public else "; the move since the catalyst went public is logged from Nov 13 (none measured yet)")
    return line + ".", []


def lines(grades: Sequence[Mapping[str, Any]], entries: Sequence[Entry], moves: Sequence[Mapping[str, Any]],
          today: date) -> tuple[list[str], list[str]]:
    """(the four card lines, the detail block)."""
    out, detail = [], ["INTELLIGENCE SCORECARD (AI trader; ruling 2026-11-13, item 3)"]
    for line, more in (calibration(grades, today), process(entries, today), selection(grades, today),
                       timing(grades, moves, today)):
        out.append(line)
        detail += more
    return out, detail
