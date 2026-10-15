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
from typing import Iterable

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
