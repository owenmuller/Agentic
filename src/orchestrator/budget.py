"""The daily research budget.

Research is the only per-signal cost in this system, and Class 1 feeds are bursty: a
single news cycle can produce more posts in an hour than a quiet week. Without a
ceiling, the spend is set by whoever is posting.

When the ceiling is reached, signals are *deferred*, not dropped. They stay queued and
are the first thing researched tomorrow. That distinction matters for the slow classes
especially — a 13F thesis is no less true for having waited a day — and it means the
ceiling costs latency rather than coverage.

The count is seeded from the audit log at startup rather than kept only in memory.
A process that crashed and restarted with a fresh counter would buy the budget again,
and a crash loop would buy it repeatedly; deriving it from what was actually recorded
makes that impossible.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger("orchestrator.budget")


class ResearchBudget:
    """Counts research passes against a per-day ceiling."""

    def __init__(
        self,
        max_per_day: int,
        clock: Optional[Callable[[], datetime]] = None,
        spent: int = 0,
        day: Optional[date] = None,
    ) -> None:
        if max_per_day < 0:
            raise ValueError(f"research budget cannot be negative, got {max_per_day}")
        self._max = max_per_day
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._day = day if day is not None else self._clock().date()
        self._spent = spent
        self._warned_for: Optional[date] = None

    @property
    def max_per_day(self) -> int:
        return self._max

    @property
    def day(self) -> date:
        return self._day

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return max(0, self._max - self._spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def _roll(self) -> None:
        """Reset at the day boundary. UTC, matching the audit log's timestamps."""
        today = self._clock().date()
        if today != self._day:
            self._day = today
            self._spent = 0

    def try_spend(self) -> bool:
        """Claim one pass. False means the ceiling is reached and nothing was claimed."""
        self._roll()
        if self.exhausted:
            self._warn_once()
            return False
        self._spent += 1
        return True

    def _warn_once(self) -> None:
        """Loud, and once per day. A warning repeated every tick is a warning ignored."""
        if self._warned_for == self._day:
            return
        self._warned_for = self._day
        logger.warning(
            "RESEARCH BUDGET EXHAUSTED for %s: %d of %d passes spent. Signals will "
            "queue unresearched until tomorrow. Raise max_research_passes_per_day in "
            "config/orchestrator.yaml if this is the wrong ceiling.",
            self._day,
            self._spent,
            self._max,
        )
