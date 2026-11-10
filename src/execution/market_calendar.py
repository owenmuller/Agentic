"""The exchange calendar: which days are sessions and when each one closes
(sweep 2026-11-10, item 3).

Before it, every session rule was a weekday and a fixed clock time: on the
2026-11-26 closure the live mirror and the main book would have run a full
session, and on the 2026-11-27 13:00 early close the AI trader's 15:45 option
close and 15:50 intraday close would have come after the market had shut (a
0DTE held through expiry, an intraday position carried to Monday).

  source     Alpaca's ``GET /v2/calendar`` (holidays and early closes), fetched
             once per process day for a window around today and held in memory.
  cutoffs    a time configured for a regular 16:00 close (15:50, 15:45 ...) is
             an OFFSET from the close: ``Session.at("15:50")`` is 15:50 on a
             normal day and 12:50 on a 13:00 day, so normal days are unchanged.
  failure    a fetch that fails or comes back empty raises a yellow card (No
             Silent Data Gaps) and is never cached as an answer; it is retried
             after RETRY. Meanwhile (Constraint #6) a weekday is assumed to be a
             session that closes at 13:00: exits keep running, entries stop
             earlier and intraday positions close earlier - fewer, smaller risks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import datahealth

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
#: The close assumed while the calendar cannot be read (Constraint #6).
FALLBACK_CLOSE = time(13, 0)
#: The window fetched around today, in calendar days.
WINDOW_BACK, WINDOW_AHEAD = 20, 45
RETRY = timedelta(minutes=15)


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime
    close: datetime
    assumed: bool = False      # the calendar could not be read: the fallback session

    def at(self, hhmm: str) -> datetime:
        """A cutoff configured against a regular 16:00 close, moved with this
        session's own close (15:50 is "ten minutes before the close")."""
        configured = datetime.combine(self.day, time.fromisoformat(hhmm), NY)
        return self.close - (datetime.combine(self.day, REGULAR_CLOSE, NY) - configured)

    @property
    def early(self) -> bool:
        return self.close.time() < REGULAR_CLOSE


def _session(day: date, open_: time, close: time, assumed: bool = False) -> Session:
    return Session(day, datetime.combine(day, open_, NY), datetime.combine(day, close, NY), assumed)


class MarketCalendar:
    """Sessions by day. ``fetch(start, end)`` returns Alpaca calendar rows
    (``{"date": "2026-11-27", "open": "09:30", "close": "13:00"}``); None for a
    calendar that treats every weekday as a regular session (tests, and code
    that has no broker keys - say so where it is built)."""

    def __init__(self, fetch: Optional[Callable[[date, date], list[dict]]],
                 clock: Callable[[], datetime], source: str = "Market calendar") -> None:
        self._fetch, self._clock, self._source = fetch, clock, source
        self._days: dict[date, Optional[Session]] = {}
        self._range: Optional[tuple[date, date]] = None
        self._failed_at: Optional[datetime] = None

    @classmethod
    def weekdays(cls, clock: Callable[[], datetime] = lambda: datetime.now(NY)) -> "MarketCalendar":
        return cls(None, clock)

    def _today(self) -> date:
        return self._clock().astimezone(NY).date()

    def _load(self, day: date) -> bool:
        """True when ``day`` is answered by a fetched calendar."""
        if self._fetch is None:
            return False
        today = self._today()
        if self._range and self._range[0] <= day <= self._range[1]:
            return True                # a published calendar does not change: what was read stays answered
        now = self._clock()
        if self._failed_at is not None and now - self._failed_at < RETRY:
            return False
        start, end = min(day, today) - timedelta(days=WINDOW_BACK), max(day, today) + timedelta(days=WINDOW_AHEAD)
        try:
            rows = self._fetch(start, end)
            if not rows:
                raise RuntimeError(f"no sessions at all for {start}..{end}")
            days: dict[date, Optional[Session]] = {}
            for row in rows:
                d = date.fromisoformat(str(row["date"])[:10])
                days[d] = _session(d, time.fromisoformat(str(row["open"])[:5]), time.fromisoformat(str(row["close"])[:5]))
        except Exception as error:  # noqa: BLE001 - any failure is a gap, never an answer
            self._failed_at = now
            logger.warning("market calendar unavailable: %s", error)
            datahealth.report(self._source, f"the exchange calendar could not be read ({error}); until it can, "
                                            f"every weekday is treated as a session closing at "
                                            f"{FALLBACK_CLOSE:%H:%M} ET (entries stop and intraday positions "
                                            f"close earlier)")
            return False
        current = start
        while current <= end:          # every day in the window is answered: a missing row is a closed day
            days.setdefault(current, None)
            current += timedelta(days=1)
        self._days, self._range, self._failed_at = days, (start, end), None
        return True

    def session(self, day: date) -> Optional[Session]:
        """The day's session, or None when the market is closed."""
        if self._load(day):
            return self._days.get(day)
        if day.weekday() >= 5:
            return None
        if self._fetch is None:
            return _session(day, REGULAR_OPEN, REGULAR_CLOSE)
        return _session(day, REGULAR_OPEN, FALLBACK_CLOSE, assumed=True)

    def is_session(self, day: date) -> bool:
        return self.session(day) is not None

    def previous_session(self, day: date) -> date:
        current = day - timedelta(days=1)
        for _ in range(15):
            if self.is_session(current):
                return current
            current -= timedelta(days=1)
        return current

    def sessions_between(self, start: date, end: date) -> int:
        """Sessions from ``start`` to ``end`` inclusive."""
        count, current = 0, start
        while current <= end:
            if self.is_session(current):
                count += 1
            current += timedelta(days=1)
        return count


def alpaca_fetch(base_url: str, api_key: str, api_secret: str,
                 timeout: float = 20.0) -> Callable[[date, date], list[dict]]:
    """The ``fetch`` for a MarketCalendar: Alpaca's trading calendar with the
    given (paper) keys. Raises on anything but a 200."""
    import httpx

    def fetch(start: date, end: date) -> list[dict]:
        response = httpx.get(f"{base_url.rstrip('/')}/v2/calendar",
                             params={"start": start.isoformat(), "end": end.isoformat()},
                             headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
                             timeout=timeout)
        if response.status_code != 200:
            raise RuntimeError(f"Alpaca calendar HTTP {response.status_code}")
        return list(response.json())
    return fetch
