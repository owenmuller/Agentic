"""Attention-momentum screen (risk-on redirect, human ruling 2026-10-08, item 6).

Hypothesis: an event this system already recorded - a congressional purchase,
a Form 4 cluster, a contract award, an 8-K - that the MARKET then confirms
keeps running as attention builds. Different from "buy every disclosure",
which was measured flat-to-negative after publication.

Once per session day (first poll at/after ``earliest_new_york``), on completed
sessions only:
  * events: the funnel's own records from the eligible sources in the last
    ``lookback_calendar_days`` (congressional PURCHASES only; Form 4 and 8-K
    bearish measurement rows excluded), one per (ticker, t0), where t0 is the
    event's first tradeable session;
  * confirmation on the LATEST completed session s, with s in t0+1..t0+3 and
    no earlier session in that window having confirmed (the pre-registered
    rule enters after the FIRST confirmation): close(s) > max close of the
    ``pre_high_sessions`` before t0; volume(s) >= ``volume_multiple`` x the
    ``volume_avg_sessions``-session average ending t0-1; and the move since
    the close before t0 beats SPY's over the same span.
A confirmed event becomes a RawItem for the aggressive sleeve's research pass:
the event, the confirmation numbers, ATR(14) and the stop it implies. The
research prompt turns the priced-in and staleness checks OFF for this source
(the ruling: momentum buys what has already moved) and asks whether the story
has room to run.

Thresholds are the pre-registered ones (SESSION_NOTES 2026-10-08). Nothing
here reads P&L, a target, or this sleeve's results (Constraint #6).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

from audit.records import DecisionRecord, StageRejectionRecord, snapshot_tickers, snapshot_transaction
from signals.scanners import RawItem

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
SOURCE_ID = "attention_momentum"
_EXCLUDED_CODES = {"bearish_measurement", "no_instrument", "pre_filter_unheld_sale"}


@dataclass(frozen=True, slots=True)
class _Event:
    ticker: str
    source_id: str
    observed_at: datetime
    content: str
    code: str


@dataclass(frozen=True, slots=True)
class _Bar:
    day: date
    high: float
    low: float
    close: float
    volume: float


def _bars_of(raw: list[dict[str, Any]]) -> list[_Bar]:
    out: list[_Bar] = []
    for bar in raw:
        try:
            out.append(
                _Bar(
                    date.fromisoformat(str(bar["t"])[:10]),
                    float(bar["h"]),
                    float(bar["l"]),
                    float(bar["c"]),
                    float(bar.get("v") or 0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _atr14(rows: list[_Bar], i: int) -> Optional[float]:
    if i < 15:
        return None
    total = 0.0
    for k in range(i - 13, i + 1):
        h, l, pc = rows[k].high, rows[k].low, rows[k - 1].close
        total += max(h - l, abs(h - pc), abs(l - pc))
    return total / 14


def first_tradeable_session(observed_at: datetime, sessions: list[date]) -> Optional[date]:
    """The session an event could first be traded in: its own session when
    observed before 16:00 New York on a session day, else the next session."""
    local = observed_at.astimezone(NEW_YORK)
    day = local.date()
    if local.time() < time(16, 0) and day in sessions:
        return day
    for session in sessions:
        if session > day:
            return session
    return None


class AttentionMomentumFetcher:
    """A router fetcher for ``attention_momentum``: ``(SourceConfig) -> items``."""

    def __init__(
        self,
        records: Callable[[], Iterable[object]],
        bars_many: Callable[[list[str], datetime, datetime], dict[str, list[dict[str, Any]]]],
        config,
        clock: Optional[Callable[[], datetime]] = None,
        seen: Iterable[str] = (),
    ) -> None:
        self._records = records
        self._bars_many = bars_many
        self._config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._seen: set[str] = set(seen)
        self._ran_on: Optional[date] = None
        self.last_tally: dict[str, int] = {}

    # -- the router contract ---------------------------------------------------

    def __call__(self, source) -> list[RawItem]:
        config = self._config
        if not config.enabled:
            return []
        now = self._clock()
        local = now.astimezone(NEW_YORK)
        hour, minute = (int(x) for x in config.earliest_new_york.split(":"))
        if local.weekday() >= 5 or local.time() < time(hour, minute) or self._ran_on == local.date():
            return []
        self._ran_on = local.date()
        try:
            return self.screen(now)
        except Exception as error:  # noqa: BLE001 - a screen must never sink a poll
            logger.warning("attention-momentum screen failed: %s", error)
            return []

    # -- the screen ------------------------------------------------------------

    def events(self, now: datetime) -> list[_Event]:
        config = self._config
        since = now - timedelta(days=config.lookback_calendar_days)
        sources = set(config.event_sources)
        out: list[_Event] = []
        for record in self._records():
            if not isinstance(record, (DecisionRecord, StageRejectionRecord)):
                continue
            signal = record.signal
            if signal.source_id not in sources or signal.observed_at < since:
                continue
            if isinstance(record, DecisionRecord) and record.sizing.strategy in ("mechanical", "cash_sweep", "baseline"):
                continue
            code = getattr(record, "code", "") or ""
            if code in _EXCLUDED_CODES:
                continue
            if signal.source_id == "congressional_disclosures" and "purchase" not in snapshot_transaction(signal).lower():
                continue
            tickers = snapshot_tickers(signal)
            if not tickers:
                continue
            out.append(_Event(tickers[0].upper(), signal.source_id, signal.observed_at, signal.content or "", code))
        return out

    def screen(self, now: datetime) -> list[RawItem]:
        config = self._config
        events = self.events(now)
        tally = {"events": len(events), "symbols": 0, "confirmed": 0, "emitted": 0, "already_seen": 0, "earlier_confirmation": 0, "no_bars": 0}
        symbols = list(dict.fromkeys(e.ticker for e in events))[: config.max_symbols]
        tally["symbols"] = len(symbols)
        if not symbols:
            self.last_tally = tally
            return []
        start = now - timedelta(days=config.lookback_calendar_days + 60)
        raw = self._bars_many(symbols + ["SPY"], start, now)
        spy = _bars_of(raw.get("SPY", []))
        sessions = [b.day for b in spy]
        if len(sessions) < config.volume_avg_sessions + config.pre_high_sessions + 2:
            self.last_tally = tally
            return []
        latest = sessions[-1]
        spy_close = {b.day: b.close for b in spy}
        by_key: dict[tuple[str, date], _Event] = {}
        for event in events:
            t0 = first_tradeable_session(event.observed_at, sessions)
            if t0 is None or t0 >= latest:
                continue
            by_key.setdefault((event.ticker, t0), event)
        items: list[RawItem] = []
        emitted_tickers: set[str] = set()
        tally["same_ticker"] = 0
        for (ticker, t0), event in sorted(by_key.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if ticker in emitted_tickers:
                # One candidate per name per morning: a ticker with events on
                # consecutive days (two filings, two disclosures) is one
                # momentum decision, not several - the earliest event speaks.
                tally["same_ticker"] += 1
                continue
            rows = _bars_of(raw.get(ticker, []))
            index = {b.day: i for i, b in enumerate(rows)}
            i0, s = index.get(t0), index.get(latest)
            if i0 is None or s is None or i0 < config.volume_avg_sessions + 1 or i0 < config.pre_high_sessions:
                tally["no_bars"] += 1
                continue
            offset = s - i0
            if not 1 <= offset <= config.confirm_sessions:
                continue
            pre_high = max(b.close for b in rows[i0 - config.pre_high_sessions:i0])
            vol_avg = sum(b.volume for b in rows[i0 - config.volume_avg_sessions:i0]) / config.volume_avg_sessions
            base = rows[i0 - 1]
            if base.day not in spy_close:
                continue

            def confirms(j: int) -> Optional[tuple[float, float]]:
                bar = rows[j]
                if bar.day not in spy_close or vol_avg <= 0:
                    return None
                excess = bar.close / base.close - spy_close[bar.day] / spy_close[base.day]
                if bar.close > pre_high and bar.volume >= float(config.volume_multiple) * vol_avg and excess > 0:
                    return excess, bar.volume / vol_avg
                return None

            if any(confirms(j) for j in range(i0 + 1, s)):
                tally["earlier_confirmation"] += 1
                continue
            hit = confirms(s)
            if hit is None:
                continue
            tally["confirmed"] += 1
            external_id = f"attn:{ticker}:{t0.isoformat()}"
            if external_id in self._seen:
                tally["already_seen"] += 1
                continue
            excess, vol_ratio = hit
            atr = _atr14(rows, s)
            close = rows[s].close
            atr_fraction = (atr / close) if atr and close > 0 else None
            items.append(self._item(ticker, event, t0, rows[s].day, offset, close, pre_high, vol_ratio, excess, atr, atr_fraction, now))
            self._seen.add(external_id)
            emitted_tickers.add(ticker)
            tally["emitted"] += 1
        self.last_tally = tally
        logger.info("attention-momentum screen: %s", tally)
        return items

    def _item(self, ticker, event: _Event, t0, confirm_day, offset, close, pre_high, vol_ratio, excess, atr, atr_fraction, now) -> RawItem:
        k = float(getattr(self._config, "stop_atr_k", 2.5))
        lines = [
            "Attention-momentum confirmation (a market-data screen over this system's own event funnel)",
            f"ticker: {ticker}",
            f"event source: {event.source_id}",
            f"event observed: {event.observed_at.isoformat()}",
            f"t0 (first tradeable session): {t0.isoformat()}",
            f"confirmation session: {confirm_day.isoformat()} (t0+{offset})",
            f"close at confirmation: {close:.2f}",
            f"pre-event high (max close of the 5 sessions before t0): {pre_high:.2f}",
            f"volume at confirmation: {vol_ratio:.1f}x the 20-session average before the event",
            f"excess vs SPY since the close before t0: {100 * excess:+.2f}%",
            f"ATR(14): {atr:.2f} ({100 * atr_fraction:.2f}% of price)" if atr and atr_fraction else "ATR(14): unavailable",
            "the event, as recorded:",
            event.content[:1200],
        ]
        fields = {
            "ticker": ticker,
            "event_source": event.source_id,
            "t0": t0.isoformat(),
            "confirm_day": confirm_day.isoformat(),
            "confirm_offset": str(offset),
            "volume_ratio": f"{vol_ratio:.2f}",
            "excess_since_event": f"{excess:.4f}",
            "atr_fraction": f"{atr_fraction:.4f}" if atr_fraction else "",
            "report_date": confirm_day.isoformat(),
            "measurement_only": "false",
        }
        _ = k  # the stop is derived by the sizing step from atr_fraction
        return RawItem(external_id=f"attn:{ticker}:{t0.isoformat()}", content="\n".join(lines), published_at=now, fields=fields)
