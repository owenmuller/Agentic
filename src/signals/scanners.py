"""The three scanners, one per latency class.

What a scanner is allowed to do
-------------------------------
Fetch, classify, and enqueue. Nothing else. A scanner is constructed with a fetcher, a
``SignalQueue`` and a clock; it is never given a ``RiskGate``, a sizing table, or a
broker adapter, and this package imports none of them. A scanner that wanted to place
a trade would have to import something it cannot reach — which
``test_scanners.py::test_signals_package_cannot_reach_execution_or_risk`` asserts by
walking the imports.

That separation is what makes Constraint #5 structural rather than aspirational. Even
if a post successfully convinced something in here of something, the only expressible
consequence is "a Signal was queued".

Fetchers
--------
A ``Fetcher`` is any callable returning ``RawItem`` records. The concrete HTTP clients
(X, Truth Social, Quiver/Unusual Whales/Capitol Trades, EDGAR) are not built yet —
they need credentials that do not exist on this machine, and an untestable HTTP client
is worse than an explicit seam. Tests supply fixture fetchers through the same
interface the real ones will use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Callable, Iterable, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from signals.classification import ClassificationResult, classify_post, extract_tickers
from signals.config import ClassConfig, SignalsConfig, SourceConfig
from signals.records import (
    Classification,
    CredibilityLog,
    CredibilityRecord,
    Priority,
    Signal,
    SignalClass,
    SignalQueue,
    signal_id_for,
)

MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


@dataclass(frozen=True, slots=True)
class RawItem:
    """One item as a source returned it, before any interpretation.

    ``content`` is verbatim and untrusted. ``fields`` carries structured facts the
    source itself provides (a disclosure's trade date, a filing's period) — never
    anything parsed out of ``content``.
    """

    external_id: str
    content: str
    published_at: datetime
    fields: dict[str, str] = field(default_factory=dict)


class Fetcher(Protocol):
    """Returns raw items for one source. Implementations do I/O; scanners do not."""

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]: ...


def is_market_hours(moment: datetime) -> bool:
    """Regular US equity session, weekdays only.

    No exchange holiday calendar — on a holiday this reports open and the scanner
    polls a quiet feed, which wastes a request and risks nothing. A real calendar is
    the proper fix and belongs with the market-data integration.
    """
    local = moment.astimezone(MARKET_TIMEZONE)
    if local.weekday() >= 5:
        return False
    return MARKET_OPEN <= local.time() <= MARKET_CLOSE


class Scanner(ABC):
    """Polls one signal class on its configured cadence."""

    signal_class: SignalClass

    def __init__(
        self,
        config: ClassConfig,
        fetcher: Fetcher,
        queue: SignalQueue,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._queue = queue
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_poll: Optional[datetime] = None

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self._config.interval_seconds)

    @property
    def last_poll(self) -> Optional[datetime]:
        return self._last_poll

    def is_due(self, now: Optional[datetime] = None) -> bool:
        """Cadence plus, for class 1, the market-hours gate."""
        moment = now or self._clock()
        if self._config.market_hours_only and not is_market_hours(moment):
            return False
        if self._last_poll is None:
            return True
        return moment - self._last_poll >= self.interval

    def poll(self, force: bool = False) -> list[Signal]:
        """Fetch, transform, enqueue. Returns what was newly enqueued.

        ``force`` bypasses the cadence check only; it does not bypass anything that
        protects the account, because a scanner has nothing like that to bypass.
        """
        now = self._clock()
        if not force and not self.is_due(now):
            return []
        self._last_poll = now

        emitted: list[Signal] = []
        for source in self._config.sources:
            for item in self._fetcher(source):
                emitted.extend(self._handle(source, item, now))
        return emitted

    def _enqueue(self, signal: Signal) -> Optional[Signal]:
        return signal if self._queue.put(signal) else None

    def _build(
        self,
        source: SourceConfig,
        item: RawItem,
        content: str,
        observed_at: datetime,
        classification: Optional[Classification] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> Signal:
        return Signal(
            signal_id=signal_id_for(source.id, item.external_id, content),
            source_id=source.id,
            signal_class=self.signal_class,
            observed_at=observed_at,
            content=content,
            # Priority comes from the class, never from the content.
            priority=Priority.for_class(self.signal_class),
            external_id=item.external_id,
            classification=classification,
            metadata=dict(item.fields) if metadata is None else metadata,
        )

    @abstractmethod
    def _handle(
        self, source: SourceConfig, item: RawItem, now: datetime
    ) -> Iterable[Signal]:
        """Turn one raw item into zero or more enqueued signals."""


class Class1RealtimeScanner(Scanner):
    """Trump posts and trade-call accounts. Polls every 60-120s during market hours.

    The only class where speed is genuine edge. Sources flagged with classification
    rules in ``signals.yaml`` go through the post classifier; sources without them
    (Trump posts) are emitted as-is for the research layer to score, because they are
    not trade calls and there is no forward/retrospective distinction to draw.
    """

    signal_class = SignalClass.CLASS_1_REALTIME

    def __init__(
        self,
        config: ClassConfig,
        fetcher: Fetcher,
        queue: SignalQueue,
        clock: Optional[Callable[[], datetime]] = None,
        credibility_log: Optional[CredibilityLog] = None,
    ) -> None:
        super().__init__(config, fetcher, queue, clock)
        # `is None`, not `or`: an empty log must not be swapped for a fresh one.
        self.credibility_log = (
            CredibilityLog() if credibility_log is None else credibility_log
        )

    def _handle(
        self, source: SourceConfig, item: RawItem, now: datetime
    ) -> Iterable[Signal]:
        if not source.classifies_posts:
            signal = self._build(
                source,
                item,
                item.content,
                now,
                metadata={**item.fields, "tickers": ",".join(extract_tickers(item.content))},
            )
            enqueued = self._enqueue(signal)
            return [enqueued] if enqueued else []

        return self._handle_trade_call(source, item, now)

    def _handle_trade_call(
        self, source: SourceConfig, item: RawItem, now: datetime
    ) -> list[Signal]:
        """Classify, split mixed posts, discard retrospectives."""
        result = classify_post(item.content)

        if result.retrospective_text:
            # Logged for credibility scoring whether or not the post also called
            # something live — the brag is evidence about the source either way.
            self.credibility_log.record(
                CredibilityRecord(
                    source_id=source.id,
                    observed_at=now,
                    content=result.retrospective_text,
                    external_id=item.external_id,
                    reason=", ".join(result.markers) or "historical content",
                )
            )

        if not result.is_actionable:
            # Retrospective or other: never reaches the research layer as an idea.
            return []

        # Mixed post: only the forward component is emitted. The historical half has
        # already gone to the credibility log and goes no further.
        content = result.forward_text or item.content
        signal = self._build(
            source,
            item,
            content,
            now,
            classification=Classification.FORWARD_CALL,
            metadata={
                **item.fields,
                "tickers": ",".join(result.tickers),
                "markers": ", ".join(result.markers),
                "treatment": source.treatment or "thesis_input_only",
                "copy_trade": "false",
            },
        )
        enqueued = self._enqueue(signal)
        return [enqueued] if enqueued else []


class Class2CongressionalScanner(Scanner):
    """Congressional disclosures, hourly.

    The STOCK Act allows up to 45 days between trade and disclosure, so the trade date
    is carried through as structured metadata. The research layer is required to reason
    about what has been priced in since that date, not since the disclosure.
    """

    signal_class = SignalClass.CLASS_2_MOMENTUM

    def _handle(
        self, source: SourceConfig, item: RawItem, now: datetime
    ) -> Iterable[Signal]:
        metadata = {
            **item.fields,
            "priced_in_analysis_required": "true",
            "copy_trade": "false",
            "disclosure_lag_note": "STOCK Act permits up to 45 days; evaluate from the "
            "trade date, not the disclosure date",
        }
        signal = self._build(source, item, item.content, now, metadata=metadata)
        enqueued = self._enqueue(signal)
        return [enqueued] if enqueued else []


class Class3Form13FScanner(Scanner):
    """13F filings, daily.

    Quarterly data with a 45-day lag, longs only, no visibility into exits between
    quarters. Carried as metadata so the research layer uses these for directional
    conviction and sector weighting, never for timing.
    """

    signal_class = SignalClass.CLASS_3_THESIS

    def _handle(
        self, source: SourceConfig, item: RawItem, now: datetime
    ) -> Iterable[Signal]:
        metadata = {
            **item.fields,
            "priced_in_analysis_required": "true",
            "longs_only": "true",
            "use_for": "directional_conviction,sector_weighting",
            "never_use_for": "timing",
        }
        signal = self._build(source, item, item.content, now, metadata=metadata)
        enqueued = self._enqueue(signal)
        return [enqueued] if enqueued else []


def build_scanners(
    config: SignalsConfig,
    fetcher: Fetcher,
    queue: SignalQueue,
    clock: Optional[Callable[[], datetime]] = None,
    credibility_log: Optional[CredibilityLog] = None,
) -> tuple[Class1RealtimeScanner, Class2CongressionalScanner, Class3Form13FScanner]:
    """All three scanners, wired from ``signals.yaml``."""
    return (
        Class1RealtimeScanner(
            config.klass("class_1"), fetcher, queue, clock, credibility_log
        ),
        Class2CongressionalScanner(config.klass("class_2"), fetcher, queue, clock),
        Class3Form13FScanner(config.klass("class_3"), fetcher, queue, clock),
    )
