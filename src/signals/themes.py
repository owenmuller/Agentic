"""Theme -> ETF expression for no-ticker Class 1 posts (human ruling 2026-09-15).

A policy post that names no instrument used to be researched on its theme alone
and traded only if the model named a ticker. Now the system PROPOSES the liquid
ETF configured for the theme (signals.yaml ``theme_etf_map``), stamps the
proposal on the signal's metadata, tells the model in the prompt, and tags the
decision ``theme_etf`` when the model expressed the thesis through it. The model
may decline: the mapping is a proposal, never a directive.

Deterministic and offline. A post matching two themes is ambiguous and gets no
mapping (Constraint #6: the fewer trades). A post with an extracted ticker is
never mapped — it already names its instrument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from signals.records import Signal

THEME_KEY = "theme"
THEME_ETF_KEY = "theme_etf"


@dataclass(frozen=True, slots=True)
class ThemeMatch:
    theme: str
    etf: str


class ThemeEtfMap:
    """Per-source theme patterns and their ETFs, compiled once from config."""

    def __init__(self, by_source: Mapping[str, Mapping[str, tuple[str, tuple[str, ...]]]]) -> None:
        self._compiled: dict[str, list[tuple[str, str, re.Pattern[str]]]] = {}
        for source_id, themes in by_source.items():
            rows = []
            for theme, (etf, stems) in themes.items():
                if not stems:
                    continue
                pattern = re.compile(
                    r"\b(?:" + "|".join(re.escape(stem.lower()) for stem in stems) + r")",
                    re.IGNORECASE,
                )
                rows.append((theme, etf.upper(), pattern))
            if rows:
                self._compiled[source_id] = rows

    @classmethod
    def from_config(cls, signals_config) -> "ThemeEtfMap":
        by_source: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {}
        sources = [source for klass in signals_config.classes.values() for source in klass.sources]
        for source in sources:
            if source.theme_etf_map:
                by_source[source.id] = {
                    theme: (row.etf, tuple(row.stems))
                    for theme, row in source.theme_etf_map.items()
                }
        # Mirror sources attribute their signals to the principal, so the
        # principal's map already applies; a mirror with no map of its own also
        # answers under its own id in case a signal is ever keyed to it.
        for source in sources:
            mirror_of = getattr(source, "mirror_of", None)
            if mirror_of and source.id not in by_source and mirror_of in by_source:
                by_source[source.id] = by_source[mirror_of]
        return cls(by_source)

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(self._compiled)

    def match(self, signal: Signal) -> Optional[ThemeMatch]:
        """The one theme this no-ticker post touches, or None."""
        rows = self._compiled.get(signal.source_id)
        if not rows:
            return None
        if (signal.metadata.get("tickers") or "").strip():
            return None  # names its own instrument
        hits = [(theme, etf) for theme, etf, pattern in rows if pattern.search(signal.content)]
        if len(hits) != 1:
            return None  # none, or ambiguous: no mapping
        theme, etf = hits[0]
        return ThemeMatch(theme=theme, etf=etf)

    def apply(self, signal: Signal) -> Signal:
        """The signal with the proposal stamped on its metadata, or unchanged."""
        matched = self.match(signal)
        if matched is None:
            return signal
        return replace(
            signal,
            metadata={**signal.metadata, THEME_KEY: matched.theme, THEME_ETF_KEY: matched.etf},
        )
