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

from pathlib import Path

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Callable, Optional, Sequence

from audit.log import AuditLog
from execution.base import BrokerAdapter
from risk_gate.gate import RiskGate
from signals import Signal, SignalQueue
from signals.scanners import Scanner

from orchestrator.budget import ResearchBudget
from orchestrator.exits import ExitEngine
from orchestrator.pipeline import PipelineResult, SignalPipeline
from orchestrator.prefilter import ResearchPreFilter

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from orchestrator.ops import CostMeter
from orchestrator.state import SessionState

logger = logging.getLogger("orchestrator.loop")


@dataclass(slots=True)
class TickReport:
    """What one pass of the loop did. Returned for tests and for operator logging."""

    polled: int = 0
    scanner_failures: int = 0
    processed: list[PipelineResult] = field(default_factory=list)
    deferred: int = 0
    #: Signals recorded as pre_filter rejections this tick — arrived, written down,
    #: not worth a research pass.
    prefiltered: int = 0
    #: Signals the triage gate stopped this tick — ~$0.02 each, no pass spent.
    triaged_out: int = 0
    settled: int = 0
    #: Thesis reviews run this tick (each spent one research pass).
    reviews_run: int = 0
    #: Exit orders that went out this tick, both guardrail and review-driven.
    exits_started: int = 0
    #: Positions fully closed this tick — each wrote an OutcomeRecord.
    positions_closed: int = 0
    #: Mechanical sleeve activity this tick (ruling 2026-08-27).
    mechanical_entries: int = 0
    mechanical_exits: int = 0
    #: Filer events recorded this tick, both arms (ruling 2026-09-01): new
    #: disclosures by originating filers in held names.
    filer_events: int = 0
    #: Cash-management sweep/unsweep orders placed this tick (ruling 2026-09-02).
    sweep_orders: int = 0
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
        exits: ExitEngine,
        prefilter: Optional[ResearchPreFilter],
        registry: Optional[object] = None,
        cost_meter: Optional["CostMeter"] = None,
        error_sink: Optional[Callable[[str], None]] = None,
        source_caps: Optional[dict[str, int]] = None,
        source_passes: Optional[dict[str, int]] = None,
        source_pass_day: Optional[date] = None,
        previously_capped: Optional[set[tuple[str, str]]] = None,
        mechanical: Optional[object] = None,
        sweeper: Optional[object] = None,
        budget: ResearchBudget,
        session: SessionState,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        tick_interval_seconds: int,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        halt_marker: Optional[Path] = None,
        halt_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._scanners = tuple(scanners)
        self._queue = queue
        self._pipeline = pipeline
        self._exits = exits
        self._prefilter = prefilter
        #: The convergence registry (ruling 2026-09-01). Ordering and context
        #: only: its bonus joins the dispatch sort, never a cap or a size.
        self._registry = registry
        self._cost_meter = cost_meter
        self._error_sink = error_sink
        #: The panic button (ruling 2026-09-02): a marker file `orchestrator
        #: halt` writes from another process. Present at the top of a tick =
        #: trip the kill switch, cancel every working order, say so. Sticky
        #: through the session file; only `orchestrator resume` removes it.
        self._halt_marker = halt_marker
        self._halt_sink = halt_sink
        #: Per-source daily caps (2026-08-25): counts seed from the audit log at
        #: startup so a restart cannot reset a cap, and roll at the day boundary.
        self._source_caps = dict(source_caps or {})
        self._source_passes = dict(source_passes or {})
        self._source_pass_day = source_pass_day
        #: Signals that lost a research slot (capped, or budget-deferred this
        #: process). Seeded from the audit log so a restart remembers. When
        #: the staleness rule later kills one of these, the rejection carries
        #: code aged_out_capped instead of pre_filter: the cap cost an
        #: evaluation, and that is a tuning signal, not routine staleness.
        self._slot_losers: set[tuple[str, str]] = set(previously_capped or ())
        #: The mechanical disclosure follower (ruling 2026-08-27). None when
        #: the sleeve weight is zero — the whole arm switches off in config.
        self._mechanical = mechanical
        #: The idle-cash yield sweeper (ruling 2026-09-02). None when
        #: cash_management.enabled is false.
        self._sweeper = sweeper
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
    def exits(self) -> ExitEngine:
        return self._exits

    @property
    def mechanical(self):
        return self._mechanical

    @property
    def sweeper(self):
        return self._sweeper

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

        # Operator halt, before anything else this tick can do: the marker is
        # the one channel another process has into this one.
        if (
            self._halt_marker is not None
            and self._halt_marker.exists()
            and not self._gate.kill_switch_tripped
        ):
            self._gate.trip_kill_switch("operator HALT marker present")
            released = len(self._pipeline.cancel_working())
            released += len(self._exits.cancel_working())
            if self._mechanical is not None:
                released += len(self._mechanical.cancel_working())
            if self._sweeper is not None:
                released += len(self._sweeper.cancel_working())
            report.settled += released
            self._session.persist(self._gate, self._clock())
            message = (
                f"operator HALT honoured: kill switch tripped, {released} working "
                f"order(s) cancelled; risk-reducing closes keep running"
            )
            logger.error(message)
            if self._halt_sink is not None:
                try:
                    self._halt_sink(message)
                except Exception:  # noqa: BLE001 - a sink must not kill the halt
                    logger.exception("halt sink failed")

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
        # The convergence registry sees the batch BEFORE the sort, so same-day
        # convergence counts; bonuses exclude the signal's own identity, so
        # nothing converges with itself (ruling 2026-09-01).
        if self._registry is not None:
            self._registry.note_signals(pending)
        # Class 1 first; within a class, the dispatch weight (ruling
        # 2026-08-26: log10(amount) - age/7, scanner-computed from structured
        # feed fields, 0 everywhere but congressional) plus the registry's
        # convergence bonus (ruling 2026-09-01: cross-filer cluster +
        # source diversity, capped) decides who spends limited slots first;
        # then oldest first; ties keep arrival order (stable sort). Every key
        # comes from the scanner, the feed's structured fields, or the
        # system's own records — never from the content of a post.
        def _dispatch_rank(signal: Signal) -> float:
            bonus = (
                float(self._registry.bonus_for(signal))
                if self._registry is not None
                else 0.0
            )
            return signal.dispatch_weight + bonus

        pending.sort(
            key=lambda signal: (
                -int(signal.priority),
                -_dispatch_rank(signal),
                signal.observed_at,
            )
        )

        held = self._exits.held_symbols()
        dispatch_now = self._clock()
        # Filer events first (ruling 2026-09-01), on the raw drained queue: a new
        # disclosure by an originating filer in a held name must reach that
        # position's review — and the mechanical audit trail — regardless of
        # what the entry funnel decides about the same signal below.
        report.filer_events = self._exits.note_disclosures(pending)
        if self._mechanical is not None:
            report.filer_events += self._mechanical.note_disclosures(pending)
        # The mechanical arm OBSERVES the same drained signals before judged
        # dispatch — it must never consume them: both arms see every signal,
        # which is what makes the experiment's comparison valid.
        if self._mechanical is not None:
            report.mechanical_entries = self._mechanical.consider(
                pending, dispatch_now
            )
        for index, signal in enumerate(pending):
            # The pre-filter runs BEFORE the budget: a signal the deterministic
            # rules can already dismiss is written down, not paid for.
            if self._prefilter is not None:
                # Cheapest rule first: a call that names nothing tradeable is
                # commentary, whatever the research verdict would have been.
                missing = self._prefilter.missing_instrument(signal)
                if missing is not None:
                    self._pipeline.record_prefiltered(
                        signal, missing, code="no_instrument"
                    )
                    report.prefiltered += 1
                    continue
                bare = self._prefilter.bare_link(signal)
                if bare is not None:
                    self._pipeline.record_prefiltered(signal, bare, code="bare_link")
                    report.prefiltered += 1
                    continue
                verdict = self._prefilter.skip_verdict(
                    signal, held=held, now=dispatch_now
                )
                if verdict is not None:
                    reason, rule = verdict
                    # Form 4 singles get their own code (ruling 2026-09-02): the
                    # forward report compares them against clustered entries to
                    # test the cluster rule itself. Bearish measurement rows
                    # (sell clusters) likewise carry their own code.
                    code = {
                        "cluster": "no_cluster",
                        "measurement": "bearish_measurement",
                    }.get(rule, "pre_filter")
                    if (
                        rule == "report_staleness"
                        and signal.external_id
                        and (signal.source_id, signal.external_id)
                        in self._slot_losers
                    ):
                        # The guillotine fell on a signal the cap (or a budget
                        # deferral) had already turned away: the cap cost an
                        # evaluation. Distinct code so the human tunes on it
                        # instead of inferring it (ruling 2026-08-26).
                        code = "aged_out_capped"
                        reason += (
                            " — and this signal previously lost a research "
                            "slot to the daily cap or a budget deferral: it "
                            "aged out unevaluated"
                        )
                    self._pipeline.record_prefiltered(signal, reason, code=code)
                    report.prefiltered += 1
                    continue
            # The judged arm's off switch (2026-08-27). Before triage and the
            # budget: an arm that cannot open a position must not spend a
            # dollar deciding what it would have opened. Recorded, never
            # sealed — the backlog re-emits when it is switched back on.
            if not self._gate.limits.equity_sleeve.entries_enabled:
                self._pipeline.record_prefiltered(
                    signal,
                    "judged entries are switched off in risk_limits.yaml "
                    "(equity_sleeve.entries_enabled); recorded, not researched, "
                    "and exits keep running",
                    code="entries_disabled",
                )
                report.prefiltered += 1
                continue
            # Per-source daily cap (2026-08-25): after the content rules so the
            # rejection code stays precise, before triage so a capped source
            # spends nothing further today.
            cap = self._source_caps.get(signal.source_id)
            if cap is not None:
                if self._source_pass_day != dispatch_now.date():
                    self._source_pass_day = dispatch_now.date()
                    self._source_passes = {}
                if self._source_passes.get(signal.source_id, 0) >= cap:
                    self._pipeline.record_prefiltered(
                        signal,
                        f"{signal.source_id} has spent its {cap}-pass daily cap; "
                        f"recorded, not researched",
                        code="source_cap",
                    )
                    if signal.external_id:
                        self._slot_losers.add(
                            (signal.source_id, signal.external_id)
                        )
                    report.prefiltered += 1
                    continue
            # The triage gate: dollars (metered) but never a budget pass. A no
            # writes the trail and stops here; a yes or a gate failure proceeds
            # exactly as before. Runs BEFORE the budget so a gated signal cannot
            # waste a pass; a deferred signal is re-triaged tomorrow (~$0.02).
            triaged = self._pipeline.triage_gate(signal)
            if triaged is not None:
                report.triaged_out += 1
                if self._cost_meter is not None and triaged.rejection is not None:
                    self._cost_meter.add(triaged.rejection.est_cost_usd)
                continue
            if not self._budget.try_spend():
                # Deferred, not dropped: these are the first thing researched tomorrow.
                self._deferred = pending[index:]
                for waiting in self._deferred:
                    if waiting.external_id:
                        self._slot_losers.add(
                            (waiting.source_id, waiting.external_id)
                        )
                report.deferred = len(self._deferred)
                break
            self._source_passes[signal.source_id] = (
                self._source_passes.get(signal.source_id, 0) + 1
            )
            result = self._pipeline.process(signal)
            report.processed.append(result)
            self._note_verdict(signal, result)
            if (
                self._error_sink is not None
                and result.rejection is not None
                and result.rejection.code == "upstream_error"
            ):
                # A failed research CALL is a typed rejection to the pipeline but
                # an ERROR to the operator: five of these in a row is a broken
                # API contract, and it must show in run.log / health, not sit
                # silently in the audit trail (2026-08-24 incident: every pass
                # 400ed for three sessions and health said 'last error: none').
                self._error_sink(
                    f"research upstream_error decision={result.decision_id}: "
                    f"{result.rejection.message[:200]}"
                )
            if self._cost_meter is not None:
                # One cost per pass: when both records exist (an execution
                # rejection after the decision) they carry the same estimate,
                # so the decision record wins and the pass bills once.
                record = result.decision or result.rejection
                self._cost_meter.add(getattr(record, "est_cost_usd", None))

        report.settled = len(self._pipeline.reconcile())

        # Exits, after entries have settled so a position opened this tick is already
        # tracked. Reconcile first (yesterday's exit orders), then the deterministic
        # guardrails — which also mark to market and can trip the kill switch — then
        # the budgeted thesis reviews. Guardrails before reviews on purpose: the layer
        # that always works goes first.
        now = self._clock()
        report.positions_closed = len(self._exits.reconcile())
        guardrail_exits = self._exits.check_guardrails(now)
        reviews_run, review_exits = self._exits.review_theses(now)
        report.reviews_run = reviews_run
        report.exits_started = len(guardrail_exits) + review_exits

        if self._mechanical is not None:
            mechanical_report = self._mechanical.tick(now)
            report.settled += mechanical_report.settled
            report.mechanical_exits = mechanical_report.exits_started
            self._session.capture_mechanical(self._mechanical, now)

        # The sweep runs LAST, after every trading decision this tick has made
        # its reservations — the buffer it defends includes them, so ordering
        # it after the entries is what keeps it from racing them.
        if self._sweeper is not None:
            report.sweep_orders = self._sweeper.tick(now)

        report.halted = self._gate.kill_switch_tripped
        self._session.capture_exits(self._exits)
        self._session.persist(self._gate, now)
        self._write_iv_watch()

        if report.halted:
            logger.warning(
                "KILL SWITCH IS TRIPPED. Opening orders are halted; risk-reducing "
                "closes still pass. Resuming requires a manual human reset."
            )
        return report

    def _write_iv_watch(self) -> None:
        """Hand the earnings shadow logger the names worth an IV history
        (ruling 2026-09-02): the book first, then the funnel's recent names.
        A plain file, never an import — the logger stays a leaf. A write
        failure costs the widening, never the tick."""
        try:
            held: list[str] = []
            for position in self._exits.tracked:
                name = position.symbol
                if position.is_option and position.entry_order is not None:
                    name = position.entry_order.underlying
                held.append(name.upper())
            if self._mechanical is not None:
                held.extend(p.symbol.upper() for p in self._mechanical.tracked)
            funnel: list[str] = []
            if self._registry is not None:
                funnel = [s.upper() for s in self._registry.in_window_symbols()]
            symbols = list(dict.fromkeys(held + sorted(set(funnel) - set(held))))
            path = self._session.path.parent / "iv_watch.json"
            path.write_text(
                json.dumps({"symbols": symbols[:60]}), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 - a watch-file bug must not kill the loop
            logger.exception(
                "iv_watch.json write failed; the shadow logger keeps its "
                "configured universe"
            )

    def _note_verdict(self, signal: Signal, result: PipelineResult) -> None:
        """Feed the convergence registry this session's verdicts, so a second
        source arriving an hour after a decline is shown that decline."""
        if self._registry is None:
            return
        if result.traded:
            confidence = (
                result.decision.research.confidence
                if result.decision is not None and result.decision.research
                else None
            )
            self._registry.note_outcome(signal, "traded", confidence)
        elif result.decision is not None and not result.decision.was_approved:
            self._registry.note_outcome(
                signal,
                "gate_rejected",
                (
                    result.decision.research.confidence
                    if result.decision.research
                    else None
                ),
                code=result.decision.gate.rejection_code or "",
            )
        elif result.rejection is not None and result.rejection.stage.value in (
            "sizing",
            "order_construction",
        ):
            self._registry.note_outcome(
                signal,
                "declined",
                (
                    result.rejection.research.confidence
                    if result.rejection.research
                    else None
                ),
                code=result.rejection.code,
            )

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
        report.positions_closed = len(self._exits.reconcile())
        report.settled += len(self._exits.cancel_working())
        if self._mechanical is not None:
            report.settled += len(self._mechanical.cancel_working())
            self._session.capture_mechanical(self._mechanical, self._clock())
        if self._sweeper is not None:
            report.settled += len(self._sweeper.cancel_working())
        report.halted = self._gate.kill_switch_tripped
        self._session.capture_exits(self._exits)
        self._session.persist(self._gate, self._clock())
        logger.info(
            "shutdown complete: %d orders settled or released, %d positions still "
            "open (replayed from the log at next startup), %d signals still queued "
            "for tomorrow, kill switch %s",
            report.settled,
            len(self._exits.tracked),
            len(self._deferred),
            "TRIPPED" if report.halted else "clear",
        )
        return report

    def __enter__(self) -> "TradingLoop":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
