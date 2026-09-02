"""Typed view of ``config/research.yaml``."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WebSearchConfig(_Strict):
    enabled: bool
    max_uses: int
    #: Replay raw search-result blocks into the report phase. False elides them
    #: (with a marker) — result content is encrypted and must be replayed
    #: byte-identical or the API 400s, so elision is the only honest payload cut.
    replay_results_in_report: bool = True


Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: The valid tier names: one per signal class, plus the exit thesis review.
TIER_NAMES = ("class_1", "class_2", "class_3", "exit_review")


class ModelTier(_Strict):
    """A model/effort pair one kind of research pass runs on."""

    model: str
    effort: Effort
    #: Per-tier web-search cap (cost architecture 2026-08-25). None inherits the
    #: global web_search.max_uses — Class 1 verification keeps the full budget;
    #: cheaper tiers declare 1 in research.yaml.
    max_searches: Optional[int] = Field(default=None, gt=0)


class TierOverrides(_Strict):
    """Per-class model selection. A tier left unset inherits the top-level
    model/effort — so Class 1 stays on the flagship config unless explicitly
    moved, and cheaper tiers are an opt-in per class."""

    class_1: Optional[ModelTier] = None
    class_2: Optional[ModelTier] = None
    class_3: Optional[ModelTier] = None
    exit_review: Optional[ModelTier] = None


class ScreenStage(_Strict):
    """Two-stage research (2026-08-25): every full pass runs this cheap tier
    first. A no_position verdict or confidence below ``graduation_confidence``
    ends there — that report IS the record, and rejections get cheap. An
    actionable report graduates to a verification pass on the source's tier,
    with the screen draft included as data; the verification report is the one
    that proceeds to sizing. No trade ever sizes on a single unverified pass."""

    model: str
    effort: Effort
    max_searches: Optional[int] = Field(default=None, gt=0)
    #: Matches the sizing floor: below it nothing trades, so nothing to verify.
    graduation_confidence: int = Field(default=55, ge=0, le=100)

    def as_tier(self) -> ModelTier:
        return ModelTier(
            model=self.model, effort=self.effort, max_searches=self.max_searches
        )


class TriageConfig(_Strict):
    """The cheap yes/no gate in front of the full research pass."""

    model: str
    max_tokens: int = Field(gt=0)


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
    #: Model pinning (human ruling 2026-09-02): the closed set of model ids this
    #: system is allowed to call. Every configured model — top-level, triage,
    #: screen, tiers — must be a member, and floating aliases (-latest) are
    #: rejected outright. Editing this list IS the model-change ruling trigger:
    #: CLAUDE.md requires a dated ruling, an attribution partition, and a golden-
    #: set replay before any change ships. Empty = validation off (tests that
    #: build minimal configs), which the shipped yaml never is.
    pinned_models: tuple[str, ...] = ()
    #: Per-class model selection; unset tiers inherit the top-level model/effort.
    tiers: Optional[TierOverrides] = None
    #: Cost-estimate table, keyed by model id. A model missing from the table
    #: yields no estimate (None), never a guessed one.
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)
    #: The triage gate. None disables it (every signal goes straight to research).
    triage: Optional[TriageConfig] = None
    #: The two-stage screen. None disables it (single pass at the source tier).
    screen: Optional[ScreenStage] = None

    @model_validator(mode="after")
    def _models_are_pinned(self) -> "ResearchConfig":
        """Startup hard-fail on an unpinned or floating model id — a model that
        can drift silently invalidates every attribution partition behind it."""
        configured: dict[str, str] = {"model": self.model}
        if self.triage is not None:
            configured["triage.model"] = self.triage.model
        if self.screen is not None:
            configured["screen.model"] = self.screen.model
        if self.tiers is not None:
            for name in TIER_NAMES:
                override = getattr(self.tiers, name, None)
                if override is not None:
                    configured[f"tiers.{name}.model"] = override.model
        for where, model in configured.items():
            if model.endswith("-latest"):
                raise ValueError(
                    f"{where} = {model!r} is a floating alias; a model that can "
                    f"change under its own name breaks every attribution "
                    f"partition (ruling 2026-09-02)"
                )
            if self.pinned_models and model not in self.pinned_models:
                raise ValueError(
                    f"{where} = {model!r} is not in pinned_models; a model "
                    f"change is a dated ruling with an attribution partition "
                    f"and a golden-set replay, never a config drift "
                    f"(ruling 2026-09-02)"
                )
        return self

    def tier_for(self, name: str) -> ModelTier:
        """Resolve which model/effort a tier runs on. Unknown names are a bug."""
        if name == "screen":
            if self.screen is None:
                raise ValueError("screen tier requested but no screen is configured")
            return self.screen.as_tier()
        if name not in TIER_NAMES:
            raise ValueError(f"unknown research tier {name!r}; expected one of {TIER_NAMES}")
        override = getattr(self.tiers, name, None) if self.tiers else None
        if override is not None:
            return override
        return ModelTier(model=self.model, effort=self.effort)

    def estimate_cost_usd(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> Optional[Decimal]:
        """Estimated dollars for one call, or None when the model is unpriced.

        Cache tokens are priced per Anthropic's published multipliers: writes at
        1.25x the input rate, reads at 0.1x — so the estimate reflects the
        savings caching actually buys rather than overstating the bill.
        """
        pricing = self.pricing.get(model)
        if pricing is None:
            return None
        mtok = Decimal(1_000_000)
        cost = (
            Decimal(input_tokens) * pricing.input_per_mtok
            + Decimal(output_tokens) * pricing.output_per_mtok
            + Decimal(cache_write_tokens)
            * pricing.input_per_mtok
            * Decimal("1.25")
            + Decimal(cache_read_tokens) * pricing.input_per_mtok * Decimal("0.1")
        ) / mtok
        return cost.quantize(Decimal("0.000001"))

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ResearchConfig":
        path = path or default_research_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_research_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "research.yaml"
