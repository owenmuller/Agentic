"""Government contract awards: the production fetcher stack and the evening
one-shot (human ruling 2026-10-06).

``build_awards_fetcher`` assembles, from config, the thing the SourceRouter
calls for ``gov_contract_awards``: the contractor map, the live sizer (Finnhub
cap today + SEC revenue), the DoD digest fetcher (reader proxy, pending-file
handoff) and the FPDS civilian fetcher. ``run_evening`` is the 17:05 ET
one-shot: it reads today's digest while it is fresh, writes the free
measurement rows (measurement-only tiers and unmapped awardees) to the audit
log the same evening, and stashes the research-tier awards for the next
session's first poll — the research pass runs at the open inside the normal
caps, with the post's own publish time rendered, so the prompt sees an
~16-hour-old announcement, which is what it is.

Nothing here sizes, orders, or calls an LLM.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from audit import AuditLog
from audit.records import RejectedStage
from signals import (
    AwardeeSizer,
    Class1RealtimeScanner,
    CombinedAwardsFetcher,
    ContractorMap,
    DodDigestFetcher,
    FinnhubProfile,
    FpdsCivilianFetcher,
    RawItem,
    SecCompanyFacts,
    SignalQueue,
    SignalsConfig,
    SourceConfig,
)
from signals.contracts import (
    MEASUREMENT_CODE_BELOW_FLOOR,
    MEASUREMENT_CODE_MEASURE,
    SOURCE_ID,
    TIER_RESEARCH,
    RatioRules,
    load_edgar_names,
)
from decimal import Decimal

logger = logging.getLogger("orchestrator.contracts_feed")

USER_AGENT = "Agentic trading research (omuller@brasacap.com)"


def pending_path_for(data_dir: Path) -> Path:
    return data_dir / "contracts_pending.jsonl"


@dataclass
class AwardsStack:
    source: SourceConfig
    contractors: ContractorMap
    sizer: AwardeeSizer
    dod: DodDigestFetcher
    fpds: Optional[FpdsCivilianFetcher]
    fetcher: CombinedAwardsFetcher
    sec: SecCompanyFacts
    finnhub: Optional[FinnhubProfile]

    def close(self) -> None:
        self.fetcher.close()
        self.sec.close()
        if self.finnhub is not None:
            self.finnhub.close()


def build_awards_fetcher(
    config: SignalsConfig,
    *,
    seen: Iterable[str] = (),
    pending_path: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
    edgar_names: bool = True,
) -> Optional[AwardsStack]:
    """The stack, or None when the source is not configured."""
    source = None
    for klass in config.classes.values():
        for candidate in klass.sources:
            if candidate.id == SOURCE_ID:
                source = candidate
    if source is None:
        return None
    sec = SecCompanyFacts(user_agent=USER_AGENT)
    names = None
    if edgar_names:
        try:
            names = load_edgar_names(sec._client, USER_AGENT)  # noqa: SLF001 - one shared client
        except Exception as error:  # noqa: BLE001 - the map still works without the fallback
            logger.warning("EDGAR company_tickers.json unavailable (%s); exact-name fallback off", error)
    contractors = ContractorMap.load(edgar_names=names)
    finnhub = FinnhubProfile() if os.environ.get("FINNHUB_API_KEY") else None
    if finnhub is None:
        logger.warning("FINNHUB_API_KEY unset: award / market cap will be unknown and every resolved award is a measure-tier row")
    # company_tickers maps NAME -> (ticker, cik); invert for ticker -> cik.
    by_ticker = {t: c for (t, c) in (names or {}).values()} if names else {}
    sizer = AwardeeSizer(finnhub, sec, ciks=lambda ticker: by_ticker.get(ticker.upper()), clock=clock)
    cfg = source.contract_awards
    dod = DodDigestFetcher(
        contractors=contractors,
        sizer=sizer,
        clock=clock,
        seen=seen,
        pending_path=pending_path,
        user_agent=USER_AGENT,
    )
    fpds = None
    if cfg is None or cfg.fpds_enabled:
        fpds = FpdsCivilianFetcher(
            contractors=contractors,
            sizer=sizer,
            min_obligated=Decimal(str(cfg.fpds_min_obligated_usd)) if cfg is not None else Decimal("25000000"),
            clock=clock,
            seen=seen,
            user_agent=USER_AGENT,
        )
    return AwardsStack(source, contractors, sizer, dod, fpds, CombinedAwardsFetcher(dod, fpds), sec, finnhub)


@dataclass(frozen=True, slots=True)
class EveningReport:
    digests: int
    awards: int
    research_stashed: int
    measurement_rows: int
    no_instrument_rows: int
    already_recorded: int
    tally: dict

    def render(self) -> str:
        return (
            f"contract awards evening run: digests {self.digests}, awards {self.awards}; "
            f"research-tier stashed for the open {self.research_stashed}; measurement rows "
            f"written {self.measurement_rows}; unmapped/no-instrument rows {self.no_instrument_rows}; "
            f"already recorded {self.already_recorded}; tally {self.tally}"
        )


def run_evening(
    *,
    stack: AwardsStack,
    config: SignalsConfig,
    audit: AuditLog,
    id_factory: Callable[[], str],
    clock: Callable[[], datetime],
    recorded_external_ids: set[str],
) -> EveningReport:
    """Read today's digest(s), write the free rows now, stash the rest."""
    now = clock()
    rules = RatioRules.from_source(stack.source)
    refs = [r for r in stack.dod.list_digests() if now - r.published_at <= timedelta(hours=30)]
    items: list[RawItem] = []
    for ref in refs:
        try:
            items.extend(stack.dod.items_for(ref, rules))
        except Exception as error:  # noqa: BLE001 - one unreadable digest is a log line
            logger.warning("evening digest %s unreadable: %s", ref.title, error)
    research = [i for i in items if i.fields.get("size_tier") == TIER_RESEARCH]
    others = [i for i in items if i.fields.get("size_tier") != TIER_RESEARCH]
    stashed = stack.dod.stash_pending(research) if research else 0

    # Build Signals through the real Class 1 scanner so ids and metadata are
    # byte-identical to what the session would have produced.
    klass = config.klass("class_1")
    queue = SignalQueue()
    pending = list(others)

    def feed(source: SourceConfig):
        if source.id != SOURCE_ID:
            return []
        out, pending[:] = list(pending), []
        return out

    scanner = Class1RealtimeScanner(klass, feed, queue, clock=clock)
    signals = list(scanner.poll(force=True)) or queue.drain()
    measurement = no_instrument = already = 0
    for signal in signals:
        if signal.external_id in recorded_external_ids:
            already += 1
            continue
        tier = signal.metadata.get("size_tier", "")
        if not signal.metadata.get("tickers"):
            code = "no_instrument"
            reason = f"award on an unmapped or non-public awardee ({signal.metadata.get('parent_how', 'unresolved')}); recorded for the funnel, not researched"
            no_instrument += 1
        elif tier == "below_floor":
            code = MEASUREMENT_CODE_BELOW_FLOOR
            reason = "award below the measure floor of market cap (control row, ruling 2026-10-06)"
            measurement += 1
        else:
            code = MEASUREMENT_CODE_MEASURE
            reason = "award in the measurement tier (modification / multiple-award / 0.2-1% of cap); graded, not researched"
            measurement += 1
        audit.record_stage_rejection(id_factory(), RejectedStage.PRE_FILTER, code, reason, signal)
        recorded_external_ids.add(signal.external_id or "")
    return EveningReport(len(refs), len(items), stashed, measurement, no_instrument, already, dict(stack.dod.last_tally))
