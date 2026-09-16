"""Theme -> ETF expression for no-ticker Class 1 posts (human ruling 2026-09-15).

A policy post that names no instrument used to be researched on its theme alone
and traded only if the model named a ticker. Now the system PROPOSES the liquid
ETF SHORTLIST configured for the theme (signals.yaml ``theme_etf_map``, confirmed
2026-09-16), stamps the proposal on the signal's metadata, tells the model in the
prompt, and tags the decision ``theme_etf`` when the model expressed the thesis
through one of the shortlisted ETFs. The model names the instrument whose
holdings actually bear the theme's exposure, or declines: the shortlist is a
proposal, never a directive.

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
    etfs: tuple[str, ...]


class ThemeEtfMap:
    """Per-source theme patterns and their ETFs, compiled once from config."""

    def __init__(
        self, by_source: Mapping[str, Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]]
    ) -> None:
        self._compiled: dict[str, list[tuple[str, tuple[str, ...], re.Pattern[str]]]] = {}
        for source_id, themes in by_source.items():
            rows = []
            for theme, (etfs, stems) in themes.items():
                if not stems or not etfs:
                    continue
                pattern = re.compile(
                    r"\b(?:" + "|".join(re.escape(stem.lower()) for stem in stems) + r")",
                    re.IGNORECASE,
                )
                rows.append((theme, tuple(etf.upper() for etf in etfs), pattern))
            if rows:
                self._compiled[source_id] = rows

    @classmethod
    def from_config(cls, signals_config) -> "ThemeEtfMap":
        by_source: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {}
        sources = [source for klass in signals_config.classes.values() for source in klass.sources]
        for source in sources:
            if source.theme_etf_map:
                by_source[source.id] = {
                    theme: (tuple(row.etfs), tuple(row.stems))
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
        hits = [(theme, etfs) for theme, etfs, pattern in rows if pattern.search(signal.content)]
        if len(hits) != 1:
            return None  # none, or ambiguous: no mapping
        theme, etfs = hits[0]
        return ThemeMatch(theme=theme, etfs=etfs)

    def apply(self, signal: Signal) -> Signal:
        """The signal with the proposal stamped on its metadata, or unchanged."""
        matched = self.match(signal)
        if matched is None:
            return signal
        return replace(
            signal,
            metadata={
                **signal.metadata,
                THEME_KEY: matched.theme,
                THEME_ETF_KEY: ",".join(matched.etfs),  # the shortlist, comma-joined
            },
        )


def shortlist_of(signal: Signal) -> tuple[str, ...]:
    """The stamped ETF shortlist, or () when no proposal was made."""
    raw = signal.metadata.get(THEME_ETF_KEY) or ""
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())
