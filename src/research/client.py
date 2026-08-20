"""LLM client seam and the Anthropic implementation.

``LLMClient`` is the interface the research pass depends on; ``AnthropicResearchClient``
is the real one. Tests drive the pass through a fake implementing the same protocol, so
prompt construction, parsing, and rejection handling are all exercised without a
network call.

Why two phases
--------------
Structured output here is a *forced* tool call — the model must answer by calling
``submit_research``, which is what makes "returned prose instead of a report" a
detectable state rather than a parsing gamble. But a forced tool choice leaves no room
to search first. So when web search is enabled the call runs in two phases:

  1. Research: web search available, tool choice free. The model gathers evidence.
  2. Report: the phase-1 transcript replayed, ``submit_research`` forced.

Phase 2 runs exactly once. If it comes back malformed, that is a rejection — there is
no re-roll (see ``reports`` module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Protocol

from research.reports import REPORT_TOOL_NAME, ResearchUsage

#: Server-side web search. Runs on Anthropic's infrastructure within the same request.
WEB_SEARCH_TOOL_TYPE = "web_search_20260209"


@dataclass(frozen=True, slots=True)
class LLMResult:
    """What the research pass gets back, independent of provider."""

    #: The validated tool input, or None if the model did not call the report tool.
    structured: Optional[dict[str, Any]]
    #: Whatever text the model produced. Used for the rejection excerpt.
    text: str
    stop_reason: Optional[str] = None
    model: str = ""
    #: Token usage summed over every API call this result took (search phases
    #: included). Estimates for the audit trail; the console bill is the truth.
    input_tokens: int = 0
    output_tokens: int = 0
    #: Estimated dollars, from the pricing table in research.yaml. None when the
    #: model is unpriced — an absent estimate, never a guessed one.
    est_cost_usd: Optional[Decimal] = None


class LLMClient(Protocol):
    """The seam. Implementations do I/O; the research pass does not.

    ``tier`` names which model tier the call runs on (a signal class or
    ``exit_review``); implementations without tiering may ignore it.
    """

    def research(
        self, *, system: str, user: str, tool: dict[str, Any], tier: str = "class_1"
    ) -> LLMResult:
        ...


def _fresh_usage() -> dict[str, int]:
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def _cached_system(system: str) -> list[dict[str, Any]]:
    """System prompt as a cache-controlled block (verified against the prompt
    caching docs 2026-08-20: list-of-blocks form, cache_control on the block;
    the tools->system hierarchy means this breakpoint covers the tools too).
    Below the model's minimum cacheable size the marker is simply ignored."""
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]


#: Marker appended to the report-phase instruction when search-result payloads
#: were elided from the replay. Honest by construction: it states what was cut
#: and why the analysis text is still trustworthy — nothing is summarised by a
#: model to save tokens.
ELISION_MARKER = (
    "[NOTE: {count} web-search result payload(s) from your research phase were "
    "elided from this replay to bound context size. Your own written analysis "
    "above was produced with the full results in view — rely on it. Do not "
    "guess at elided content.]"
)


def _elide_search_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip server-tool search blocks from the transcript, keeping the model's
    own text. Result payloads are encrypted and must otherwise be replayed
    byte-identical (API contract), so the only honest budget is elision — the
    marker says exactly what happened."""
    elided = 0
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "assistant" or isinstance(content, str):
            out.append(message)
            continue
        kept = []
        for block in content or []:
            block_type = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None
            )
            if block_type in ("server_tool_use", "web_search_tool_result"):
                if block_type == "web_search_tool_result":
                    elided += 1
                continue
            kept.append(block)
        if not kept:
            # Never drop the whole assistant turn: role alternation must hold.
            kept = [{"type": "text", "text": "(ran web searches; payloads elided)"}]
        out.append({"role": "assistant", "content": kept})
    if elided:
        out.append(
            {"role": "user", "content": ELISION_MARKER.format(count=elided)}
        )
    return out


class AnthropicResearchClient:
    """Calls Claude for a structured research verdict."""

    def __init__(
        self,
        config: "ResearchConfig",  # noqa: F821 - imported lazily to avoid a cycle
        client: Optional[Any] = None,
    ) -> None:
        self._config = config
        if client is not None:
            self._client = client
        else:
            import anthropic  # imported here so tests need no API key

            from execution.environment import load_environment, require_env

            load_environment()
            self._client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))

    # -- the two phases ----------------------------------------------------------

    def research(
        self, *, system: str, user: str, tool: dict[str, Any], tier: str = "class_1"
    ) -> LLMResult:
        resolved = self._config.tier_for(tier)
        usage = _fresh_usage()
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

        if self._config.web_search.enabled:
            messages = self._gather_evidence(system, messages, resolved, usage)
            if not self._config.web_search.replay_results_in_report:
                messages = _elide_search_results(messages)

        return self._request_report(system, messages, tool, resolved, usage)

    def triage(
        self, *, system: str, user: str, tool: dict[str, Any]
    ) -> LLMResult:
        """One cheap forced-tool call. No search, no phases, no authority."""
        triage_config = self._config.triage
        if triage_config is None:
            raise RuntimeError("triage called with no triage config")
        usage = _fresh_usage()
        response = self._client.messages.create(
            model=triage_config.model,
            max_tokens=triage_config.max_tokens,
            system=_cached_system(system),
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
        )
        self._track_usage(response, usage)
        return self._to_result(
            response,
            usage=usage,
            est_cost_usd=self._estimate(triage_config.model, usage),
        )

    def _estimate(self, model: str, usage: dict[str, int]) -> Optional[Decimal]:
        return self._config.estimate_cost_usd(
            model,
            usage["input"],
            usage["output"],
            cache_write_tokens=usage["cache_write"],
            cache_read_tokens=usage["cache_read"],
        )

    @staticmethod
    def _track_usage(response: Any, usage: dict[str, int]) -> None:
        reported = getattr(response, "usage", None)
        usage["input"] += int(getattr(reported, "input_tokens", 0) or 0)
        usage["output"] += int(getattr(reported, "output_tokens", 0) or 0)
        usage["cache_write"] += int(
            getattr(reported, "cache_creation_input_tokens", 0) or 0
        )
        usage["cache_read"] += int(
            getattr(reported, "cache_read_input_tokens", 0) or 0
        )

    def _gather_evidence(
        self,
        system: str,
        messages: list[dict[str, Any]],
        resolved: "ModelTier",  # noqa: F821 - lazy import, see __init__
        usage: dict[str, int],
    ) -> list[dict[str, Any]]:
        """Phase 1: let the model search. Returns the transcript to replay.

        Bounded by ``max_search_continuations``. A ``pause_turn`` means the server-side
        tool loop hit its own iteration limit and can be resumed by re-sending; the cap
        stops that becoming unbounded.
        """
        transcript = list(messages)
        for _ in range(self._config.max_search_continuations + 1):
            response = self._client.messages.create(
                model=resolved.model,
                max_tokens=self._config.max_tokens,
                system=_cached_system(system),
                messages=transcript,
                output_config={"effort": resolved.effort},
                tools=[
                    {
                        "type": WEB_SEARCH_TOOL_TYPE,
                        "name": "web_search",
                        "max_uses": self._config.web_search.max_uses,
                    }
                ],
            )
            self._track_usage(response, usage)
            transcript.append({"role": "assistant", "content": response.content})
            if getattr(response, "stop_reason", None) != "pause_turn":
                break
        return transcript

    def _request_report(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tool: dict[str, Any],
        resolved: "ModelTier",  # noqa: F821
        usage: dict[str, int],
    ) -> LLMResult:
        """Phase 2: force the report tool. One attempt."""
        if messages and messages[-1].get("role") == "assistant":
            messages = messages + [
                {
                    "role": "user",
                    "content": (
                        "Submit your verdict now by calling the submit_research tool."
                    ),
                }
            ]

        response = self._client.messages.create(
            model=resolved.model,
            max_tokens=self._config.max_tokens,
            system=_cached_system(system),
            messages=messages,
            output_config={"effort": resolved.effort},
            tools=[tool],
            tool_choice={"type": "tool", "name": REPORT_TOOL_NAME},
        )
        self._track_usage(response, usage)
        return self._to_result(
            response,
            usage=usage,
            est_cost_usd=self._estimate(resolved.model, usage),
        )

    @staticmethod
    def _to_result(
        response: Any,
        usage: Optional[dict[str, int]] = None,
        est_cost_usd: Optional[Decimal] = None,
    ) -> LLMResult:
        structured: Optional[dict[str, Any]] = None
        texts: list[str] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use" and getattr(block, "name", "") == (
                REPORT_TOOL_NAME
            ):
                structured = getattr(block, "input", None)
            elif block_type == "text":
                texts.append(getattr(block, "text", ""))
        usage = usage or _fresh_usage()
        return LLMResult(
            structured=structured,
            text="\n".join(texts),
            stop_reason=getattr(response, "stop_reason", None),
            model=getattr(response, "model", ""),
            # Total context handled, cache tiers included — the record shows the
            # size; the cost estimate prices each tier at its own rate.
            input_tokens=usage["input"] + usage["cache_write"] + usage["cache_read"],
            output_tokens=usage["output"],
            est_cost_usd=est_cost_usd,
        )
