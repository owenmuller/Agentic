"""Source credibility, fed to the research pass as context.

CLAUDE.md keeps retrospectives "for source-credibility tracking only". This turns that
log, plus whatever trade outcomes have been resolved, into a short factual summary the
research layer can weigh when setting confidence.

An honest limitation: a real hit rate needs resolved outcomes, and outcomes come from
the audit layer, which is not built. Until it is, ``hit_rate`` is None and the summary
says so in as many words. The volume figures — how many forward calls a source has
made, how many of its posts were retrospective brags — are available today and are
themselves informative: a source that posts ten brags for every live call is
describing its own selection bias.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from signals import CredibilityLog, Signal


@dataclass(frozen=True, slots=True)
class CredibilitySummary:
    """What the system knows about a source's track record."""

    source_id: str
    forward_calls_seen: int
    retrospectives_discarded: int
    resolved_calls: int
    winning_calls: int

    @property
    def hit_rate(self) -> Optional[float]:
        """Fraction of resolved calls that won, or None if nothing has resolved yet."""
        if self.resolved_calls <= 0:
            return None
        return self.winning_calls / self.resolved_calls

    @property
    def brag_ratio(self) -> Optional[float]:
        """Retrospective posts per forward call. High means mostly self-promotion."""
        if self.forward_calls_seen <= 0:
            return None
        return self.retrospectives_discarded / self.forward_calls_seen

    def as_context(self) -> str:
        """A plain-text summary for the prompt. States absence rather than implying it."""
        lines = [
            f"- source: {self.source_id}",
            f"- forward-looking calls observed: {self.forward_calls_seen}",
            f"- retrospective posts discarded: {self.retrospectives_discarded}",
        ]
        if self.brag_ratio is not None:
            lines.append(
                f"- retrospective posts per forward call: {self.brag_ratio:.1f}"
            )
        if self.hit_rate is None:
            lines.append(
                "- realised hit rate: NOT YET AVAILABLE. No calls from this source "
                "have been resolved to an outcome, so there is no track record to "
                "credit. Do not treat the absence of a bad record as a good one."
            )
        else:
            lines.append(
                f"- realised hit rate: {self.hit_rate:.0%} "
                f"({self.winning_calls} of {self.resolved_calls} resolved calls)"
            )
        return "\n".join(lines)


class CredibilityTracker:
    """Accumulates per-source counts from signals, the credibility log, and outcomes."""

    def __init__(self, credibility_log: Optional[CredibilityLog] = None) -> None:
        self._log = credibility_log
        self._forward_calls: dict[str, int] = {}
        self._resolved: dict[str, int] = {}
        self._wins: dict[str, int] = {}

    def observe(self, signal: Signal) -> None:
        """Count an emitted signal. Only forward calls reach the queue at all."""
        self._forward_calls[signal.source_id] = (
            self._forward_calls.get(signal.source_id, 0) + 1
        )

    def record_outcome(self, source_id: str, *, won: bool) -> None:
        """Resolve one call to a win or a loss.

        Called by the audit layer once a position closes. Nothing calls it yet — see
        this module's docstring.
        """
        self._resolved[source_id] = self._resolved.get(source_id, 0) + 1
        if won:
            self._wins[source_id] = self._wins.get(source_id, 0) + 1

    def _retrospectives_for(self, source_id: str) -> int:
        if self._log is None:
            return 0
        return sum(1 for record in self._log.records if record.source_id == source_id)

    def summary_for(self, source_id: str) -> CredibilitySummary:
        return CredibilitySummary(
            source_id=source_id,
            forward_calls_seen=self._forward_calls.get(source_id, 0),
            retrospectives_discarded=self._retrospectives_for(source_id),
            resolved_calls=self._resolved.get(source_id, 0),
            winning_calls=self._wins.get(source_id, 0),
        )
