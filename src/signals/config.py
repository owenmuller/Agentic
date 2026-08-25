"""Typed view of ``config/signals.yaml``.

As with the risk limits, the YAML is the source of truth and nothing here invents a
default for a source or a cadence. Adding a signal source or a watchlist account
requires human approval (CLAUDE.md § Requires Explicit Human Approval), so a source
that is not in the file is a source that does not get scanned.
"""

from __future__ import annotations

from decimal import Decimal
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


class PrefilterConfig(_Strict):
    """Deterministic research pre-filter thresholds (cost pass, 2026-08-19).

    Free filters in front of expensive LLM judgment. Every field is optional —
    a rule left unset simply does not run. Skips are recorded as ``pre_filter``
    stage rejections, same auditable pattern as the trump_posts theme filter.
    """

    #: Class 2: skip disclosures whose amount-range MAXIMUM is below this many
    #: dollars. Strictly below — a "$1,001 - $15,000" range with the threshold at
    #: 15000 still goes to research.
    min_amount_max: Optional[int] = Field(default=None, gt=0)
    #: Class 2: skip disclosures observed more than this many days after the
    #: trade (ReportDate - TransactionDate). Strictly above.
    max_lag_days: Optional[int] = Field(default=None, gt=0)
    #: Class 2: skip sale disclosures in names the system does not hold — someone
    #: else's exit from a position we never entered is not an entry thesis.
    skip_unheld_sales: bool = False
    #: Class 3: skip filings whose period-of-report is older than this many days.
    #: The research layer has already proven it rejects these; stop paying it to.
    max_period_age_days: Optional[int] = Field(default=None, gt=0)


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
    #: Source type. "mirror" marks an unofficial account that republishes another
    #: source's posts; its signals are attributed to ``mirror_of`` for research and
    #: attribution, while the audit record preserves which mirror delivered them.
    type: Optional[str] = None
    #: For mirror sources: the source id the content actually belongs to.
    mirror_of: Optional[str] = None
    #: For mirror sources: a regex the delivered content MUST match to be treated
    #: as the principal's words. Mirror accounts post their own commentary between
    #: relays (verified live 2026-08-20: @TrumpDailyPosts' recent output was 100%
    #: own commentary), and headerless content mislabeled as the principal is a
    #: manipulation channel. Content without the marker is logged to the mirror's
    #: credibility record and never emitted as a principal signal.
    required_marker: Optional[str] = None
    #: For mirror sources: warn in run.log after this many trading days without a
    #: delivery — silence might mean the principal is quiet, or the bot died, and a
    #: human should check which.
    silence_warning_trading_days: Optional[int] = None
    #: When set, this source's signals are researched ONLY if they name an
    #: instrument (per the scanner's ticker extraction) or match one of these theme
    #: stems. Everything else is recorded as a pre_filter stage rejection instead of
    #: spending a research pass. Human-approved 2026-08-18 for trump_posts.
    research_prefilter_themes: tuple[str, ...] = ()
    #: Deterministic pre-filter thresholds for this source (class 2/3 rules).
    prefilter: Optional[PrefilterConfig] = None
    #: When true, a forward_call from this source is researched ONLY if the
    #: scanner extracted a ticker (hardening ruling 2026-08-25, nolimitgains:
    #: his genuine calls always name instruments; instrument-less commentary
    #: cannot be traded regardless of research verdict). Everything else is a
    #: no_instrument stage rejection. Scoped per source — Trump theme posts
    #: legitimately trade without tickers via sector effects.
    require_instrument: bool = False
    #: Bare-link rule (2026-08-25): a theme-matched post with no ticker whose
    #: content MINUS URLs is shorter than this is pre_filtered with code
    #: bare_link — a headline with a link is not a thesis, and researching it
    #: is paying a frontier model to read a t.co URL. None = rule off.
    bare_link_min_chars: Optional[int] = None
    #: For pay-per-use feeds (X): warn once the day's posts read passes this. A
    #: since_id regression re-reads the same posts every poll, and the bug should
    #: show in run.log before it shows on the bill.
    daily_read_warning: Optional[int] = None
    #: What this feed costs per month, in dollars. Attribution prorates it against the
    #: class's P&L: a signal class must out-earn its own feed, and the weekly report
    #: is where that verdict lives. Zero for free feeds and for sources not yet built.
    monthly_cost: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))

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

    def monthly_feed_costs(self) -> dict[str, Decimal]:
        """Total feed cost per class key ("class_1"...), from the sources' own fields."""
        return {
            key: sum((source.monthly_cost for source in klass.sources), Decimal("0"))
            for key, klass in self.classes.items()
        }

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
