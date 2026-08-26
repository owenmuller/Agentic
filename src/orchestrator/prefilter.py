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
from datetime import date, datetime, timezone
from typing import Collection, Optional

from signals import Signal
from signals.config import PrefilterConfig, SignalsConfig

logger = logging.getLogger("orchestrator.prefilter")

_NUMBER = re.compile(r"\d[\d,]*")


def _amount_range_max(rendered: str) -> Optional[int]:
    """The top of a disclosure amount range like ``"$1,001 - $15,000"``.

    Returns None when nothing numeric can be read — the caller fails OPEN
    (research), because a skipped signal we could not price is a silent drop.
    """
    figures = [int(match.replace(",", "")) for match in _NUMBER.findall(rendered)]
    return max(figures) if figures else None


def _parse_int(raw: str) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_date(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(raw.strip())
    except (TypeError, ValueError, AttributeError):
        return None


_URLS = re.compile(r"https?://\S+", re.IGNORECASE)


class ResearchPreFilter:
    """Decides which signals earn a research pass. Built from ``signals.yaml``.

    Three rule families, all deterministic, all free:

    - Theme/ticker gating (trump_posts): a post earns the pass by naming an
      instrument or touching a configured policy theme.
    - Disclosure rules (class 2): amount too small, lag too long, or a sale in a
      name the system does not hold.
    - Staleness (class 3): a 13F whose period-of-report is older than the cutoff.

    Every rule fails OPEN: a field the rule cannot read sends the signal to
    research (bounded by the budget) rather than silently dropping it. Every skip
    is written as a ``pre_filter`` stage rejection with a readable reason.
    """

    def __init__(
        self,
        themes_by_source: dict[str, tuple[str, ...]],
        rules_by_source: Optional[dict[str, PrefilterConfig]] = None,
        instrument_required: Collection[str] = (),
        bare_link_min_chars: Optional[dict[str, int]] = None,
    ) -> None:
        self._patterns: dict[str, re.Pattern] = {}
        self._rules: dict[str, PrefilterConfig] = dict(rules_by_source or {})
        self._instrument_required = frozenset(instrument_required)
        self._bare_link_min_chars = dict(bare_link_min_chars or {})
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
        rules: dict[str, PrefilterConfig] = {}
        for klass in config.classes.values():
            for source in klass.sources:
                if source.research_prefilter_themes:
                    themes[source.id] = source.research_prefilter_themes
                if source.prefilter is not None:
                    rules[source.id] = source.prefilter
        instrument_required = tuple(
            source.id
            for klass in config.classes.values()
            for source in klass.sources
            if source.require_instrument
        )
        bare_link = {
            source.id: source.bare_link_min_chars
            for klass in config.classes.values()
            for source in klass.sources
            if source.bare_link_min_chars is not None
        }
        return cls(
            themes,
            rules,
            instrument_required=instrument_required,
            bare_link_min_chars=bare_link,
        )

    @property
    def filtered_sources(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._patterns) | set(self._rules)))

    def missing_instrument(self, signal: Signal) -> Optional[str]:
        """The no_instrument rule (2026-08-25), for sources configured with
        ``require_instrument``: a forward_call that names no ticker cannot be
        traded regardless of research verdict, so it never spends triage or a
        pass. Deterministic — the scanner's extraction already ran."""
        if signal.source_id not in self._instrument_required:
            return None
        if (signal.metadata.get("tickers") or "").strip():
            return None
        return (
            f"{signal.source_id} forward_call names no instrument; a call that "
            f"cannot be traded is commentary, and commentary is not researched"
        )

    def bare_link(self, signal: Signal) -> Optional[str]:
        """The bare-link rule (2026-08-25), for sources configured with
        ``bare_link_min_chars``: a THEME-MATCHED post with no ticker whose
        content minus URLs is under the threshold is a headline with a link,
        not a thesis. Posts that fail the theme gate entirely are left to the
        theme rule — this rule only names the reason more precisely for the
        ones that would otherwise have earned a pass."""
        threshold = self._bare_link_min_chars.get(signal.source_id)
        if threshold is None:
            return None
        if (signal.metadata.get("tickers") or "").strip():
            return None
        pattern = self._patterns.get(signal.source_id)
        if pattern is not None and not pattern.search(signal.content):
            return None  # unthemed: the theme rule owns that rejection
        remaining = _URLS.sub("", signal.content).strip()
        if len(remaining) >= threshold:
            return None
        return (
            f"theme-matched but {len(remaining)} chars once URLs are removed "
            f"(threshold {threshold}) and no ticker named — a headline with a "
            f"link is not a thesis"
        )

    def skip_reason(
        self,
        signal: Signal,
        *,
        held: Collection[str] = (),
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """Why this signal should NOT be researched, or None to research it.

        Keyed on ``signal.source_id`` — the attributed source — so content delivered
        by a mirror is filtered exactly like content from the principal would be.
        ``held`` is the set of currently held symbols (for the unheld-sale rule);
        ``now`` anchors the staleness rule and defaults to the wall clock.
        """
        verdict = self.skip_verdict(signal, held=held, now=now)
        return verdict[0] if verdict is not None else None

    def skip_verdict(
        self,
        signal: Signal,
        *,
        held: Collection[str] = (),
        now: Optional[datetime] = None,
    ) -> Optional[tuple[str, str]]:
        """``(reason, rule)`` or None — like ``skip_reason``, but naming which
        rule family fired, so the loop can give a staleness kill of a
        previously capped signal its own code (aged_out_capped, 2026-08-26)."""
        theme_reason = self._theme_reason(signal)
        if theme_reason is not None:
            return theme_reason, "theme"

        rules = self._rules.get(signal.source_id)
        if rules is None:
            return None
        return self._rule_verdict(signal, rules, held, now)

    # -- rule families -------------------------------------------------------------

    def _theme_reason(self, signal: Signal) -> Optional[str]:
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

    def _rule_verdict(
        self,
        signal: Signal,
        rules: PrefilterConfig,
        held: Collection[str],
        now: Optional[datetime],
    ) -> Optional[tuple[str, str]]:
        meta = signal.metadata

        if rules.min_amount_max is not None:
            amount_max = _amount_range_max(meta.get("amount_range", ""))
            if amount_max is not None and amount_max < rules.min_amount_max:
                return (
                    f"amount range tops out at ${amount_max:,}, below the "
                    f"${rules.min_amount_max:,} floor (signals.yaml "
                    f"prefilter.min_amount_max) — too small to signal conviction"
                ), "amount"

        if rules.max_lag_days is not None:
            lag = _parse_int(meta.get("disclosure_lag_days", ""))
            if lag is not None and lag > rules.max_lag_days:
                return (
                    f"disclosure lag of {lag} days exceeds the "
                    f"{rules.max_lag_days}-day cutoff (signals.yaml "
                    f"prefilter.max_lag_days) — the move is long priced in"
                ), "lag"

        if rules.max_report_age_days is not None:
            report_date = _parse_date(meta.get("report_date", ""))
            if report_date is not None:
                moment = now or datetime.now(timezone.utc)
                age_days = (moment.date() - report_date).days
                if age_days > rules.max_report_age_days:
                    return (
                        f"reported {report_date.isoformat()}, {age_days} days "
                        f"ago, beyond the {rules.max_report_age_days}-day "
                        f"report-staleness cutoff (signals.yaml "
                        f"prefilter.max_report_age_days) — a backfill row, not "
                        f"news; the daily cap must not be spent on it"
                    ), "report_staleness"

        if rules.skip_unheld_sales:
            transaction = meta.get("transaction", "").lower()
            ticker = meta.get("ticker", "").upper().strip()
            held_upper = {symbol.upper() for symbol in held}
            if "sale" in transaction and ticker and ticker not in held_upper:
                return (
                    f"sale disclosure in {ticker}, which the system does not "
                    f"hold (signals.yaml prefilter.skip_unheld_sales) — someone "
                    f"else's exit from a position we never entered is not an "
                    f"entry thesis"
                ), "unheld_sale"

        if rules.max_period_age_days is not None:
            period = _parse_date(meta.get("period_of_report", ""))
            if period is not None:
                moment = now or datetime.now(timezone.utc)
                age_days = (moment.date() - period).days
                if age_days > rules.max_period_age_days:
                    return (
                        f"period of report {period.isoformat()} is {age_days} "
                        f"days old, beyond the {rules.max_period_age_days}-day "
                        f"staleness cutoff (signals.yaml "
                        f"prefilter.max_period_age_days)"
                    ), "period_staleness"

        return None
