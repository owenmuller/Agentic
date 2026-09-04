"""Overreaction-fade hypothesis, MEASUREMENT HALF (human ruling 2026-09-03).

No LLM, no prompt, no trade, no API spend: a deterministic screen over completed
SIP daily bars that writes measurement-only rows for the forward-return engine
to grade. The question it exists to answer, for free, before any prompt is
written: do sharp single-session drops on OUR signal universe revert at all?

Universe, two tiers (both logged, sliceable in the report):
  core   held judged positions + names the system actually researched in the
         window (any verdict — traded, declined, gate-refused, triaged)
  broad  every other name carrying a purchase-side active signal in the
         convergence registry (raw Form 4 singles included) — the control

Event: on a completed session, close-to-close return <= -6% (the lowest flag)
AND session volume >= ``volume_ratio_min`` x the prior ``adv_days`` average
share volume. The ruled threshold is 7%; 6% and 8% ride the same row as flags
so X is tuned from the data, not re-run. SPY's same-day return and the mapped
sector are stamped so market/sector days split from idiosyncratic days
deterministically.

Rows are ``StageRejectionRecord``s at PRE_FILTER with code
``overreaction_candidate`` and source ``overreaction_screen`` — the same path
the Form 4 sell-cluster measurement rows ride. They are OUTSIDE convergence (the
registry ignores measurement rows) and never seal the underlying name: their
external id is ``SYMBOL:SESSION`` under their own source. This module is
offline by topology; bars arrive as a callable from ``execution``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

from audit.records import RejectedStage, StageRejectionRecord
from signals import SignalClass
from signals.classification import is_us_listed_symbol
from signals.records import Priority, Signal, signal_id_for

from orchestrator.config import OverreactionScreenConfig

logger = logging.getLogger("orchestrator.overreaction")

SOURCE_ID = "overreaction_screen"
CODE = "overreaction_candidate"
MARKET_TZ = ZoneInfo("America/New_York")
SESSION_CLOSE = time(16, 0)
#: Detection may run from this ET time on the session's own day; before it the
#: day's bar is not complete and "last completed session" is the prior one.
DETECTION_READY = time(16, 15)

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    tier: str  # "core" | "broad"
    held: bool


def build_universe(
    held: Iterable[str],
    researched: Iterable[str],
    active_purchase: Iterable[str],
) -> dict[str, UniverseMember]:
    """Core = held ∪ researched; broad = purchase-side active names not in core.
    Symbols the venue cannot serve (foreign home-market shapes) never enter."""
    held_set = {s.upper() for s in held if is_us_listed_symbol(s.upper())}
    core = held_set | {s.upper() for s in researched if is_us_listed_symbol(s.upper())}
    out: dict[str, UniverseMember] = {
        symbol: UniverseMember(symbol, "core", symbol in held_set) for symbol in core
    }
    for symbol in active_purchase:
        symbol = symbol.upper()
        if symbol not in out and is_us_listed_symbol(symbol):
            out[symbol] = UniverseMember(symbol, "broad", False)
    return out


@dataclass(frozen=True, slots=True)
class Event:
    symbol: str
    session: date
    close: Decimal
    previous_close: Decimal
    drop_pct: Decimal  # negative, percent
    volume_ratio: Decimal
    spy_return_pct: Optional[Decimal]
    market_day: Optional[bool]
    sector: str
    tier: str
    held: bool
    flags: tuple[int, ...]  # the thresholds (percent) this drop met, ascending

    @property
    def external_id(self) -> str:
        return f"{self.symbol}:{self.session.isoformat()}"


def rows_of(bars: Iterable[dict[str, Any]]) -> list[tuple[date, Decimal, Decimal]]:
    """(session, close, volume) oldest-first from raw bars; junk skipped."""
    out: list[tuple[date, Decimal, Decimal]] = []
    for bar in bars:
        try:
            session = date.fromisoformat(str(bar.get("t", ""))[:10])
            close = Decimal(str(bar.get("c")))
            volume = Decimal(str(bar.get("v")))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if close > ZERO and volume >= ZERO:
            out.append((session, close, volume))
    out.sort(key=lambda r: r[0])
    return out


def session_return_pct(
    rows: Sequence[tuple[date, Decimal, Decimal]], session: date
) -> Optional[Decimal]:
    """Close-to-close return of ``session`` over the prior row, percent."""
    for index, (day, close, _volume) in enumerate(rows):
        if day == session:
            if index == 0 or rows[index - 1][1] <= ZERO:
                return None
            return (close / rows[index - 1][1] - 1) * HUNDRED
    return None


def detect(
    member: UniverseMember,
    rows: Sequence[tuple[date, Decimal, Decimal]],
    session: date,
    config: OverreactionScreenConfig,
    spy_return_pct: Optional[Decimal],
    sector: str,
) -> Optional[Event]:
    """The event on ``session`` for one name, or None. Deterministic; fails
    closed on thin history (fewer than ``adv_days`` prior bars = unmeasurable)."""
    index = next((i for i, row in enumerate(rows) if row[0] == session), None)
    if index is None or index < config.adv_days:
        return None
    _day, close, volume = rows[index]
    previous_close = rows[index - 1][1]
    if previous_close <= ZERO:
        return None
    drop_pct = (close / previous_close - 1) * HUNDRED
    thresholds = sorted({config.drop_threshold, *config.flag_thresholds})
    lowest = thresholds[0]
    if drop_pct > -lowest * HUNDRED:
        return None
    prior_volumes = [row[2] for row in rows[index - config.adv_days : index]]
    average_volume = sum(prior_volumes, ZERO) / Decimal(len(prior_volumes))
    if average_volume <= ZERO:
        return None
    volume_ratio = volume / average_volume
    if volume_ratio < config.volume_ratio_min:
        return None
    flags = tuple(
        int((threshold * HUNDRED).quantize(Decimal("1")))
        for threshold in thresholds
        if drop_pct <= -threshold * HUNDRED
    )
    market_day = (
        None
        if spy_return_pct is None
        else spy_return_pct <= -config.market_day_threshold * HUNDRED
    )
    return Event(
        symbol=member.symbol,
        session=session,
        close=close,
        previous_close=previous_close,
        drop_pct=drop_pct.quantize(Decimal("0.01")),
        volume_ratio=volume_ratio.quantize(Decimal("0.01")),
        spy_return_pct=(
            spy_return_pct.quantize(Decimal("0.01")) if spy_return_pct is not None else None
        ),
        market_day=market_day,
        sector=sector,
        tier=member.tier,
        held=member.held,
        flags=flags,
    )


def render_content(event: Event) -> str:
    """Labelled lines: the forward funnel parses these back (records.snapshot_overreaction)."""
    spy = f"{event.spy_return_pct:+.2f}%" if event.spy_return_pct is not None else "n/a"
    market = {True: "yes", False: "no", None: "n/a"}[event.market_day]
    return "\n".join(
        [
            "OVERREACTION CANDIDATE — MEASUREMENT ONLY (ruling 2026-09-03): no "
            "research, no trade; graded by the forward-return engine.",
            f"ticker: {event.symbol}",
            f"session: {event.session.isoformat()}",
            f"drop: {event.drop_pct:+.2f}%",
            f"volume ratio: {event.volume_ratio:.2f}x",
            f"SPY same-day: {spy}",
            f"market day: {market}",
            f"sector: {event.sector or 'unmapped'}",
            f"tier: {event.tier}",
            f"held: {'yes' if event.held else 'no'}",
            f"flags: {','.join(str(f) for f in event.flags)}",
        ]
    )


def event_signal(event: Event) -> Signal:
    """A Signal shaped like the rest of the funnel, observed at the session close."""
    observed_at = datetime.combine(event.session, SESSION_CLOSE, tzinfo=MARKET_TZ)
    content = render_content(event)
    return Signal(
        signal_id=signal_id_for(SOURCE_ID, event.external_id, content),
        source_id=SOURCE_ID,
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        observed_at=observed_at.astimezone(timezone.utc),
        content=content,
        raw_content=content,
        priority=Priority.for_class(SignalClass.CLASS_2_MOMENTUM),
        external_id=event.external_id,
        classification=None,
        metadata={
            "tickers": event.symbol,
            "measurement_only": "true",
            "measurement_code": CODE,
            "tier": event.tier,
            "held": "true" if event.held else "false",
        },
    )


def recorded_external_ids(audit) -> set[str]:
    """Every screen row already in the log — the idempotency set."""
    return {
        record.signal.external_id
        for record in audit.records()
        if isinstance(record, StageRejectionRecord)
        and record.signal.source_id == SOURCE_ID
        and record.signal.external_id
    }


@dataclass
class ScreenReport:
    sessions: tuple[date, ...]
    symbols_scanned: int = 0
    unmeasurable: int = 0  # names without enough history for a session
    events: list[Event] = field(default_factory=list)
    recorded: int = 0
    skipped_existing: int = 0
    #: Names whose bars fetch returned nothing. All of them = a data failure
    #: masquerading as a quiet market; the render says so.
    empty_symbols: int = 0

    def render(self) -> str:
        span = (
            f"{self.sessions[0]} → {self.sessions[-1]} ({len(self.sessions)} sessions)"
            if self.sessions
            else "no sessions"
        )
        lines = [
            f"OVERREACTION SCREEN — {span}; {self.symbols_scanned} names scanned",
            f"  events: {len(self.events)}  recorded: {self.recorded}  "
            f"already on record: {self.skipped_existing}  "
            f"unmeasurable name-sessions: {self.unmeasurable}",
        ]
        by_tier = {"core": 0, "broad": 0}
        for event in self.events:
            by_tier[event.tier] = by_tier.get(event.tier, 0) + 1
        lines.append(f"  by tier: core {by_tier.get('core', 0)}, broad {by_tier.get('broad', 0)}")
        if self.empty_symbols:
            lines.append(
                f"  WARNING: {self.empty_symbols} of {self.symbols_scanned} names "
                f"returned NO bars"
                + (" — every fetch failed; this run measured nothing"
                   if self.empty_symbols == self.symbols_scanned else "")
            )
        for event in sorted(self.events, key=lambda e: (e.session, e.symbol))[-25:]:
            lines.append(
                f"  {event.session} {event.symbol:<6} {event.drop_pct:+.2f}% on "
                f"{event.volume_ratio:.1f}x volume  SPY {event.spy_return_pct if event.spy_return_pct is not None else 'n/a'}  "
                f"{event.tier}{' held' if event.held else ''}  flags {','.join(map(str, event.flags))}"
            )
        if len(self.events) > 25:
            lines.append(f"  … {len(self.events) - 25} earlier events not listed")
        return "\n".join(lines)


def run_screen(
    *,
    sessions: Sequence[date],
    universe: dict[str, UniverseMember],
    bars: Callable[[str, datetime, datetime], Iterable[dict[str, Any]]],
    sector_of: Callable[[str], str],
    config: OverreactionScreenConfig,
    audit,
    id_factory: Callable[[], str],
    pace: Callable[[], None] = lambda: None,
    now: Optional[datetime] = None,
) -> ScreenReport:
    """Scan every universe name over ``sessions``; record new events. One bars
    fetch per name covers every session, plus one for SPY. The fetch end never
    reaches into the last hour: the free data plan refuses SIP queries into the
    most recent 15 minutes (probed 2026-09-03 — the first backfill silently
    scanned 404 names against empty bar lists), and a completed session's bar
    is all this screen ever reads."""
    report = ScreenReport(sessions=tuple(sorted(sessions)))
    if not sessions or not universe:
        return report
    moment = now or datetime.now(timezone.utc)
    start = datetime.combine(
        min(sessions) - timedelta(days=config.adv_days * 2 + 15), time.min, tzinfo=timezone.utc
    )
    end = min(
        datetime.combine(max(sessions) + timedelta(days=1), time.max, tzinfo=timezone.utc),
        moment - timedelta(hours=1),
    )
    spy_rows = rows_of(bars("SPY", start, end))
    if not spy_rows:
        logger.warning(
            "no SPY bars for %s → %s: market-day stamps will be n/a this run",
            start.date(),
            end.date(),
        )
    empty_symbols = 0
    spy_by_session = {
        session: session_return_pct(spy_rows, session) for session in sessions
    }
    seen = recorded_external_ids(audit)
    for index, symbol in enumerate(sorted(universe)):
        if index:
            pace()
        member = universe[symbol]
        try:
            rows = rows_of(bars(symbol, start, end))
        except Exception as error:  # noqa: BLE001 - missing, never invented
            logger.warning("bars for %s unavailable: %s", symbol, error)
            rows = []
        report.symbols_scanned += 1
        if not rows:
            empty_symbols += 1
        sector = sector_of(symbol) or ""
        for session in sessions:
            if not any(row[0] == session for row in rows):
                continue  # no bar that day (holiday, halt, not yet listed)
            event = detect(member, rows, session, config, spy_by_session.get(session), sector)
            if event is None:
                first = next(i for i, row in enumerate(rows) if row[0] == session)
                if first < config.adv_days:
                    report.unmeasurable += 1
                continue
            report.events.append(event)
            if event.external_id in seen:
                report.skipped_existing += 1
                continue
            signal = event_signal(event)
            audit.record_stage_rejection(
                id_factory(),
                RejectedStage.PRE_FILTER,
                CODE,
                f"overreaction candidate ({event.tier}{', held' if event.held else ''}): "
                f"{event.symbol} {event.drop_pct:+.2f}% on {event.volume_ratio:.1f}x "
                f"volume, {event.session}; measurement only — recorded for the "
                f"forward-return engine, never researched, never traded, never "
                f"sealing the name",
                signal,
            )
            seen.add(event.external_id)
            report.recorded += 1
    report.empty_symbols = empty_symbols
    if empty_symbols and empty_symbols == report.symbols_scanned:
        logger.error(
            "every one of %d bar fetches returned nothing — a data-plan or "
            "connectivity failure, not a quiet market; nothing was measured",
            empty_symbols,
        )
    return report


def last_completed_session(now: datetime) -> date:
    """The most recent weekday whose bar is complete: today if it is a weekday
    past DETECTION_READY in New York, else the previous weekday. Holidays
    resolve themselves — a day with no bar produces no event."""
    local = now.astimezone(MARKET_TZ)
    day = local.date()
    if local.weekday() >= 5 or local.time() < DETECTION_READY:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def weekdays_between(start: date, end: date) -> list[date]:
    out = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out
