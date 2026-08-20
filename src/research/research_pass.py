"""The research pass: Signal in, ResearchReport or typed rejection out.

The pass owns three rules that the schema alone cannot express:

  1. Class 2 and Class 3 signals must carry ``priced_in_analysis``. Both classes
     describe events that happened weeks ago; a verdict that has not reasoned about
     what moved since is not a verdict about a tradeable opportunity.
  2. Malformed output is a rejection, logged, once. No retry loop.
  3. Credibility context is supplied by the system, from the system's own records —
     never from anything the source said about itself.

The pass produces a report. It does not size, approve, or send anything, and this
package imports nothing that could.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional, Union

from pydantic import ValidationError

from research.client import LLMClient, ResearchUsage
from research.credibility import CredibilityTracker
from research.prompts import SYSTEM_PROMPT, build_user_prompt
from research.reports import (
    ResearchRejection,
    ResearchRejectionCode,
    ResearchReport,
    report_tool_definition,
)
from signals import Signal, SignalClass

#: Classes whose signals describe already-executed events, and therefore require an
#: explicit view on what has been priced in since.
LAGGED_CLASSES = frozenset({SignalClass.CLASS_2_MOMENTUM, SignalClass.CLASS_3_THESIS})

#: How much of a malformed response to keep for the audit record.
_EXCERPT_CHARS = 500

ResearchOutcome = Union[ResearchReport, ResearchRejection]


class ResearchPass:
    """Scores one signal at a time."""

    def __init__(
        self,
        client: LLMClient,
        credibility: Optional[CredibilityTracker] = None,
        clock: Optional[Callable[[], datetime]] = None,
        rejection_sink: Optional[Callable[[ResearchRejection], None]] = None,
        market_context: Optional[Callable[[Signal], str]] = None,
    ) -> None:
        self._client = client
        self._credibility = credibility
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rejections: list[ResearchRejection] = []
        self._sink = rejection_sink
        self._market_context = market_context
        self._last_usage: Optional["ResearchUsage"] = None

    @property
    def last_usage(self) -> Optional["ResearchUsage"]:
        """Token/cost estimate of the most recent LLM call, for the audit record.

        None when the last run never reached the model (upstream exception before
        a response). Tokens billed by a call that later failed validation are
        still tokens billed — the estimate survives the rejection.
        """
        return self._last_usage

    @property
    def rejections(self) -> tuple[ResearchRejection, ...]:
        """Every rejection this pass has produced, for the audit trail."""
        return tuple(self._rejections)

    def run(self, signal: Signal) -> ResearchOutcome:
        """Score a signal. Returns a validated report or a typed rejection."""
        credibility_context = self._context_for(signal)
        market_context = None
        if self._market_context is not None:
            try:
                market_context = self._market_context(signal)
            except Exception as error:  # noqa: BLE001 - context must never block a pass
                market_context = (
                    "market context unavailable (context builder failed: "
                    f"{type(error).__name__}). Proceed without it; never infer "
                    "or invent these numbers."
                )
        user_prompt = build_user_prompt(
            signal, credibility_context, market_context=market_context
        )

        self._last_usage = None
        try:
            result = self._client.research(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                tool=report_tool_definition(),
                tier=str(signal.signal_class),
            )
        except Exception as error:  # noqa: BLE001 - upstream failures are data here
            return self._reject(
                signal,
                ResearchRejectionCode.UPSTREAM_ERROR,
                f"research call failed: {type(error).__name__}: {error}",
            )
        self._last_usage = ResearchUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.est_cost_usd,
        )

        if not result.structured:
            # The model answered in prose, or not at all. One attempt, no re-roll.
            return self._reject(
                signal,
                ResearchRejectionCode.NO_STRUCTURED_OUTPUT,
                "model did not return a structured report",
                excerpt=result.text,
            )

        try:
            report = ResearchReport.model_validate(result.structured)
        except ValidationError as error:
            return self._reject(
                signal,
                ResearchRejectionCode.SCHEMA_VALIDATION_FAILED,
                f"report failed schema validation: {error.error_count()} error(s)",
                excerpt=str(result.structured),
            )

        if signal.signal_class in LAGGED_CLASSES and not report.has_priced_in_analysis:
            return self._reject(
                signal,
                ResearchRejectionCode.MISSING_PRICED_IN_ANALYSIS,
                (
                    f"{signal.signal_class} signals carry disclosure lag; "
                    f"priced_in_analysis is mandatory and was not provided"
                ),
                excerpt=report.thesis,
            )

        if self._credibility is not None:
            # Every accepted report updates the source's record, flagged or not.
            self._credibility.record_report(
                signal.source_id,
                report,
                delivered_by=signal.metadata.get("delivered_by") or None,
            )

        return report

    # -- internals ----------------------------------------------------------------

    def _context_for(self, signal: Signal) -> Optional[str]:
        """Credibility context, for sources the system actually tracks.

        A mirror-delivered signal also carries the CHANNEL's record when it has
        one — a mirror that has previously delivered mislabeled commentary is a
        fact about this delivery, not about the principal.
        """
        if self._credibility is None:
            return None
        parts: list[str] = []
        summary = self._credibility.summary_for(signal.source_id)
        if summary.has_record:
            parts.append(summary.as_context())
        delivered_by = signal.metadata.get("delivered_by")
        if delivered_by:
            channel = self._credibility.summary_for(delivered_by)
            if channel.has_record:
                parts.append(
                    "DELIVERY CHANNEL RECORD (the unofficial mirror that "
                    "delivered this post, tracked separately from the "
                    "principal):\n" + channel.as_context()
                )
        return "\n".join(parts) if parts else None

    def _reject(
        self,
        signal: Signal,
        code: ResearchRejectionCode,
        message: str,
        excerpt: str = "",
    ) -> ResearchRejection:
        rejection = ResearchRejection(
            code=code,
            message=message,
            signal_id=signal.signal_id,
            raw_excerpt=excerpt[:_EXCERPT_CHARS],
            occurred_at=self._clock(),
        )
        self._rejections.append(rejection)
        if self._sink is not None:
            self._sink(rejection)
        return rejection
