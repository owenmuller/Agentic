"""Weekly pattern mining (ruling 2026-10-14, item 2): CODE, no LLM.

Outcomes (excess over SPY) are tallied by every tag value and every pair of
tag values, over the entries that TEACH (item 5: *_luck entries are logged
and excluded). Each cell gets n, its mean, and a confidence interval; a cell
is FLAGGED only when n >= ``min_n`` (20) and its interval excludes zero after
a Bonferroni correction for the number of cells tested - the conservative
correction, so the loop opens fewer hypotheses rather than more
(Constraint #6). Nothing here reads P&L totals, account value or a target.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from statistics import NormalDist, mean, stdev
from typing import Iterable, Optional

from learning.journal import Entry
from learning.vocabulary import TAG_NAMES


@dataclass(frozen=True)
class Cell:
    key: tuple[tuple[str, str], ...]   # ((tag, value),) or two of them
    n: int
    mean: float
    low: float
    high: float
    flagged: bool

    @property
    def label(self) -> str:
        return " & ".join(f"{tag}={value}" for tag, value in self.key)


def tally_by_kind(entries: Iterable[Entry], *, min_n: int = 20, alpha: float = 0.05) -> dict[str, list[Cell]]:
    """Trades, declines and vetoes apart: a decline's excess is the trade it
    did NOT make, so a negative one is a good decline - pooling the two would
    tally opposite meanings together."""
    by_kind: dict[str, list[Entry]] = {}
    for entry in entries:
        by_kind.setdefault(entry.kind, []).append(entry)
    return {kind: tally(rows, min_n=min_n, alpha=alpha) for kind, rows in sorted(by_kind.items())}


@dataclass(frozen=True)
class Focus:
    """One named pattern, reported whether or not it is flagged: n, the mean
    excess and a plain 95% interval over every resolved case (uncorrected - it
    is one question asked in advance, not a search), and the mean over the
    cases that teach (item 5: luck excluded)."""
    label: str
    n: int
    mean: Optional[float]
    low: Optional[float]
    high: Optional[float]
    teaching_n: int
    teaching_mean: Optional[float]


def focus(entries: Iterable[Entry], label: str, **tags: str) -> Focus:
    rows = [e for e in entries if e.excess is not None and all(e.tags.get(k) == v for k, v in tags.items())]
    values = [e.excess for e in rows]
    teaching = [e.excess for e in rows if e.teaches]
    if not values:
        return Focus(label, 0, None, None, None, 0, None)
    m = mean(values)
    half = 1.96 * stdev(values) / math.sqrt(len(values)) if len(values) >= 2 else math.inf
    return Focus(label, len(values), m, m - half, m + half, len(teaching), mean(teaching) if teaching else None)


#: Questions asked IN ADVANCE, reported every run whether or not they flag
#: (human ruling 2026-11-04: is "buy after an earnings gap-up" a losing pattern?).
FOCUS = (
    ("buy after an earnings gap-up", {"entry": "long_after_gap_up", "catalyst": "earnings"}),
    ("any buy after a gap-up", {"entry": "long_after_gap_up"}),
    ("bearish bet after a gap-down", {"entry": "short_after_gap_down"}),
)


def _stat(value: Optional[float]) -> str:
    return "n/a" if value is None or math.isinf(value) else f"{value:+.2f}"


def render(entries: Iterable[Entry], *, top: int = 12, min_n: int = 20) -> str:
    """The miner's report in plain text: per kind, the process verdicts, the
    flagged cells, and the advance questions (overall and per account).
    Excess is in percentage points over SPY; a decline's is the trade it did
    not make."""
    rows = list(entries)
    lines = [f"PATTERNS - {len(rows)} journal entries (excess = points over SPY; flagged = n >= {min_n} and the "
             f"interval excludes zero after a Bonferroni correction; luck excluded)"]
    for kind, cells in tally_by_kind(rows, min_n=min_n).items():
        group = [e for e in rows if e.kind == kind]
        verdicts = {p: sum(1 for e in group if e.process == p) for p in
                    ("right_reason", "right_luck", "wrong_reason", "wrong_luck", "unclear")}
        lines += ["", f"{kind.upper()}S: n={len(group)}; " + ", ".join(f"{k.replace('_', ' ')} {v}"
                                                                     for k, v in verdicts.items())]
        flagged = [c for c in cells if c.flagged]
        lines.append(f"  flagged: {len(flagged) or 'none'}")
        for cell in flagged[:top]:
            lines.append(f"    {cell.label}: n={cell.n}, mean {cell.mean:+.2f} [{_stat(cell.low)}, {_stat(cell.high)}]")
        accounts = sorted({e.account for e in group})
        for label, tags in FOCUS:
            for account in (None, *accounts) if len(accounts) > 1 else (None,):
                subset = [e for e in group if account is None or e.account == account]
                f = focus(subset, label, **tags)
                where = f" ({account})" if account else ""
                if f.n == 0:
                    lines.append(f"  asked: {label}{where}: no cases yet")
                    continue
                lines.append(
                    f"  asked: {label}{where}: n={f.n}, mean {f.mean:+.2f} [{_stat(f.low)}, {_stat(f.high)}] "
                    f"(95%, uncorrected); over the {f.teaching_n} that teach: {_stat(f.teaching_mean)}"
                    + ("" if f.n >= min_n else f" - below n {min_n}: a reading, not a finding"))
    return "\n".join(lines)


def tally(entries: Iterable[Entry], *, min_n: int = 20, alpha: float = 0.05) -> list[Cell]:
    usable = [e for e in entries if e.teaches and e.excess is not None]
    groups: dict[tuple, list[float]] = {}
    for entry in usable:
        pairs = sorted((tag, str(entry.tags.get(tag))) for tag in TAG_NAMES if entry.tags.get(tag) is not None)
        for single in pairs:
            groups.setdefault((single,), []).append(entry.excess)
        for two in combinations(pairs, 2):
            groups.setdefault(two, []).append(entry.excess)
    tested = sum(1 for values in groups.values() if len(values) >= max(2, min_n))
    z = NormalDist().inv_cdf(1 - alpha / (2 * max(1, tested)))
    cells = []
    for key, values in groups.items():
        n = len(values)
        m = mean(values)
        if n >= 2:
            half = z * stdev(values) / math.sqrt(n)
        else:
            half = math.inf
        low, high = m - half, m + half
        flagged = n >= min_n and (low > 0 or high < 0)
        cells.append(Cell(key, n, m, low, high, flagged))
    return sorted(cells, key=lambda c: (not c.flagged, -c.n, c.label))
