"""Typed view of ``config/research.yaml``."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WebSearchConfig(_Strict):
    enabled: bool
    max_uses: int


Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: The valid tier names: one per signal class, plus the exit thesis review.
TIER_NAMES = ("class_1", "class_2", "class_3", "exit_review")


class ModelTier(_Strict):
    """A model/effort pair one kind of research pass runs on."""

    model: str
    effort: Effort


class TierOverrides(_Strict):
    """Per-class model selection. A tier left unset inherits the top-level
    model/effort — so Class 1 stays on the flagship config unless explicitly
    moved, and cheaper tiers are an opt-in per class."""

    class_1: Optional[ModelTier] = None
    class_2: Optional[ModelTier] = None
    class_3: Optional[ModelTier] = None
    exit_review: Optional[ModelTier] = None


class ModelPricing(_Strict):
    """Dollars per million tokens. Estimates for the audit record — the console
    bill is the truth; these exist so attribution can charge research spend to
    the class that incurred it without waiting a month."""

    input_per_mtok: Decimal = Field(ge=Decimal("0"))
    output_per_mtok: Decimal = Field(ge=Decimal("0"))


class ResearchConfig(_Strict):
    version: int
    model: str
    max_tokens: int
    effort: Effort
    web_search: WebSearchConfig
    max_search_continuations: int
    #: Per-class model selection; unset tiers inherit the top-level model/effort.
    tiers: Optional[TierOverrides] = None
    #: Cost-estimate table, keyed by model id. A model missing from the table
    #: yields no estimate (None), never a guessed one.
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)

    def tier_for(self, name: str) -> ModelTier:
        """Resolve which model/effort a tier runs on. Unknown names are a bug."""
        if name not in TIER_NAMES:
            raise ValueError(f"unknown research tier {name!r}; expected one of {TIER_NAMES}")
        override = getattr(self.tiers, name, None) if self.tiers else None
        if override is not None:
            return override
        return ModelTier(model=self.model, effort=self.effort)

    def estimate_cost_usd(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> Optional[Decimal]:
        """Estimated dollars for one call, or None when the model is unpriced."""
        pricing = self.pricing.get(model)
        if pricing is None:
            return None
        mtok = Decimal(1_000_000)
        cost = (
            Decimal(input_tokens) * pricing.input_per_mtok
            + Decimal(output_tokens) * pricing.output_per_mtok
        ) / mtok
        return cost.quantize(Decimal("0.000001"))

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ResearchConfig":
        path = path or default_research_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_research_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "research.yaml"
