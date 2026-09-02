"""The CBOE VIX close — the regime scalar's one external input.

Lives in ``execution`` because it does network I/O and the orchestrator package
stays offline (topology rule); the orchestrator's ``SizingScalars`` receives it
as an injected callable. Public CSV, keyless, probed live 2026-09-02.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

import httpx

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
        self._cached: Optional[tuple[date, Decimal]] = None

    def __call__(self) -> Optional[tuple[date, Decimal]]:
        today = self._clock().date()
        if self._fetched_on == today:
            return self._cached
        self._fetched_on = today
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
        except Exception as error:  # noqa: BLE001 - sizing must not die on a CDN blip
            logger.warning(
                "VIX close unavailable from CBOE (%s); regime scalar runs on "
                "%s until the next UTC day",
                error,
                "the cached close" if self._cached else "1.0 (no data)",
            )
        return self._cached
