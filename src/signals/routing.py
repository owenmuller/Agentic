"""The source router — one place to wire a fetcher per source.

``build_scanners`` takes a single ``Fetcher`` for all three classes; this is that
fetcher for production, dispatching on ``source.id``. Three kinds of source exist:

  built        routed to its real fetcher (EDGAR for form_13f, Quiver for
               congressional_disclosures).
  unbuilt      declared, awaiting credentials (the Class 1 accounts). Polls nothing,
               and says so once per source per process — a warning every 60-second
               Class 1 cycle would be noise, silence would be a lie.
  unknown      not routed and not declared. Raises: a source that appears in
               signals.yaml without a wiring decision is config drift, and the loop
               will log the failed cycle loudly every poll until someone decides.

Wiring a future fetcher is one line in the routes mapping and the removal of one id
from the unbuilt set.
"""

from __future__ import annotations

import logging
from typing import Collection, Mapping, Sequence

from signals.config import SourceConfig
from signals.scanners import Fetcher, RawItem

logger = logging.getLogger("signals.routing")


class FeedNotConfigured(RuntimeError):
    """A source with neither a fetcher nor an unbuilt declaration."""


class SourceRouter:
    """Dispatches each source to its fetcher; the seams stay visible."""

    def __init__(
        self,
        routes: Mapping[str, Fetcher],
        unbuilt: Collection[str] = (),
    ) -> None:
        overlap = set(routes) & set(unbuilt)
        if overlap:
            raise ValueError(
                f"sources cannot be both routed and unbuilt: {sorted(overlap)}"
            )
        self._routes = dict(routes)
        self._unbuilt = frozenset(unbuilt)
        self._warned: set[str] = set()

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        fetcher = self._routes.get(source.id)
        if fetcher is not None:
            return fetcher(source)
        if source.id in self._unbuilt:
            if source.id not in self._warned:
                self._warned.add(source.id)
                logger.warning(
                    "source %r has no fetcher built (credentials pending); polling "
                    "nothing for it. This is logged once per process.",
                    source.id,
                )
            return []
        raise FeedNotConfigured(
            f"source {source.id!r} is neither routed to a fetcher nor declared "
            f"unbuilt — a source in signals.yaml needs a wiring decision"
        )
