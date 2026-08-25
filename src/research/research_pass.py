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
from research.prompts import SYSTEM_PROMPT, build_user_prompt, build_verification_prompt
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


def _sum_usage(
    first: Optional[ResearchUsage], second: Optional[ResearchUsage]
) -> Optional[ResearchUsage]:
    """Both stages billed; the record carries the total. One unpriced side
    keeps the priced side's number rather than erasing it."""
    if first is None:
        return second
    if second is None:
        return first
    if first.cost_usd is None:
        cost = second.cost_usd
    elif second.cost_usd is None:
        cost = first.cost_usd
    else:
        cost = first.cost_usd + second.cost_usd
    return ResearchUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cost_usd=cost,
    )


class ResearchPass:
    """Scores one signal at a time."""

    def __init__(
        self,
        client: LLMClient,
        credibility: Optional[CredibilityTracker] = None,
        clock: Optional[Callable[[], datetime]] = None,
        rejection_sink: Optional[Callable[[ResearchRejection], None]] = None,
        market_context: Optional[Callable[[Signal], str]] = None,
        source_tiers: Optional[dict[str, str]] = None,
        screen_graduation: Optional[int] = None,
    ) -> None:
        self._client = client
        self._credibility = credibility
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rejections: list[ResearchRejection] = []
        self._sink = rejection_sink
        self._market_context = market_context
        #: Per-source verification tier names (cost architecture 2026-08-25),
        #: falling back to the signal class. Validated at startup by bootstrap.
        self._source_tiers = dict(source_tiers or {})
        #: Two-stage switch: None = single pass (the pre-2026-08-25 behaviour,
        #: and what harnesses without a screen config get); an int = the screen
        #: confidence a report must reach to graduate to verification.
        self._screen_graduation = screen_graduation
        self._last_usage: Optional["ResearchUsage"] = None
        self._last_screen: Optional[ResearchReport] = None
        self._last_screen_usage: Optional["ResearchUsage"] = None

    @property
    def last_usage(self) -> Optional["ResearchUsage"]:
        """Token/cost estimate of the most recent LLM call, for the audit record.

        None when the last run never reached the model (upstream exception before
        a response). Tokens billed by a call that later failed validation are
        still tokens billed — the estimate survives the rejection.
        """
        return self._last_usage

    @property
    def last_screen(self) -> Optional[ResearchReport]:
        """The stage-one draft behind the most recent VERIFIED verdict, for the
        audit record. None when the pass ended at stage one (the screen report
        was the record itself) or two-stage is off."""
        return self._last_screen

    @property
    def last_screen_usage(self) -> Optional["ResearchUsage"]:
        return self._last_screen_usage

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
        self._last_screen = None
        self._last_screen_usage = None
        tier = self._source_tiers.get(signal.source_id) or str(signal.signal_class)

        if self._screen_graduation is None:
            # Single pass at the source tier — two-stage is not configured.
            outcome, usage = self._call(signal, user_prompt, tier)
            self._last_usage = usage
            return self._record_final(signal, outcome)

        # Stage one: the cheap screen. Its failures and its unactionable verdicts
        # are the record — rejections get cheap.
        screen_outcome, screen_usage = self._call(signal, user_prompt, "screen")
        self._last_usage = screen_usage
        if not isinstance(screen_outcome, ResearchReport):
            return screen_outcome
        if (
            screen_outcome.recommends_no_position
            or screen_outcome.confidence < self._screen_graduation
        ):
            return self._record_final(signal, screen_outcome)

        # Stage two: independent verification on the source tier, screen draft
        # included as data. THE VERIFICATION REPORT IS THE RECORD — a trade never
        # sizes on a single unverified pass, and an override wins by construction
        # because the screen verdict is never returned past this point.
        self._last_screen = screen_outcome
        self._last_screen_usage = screen_usage
        verified_outcome, verify_usage = self._call(
            signal,
            build_verification_prompt(user_prompt, screen_outcome),
            tier,
        )
        self._last_usage = _sum_usage(screen_usage, verify_usage)
        return self._record_final(signal, verified_outcome)

    def _call(
        self, signal: Signal, user_prompt: str, tier: str
    ) -> tuple[ResearchOutcome, Optional["ResearchUsage"]]:
        """One model call plus every validation rule. Returns (outcome, usage)."""
        try:
            result = self._client.research(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                tool=report_tool_definition(),
                tier=tier,
            )
        except Exception as error:  # noqa: BLE001 - upstream failures are data here
            return (
                self._reject(
                    signal,
                    ResearchRejectionCode.UPSTREAM_ERROR,
                    f"research call failed: {type(error).__name__}: {error}",
                ),
                None,
            )
        usage = ResearchUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.est_cost_usd,
        )

        if not result.structured:
            # The model answered in prose, or not at all. One attempt, no re-roll.
            return (
                self._reject(
                    signal,
                    ResearchRejectionCode.NO_STRUCTURED_OUTPUT,
                    "model did not return a structured report",
                    excerpt=result.text,
                ),
                usage,
            )

        try:
            report = ResearchReport.model_validate(result.structured)
        except ValidationError as error:
            return (
                self._reject(
                    signal,
                    ResearchRejectionCode.SCHEMA_VALIDATION_FAILED,
                    f"report failed schema validation: {error.error_count()} error(s)",
                    excerpt=str(result.structured),
                ),
                usage,
            )

        if signal.signal_class in LAGGED_CLASSES and not report.has_priced_in_analysis:
            return (
                self._reject(
                    signal,
                    ResearchRejectionCode.MISSING_PRICED_IN_ANALYSIS,
                    (
                        f"{signal.signal_class} signals carry disclosure lag; "
                        f"priced_in_analysis is mandatory and was not provided"
                    ),
                    excerpt=report.thesis,
                ),
                usage,
            )

        return report, usage

    def _record_final(
        self, signal: Signal, outcome: ResearchOutcome
    ) -> ResearchOutcome:
        """Credibility sees exactly one report per signal: the one that is the
        record — never a superseded screen draft."""
        if isinstance(outcome, ResearchReport) and self._credibility is not None:
            self._credibility.record_report(
                signal.source_id,
                outcome,
                delivered_by=signal.metadata.get("delivered_by") or None,
            )
        return outcome

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
