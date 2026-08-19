"""Static sector mapping for the concentration guard.

Deterministic by construction: a human-editable table in ``config/sectors.yaml``
maps sectors to tickers. No LLM, no network, no inference — the same rules as the
rest of the gate. An unmapped ticker does NOT silently join a shared bucket: it
becomes its own singleton sector (``unmapped:XYZ``), so a name nobody classified
is capped as if it were its own sector rather than diluted into someone else's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional

import yaml


class SectorMapError(ValueError):
    """The sectors file is malformed or self-contradictory."""


class SectorMap:
    """Symbol -> sector, from the static config table."""

    def __init__(self, sector_of_symbol: Mapping[str, str]) -> None:
        self._by_symbol = {
            symbol.upper(): sector for symbol, sector in sector_of_symbol.items()
        }

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SectorMap":
        path = path or default_sectors_path()
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        sectors = raw.get("sectors")
        if not isinstance(sectors, dict):
            raise SectorMapError(f"{path} must contain a 'sectors' mapping")
        by_symbol: dict[str, str] = {}
        for sector, tickers in sectors.items():
            if not isinstance(tickers, list):
                raise SectorMapError(f"sector {sector!r} must list its tickers")
            for ticker in tickers:
                symbol = str(ticker).upper()
                existing = by_symbol.get(symbol)
                if existing is not None and existing != sector:
                    raise SectorMapError(
                        f"{symbol} is mapped to both {existing!r} and {sector!r}; "
                        f"a ticker belongs to exactly one sector"
                    )
                by_symbol[symbol] = str(sector)
        return cls(by_symbol)

    def sector_of(self, symbol: str) -> str:
        """The symbol's sector, or its own singleton bucket when unmapped."""
        upper = symbol.upper()
        mapped = self._by_symbol.get(upper)
        if mapped is not None:
            return mapped
        return f"unmapped:{upper}"


def default_sectors_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "sectors.yaml"
