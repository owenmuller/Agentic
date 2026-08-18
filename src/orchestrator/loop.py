"""The trading loop.

Poll the scanners, research what they queued, size it, gate it, send it, settle it,
write all of it down. One tick does each of those once.

Degrading rather than stopping
------------------------------
The loop is the only place in this system where a failure has somewhere to go. A
scanner whose feed is down, a research call that times out, a broker that returns a
500 — none of these are reasons to stop trading the other signals, so each is caught
where it happens, recorded, and stepped over. The cadence carries on.

Two failures are deliberately *not* survivable, and both leave through
``SignalPipeline``:

  ``BuyingPowerBreached``  a fill printed above what the gate reserved. Constraint #1
                           has been violated in reality; the gate has already tripped
                           the kill switch, and the loop stops so a human sees it now
                           rather than in a reconciliation later.
  ``AuditLogError``        the log refused a write. Continuing would mean trading
                           without a record of it, which is worse than not trading.

Everything else — a bug in the pipeline included — becomes a record and a skipped
signal.

Ordering
--------
Deferred signals go first, then new ones, and within that Class 1 outranks the rest.
Priority comes from the latency class and nothing else (``signals.Priority``): a post
claiming to be urgent is a post claiming something.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from audit.log import AuditLog
from execution.base import BrokerAdapter
from risk_gate.gate import RiskGate
from signals import Signal, SignalQueue
from signals.scanners import Scanner

from orchestrator.budget import ResearchBudget
from orchestrator.pipeline import PipelineResult, SignalPipeline
from orchestrator.state import SessionState

logger = logging.getLogger("orchestrator.loop")


@dataclass(slots=True)
class TickReport:
    """What one pass of the loop did. Returned for tests and for operator logging."""

    polled: int = 0
    scanner_failures: int = 0
    processed: list[PipelineResult] = field(default_factory=list)
    deferred: int = 0
    settled: int = 0
    halted: bool = False

    @property
    def traded(self) -> int:
        return sum(1 for result in self.processed if result.traded)


class TradingLoop:
    """Owns the cadence, the queue, and the shutdown."""

    def __init__(
        self,
        *,
        scanners: Sequence[Scanner],
        queue: SignalQueue,
        pipeline: SignalPipeline,
        budget: ResearchBudget,
        session: SessionState,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        tick_interval_seconds: int,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._scanners = tuple(scanners)
        self._queue = queue
        self._pipeline = pipeline
        self._budget = budget
        self._session = session
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._interval = tick_interval_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._deferred: list[Signal] = []
        self._running = False

    @property
    def pipeline(self) -> SignalPipeline:
        return self._pipeline

    @property
    def deferred(self) -> tuple[Signal, ...]:
        """Signals that were dequeued but not researched, oldest first."""
        return tuple(self._deferred)

    @property
    def is_running(self) -> bool:
        return self._running

    # -- one pass -----------------------------------------------------------------

    def tick(self) -> TickReport:
        """Poll, research, trade, settle, persist. Never raises for a fetcher or a bug."""
        report = TickReport()

        for scanner in self._scanners:
            try:
                emitted = scanner.poll()
            except Exception:  # noqa: BLE001 - a dead feed is not a dead loop
                report.scanner_failures += 1
                logger.exception(
                    "%s failed to poll; skipping its cycle",
                    type(scanner).__name__,
                )
                continue
            report.polled += len(emitted)

        pending = self._deferred + self._queue.drain()
        self._deferred = []
        # Class 1 first, then oldest first. Both come from the scanner, never from the
        # content of a post.
        pending.sort(key=lambda signal: (-int(signal.priority), signal.observed_at))

        for index, signal in enumerate(pending):
            if not self._budget.try_spend():
                # Deferred, not dropped: these are the first thing researched tomorrow.
                self._deferred = pending[index:]
                report.deferred = len(self._deferred)
                break
            report.processed.append(self._pipeline.process(signal))

        report.settled = len(self._pipeline.reconcile())
        report.halted = self._gate.kill_switch_tripped
        self._session.persist(self._gate, self._clock())

        if report.halted:
            logger.warning(
                "KILL SWITCH IS TRIPPED. Opening orders are halted; risk-reducing "
                "closes still pass. Resuming requires a manual human reset."
            )
        return report

    # -- running ------------------------------------------------------------------

    def run(self, max_ticks: Optional[int] = None) -> list[TickReport]:
        """Tick until stopped, or until ``max_ticks`` have run.

        ``max_ticks`` exists so tests can drive a bounded number of passes. In
        production it is None and the loop runs until ``stop()``.
        """
        self._running = True
        reports: list[TickReport] = []
        try:
            while self._running and (max_ticks is None or len(reports) < max_ticks):
                reports.append(self.tick())
                if self._running and (max_ticks is None or len(reports) < max_ticks):
                    self._sleep(self._interval)
        finally:
            self._running = False
        return reports

    def stop(self) -> None:
        """Ask the loop to finish its current tick and return."""
        self._running = False

    # -- shutdown -----------------------------------------------------------------

    def shutdown(self) -> TickReport:
        """Finish in-flight work and leave the account in a state a restart can read.

        Settle whatever the broker has already finished with, then cancel and account
        for whatever it has not. Nothing is left working: the reservation behind a
        working order lives in an ``ApprovedOrder``, which cannot outlive this process,
        so an order left resting would be exposure no future gate knows about.
        """
        self._running = False
        report = TickReport()
        report.settled = len(self._pipeline.reconcile())
        report.settled += len(self._pipeline.cancel_working())
        report.halted = self._gate.kill_switch_tripped
        self._session.persist(self._gate, self._clock())
        logger.info(
            "shutdown complete: %d orders settled or released, %d signals still "
            "queued for tomorrow, kill switch %s",
            report.settled,
            len(self._deferred),
            "TRIPPED" if report.halted else "clear",
        )
        return report

    def __enter__(self) -> "TradingLoop":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
