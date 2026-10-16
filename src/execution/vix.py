"""The CBOE VIX close — the regime scalar's one external input.

Lives in ``execution`` because it does network I/O and the orchestrator package
stays offline (topology rule); the orchestrator's ``SizingScalars`` receives it
as an injected callable. Public CSV, keyless, probed live 2026-09-02.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

import httpx

import datahealth

VIX_HISTORY_URL = (
    "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
)

logger = logging.getLogger("execution.vix")


class CboeVixSource:
    """The last VIX daily close from CBOE's public CSV. One fetch per UTC day,
    cached; a failed fetch returns the cached value if any, else None — and
    never raises into the sizing path."""

    def __init__(
        self,
        get: Optional[Callable[[str], httpx.Response]] = None,
        *,
        url: str = VIX_HISTORY_URL,
        timeout: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        # One request per UTC day: a persistent client buys nothing, and a
        # short-lived request leaves nothing to close on shutdown.
        self._get = get or (
            lambda target: httpx.get(target, timeout=timeout, follow_redirects=True)
        )
        self._url = url
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._fetched_on: Optional[date] = None
        self._retry_at: Optional[datetime] = None
        self._cached: Optional[tuple[date, Decimal]] = None

    #: After a failed fetch, try again after this long (2026-10-15: one blip
    #: used to lock the day out until the next UTC day).
    RETRY_AFTER = timedelta(minutes=30)
    #: A close older than this many calendar days is stale (a long weekend is 4).
    STALE_DAYS = 4

    def __call__(self) -> Optional[tuple[date, Decimal]]:
        now = self._clock()
        today = now.date()
        if self._fetched_on == today:
            return self._cached
        if self._retry_at is not None and now < self._retry_at:
            return self._cached
        try:
            response = self._get(self._url)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            last = response.text.strip().splitlines()[-1]
            date_raw, _open, _high, _low, close_raw = last.split(",")
            month, day, year = date_raw.split("/")
            self._cached = (
                date(int(year), int(month), int(day)),
                Decimal(close_raw),
            )
            self._fetched_on = today
            self._retry_at = None
            if (today - self._cached[0]).days > self.STALE_DAYS:
                datahealth.report("VIX (CBOE)", f"the latest VIX close is from {self._cached[0]}, "
                                                f"{(today - self._cached[0]).days} days old; the regime scalar "
                                                f"runs on it unless older than its own limit")
        except Exception as error:  # noqa: BLE001 - sizing must not die on a CDN blip
            self._retry_at = now + self.RETRY_AFTER
            # never silent (data-health rule 2026-10-15); the scalar falls back as ruled
            datahealth.report("VIX (CBOE)", f"VIX close unavailable from CBOE ({error}); the regime scalar runs on "
                                            f"{'the cached close' if self._cached else '1.0 (no data)'}; retried in "
                                            f"{int(self.RETRY_AFTER.total_seconds() // 60)} minutes")
        return self._cached
