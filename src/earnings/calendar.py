"""The earnings calendar: a dated, scheduled catalyst.

Every other signal source in this system is reactive — someone files or posts and
we are late by construction. An earnings date is known in advance, which is the
whole reason this is worth observing.

Finnhub, free tier
------------------
Verified 2026-08-31: the free tier allows **60 API calls per minute** and includes
the earnings calendar for US names. Two limits shape the design:

- The free calendar covers a **short forward window** (about a month), not deep
  history. So this fetcher asks only for the window ahead, and the historical
  realised-move series is built *forward* from the day the logger starts rather
  than backfilled. A shadow logger accumulating its own history is exactly the
  right shape for that constraint.
- The free tier is documented as being for personal, non-commercial use. This is
  a personal paper-trading account, which fits; a human should re-read that
  clause before any live money rides on it.

No key, no calendar
-------------------
``FINNHUB_API_KEY`` is not set anywhere yet. Without it this fetcher raises
rather than returning an empty list: "no earnings this fortnight" and "we cannot
see the calendar" are different facts, and silently conflating them would make
the shadow log quietly incomplete — the one failure this exercise cannot afford,
because an incomplete series looks exactly like a real one.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional, Protocol, Sequence

import httpx

FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/earnings"

logger = logging.getLogger("earnings.calendar")


class EarningsCalendarError(RuntimeError):
    """A calendar pull that could not be completed. The caller logs and skips."""


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    """One scheduled print."""

    symbol: str
    report_date: date
    #: "bmo" (before market open), "amc" (after market close), or "" when the
    #: feed does not say. It decides which session the move lands in, so an
    #: unknown session is recorded as unknown rather than assumed.
    session: str = ""
    eps_estimate: Optional[float] = None
    revenue_estimate: Optional[float] = None

    @property
    def session_known(self) -> bool:
        return self.session in {"bmo", "amc"}


class EarningsCalendar(Protocol):
    """Anything that can name the upcoming prints in a window."""

    def upcoming(self, start: date, end: date) -> Sequence[EarningsEvent]:
        ...


class FinnhubEarningsCalendar:
    """The free-tier Finnhub earnings calendar.

    One request per pass covering the whole window — the endpoint returns every
    US name in a date range, and filtering to the configured universe locally
    costs nothing and keeps us far inside 60 calls/minute.
    """

    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _RETRY_PAUSE_SECONDS = 2.0

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        api_key: Optional[str] = None,
        timeout: float = 15.0,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout)
        self._api_key = api_key
        self._sleep = sleeper or time.sleep

    def upcoming(self, start: date, end: date) -> Sequence[EarningsEvent]:
        response = self._get(
            {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": self._resolve_key(),
            }
        )
        if response.status_code in (401, 403):
            raise EarningsCalendarError(
                f"Finnhub refused the API key (HTTP {response.status_code}); "
                f"check FINNHUB_API_KEY in .env"
            )
        if response.status_code == 429:
            raise EarningsCalendarError(
                "Finnhub rate limit reached (HTTP 429); the free tier allows 60 "
                "calls per minute"
            )
        if response.status_code != 200:
            raise EarningsCalendarError(
                f"Finnhub earnings calendar returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise EarningsCalendarError("Finnhub returned a non-JSON body") from error
        rows = (payload or {}).get("earningsCalendar")
        if not isinstance(rows, list):
            raise EarningsCalendarError(
                "Finnhub response carried no earningsCalendar list"
            )
        events: list[EarningsEvent] = []
        for row in rows:
            event = _event_from_row(row)
            if event is not None:
                events.append(event)
        return events

    # -- plumbing ------------------------------------------------------------------

    def _get(self, params: dict) -> httpx.Response:
        response = self._client.get(FINNHUB_CALENDAR_URL, params=params)
        if response.status_code in self._RETRY_STATUSES:
            logger.warning(
                "Finnhub returned HTTP %d; retrying once after %.0fs",
                response.status_code,
                self._RETRY_PAUSE_SECONDS,
            )
            self._sleep(self._RETRY_PAUSE_SECONDS)
            response = self._client.get(FINNHUB_CALENDAR_URL, params=params)
        return response

    def _resolve_key(self) -> str:
        key = (self._api_key or os.environ.get("FINNHUB_API_KEY") or "").strip()
        if not key:
            raise EarningsCalendarError(
                "FINNHUB_API_KEY is not set. The earnings calendar cannot be read "
                "without it, and an unreadable calendar is NOT an empty one — put "
                "a free-tier key in .env (gitignored) to start the shadow log."
            )
        return key

    def close(self) -> None:
        self._client.close()


def _event_from_row(row: object) -> Optional[EarningsEvent]:
    if not isinstance(row, dict):
        return None
    symbol = str(row.get("symbol") or "").strip().upper()
    report_date = _date_of(row.get("date"))
    if not symbol or report_date is None:
        logger.warning("earnings row missing symbol or date; skipping: %r", row)
        return None
    hour = str(row.get("hour") or "").strip().lower()
    return EarningsEvent(
        symbol=symbol,
        report_date=report_date,
        session=hour if hour in {"bmo", "amc"} else "",
        eps_estimate=_float_or_none(row.get("epsEstimate")),
        revenue_estimate=_float_or_none(row.get("revenueEstimate")),
    )


def _date_of(raw: object) -> Optional[date]:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        logger.warning("unparseable earnings date: %r", raw)
        return None


def _float_or_none(raw: object) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def utc_today(clock: Optional[Callable[[], datetime]] = None) -> date:
    return (clock or (lambda: datetime.now(timezone.utc)))().date()
