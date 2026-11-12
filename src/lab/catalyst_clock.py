"""Move since public (human ruling 2026-11-12, item 4): LOGGED ONLY.

The AI trader review found every dated catalyst of the week released outside
market hours, a median 6.5% of the move gone by our entry, and the entries
then lagging SPY. This measures that for every proposed idea, at proposal
time, so the question can be answered on the record:

  public    the first headline about the symbol in the news feed (Alpaca's
            news API) that shares at least ``min_word_overlap`` significant
            words with the idea's catalyst, inside the lookback. No match:
            no time is recorded (never a guess; the shadow skips the idea).
  reference the price when it became public: the minute bar at that time if
            it fell inside a regular session, else the prior regular close
            (an overnight release's gap counts as move already gone).
  move      in the idea's direction (a put's falling underlying is a gain),
            from the reference to the price the rules engine anchored on.
  shadow    ``would_skip``: the move exceeds ``skip_above_atr`` x ATR(14).

Nothing here can stop, size or delay a trade: the rules engine calls it after
its entries are placed, and every failure is a logged row and a data card,
never an exception into the scan.
"""
from __future__ import annotations

import re
from datetime import datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import datahealth
from execution.atr import atr_fraction_from_bars

NY = ZoneInfo("America/New_York")
#: Recent data on the free feed: SIP refuses its last 15 minutes, so a release
#: younger than this is priced from IEX bars.
SIP_DELAY = timedelta(minutes=16)
_WORD = re.compile(r"[a-z][a-z0-9]{3,}")
_STOP = frozenset("""
    about after against ahead also amid analyst analysts announces announced back been before being below
    company could does down during each first from have here higher into just last lower market markets more
    most much news over price prices quarter report reports reported said says session share shares since
    some stock stocks than that their them then there these they this those through today trading under
    update what when where which while will with would year years 2025 2026 2027
""".split())


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


class CatalystClock:
    def __init__(self, config: Any, client: Any, *, calendar: Any = None,
                 report: Callable[..., None] = datahealth.report) -> None:
        self.config = config
        self._client = client          # an httpx client on Alpaca's data host
        self._calendar = calendar      # sessions and closes; None: weekdays 09:30-16:00
        self._report = report

    # -- the measurement -----------------------------------------------------------

    def measure(self, symbol: str, catalyst: str, sign: int, now: datetime, price: Optional[Decimal]) -> dict:
        out: dict[str, Any] = {"status": "ok"}
        headlines = self._news(symbol, now)
        if headlines is None:
            out["status"] = "news_failed"
            return out
        out["headlines_seen"] = len(headlines)
        wanted = words(catalyst)
        matched = [h for h in headlines if len(wanted & words(h["headline"])) >= self.config.min_word_overlap]
        if not matched:
            # legitimately empty: an ETF or a macro catalyst often has no
            # symbol-tagged headline, and a name can be quiet - logged, never guessed
            out["status"] = "no_matching_headline"
            out["first_headlines"] = [f"{h['created_at']} {h['headline'][:120]}" for h in headlines[:3]]
            return out
        first = min(matched, key=lambda h: h["created_at"])
        public = _ts(first["created_at"])
        out.update(public_at=public.isoformat(), headline=first["headline"][:200], matched=len(matched),
                   overlap=sorted(wanted & words(first["headline"]))[:8])
        daily = self._bars(symbol, "1Day", public - timedelta(days=45), now, "sip" if now - public > SIP_DELAY else "iex")
        if daily is None:
            out["status"] = "bars_failed"
            return out
        atr = atr_fraction_from_bars([b for b in daily if self._before_today(b, now)])
        out["atr_fraction"] = str(atr) if atr is not None else None
        reference, how = self._reference(symbol, public, now, daily)
        out.update(reference=str(reference) if reference is not None else None, reference_kind=how,
                   price=str(price) if price is not None else None)
        if reference is None or price is None or reference <= 0:
            out["status"] = "no_reference_price" if reference is None else "no_price"
            return out
        move = Decimal(sign) * (price / reference - 1)
        out["move"] = str(round(move, 5))
        if atr is not None and atr > 0:
            out["move_in_atr"] = str(round(move / atr, 3))
            out["would_skip"] = move > self.config.skip_above_atr * atr
        out["minutes_since_public"] = int((now - public).total_seconds() // 60)
        return out

    # -- the reference price ---------------------------------------------------------

    def _reference(self, symbol: str, public: datetime, now: datetime,
                   daily: list[dict]) -> tuple[Optional[Decimal], str]:
        local = public.astimezone(NY)
        opens, closes = self._hours(local.date())
        if opens is not None and opens <= local < closes:
            feed = "sip" if now - public > SIP_DELAY else "iex"
            minute = self._bars(symbol, "1Min", public - timedelta(minutes=1), public + timedelta(minutes=5), feed)
            for bar in minute or []:
                if _ts(bar["t"]) >= public - timedelta(minutes=1):
                    return _dec(bar.get("o")), "minute_at_release"
            return None, "minute_at_release"
        # an off-session release: the last regular close before it
        for bar in reversed(daily):
            day = _ts(bar["t"]).astimezone(NY).date()
            _, close_at = self._hours(day)
            if close_at is not None and close_at <= local:
                return _dec(bar.get("c")), "prior_close"
        return None, "prior_close"

    def _hours(self, day) -> tuple[Optional[datetime], Optional[datetime]]:
        if self._calendar is not None:
            session = self._calendar.session(day)
            return (session.open, session.close) if session is not None else (None, None)
        if day.weekday() >= 5:
            return None, None
        return (datetime.combine(day, dtime(9, 30), NY), datetime.combine(day, dtime(16, 0), NY))

    @staticmethod
    def _before_today(bar: dict, now: datetime) -> bool:
        return _ts(bar["t"]).astimezone(NY).date() < now.astimezone(NY).date()

    # -- the data host -----------------------------------------------------------------

    def _news(self, symbol: str, now: datetime) -> Optional[list[dict]]:
        params = {"symbols": symbol, "start": _z(now - timedelta(hours=self.config.news_lookback_hours)),
                  "end": _z(now), "limit": 50, "sort": "desc"}
        body = self._get("/v1beta1/news", params, f"news for {symbol}")
        if body is None:
            return None
        rows = []
        for item in body.get("news") or []:
            if isinstance(item, dict) and item.get("created_at") and item.get("headline"):
                rows.append({"created_at": str(item["created_at"]), "headline": str(item["headline"])})
        return rows

    def _bars(self, symbol: str, timeframe: str, start: datetime, end: datetime, feed: str) -> Optional[list[dict]]:
        params = {"timeframe": timeframe, "start": _z(start), "end": _z(end), "feed": feed, "limit": 10000,
                  "adjustment": "split"}
        body = self._get(f"/v2/stocks/{quote(symbol)}/bars", params, f"{timeframe} bars for {symbol}")
        return None if body is None else [b for b in body.get("bars") or [] if isinstance(b, dict) and b.get("t")]

    def _get(self, path: str, params: dict, what: str) -> Optional[dict]:
        try:
            response = self._client.get(path, params=params)
        except Exception as error:  # noqa: BLE001 - a failed read is a logged gap, never a stopped scan
            self._report("AI trader move since public", f"{what} failed: {type(error).__name__}: {error}")
            return None
        if response.status_code >= 400:
            self._report("AI trader move since public", f"{what} returned HTTP {response.status_code}")
            return None
        try:
            body = response.json()
        except ValueError:
            self._report("AI trader move since public", f"{what} returned a body that is not JSON")
            return None
        return body if isinstance(body, dict) else {}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _dec(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None
