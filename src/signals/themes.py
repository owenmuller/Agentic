"""Theme -> ETF expression for no-ticker Class 1 posts (human ruling 2026-09-15).

A policy post that names no instrument used to be researched on its theme alone
and traded only if the model named a ticker. Now the system PROPOSES the liquid
ETF SHORTLIST configured for the theme (signals.yaml ``theme_etf_map``, confirmed
2026-09-16), stamps the proposal on the signal's metadata, tells the model in the
prompt, and tags the decision ``theme_etf`` when the model expressed the thesis
through one of the shortlisted ETFs. The model names the instrument whose
holdings actually bear the theme's exposure, or declines: the shortlist is a
proposal, never a directive.

Deterministic and offline. A post matching SEVERAL themes gets the UNION of
their shortlists, deduplicated, capped at ``MAX_CANDIDATES`` (ruling 2026-09-16;
until then a dual-theme post got no mapping). If the union would exceed the cap,
only the two highest-weighted matched themes' lists are kept (weight from
config, ties by config order), then truncated. Same rule for the model: pick the
one instrument whose holdings bear the post's actual exposure, or decline. A
post with an extracted ticker is never mapped — it already names its instrument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Mapping, Optional

from signals.records import Signal

THEME_KEY = "theme"
THEME_ETF_KEY = "theme_etf"
#: The most ETFs one proposal may carry (ruling 2026-09-16).
MAX_CANDIDATES = 6
#: When the union overflows the cap, this many highest-weighted themes survive.
FALLBACK_THEMES = 2


@dataclass(frozen=True, slots=True)
class ThemeMatch:
    #: The matched theme names joined with "+" in config order ("tariffs+china_trade").
    theme: str
    etfs: tuple[str, ...]
    themes: tuple[str, ...] = ()


class ThemeEtfMap:
    """Per-source theme patterns and their ETFs, compiled once from config."""

    def __init__(self, by_source: Mapping[str, Mapping[str, tuple]]) -> None:
        """``by_source[source_id][theme] = (etfs, stems)`` or ``(etfs, stems, weight)``."""
        self._compiled: dict[str, list[tuple[str, tuple[str, ...], re.Pattern[str], int]]] = {}
        for source_id, themes in by_source.items():
            rows = []
            for theme, spec in themes.items():
                etfs, stems = spec[0], spec[1]
                weight = int(spec[2]) if len(spec) > 2 else 0
                if not stems or not etfs:
                    continue
                pattern = re.compile(
                    r"\b(?:" + "|".join(re.escape(stem.lower()) for stem in stems) + r")",
                    re.IGNORECASE,
                )
                rows.append((theme, tuple(etf.upper() for etf in etfs), pattern, weight))
            if rows:
                self._compiled[source_id] = rows

    @classmethod
    def from_config(cls, signals_config) -> "ThemeEtfMap":
        by_source: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {}
        sources = [source for klass in signals_config.classes.values() for source in klass.sources]
        for source in sources:
            if source.theme_etf_map:
                by_source[source.id] = {
                    theme: (tuple(row.etfs), tuple(row.stems), int(row.weight))
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
        hits = [
            (theme, etfs, weight)
            for theme, etfs, pattern, weight in rows
            if pattern.search(signal.content)
        ]
        if not hits:
            return None
        # Union in config order, deduplicated (ruling 2026-09-16).
        union = _dedup(etf for _, etfs, _ in hits for etf in etfs)
        if len(union) > MAX_CANDIDATES:
            # Too wide a proposal: keep the two highest-weighted matched themes
            # (ties by config order — stable sort keeps it), then cap.
            kept = sorted(hits, key=lambda hit: -hit[2])[:FALLBACK_THEMES]
            kept = [hit for hit in hits if hit in kept]  # back to config order
            union = _dedup(etf for _, etfs, _ in kept for etf in etfs)[:MAX_CANDIDATES]
            hits = kept
        names = tuple(theme for theme, _, _ in hits)
        return ThemeMatch(theme="+".join(names), etfs=tuple(union), themes=names)

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


def _dedup(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def shortlist_of(signal: Signal) -> tuple[str, ...]:
    """The stamped ETF shortlist, or () when no proposal was made."""
    raw = signal.metadata.get(THEME_ETF_KEY) or ""
    return tuple(part.strip().upper() for part in raw.split(",") if part.strip())
