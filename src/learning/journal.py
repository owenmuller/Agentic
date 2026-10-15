"""The learning journal (ruling 2026-10-14, item 1): one structured
post-mortem per RESOLVED decision - a trade closed, or a decline or veto past
its grading horizon. Append-only JSON lines; an entry is never rewritten.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from learning import vocabulary


@dataclass(frozen=True)
class Entry:
    decision_id: str
    account: str                  # "main", "ladder", "ai-trader"
    strategy: str                 # judged, momentum, aggressive, ai_trader, ...
    kind: str                     # trade | decline | veto
    resolved_at: str              # ISO date the outcome became known
    thesis: str
    expected: str                 # what the decision expected, in words
    actual: str                   # what happened, in words
    #: the outcome the miner tallies: return in excess of SPY over the same
    #: window (a decline's or veto's is the trade it did NOT make)
    excess: Optional[float]
    process: str                  # vocabulary.PROCESS
    tags: dict = field(default_factory=dict)
    post_mortem: str = ""
    model: str = ""
    cost_usd: Optional[float] = None

    def __post_init__(self) -> None:
        if self.kind not in vocabulary.KINDS:
            raise ValueError(f"kind {self.kind!r} is not one of {vocabulary.KINDS}")
        if self.process not in vocabulary.PROCESS:
            raise ValueError(f"process {self.process!r} is not one of {vocabulary.PROCESS}")

    @property
    def teaches(self) -> bool:
        """Item 5: luck teaches nothing."""
        return self.process in vocabulary.TEACHES


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[Entry]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Entry(**json.loads(line)))
        return out

    def ids(self) -> set[str]:
        return {(e.account, e.decision_id) for e in self.entries()}

    def append(self, entries: Iterable[Entry], sources: tuple[str, ...]) -> int:
        """Append new entries (an already-journaled decision is skipped);
        every entry's tags are checked against the vocabulary first."""
        seen = self.ids()
        fresh = []
        for entry in entries:
            if (entry.account, entry.decision_id) in seen:
                continue
            vocabulary.check(entry.tags, sources)
            fresh.append(entry)
            seen.add((entry.account, entry.decision_id))
        if fresh:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                for entry in fresh:
                    handle.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
        return len(fresh)
