"""Intraday context for the AI trader (human ruling 2026-11-16, "STOP
CHASING, TRADE LIKE A DAY TRADER", items 1-3).

For every idea the model proposes, the rules engine measures where the name
stands in its session, in plain numbers:

  prior close   the last regular close before today (SIP daily bars)
  ATR(14)       as a fraction of the prior close (``execution.atr``)
  open          today's first regular-session minute bar's open
  VWAP          today's volume-weighted average price so far
  opening range the low and high of the first ``opening_range_minutes``
  rel. volume   today's volume so far against the average of the last
                ``relvol_sessions`` sessions to the same time of day
  extended      the move IN THE IDEA'S DIRECTION since the prior close, or
                since its catalyst went public when that is known, passes
                ``extended_atr`` x ATR(14) (item 1)

Today's bars come from IEX (the free feed refuses the consolidated tape's last
15 minutes, so SIP cannot serve a live decision); VWAP, the opening range and
relative volume are therefore IEX's (relative volume compares IEX with IEX).
The prior close and ATR come from SIP daily bars, which are complete.

Pure computations (``compute``, ``pullback_level``) are separate from the
fetches so they are tested on fixed bars; a fetch that fails or comes back
empty is reported (the no-silent-gaps rule, 2026-10-15) and its figure reads
None - and the rules engine never treats a None as "not extended"."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from typing import Any, Callable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import datahealth
from execution.atr import atr_fraction_from_bars

NY = ZoneInfo("America/New_York")
CENTS = Decimal("0.01")
SOURCE = "AI trader intraday context"


@dataclass
class Context:
    symbol: str
    as_of: datetime
    price: Optional[Decimal] = None
    prior_close: Optional[Decimal] = None
    atr_fraction: Optional[Decimal] = None
    open: Optional[Decimal] = None
    vwap: Optional[Decimal] = None
    or_low: Optional[Decimal] = None
    or_high: Optional[Decimal] = None
    or_complete: bool = False
    rel_volume: Optional[Decimal] = None
    #: the catalyst clock's measurement (``lab.catalyst_clock``), when it ran
    catalyst: dict = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    relvol_sessions: int = 20

    # -- the measures ---------------------------------------------------------

    @staticmethod
    def _rel(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
        return a / b - 1 if a is not None and b is not None and b > 0 else None

    @property
    def from_close(self) -> Optional[Decimal]:
        return self._rel(self.price, self.prior_close)

    @property
    def from_open(self) -> Optional[Decimal]:
        return self._rel(self.price, self.open)

    @property
    def from_vwap(self) -> Optional[Decimal]:
        return self._rel(self.price, self.vwap)

    def catalyst_move(self) -> Optional[Decimal]:
        """The move since the catalyst went public, in the idea's direction
        (the catalyst clock measures it signed for the idea)."""
        if self.catalyst.get("status") != "ok" or self.catalyst.get("move") in (None, ""):
            return None
        try:
            return Decimal(str(self.catalyst["move"]))
        except (InvalidOperation, ValueError):
            return None

    def extended(self, sign: int, multiple: Decimal) -> Optional[bool]:
        """Item 1: the move in the idea's direction since the prior close, or
        since its catalyst when known, passes ``multiple`` x ATR(14). EITHER
        measure passing counts (the reading that refuses more entries,
        Constraint #6; flagged). None when the prior-close measure cannot be
        taken (no price, prior close or ATR): the engine then refuses rather
        than guess."""
        if self.atr_fraction is None or self.atr_fraction <= 0 or self.from_close is None:
            return None
        bar = multiple * self.atr_fraction
        if sign * self.from_close > bar:
            return True
        moved = self.catalyst_move()
        return moved is not None and moved > bar

    def move_in_atr(self, sign: int) -> Optional[Decimal]:
        """The larger of the two measures, in the idea's direction, in ATRs."""
        if self.atr_fraction is None or self.atr_fraction <= 0 or self.from_close is None:
            return None
        moves = [sign * self.from_close] + ([self.catalyst_move()] if self.catalyst_move() is not None else [])
        return max(moves) / self.atr_fraction

    def extension(self, sign: int, multiple: Decimal, partial: Decimal) -> Optional[str]:
        """A TAG (human ruling 2026-11-16, later): "extended" past
        ``multiple`` ATRs; "partial" from ``partial`` up to it (below-threshold
        chasing, for the monthly review to rule on); "none" under it."""
        ratio = self.move_in_atr(sign)
        if ratio is None:
            return None
        return "extended" if ratio > multiple else "partial" if ratio >= partial else "none"

    # -- the prompt line (item 3) ----------------------------------------------

    def line(self, sign: int, multiple: Decimal) -> str:
        parts = [f"price {_money(self.price)}"]
        parts.append(f"{_pct(self.from_close)} from the prior close {_money(self.prior_close)}")
        parts.append(f"{_pct(self.from_open)} from today's open {_money(self.open)}")
        vw = self.from_vwap
        parts.append("VWAP n/a" if vw is None else
                     f"{abs(vw):.1%} {'above' if vw >= 0 else 'below'} VWAP {_money(self.vwap)}")
        parts.append("relative volume n/a" if self.rel_volume is None else
                     f"relative volume {self.rel_volume:.1f}x the {self.relvol_sessions}-day average at this time of day")
        parts.append("ATR(14) n/a" if self.atr_fraction is None else f"ATR(14) {self.atr_fraction:.1%} of price")
        moved = self.catalyst_move()
        if moved is not None:
            when = _clock(self.catalyst.get("public_at"))
            parts.append(f"{moved:+.1%} in the idea's direction since its catalyst went public ({when})")
        verdict = self.extended(sign, multiple)
        if verdict is None:
            parts.append("EXTENSION UNKNOWN (no prior close or ATR)")
        elif verdict:
            parts.append(f"EXTENDED: moved more than {multiple:g}x ATR in the idea's direction")
        return "; ".join(parts)



def _money(value: Optional[Decimal]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _pct(value: Optional[Decimal]) -> str:
    return "n/a" if value is None else f"{value:+.1%}"


def _clock(stamp: Any) -> str:
    try:
        local = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(NY)
    except ValueError:
        return "time unknown"
    return f"{local:%b} {local.day} {local:%H:%M} ET"


def _ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _d(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


# -- the pure computation ------------------------------------------------------------

def compute(symbol: str, as_of: datetime, price: Optional[Decimal], *, daily: list[dict], today: list[dict],
            history: list[dict], session_open: datetime, opens: dict[date, datetime],
            opening_range_minutes: int, relvol_sessions: int) -> Context:
    """The context from bars already fetched (oldest first):
    ``daily``   1Day bars up to and excluding today;
    ``today``   1Min bars of today's regular session that CLOSED by ``as_of``;
    ``history`` 5Min bars of earlier sessions (relative volume);
    ``opens``   each earlier session's open (early closes have their own)."""
    ctx = Context(symbol, as_of)
    ctx.relvol_sessions = relvol_sessions
    day = as_of.astimezone(NY).date()
    prior = [b for b in daily if _ts(b["t"]).astimezone(NY).date() < day]
    if prior:
        ctx.prior_close = _d(prior[-1].get("c"))
        ctx.atr_fraction = atr_fraction_from_bars(prior)
    bars = [b for b in today if session_open <= _ts(b["t"]) and _ts(b["t"]) + timedelta(minutes=1) <= as_of]
    if bars:
        ctx.open = _d(bars[0].get("o"))
        volume = sum((_d(b.get("v")) or Decimal(0)) for b in bars)
        weighted = sum(((_d(b.get("vw")) or _d(b.get("c")) or Decimal(0)) * (_d(b.get("v")) or Decimal(0)))
                       for b in bars)
        ctx.vwap = weighted / volume if volume > 0 else None
        window_end = session_open + timedelta(minutes=opening_range_minutes)
        first = [b for b in bars if _ts(b["t"]) < window_end]
        if first:
            ctx.or_low = min(_d(b.get("l")) for b in first)
            ctx.or_high = max(_d(b.get("h")) for b in first)
        ctx.or_complete = as_of >= window_end
    ctx.price = price if price is not None else (_d(bars[-1].get("c")) if bars else None)
    ctx.rel_volume = _relative_volume(bars, history, session_open, as_of, opens, relvol_sessions)
    return ctx


def _relative_volume(bars: list[dict], history: list[dict], session_open: datetime, as_of: datetime,
                     opens: dict[date, datetime], sessions: int) -> Optional[Decimal]:
    """Today's volume to the last whole 5 minutes against the mean of the last
    ``sessions`` sessions' volume to the same minutes after their own open."""
    elapsed = int((as_of - session_open).total_seconds() // 60) // 5 * 5
    if elapsed <= 0 or not bars:
        return None
    cut = session_open + timedelta(minutes=elapsed)
    today = sum((_d(b.get("v")) or Decimal(0)) for b in bars if _ts(b["t"]) < cut)
    by_day: dict[date, Decimal] = {}
    for bar in history:
        stamp = _ts(bar["t"])
        day = stamp.astimezone(NY).date()
        start = opens.get(day)
        if start is None or not (start <= stamp and stamp + timedelta(minutes=5) <= start + timedelta(minutes=elapsed)):
            continue
        by_day[day] = by_day.get(day, Decimal(0)) + (_d(bar.get("v")) or Decimal(0))
    days = sorted(by_day)[-sessions:]
    values = [by_day[d] for d in days if by_day[d] > 0]
    if not values:
        return None
    return today / (sum(values) / len(values))


def pullback_level(ctx: Context, bullish: bool) -> tuple[Optional[Decimal], str]:
    """Item 2: for a long, the HIGHER of session VWAP and the opening-range
    low; for a put, the LOWER of VWAP and the opening-range high. The level
    must be on the far side of the current price (below it for a long): an
    order at or through the market would be the at-quote entry item 1 refuses.
    Rounded away from the market (a long's limit never higher than the level)."""
    if ctx.vwap is None or (ctx.or_low if bullish else ctx.or_high) is None or not ctx.or_complete:
        return None, "no pullback level: the session has no VWAP or no complete opening range yet"
    if ctx.price is None:
        return None, "no pullback level: no current price"
    if bullish:
        level = max(ctx.vwap, ctx.or_low).quantize(CENTS, rounding=ROUND_DOWN)
        if level >= ctx.price:
            return None, (f"no pullback level below the price: {level} (the higher of VWAP and the opening-range "
                          f"low) is at or above {ctx.price}, so an order there would buy at the market")
    else:
        level = min(ctx.vwap, ctx.or_high).quantize(CENTS, rounding=ROUND_UP)
        if level <= ctx.price:
            return None, (f"no pullback level above the price: {level} (the lower of VWAP and the opening-range "
                          f"high) is at or below {ctx.price}, so a trigger there would enter at the market")
    return level, ""


# -- the fetches ------------------------------------------------------------------------

class IntradayData:
    """Bars on Alpaca's data host (the httpx client the catalyst clock uses)."""

    def __init__(self, config: Any, client: Any, *, calendar: Any = None,
                 report: Callable[..., None] = datahealth.report) -> None:
        self.config = config
        self._client = client
        self._calendar = calendar
        self._report = report

    def context(self, symbol: str, as_of: datetime, price: Optional[Decimal], *,
                today_feed: str = "iex") -> Context:
        day = as_of.astimezone(NY).date()
        session_open = self._open(day)
        gaps: list[str] = []
        daily = self._bars(symbol, "1Day", as_of - timedelta(days=45), session_open, "sip", gaps)
        today = self._bars(symbol, "1Min", session_open, as_of, today_feed, gaps)
        start = session_open - timedelta(days=int(self.config.relvol_sessions * 1.6) + 5)
        history = self._bars(symbol, "5Min", start, session_open, "iex", gaps)
        opens = {d: self._open(d) for d in {_ts(b["t"]).astimezone(NY).date() for b in history}}
        ctx = compute(symbol, as_of, price, daily=daily, today=today, history=history, session_open=session_open,
                      opens=opens, opening_range_minutes=self.config.opening_range_minutes,
                      relvol_sessions=self.config.relvol_sessions)
        ctx.gaps = gaps
        if not daily:
            gaps.append("no daily bars")
        if not today:
            # an illiquid name can print nothing on IEX early in the session: argued, but still a gap
            gaps.append("no IEX minute bars today")
        if gaps:
            self._report(SOURCE, f"{symbol}: {'; '.join(gaps)}")
        return ctx

    def _open(self, day: date) -> datetime:
        if self._calendar is not None:
            session = self._calendar.session(day)
            if session is not None:
                return session.open
        return datetime.combine(day, dtime(9, 30), NY)

    def _bars(self, symbol: str, timeframe: str, start: datetime, end: datetime, feed: str,
              gaps: list[str]) -> list[dict]:
        params = {"timeframe": timeframe, "start": _z(start), "end": _z(end), "feed": feed, "limit": 10000,
                  "adjustment": "split"}
        out: list[dict] = []
        token = None
        for _ in range(5):
            if token:
                params["page_token"] = token
            try:
                response = self._client.get(f"/v2/stocks/{quote(symbol)}/bars", params=params)
            except Exception as error:  # noqa: BLE001 - a failed read is a gap, never a stopped scan
                gaps.append(f"{timeframe} bars failed ({type(error).__name__})")
                return out
            if response.status_code >= 400:
                gaps.append(f"{timeframe} bars HTTP {response.status_code}")
                return out
            try:
                body = response.json()
            except ValueError:
                gaps.append(f"{timeframe} bars not JSON")
                return out
            out += [b for b in (body or {}).get("bars") or [] if isinstance(b, dict) and b.get("t")]
            token = (body or {}).get("next_page_token")
            if not token:
                break
        return out


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
