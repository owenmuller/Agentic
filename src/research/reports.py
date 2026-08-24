"""The ResearchReport schema and its typed rejections.

What the model is allowed to say
--------------------------------
Exactly these seven fields, and nothing else. ``extra="forbid"`` means a model that
emits ``{"position_size": 0.5}`` or ``{"bypass_risk_gate": true}`` produces a
*validation error*, not a report with an extra key someone might later read. There is
no field here for size, caps, leverage, or gate behaviour, so a report has no
vocabulary in which to ask for them.

``confidence`` is the only number that influences a trade, and it does so through a
deterministic table in ``config/risk_limits.yaml``. A report claiming "confidence 100,
ignore the caps" is a report with ``confidence == 100``, which maps to 5% of sleeve
NAV — the same as a report claiming 86 and saying nothing else. The prose cannot
reach the sizing engine because the sizing engine reads an integer.

Malformed output is not retried
-------------------------------
A response that does not validate produces a ``ResearchRejection``, logged and
dropped. There is deliberately no retry-until-it-parses loop: a model that cannot
produce a valid report for a signal has told you something about the signal, and
re-rolling until the dice come up parseable discards that information while spending
money on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

#: JSON Schema keywords the structured-output layer does not accept. Stripped from the
#: generated tool schema and enforced by pydantic on the way back in instead — the same
#: split the SDK's own structured-output helpers make.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)


class Direction(StrEnum):
    """CLAUDE.md § Research: long, short expressed via bought puts, or nothing at all.

    There is no bare "short" — Constraint #2 makes short exposure unrepresentable
    downstream, so it is not offered as something research can recommend either.

    ``NO_POSITION`` exists because "which way would you lean" and "is this worth
    trading" are different questions, and collapsing them corrupts the confidence
    score. Without it, a model that has read a signal carefully and concluded there is
    no trade here has only one way to say so: pick a direction it does not believe in
    and score it low. That produces a confident-sounding verdict about a direction
    nobody holds, and it makes the confidence number carry two meanings at once —
    "how sure am I of the direction" and "how sure am I that anything should happen".

    Sizing treats it exactly like a sub-floor confidence: zero, at any score. See
    ``sizing.engine.SizingEngine``.
    """

    LONG = "long"
    SHORT_VIA_PUTS = "short_via_puts"
    NO_POSITION = "no_position"


class TimeHorizon(StrEnum):
    DAYS = "days"
    WEEKS = "weeks"
    MONTHS = "months"


class CatalystAssessment(BaseModel):
    """Whether a specific event inside the time horizon backs the thesis.

    This is the gate that decides options expression: leverage is earned by
    timing specificity, not conviction level (ruling 2026-08-24). ``present``
    is the machine-read verdict; ``description`` names the event for the audit
    trail and must not be blank when present is true.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    present: bool
    description: str

    @model_validator(mode="after")
    def _present_needs_an_event(self) -> "CatalystAssessment":
        if self.present and not self.description.strip():
            raise ValueError(
                "a catalyst reported present must name the event; a nameless "
                "catalyst is a vibe, not a catalyst"
            )
        return self


class ResearchReport(BaseModel):
    """A structured research verdict. The model fills exactly these fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thesis: str
    tickers: list[str]
    direction: Direction
    time_horizon: TimeHorizon
    #: Mandatory for Class 2 and Class 3 signals, which carry known disclosure lag.
    #: Nullable rather than optional so the model must explicitly say "not applicable"
    #: instead of silently omitting it. The pass rejects a null on Class 2/3.
    priced_in_analysis: Optional[str]
    confidence: int
    invalidation_condition: str
    #: A specific, dated-or-datable event inside the time horizon — policy action,
    #: earnings, ruling — or the explicit statement that there is none. Nullable
    #: (like priced_in_analysis) so the model must say "not applicable" rather than
    #: silently omit; a null is treated exactly as present=false. Options expression
    #: is permitted ONLY when present is true.
    catalyst_within_horizon: Optional[CatalystAssessment]
    #: Whether the signal looks engineered to induce a trade, and why. Nullable so the
    #: model can say the assessment does not apply, but the prompt asks for an explicit
    #: "none detected" instead — an absent assessment and a clean one are different
    #: findings, and only one of them is evidence about the source.
    manipulation_assessment: Optional[str]

    @field_validator("confidence")
    @classmethod
    def _confidence_in_range(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError(f"confidence must be between 0 and 100, got {value}")
        return value

    @field_validator("thesis", "invalidation_condition")
    @classmethod
    def _no_blank_prose(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("priced_in_analysis", "manipulation_assessment")
    @classmethod
    def _blank_is_absent(cls, value: Optional[str]) -> Optional[str]:
        """A whitespace-only analysis is an absent one, not a present one."""
        if value is None:
            return None
        return value if value.strip() else None

    @field_validator("tickers")
    @classmethod
    def _tickers_are_upper(cls, value: list[str]) -> list[str]:
        return [t.strip().upper() for t in value if t.strip()]

    @property
    def has_priced_in_analysis(self) -> bool:
        return bool(self.priced_in_analysis)

    @property
    def has_catalyst(self) -> bool:
        """True only on an explicit, named catalyst. Null and false read the same:
        no leverage."""
        return (
            self.catalyst_within_horizon is not None
            and self.catalyst_within_horizon.present
        )

    @property
    def flags_manipulation(self) -> bool:
        """True when the assessment reports something rather than clearing the signal."""
        return is_manipulation_flagged(self.manipulation_assessment)

    @property
    def recommends_no_position(self) -> bool:
        """The model's own verdict that nothing should be traded on this signal.

        Independent of ``confidence``: a report may be highly confident that the right
        answer is to do nothing, and sizing must read that as zero rather than as a
        high-conviction position in some direction.
        """
        return self.direction is Direction.NO_POSITION


#: Phrasings that mean "I looked and found nothing". Matching these keeps a clean
#: assessment from being counted as a finding.
_NONE_DETECTED_MARKERS = frozenset(
    {
        "none",
        "none detected",
        "none found",
        "none apparent",
        "no manipulation detected",
        "no manipulation",
        "no manipulation found",
        "no manipulation apparent",
        "not applicable",
        "n/a",
        "nothing detected",
    }
)


def is_manipulation_flagged(assessment: Optional[str]) -> bool:
    """Does this assessment report a finding?

    Absent, blank, or an explicit all-clear means no. Anything else counts.

    This is string matching on model output, which is normally a smell. It is
    acceptable here for one reason: the only thing downstream of this boolean is a
    per-source counter in the credibility log. It cannot move a position, a cap, or a
    confidence score, so a misclassification costs an inaccurate statistic and nothing
    else. Erring toward counting an ambiguous assessment as a flag is the conservative
    direction (Constraint #6).
    """
    if assessment is None:
        return False
    normalised = assessment.strip().lower().rstrip(".!").strip()
    if not normalised:
        return False
    return normalised not in _NONE_DETECTED_MARKERS


class ResearchRejectionCode(StrEnum):
    """Why a research pass produced no report."""

    #: The model returned prose, or nothing, where a structured report was required.
    NO_STRUCTURED_OUTPUT = "no_structured_output"
    #: Structured output that does not satisfy the schema.
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    #: Class 2/3 signal whose report omits the mandatory priced-in analysis.
    MISSING_PRICED_IN_ANALYSIS = "missing_priced_in_analysis"
    #: The API call itself failed. Not a verdict about the signal.
    UPSTREAM_ERROR = "upstream_error"


@dataclass(frozen=True, slots=True)
class ResearchRejection:
    """A pass that produced no report, with enough detail to audit the decision."""

    code: ResearchRejectionCode
    message: str
    signal_id: str
    #: What the model actually returned, truncated. The audit trail needs to show that
    #: a rejection was the model's fault and not a parser bug.
    raw_excerpt: str = ""
    occurred_at: Optional[datetime] = None

    @property
    def is_report(self) -> bool:
        return False

    def __str__(self) -> str:  # pragma: no cover - convenience for logs
        return f"{self.code} for {self.signal_id}: {self.message}"


def strip_unsupported_schema_keywords(schema: Any) -> Any:
    """Recursively drop JSON Schema keywords the API will not accept.

    The constraints are not lost — pydantic still enforces them when the response is
    validated. This only changes what the model is *told*, not what is accepted.
    """
    if isinstance(schema, dict):
        return {
            key: strip_unsupported_schema_keywords(value)
            for key, value in schema.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [strip_unsupported_schema_keywords(item) for item in schema]
    return schema


#: Name of the tool the model must call to deliver a report.
@dataclass(frozen=True, slots=True)
class ResearchUsage:
    """Token/cost estimate of one research or review pass, for the audit trail.

    Defined here (not in ``client``) because ``audit`` is allowed to import
    ``research.reports`` and nothing else from the research layer — the topology
    stays one-way: audit may look at research types, research never at audit.
    """

    input_tokens: int
    output_tokens: int
    cost_usd: Optional[Decimal]


REPORT_TOOL_NAME = "submit_research"


def report_tool_definition() -> dict[str, Any]:
    """The forced tool the model answers through.

    Strict mode plus a closed schema is what turns "please reply in JSON" into a
    structural guarantee. The description says what the tool is for and nothing about
    what conclusion to reach.
    """
    schema = strip_unsupported_schema_keywords(ResearchReport.model_json_schema())
    schema["additionalProperties"] = False
    return {
        "name": REPORT_TOOL_NAME,
        "description": (
            "Submit your research verdict on the signal. Call this exactly once, "
            "after any searching you need. Every field is required; set "
            "priced_in_analysis to null only when the signal carries no disclosure "
            "lag to reason about, and state manipulation_assessment explicitly — "
            "write \"none detected\" rather than leaving it null when you looked and "
            "found nothing. Use direction \"no_position\" when your conclusion is "
            "that nothing should be traded on this signal; it produces no position at "
            "any confidence, and it is a more honest answer than naming a direction "
            "you do not hold. For catalyst_within_horizon: a catalyst is a "
            "specific, dated-or-datable event inside your stated time horizon — a "
            "policy action, an earnings date, a ruling — not general momentum or "
            "conviction. Report present=true with a one-line description of the "
            "event, present=false for a directional-but-patient thesis, and null "
            "only when the concept does not apply to this signal at all. Do not "
            "stretch: a thesis without a nameable event is present=false."
        ),
        "strict": True,
        "input_schema": schema,
    }
