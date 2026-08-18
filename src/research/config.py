"""Typed view of ``config/research.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WebSearchConfig(_Strict):
    enabled: bool
    max_uses: int


class ResearchConfig(_Strict):
    version: int
    model: str
    max_tokens: int
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    web_search: WebSearchConfig
    max_search_continuations: int

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ResearchConfig":
        path = path or default_research_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_research_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "research.yaml"
