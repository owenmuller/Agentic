"""The AI trader's model call: the PRODUCTION request path, reused.

``research.client.AnthropicResearchClient`` is the main book's live-validated
two-phase call (search with a free tool choice, then the report tool forced;
search results elided from the report phase; report T=0 on the configured
models). The lab reuses it rather than writing a second request path
(CLAUDE.md § LLM Request-Path Changes): it hands the client a copy of
research.yaml whose ``class_2`` tier is the AI trader's model, effort and
search budget, and calls that tier. Nothing in the main book's config or
calls changes.

Spend: the client's estimate prices tokens (cache tiers included) from
research.yaml's pricing table; web searches are billed separately ($10 per
1,000), so this wrapper counts them from each response's
``usage.server_tool_use.web_search_requests`` and adds ``search_fee_usd``
each. The total is the figure the daily cap is enforced on and reported as.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from research.client import AnthropicResearchClient
from research.config import ModelTier, ResearchConfig, TierOverrides

ZERO = Decimal("0")


@dataclass(frozen=True)
class PassResult:
    structured: Optional[dict[str, Any]]
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    searches: int
    token_cost_usd: Optional[Decimal]
    cost_usd: Decimal
    transcript_hash: str
    stop_reason: Optional[str]


class _CountingClient(AnthropicResearchClient):
    """The production client, plus a count of web searches per call."""

    searches = 0

    def _track_usage(self, response: Any, usage: dict[str, int]) -> None:  # type: ignore[override]
        AnthropicResearchClient._track_usage(response, usage)
        server = getattr(getattr(response, "usage", None), "server_tool_use", None)
        self.searches += int(getattr(server, "web_search_requests", 0) or 0)


class LabLLM:
    TIER = "class_2"

    def __init__(self, ai_config: Any, *, client: Any = None, research: Optional[ResearchConfig] = None) -> None:
        base = research or ResearchConfig.load()
        tier = ModelTier(model=ai_config.model, effort=ai_config.effort, max_searches=ai_config.max_searches)
        overrides = base.tiers.model_dump() if base.tiers is not None else {}
        overrides[self.TIER] = tier
        self.config = base.model_copy(update={"tiers": TierOverrides(**overrides)})
        self._fee = Decimal(str(ai_config.search_fee_usd))
        self._client = _CountingClient(self.config, client=client)

    def scan(self, *, system: str, user: str, tool: dict[str, Any]) -> PassResult:
        self._client.searches = 0
        result = self._client.research(system=system, user=user, tool=tool, tier=self.TIER)
        searches = self._client.searches
        tokens = result.est_cost_usd
        # An unpriced model has no estimate; the cap then counts the pass at
        # nothing it can prove - so the config validator keeps the model
        # priced (pinned and in the pricing table), and a None here is a bug.
        if tokens is None:
            raise RuntimeError(f"model {result.model or 'unknown'} has no price in research.yaml; refusing to meter blind")
        return PassResult(
            structured=result.structured,
            text=result.text,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            searches=searches,
            token_cost_usd=tokens,
            cost_usd=tokens + self._fee * searches,
            transcript_hash=result.transcript_hash,
            stop_reason=result.stop_reason,
        )
