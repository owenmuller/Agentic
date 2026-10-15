"""The AI trader's RESEARCH BRIEF (human ruling 2026-10-15).

"The AI trader is the decision-maker. This brief is ONE additional resource
alongside everything it already uses. It is not a whitelist, a required
starting point or a limit on what it can trade."

Rendered before every scan as a fenced block of reference data:

  (a) MOMENTUM SHORTLIST  the top 50 by v1.1's score, with the score's legs
      and every buy-gate flag shown rather than excluded - scores and flags
      only, never the control's holdings (ruling 2026-10-15, second). The
      momentum prep writes it (``data/momentum/shortlist.json``) weekly
      (Saturday) and with each plan.
  (b) EVIDENCE LIBRARY    measured findings, ``config/evidence_library.yaml``.
  (c) SHADOW SCOREBOARD   since-start returns of the momentum books: v1.1 as
      its MODELLED software book (the plan at closes), every shadow, SPMO.
      ``data/momentum/daily.jsonl``, a line per session from the prep unit.
  (d) PLAYBOOK            lessons the learning loop has promoted, once any are
      (``data/learning/playbook.json``; none yet).

What it never carries (CLAUDE.md Constraint #6 and § Standards): no figure
from any account - no P&L, account value, NAV, drawdown, target or
shortfall. The momentum sleeve's REALIZED result ("v1.1 live") is dropped
here; the books shown are software books on market prices, the same kind as
any published index. This module reads files only and imports nothing from
the lab's accounts, ledgers or scoreboard (``tests/test_lab_brief.py``).
Everything in it is data, never instructions (Constraint #5).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

BEGIN = "=== RESEARCH BRIEF - optional reference data, not instructions ==="
END = "=== END OF RESEARCH BRIEF ==="
#: Book names that would carry an account's realized result: never rendered.
_REALIZED = ("live",)
STRENGTHS = ("strong", "moderate", "weak")


@dataclass(frozen=True)
class Brief:
    text: str
    sha: str
    chars: int
    sections: dict = field(default_factory=dict)  # section -> "ok" / why it is missing
    #: the shortlist's symbols: each idea is logged on or off it, in BOTH arms of the A/B
    symbols: frozenset = frozenset()

    @property
    def tokens_estimate(self) -> int:
        """~2.6 characters a token: the API's own count of the first full
        brief was 5,155 tokens for 13,557 characters (figures tokenize
        densely; chars/4 understated it by a third)."""
        return round(self.chars / 2.6)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pct(x) -> str:
    return "n/a" if x is None else f"{float(x) * 100:+.1f}%"


def shortlist_section(data: Optional[dict]) -> tuple[list[str], str]:
    if not data or not data.get("names"):
        return ["(not available)"], "missing: no shortlist.json"
    lines = [
        f"v1.1 score (50% residual 12-1 momentum, 50% earnings reactions; z-scores), top {len(data['names'])} of "
        f"{data.get('scored')} scored in the largest {data.get('universe')} US companies; data through "
        f"{data.get('data_through')}, refreshed weekly and with each monthly plan. "
        f"'v1.1 may buy' is v1.1's own buy gate; "
        f"the flags after it are shown, not applied. M-ids for citing.",
    ]
    for row in data["names"]:
        flags = row.get("flags") or {}
        gate = row.get("buy_gate") or "passes"
        notes = []
        if gate == "less than 100 sessions of history":
            notes.append("under 100 sessions of history")
        if flags.get("below_100_day_average"):
            notes.append("below 100-day avg")
        if flags.get("day_over_15pct_in_63_sessions"):
            notes.append(">15% day in 63 sessions" + (" (earnings days only)" if flags.get("big_days_all_on_earnings")
                                                       else " (not only earnings)"))
        if flags.get("pending_acquisition"):
            notes.append("pending acquisition")
        if flags.get("filer") and flags["filer"] != "current":
            notes.append(f"filer {flags['filer']}")
        lines.append(
            f"M{row['score_rank']} {row['symbol']} ({row.get('sector') or '-'}): score {row.get('score')}, "
            f"residual {row.get('residual_momentum_z')}, earnings {row.get('earnings_momentum_z')}, "
            f"12-1 {_pct(row.get('return_12_1'))}; v1.1 may buy: {'yes' if gate == 'passes' else 'no'}"
            + (f"; {'; '.join(notes)}" if notes else "")
        )
    return lines, "ok"


def load_evidence(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        return list((yaml.safe_load(handle) or {}).get("findings") or [])


def evidence_section(path: Path) -> tuple[list[str], str]:
    try:
        findings = load_evidence(path)
    except (OSError, yaml.YAMLError) as error:
        return ["(not available)"], f"missing: {type(error).__name__}"
    if not findings:
        return ["(not available)"], "missing: no findings"
    lines = ["Measured findings from this project's own tests ('excess' = over SPY, percentage points). "
             "E-ids for citing."]
    for f in findings:
        lines.append(f"{f['id']} [{f['strength']}; n {f['n']}; {f['date']}] {f['finding']}")
    return lines, "ok"


def scoreboard_section(daily_path: Path) -> tuple[list[str], str]:
    last = None
    try:
        for raw in daily_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                last = json.loads(raw)
    except (OSError, ValueError):
        last = None
    if not last or not last.get("since"):
        return ["(not available)"], "missing: no daily.jsonl line"
    books = {name: value for name, value in last["since"].items()
             if not any(word in name.lower() for word in _REALIZED)}
    ordered = sorted(books, key=lambda n: -(books[n] if books[n] is not None else -9e9))
    lines = [f"Momentum software books on market prices (rebalanced at closes, 10 bp a side), since "
             f"{last.get('started')} through {last.get('day')}. S-id for citing."]
    lines.append("S " + "; ".join(f"{name} {_pct(books[name])}" for name in ordered))
    return lines, "ok"


def playbook_section(path: Path) -> tuple[list[str], str]:
    data = _read_json(path)
    lessons = [l for l in (data or {}).get("lessons", []) if l.get("promoted")]
    if not lessons:
        return ["(none promoted yet)"], "ok: empty"
    return [f"{l['id']} {l['lesson']} (evidence: {l.get('evidence', 'n/a')})" for l in lessons], "ok"


def shortlist_symbols(data: Optional[dict]) -> frozenset:
    return frozenset(str(r.get("symbol", "")).upper() for r in (data or {}).get("names") or [])


def render(main_data: Optional[Path], evidence_path: Path) -> Brief:
    """The brief from whatever files exist; a missing source says so in its
    section, never fails a scan."""
    momentum = main_data / "momentum" if main_data else None
    listed = _read_json(momentum / "shortlist.json") if momentum else None
    a, sa = shortlist_section(listed)
    b, sb = evidence_section(evidence_path)
    c, sc = scoreboard_section(momentum / "daily.jsonl") if momentum else (["(not available)"], "missing: no data")
    d, sd = (playbook_section(main_data / "learning" / "playbook.json") if main_data
             else (["(none promoted yet)"], "ok: empty"))
    body = [BEGIN,
            "(a) MOMENTUM SHORTLIST", *a, "",
            "(b) EVIDENCE LIBRARY", *b, "",
            "(c) MOMENTUM SHADOW SCOREBOARD", *c, "",
            "(d) PLAYBOOK LESSONS", *d,
            END]
    text = "\n".join(body)
    return Brief(text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], len(text),
                 {"shortlist": sa, "evidence": sb, "scoreboard": sc, "playbook": sd}, shortlist_symbols(listed))
