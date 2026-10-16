"""DATA HEALTH (global rule, human ruling 2026-10-15): any data fetch or report
that returns empty when data is expected - or fails - raises a YELLOW CARD.
It never passes silently.

Origin, a pattern rather than an accident: an alerter built before the keys
were loaded (silently disabled); SEC fetch failures cached as "no shares";
an Alpaca bar request the free feed refused (403) that came back as no data,
so the day's momentum card was never sent.

A fetcher that cannot get what it should calls ``report(source, message)``.
Raising is still right where the caller can carry on without the data only
by guessing; ``report`` is for the paths that degrade and keep going (a
missing quote, a failed page, a fallback taken), so the degradation is seen.

Each process installs ONE sink at its entry point (``set_sink``): the main
session writes a run-log ``DATA`` line, which its observer turns into a
yellow card; every other process sends the card directly
(``execution.alerts.data_sink``). Reports made before a sink exists are
held and delivered when one is installed, and always logged at WARNING.

Stdlib only and imports nothing first-party: a leaf every package may
import, like ``datasafety`` (``tests/test_topology.py``).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("datahealth")

Sink = Callable[[str, str, bool], None]

_lock = threading.Lock()
_sink: Optional[Sink] = None
_held: list[tuple[str, str, bool]] = []


def set_sink(sink: Optional[Sink]) -> None:
    """Install this process's sink (replacing any earlier one) and deliver
    the reports held until now."""
    global _sink
    with _lock:
        _sink = sink
        held, _held[:] = list(_held), []
    for source, message, urgent in held:
        _deliver(sink, source, message, urgent)


#: An outage fails many fetches at once (every held symbol's quote, a batch
#: of bars): at most this many cards per source per window reach the sink;
#: the rest are counted, and the next delivered card says how many.
BURST, WINDOW_SECONDS = 3, 1800
_recent: dict[str, list[float]] = {}
_suppressed: dict[str, int] = {}


def report(source: str, message: str, *, urgent: bool = False) -> None:
    """An expected fetch came back empty or failed. Logged, and a yellow card
    through the process's sink (``urgent`` sends it to #urgent rather than
    #reports: a gap that blinds a live safeguard). Every report is logged; at
    most ``BURST`` per source per ``WINDOW_SECONDS`` become cards."""
    logger.warning("DATA %s: %s", source, message)
    now = time.monotonic()
    with _lock:
        stamps = [t for t in _recent.get(source, []) if now - t < WINDOW_SECONDS]
        if len(stamps) >= BURST:
            _recent[source] = stamps
            _suppressed[source] = _suppressed.get(source, 0) + 1
            return
        stamps.append(now)
        _recent[source] = stamps
        extra = _suppressed.pop(source, 0)
        if extra:
            message = f"{message} (+{extra} more from {source} not carded in the last 30 minutes; see the log)"
        sink = _sink
        if sink is None:
            _held.append((source, message, urgent))
            return
    _deliver(sink, source, message, urgent)


def reset() -> None:
    """Forget the burst windows and held reports (tests)."""
    with _lock:
        _recent.clear()
        _suppressed.clear()
        _held.clear()


def held() -> list[tuple[str, str, bool]]:
    """Reports waiting for a sink (a test, or a process that never set one)."""
    with _lock:
        return list(_held)


def _deliver(sink: Optional[Sink], source: str, message: str, urgent: bool) -> None:
    if sink is None:
        return
    try:
        sink(source, message, urgent)
    except Exception:  # noqa: BLE001 - reporting a data gap must never break the fetch that found it
        logger.exception("data-health sink failed for %s", source)
