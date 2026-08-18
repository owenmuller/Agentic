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
from typing import TYPE_CHECKING, Optional

from signals import CredibilityLog, Signal

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from research.reports import ResearchReport


@dataclass(frozen=True, slots=True)
class CredibilitySummary:
    """What the system knows about a source's track record."""

    source_id: str
    forward_calls_seen: int
    retrospectives_discarded: int
    resolved_calls: int
    winning_calls: int
    #: Reports produced for this source, whether or not they flagged anything.
    reports_scored: int = 0
    #: Reports whose manipulation_assessment found something.
    manipulation_flags: int = 0
    #: The most recent findings, newest last, each truncated to
    #: ``CredibilityTracker.NOTE_CHARS``. Bounded in both directions — how many notes,
    #: and how long each may be — so the prompt cannot grow without limit as a source
    #: accumulates history. The untruncated text lives in the audit record.
    recent_manipulation_notes: tuple[str, ...] = ()

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

    @property
    def has_record(self) -> bool:
        """Is there anything here worth showing the model?

        Deliberately covers every counter, not just signal volume: a source whose only
        history is a manipulation finding has the most relevant record of all.
        """
        return any(
            (
                self.forward_calls_seen,
                self.retrospectives_discarded,
                self.resolved_calls,
                self.reports_scored,
                self.manipulation_flags,
            )
        )

    @property
    def manipulation_rate(self) -> Optional[float]:
        """Fraction of scored reports that flagged manipulation."""
        if self.reports_scored <= 0:
            return None
        return self.manipulation_flags / self.reports_scored

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

        if self.reports_scored:
            lines.append(
                f"- manipulation flagged on {self.manipulation_flags} of "
                f"{self.reports_scored} scored reports"
            )
            for note in self.recent_manipulation_notes:
                lines.append(f"  - previously flagged: {note}")
        return "\n".join(lines)


class CredibilityTracker:
    """Accumulates per-source counts from signals, the credibility log, and outcomes."""

    #: How many past manipulation findings to replay into a prompt.
    NOTE_HISTORY = 3

    #: How much of each finding to keep for replay. A manipulation note is a pointer —
    #: "this account posts entry prices it has already exited" — and the first
    #: sentences carry that; what follows is elaboration the model does not need in
    #: order to weigh the source. Capping it keeps a verbose model, or a source with a
    #: long history, from crowding the signal under analysis out of its own prompt.
    #:
    #: This truncation is for prompt replay ONLY. The full assessment is written
    #: verbatim to the audit record (``audit.records.ResearchSnapshot``), which is where
    #: an incident review reads it. Nothing here is the system of record.
    NOTE_CHARS = 300

    def __init__(self, credibility_log: Optional[CredibilityLog] = None) -> None:
        self._log = credibility_log
        self._forward_calls: dict[str, int] = {}
        self._resolved: dict[str, int] = {}
        self._wins: dict[str, int] = {}
        self._reports: dict[str, int] = {}
        self._flags: dict[str, int] = {}
        self._notes: dict[str, list[str]] = {}

    def observe(self, signal: Signal) -> None:
        """Count an emitted signal. Only forward calls reach the queue at all."""
        self._forward_calls[signal.source_id] = (
            self._forward_calls.get(signal.source_id, 0) + 1
        )

    def record_report(self, source_id: str, report: "ResearchReport") -> None:
        """Fold a finished report's manipulation assessment into the source's record.

        Called by the research pass on every report it produces, flagged or not — the
        denominator matters as much as the numerator. A source flagged once in fifty
        reports is a different source from one flagged once in two.
        """
        self._reports[source_id] = self._reports.get(source_id, 0) + 1
        if not report.flags_manipulation:
            return
        self._flags[source_id] = self._flags.get(source_id, 0) + 1
        notes = self._notes.setdefault(source_id, [])
        notes.append(_truncate(str(report.manipulation_assessment), self.NOTE_CHARS))
        del notes[: -self.NOTE_HISTORY]

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
            reports_scored=self._reports.get(source_id, 0),
            manipulation_flags=self._flags.get(source_id, 0),
            recent_manipulation_notes=tuple(self._notes.get(source_id, ())),
        )


def _truncate(note: str, limit: int) -> str:
    """Cap a note at ``limit`` characters, marking it when anything was dropped.

    The ellipsis is inside the budget rather than appended to it, so the result is
    never longer than ``limit``. Marking matters: an unmarked truncation reads as a
    complete finding that happens to end mid-sentence, and a model shown one may treat
    the missing half as absent rather than elided.
    """
    if len(note) <= limit:
        return note
    return note[: limit - 1].rstrip() + "…"
