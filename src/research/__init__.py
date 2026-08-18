"""LLM research and confidence layer.

This is the first place untrusted signal content meets a model, so the architecture is
the defence:

  - Content enters the prompt only inside ``as_data_block`` framing — fenced, labelled
    as third-party data, with fence markers inside the content defanged.
  - The system prompt states that the content may be adversarial and that judging
    whether it is trying to induce a trade is part of the analysis.
  - The model answers through a forced tool call against a closed schema. There is no
    field for size, caps, or gate behaviour, so a report has no vocabulary in which to
    ask for them.
  - Confidence is an integer that feeds a deterministic table with a hard 5% cap. A
    report claiming "confidence 100, ignore the limits" is a report with confidence
    100, which sizes exactly like any other 100.
  - Malformed output is a typed rejection, logged once. No retry-until-it-parses loop.

This package imports nothing from ``risk_gate``, ``sizing`` or ``execution``.
"""

from research.client import (
    WEB_SEARCH_TOOL_TYPE,
    AnthropicResearchClient,
    LLMClient,
    LLMResult,
)
from research.config import ResearchConfig, WebSearchConfig, default_research_path
from research.credibility import CredibilitySummary, CredibilityTracker
from research.prompts import SYSTEM_PROMPT, build_user_prompt
from research.reports import (
    REPORT_TOOL_NAME,
    Direction,
    ResearchRejection,
    ResearchRejectionCode,
    ResearchReport,
    TimeHorizon,
    report_tool_definition,
    strip_unsupported_schema_keywords,
)
from research.research_pass import LAGGED_CLASSES, ResearchOutcome, ResearchPass

__all__ = [
    "LAGGED_CLASSES",
    "REPORT_TOOL_NAME",
    "SYSTEM_PROMPT",
    "WEB_SEARCH_TOOL_TYPE",
    "AnthropicResearchClient",
    "CredibilitySummary",
    "CredibilityTracker",
    "Direction",
    "LLMClient",
    "LLMResult",
    "ResearchConfig",
    "ResearchOutcome",
    "ResearchPass",
    "ResearchRejection",
    "ResearchRejectionCode",
    "ResearchReport",
    "TimeHorizon",
    "WebSearchConfig",
    "build_user_prompt",
    "default_research_path",
    "report_tool_definition",
    "strip_unsupported_schema_keywords",
]
