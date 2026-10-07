"""The weekly's self-consistency vote lines (ruling 2026-10-07, requirement b):
votes held, votes overturned by direction and by source, extra passes and
dollars spent. Reads the audit records and nothing about P&L."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from audit.records import DecisionRecord, StageRejectionRecord, ThesisReviewRecord, VoteSnapshot


def _votes(records: Iterable[object], since: Optional[datetime]) -> list[tuple[str, str, VoteSnapshot]]:
    """(kind, source, vote) for every record carrying a vote in the window."""
    out: list[tuple[str, str, VoteSnapshot]] = []
    for record in records:
        vote = getattr(record, "vote", None)
        if vote is None:
            continue
        recorded = getattr(record, "recorded_at", None)
        if since is not None and recorded is not None and recorded < since:
            continue
        if isinstance(record, ThesisReviewRecord):
            out.append(("review", "review", vote))
        elif isinstance(record, (DecisionRecord, StageRejectionRecord)):
            signal = getattr(record, "signal", None)
            out.append(("entry", str(getattr(signal, "source_id", "?") or "?"), vote))
    return out


def vote_lines(records: Iterable[object], since: Optional[datetime] = None) -> list[str]:
    votes = _votes(records, since)
    window = f"since {since.date().isoformat()}" if since is not None else "all records"
    if not votes:
        return [f"Self-consistency votes ({window}): none triggered yet"]
    held = sum(1 for _, _, v in votes if v.held)
    overturned = len(votes) - held
    extra = sum(v.extra_passes for _, _, v in votes)
    dollars = sum((v.extra_est_cost_usd for _, _, v in votes if v.extra_est_cost_usd is not None), Decimal("0"))
    incomplete = sum(1 for _, _, v in votes if v.failure)
    lines = [
        f"Self-consistency votes ({window}): {len(votes)} triggered -> {held} held, {overturned} overturned"
        + (f", {incomplete} incomplete" if incomplete else "")
        + f"; extra passes {extra}, ~${dollars:.2f} (estimates; console bill is truth)",
    ]
    by_direction: Counter = Counter()
    for _, _, v in votes:
        key = f"{v.first_direction} -> {v.majority_direction}" if not v.held else f"{v.first_direction} held"
        by_direction[key] += 1
    lines.append("  by direction: " + ", ".join(f"{k} {n}" for k, n in by_direction.most_common()))
    by_source: dict[str, Counter] = defaultdict(Counter)
    for kind, source, v in votes:
        by_source[source]["held" if v.held else "overturned"] += 1
        by_source[source]["extra"] += v.extra_passes
    for source in sorted(by_source):
        c = by_source[source]
        lines.append(f"  {source}: {c['held']} held, {c['overturned']} overturned, {c['extra']} extra passes")
    triggers: Counter = Counter(v.trigger for _, _, v in votes)
    lines.append("  by trigger: " + ", ".join(f"{k} {n}" for k, n in triggers.most_common()))
    return lines


def window_start(days: int = 7) -> datetime:
    from datetime import timedelta

    return datetime.now(timezone.utc) - timedelta(days=days)
