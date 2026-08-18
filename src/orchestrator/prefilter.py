"""The deterministic research pre-filter — approved by the human 2026-08-18.

The problem it solves: Trump can out-post the research budget by himself (40+ Truths
in a day against 40 passes/day for the whole system), and most of those posts are not
about markets. The approved remedy is not a bigger budget but a cheaper gate: a
``trump_posts`` signal is only worth a research pass when it names an instrument (a
cashtag or a context-gated ticker, per the scanner's own extraction) or touches one
of the policy themes configured in ``signals.yaml`` — the things CLAUDE.md already
frames this source as being about.

Placement is deliberate: the filter runs at research dispatch in the orchestrator's
loop, before a budget pass is spent. Scanners stay dumb emitters — every Truth still
arrives, is deduplicated, and is written down. A filtered post writes a
``stage_rejection`` record with code ``pre_filter``, so the audit trail shows every
Truth that arrived and exactly why it was not researched. If attribution ever
suggests the filter is eating alpha, the skipped posts are all there to read.

Deterministic by construction: word-stem matching against a configured list, plus the
scanner's own deterministic ticker extraction. No LLM decides what the LLM gets to
see — that would be spending the budget to guard the budget.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from signals import Signal
from signals.config import SignalsConfig

logger = logging.getLogger("orchestrator.prefilter")


class ResearchPreFilter:
    """Decides which signals earn a research pass. Built from ``signals.yaml``."""

    def __init__(self, themes_by_source: dict[str, tuple[str, ...]]) -> None:
        self._patterns: dict[str, re.Pattern] = {}
        for source_id, themes in themes_by_source.items():
            if not themes:
                continue
            # Stem-prefix matching on word boundaries: "tariff" covers tariffs and
            # tariffed; "rate" covers rates. Erring toward matching sends a post to
            # research (bounded by the budget); erring away silently drops it.
            stems = "|".join(re.escape(theme.lower()) for theme in themes)
            self._patterns[source_id] = re.compile(
                r"\b(?:" + stems + r")\w*", re.IGNORECASE
            )

    @classmethod
    def from_config(cls, config: SignalsConfig) -> "ResearchPreFilter":
        themes: dict[str, tuple[str, ...]] = {}
        for klass in config.classes.values():
            for source in klass.sources:
                if source.research_prefilter_themes:
                    themes[source.id] = source.research_prefilter_themes
        return cls(themes)

    @property
    def filtered_sources(self) -> tuple[str, ...]:
        return tuple(sorted(self._patterns))

    def skip_reason(self, signal: Signal) -> Optional[str]:
        """Why this signal should NOT be researched, or None to research it.

        Keyed on ``signal.source_id`` — the attributed source — so content delivered
        by a mirror is filtered exactly like content from the principal would be.
        Sources with no configured themes are never filtered.
        """
        pattern = self._patterns.get(signal.source_id)
        if pattern is None:
            return None

        tickers = (signal.metadata.get("tickers") or "").strip()
        if tickers:
            return None  # names an instrument: always worth the pass

        matched = pattern.search(signal.content)
        if matched:
            return None  # touches a configured policy theme

        return (
            f"no tickers extracted and no configured policy theme matched; "
            f"{signal.source_id} posts are researched only when they name an "
            f"instrument or touch a theme from signals.yaml "
            f"(research_prefilter_themes)"
        )
