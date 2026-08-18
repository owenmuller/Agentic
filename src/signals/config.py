"""Typed view of ``config/signals.yaml``.

As with the risk limits, the YAML is the source of truth and nothing here invents a
default for a source or a cadence. Adding a signal source or a watchlist account
requires human approval (CLAUDE.md § Requires Explicit Human Approval), so a source
that is not in the file is a source that does not get scanned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class PollWindow(_Strict):
    min: int
    max: int

    @property
    def seconds(self) -> int:
        """Poll at the fast end of the window. Class 1 decay is measured in minutes."""
        return self.min


class ClassificationRules(_Strict):
    required_before_research_pass: bool
    labels: tuple[str, ...]
    default_when_ambiguous: str
    when_in_doubt: str
    ambiguity_markers: tuple[str, ...] = ()


class SourceConfig(_Strict):
    id: str
    platforms: tuple[str, ...] = ()
    handle: Optional[str] = None
    treatment: Optional[str] = None
    copy_trade: bool = False
    classification: Optional[ClassificationRules] = None
    providers: tuple[str, ...] = ()
    provider: Optional[str] = None
    #: Named accounts/funds this source watches. Adding an entry requires human
    #: approval (CLAUDE.md); the fetchers read the list, they never extend it.
    watchlist: tuple[dict[str, str], ...] = ()
    #: Contact header for sources that require one (SEC EDGAR). Must name an email.
    user_agent: Optional[str] = None

    @property
    def classifies_posts(self) -> bool:
        return self.classification is not None


class ClassConfig(_Strict):
    name: str
    poll_interval_seconds: int | PollWindow
    market_hours_only: bool = False
    sources: tuple[SourceConfig, ...] = ()
    copy_trade: bool = False
    priced_in_analysis_required: bool = False

    @property
    def interval_seconds(self) -> int:
        interval = self.poll_interval_seconds
        return interval.seconds if isinstance(interval, PollWindow) else interval


class SignalsConfig(_Strict):
    version: int
    classes: dict[str, ClassConfig] = Field(default_factory=dict)

    def klass(self, key: str) -> ClassConfig:
        try:
            return self.classes[key]
        except KeyError as exc:
            raise KeyError(
                f"{key} is not configured in signals.yaml; adding a signal class or "
                f"source requires human approval"
            ) from exc

    def source(self, class_key: str, source_id: str) -> SourceConfig:
        for source in self.klass(class_key).sources:
            if source.id == source_id:
                return source
        raise KeyError(f"{source_id} is not a configured source in {class_key}")

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SignalsConfig":
        path = path or default_signals_path()
        with open(path, "r", encoding="utf-8") as handle:
            return cls.model_validate(yaml.safe_load(handle))


def default_signals_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "signals.yaml"
