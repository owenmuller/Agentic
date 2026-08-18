"""One signal, all the way through, with a record at every stage.

The stages are research -> sizing -> order construction -> risk gate -> broker, and a
signal can stop at any of them. Every stop writes a record. That is not tidiness: a
system that logs only what it traded cannot answer the questions worth asking of it —
how much of this source is unresearchable, how often does sizing decline what research
liked, what did the caps cost us this month. CLAUDE.md says risk-gate rejections are
signal and not noise, and the same is true one stage earlier.

The ``decision_id`` is allocated when the signal is dequeued, before anything can fail,
so a signal that dies in the first stage is still followable by the same key as one
that trades.

What this pipeline will and will not build
------------------------------------------
It opens long equity positions, and nothing else. That is not a placeholder that got
left in — it is where CLAUDE.md's build order actually is, and each of the other paths
is blocked on a component that does not exist:

  ``short_via_puts``  needs a contract off an options chain — expiry, strike, the OCC
                      symbol. There is no chain source, and picking a contract is a
                      sizing-relevant decision in its own right, not a detail.
  event contracts     need Kalshi, which the build order puts after the equity leg has
                      proved itself in paper.
  exits               ``invalidation_condition`` is meant to feed automated exit logic.
                      Evaluating a natural-language condition against a live position is
                      a research-layer job that has not been built, so this loop opens
                      positions and reconciles them; it does not yet close them.

Each of those writes an ``order_construction`` rejection naming the reason rather than
silently ignoring the report. A signal that could not be acted on is a fact about the
system, and the log should be able to say how often it happened.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Optional, Protocol

from audit.log import AuditLog, AuditLogError
from audit.records import DecisionRecord, RejectedStage, StageRejectionRecord
from execution.base import BrokerAdapter, BrokerError, OrderReceipt
from research.reports import Direction, ResearchReport
from research.research_pass import ResearchPass
from risk_gate.gate import ApprovedOrder, BuyingPowerBreached, RiskGate
from risk_gate.schema import EquityBuyOrder, LimitExecution
from risk_gate.state import Sleeve, units_of
from signals import Signal
from sizing.engine import SizedProposal, SizingEngine

ZERO = Decimal("0")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.pipeline")


class PriceSource(Protocol):
    """Per-unit price to bound a buy with, or None when no usable price is available.

    A seam, in the same spirit as ``signals.scanners.Fetcher``: the concrete market-data
    client needs credentials this machine does not have, and an HTTP client nothing can
    exercise is worse than an honest interface. Implementations return the price the
    order should be *bounded* at — the offer for a buy, not the last trade — because
    that is the number the risk gate cash-secures against.

    Returning None is a normal answer, not an error. A stale or missing quote should
    produce no order rather than an order priced on a guess.
    """

    def __call__(self, symbol: str) -> Optional[Decimal]: ...


@dataclass(frozen=True, slots=True)
class WorkingOrder:
    """An approved order the broker has accepted and not yet finished with."""

    decision_id: str
    approved: ApprovedOrder
    receipt: OrderReceipt
    signal: Signal
    report: ResearchReport
    proposal: SizedProposal


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Where one signal got to, and what was written about it."""

    decision_id: str
    signal_id: str
    stage_reached: str
    traded: bool
    decision: Optional[DecisionRecord] = None
    rejection: Optional[StageRejectionRecord] = None
    receipt: Optional[OrderReceipt] = None


class SignalPipeline:
    """Runs one signal through every stage, and settles what the broker does next."""

    def __init__(
        self,
        *,
        research: ResearchPass,
        sizing: SizingEngine,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: PriceSource,
        id_factory: Optional[Callable[[], str]] = None,
        fill_sink: Optional[Callable[["WorkingOrder", int, Decimal], None]] = None,
    ) -> None:
        self._research = research
        self._sizing = sizing
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:16])
        #: Told about every settled opening fill — this is how the exit engine learns
        #: a position exists without the pipeline knowing what an exit engine is.
        self._fill_sink = fill_sink
        self._working: dict[str, WorkingOrder] = {}

    @property
    def working_orders(self) -> tuple[WorkingOrder, ...]:
        return tuple(self._working.values())

    # -- the pipeline ----------------------------------------------------------------

    def record_prefiltered(self, signal: Signal, reason: str) -> PipelineResult:
        """Write the trail for a signal the pre-filter kept from research.

        No budget was spent and no model was called; the record is the whole point —
        every post that arrived and was not researched stays readable, with the
        reason, so the filter itself can be audited against what it skipped.
        """
        decision_id = self._id_factory()
        rejection = self._audit.record_stage_rejection(
            decision_id,
            RejectedStage.PRE_FILTER,
            "pre_filter",
            reason,
            signal,
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached=str(RejectedStage.PRE_FILTER),
            traded=False,
            rejection=rejection,
        )

    def process(self, signal: Signal) -> PipelineResult:
        """Research, size, build, submit. Returns where it stopped."""
        decision_id = self._id_factory()
        try:
            return self._process(decision_id, signal)
        except (BuyingPowerBreached, AuditLogError):
            # Constraint #1 violated in reality, or the log itself failed. Neither is a
            # verdict about this signal, and neither is survivable: trading on without
            # an audit trail is worse than stopping.
            raise
        except Exception as error:  # noqa: BLE001 - a bug must not become a trade
            logger.exception("pipeline failed on signal %s", signal.signal_id)
            rejection = self._audit.record_stage_rejection(
                decision_id,
                RejectedStage.INTERNAL_ERROR,
                type(error).__name__,
                f"pipeline raised: {error}",
                signal,
            )
            return PipelineResult(
                decision_id=decision_id,
                signal_id=signal.signal_id,
                stage_reached=str(RejectedStage.INTERNAL_ERROR),
                traded=False,
                rejection=rejection,
            )

    def _process(self, decision_id: str, signal: Signal) -> PipelineResult:
        # 1. Research.
        outcome = self._research.run(signal)
        if not isinstance(outcome, ResearchReport):
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.RESEARCH,
                str(outcome.code),
                outcome.message,
            )
        report = outcome

        # 2. Sizing. Sub-floor confidence and a no_position verdict both land here.
        sleeve_nav = self._gate.sleeve_nav(Sleeve.EQUITY)
        proposal = self._sizing.propose_equity(report, sleeve_nav)
        if not proposal.is_tradeable:
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.SIZING,
                "no_position" if report.recommends_no_position else "below_floor",
                proposal.rationale,
                report=report,
                proposal=proposal,
            )

        # 3. Order construction.
        order, problem = self._build_order(signal, report, proposal)
        if order is None:
            code, message = problem  # type: ignore[misc]
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.ORDER_CONSTRUCTION,
                code,
                message,
                report=report,
                proposal=proposal,
            )

        # 4. The risk gate. Approved or rejected, this writes the full decision record.
        decision = self._gate.submit(order)
        record = self._audit.record_decision(
            signal, report, proposal, decision, decision_id=decision_id
        )
        if not decision.is_approved:
            return PipelineResult(
                decision_id=decision_id,
                signal_id=signal.signal_id,
                stage_reached="risk_gate",
                traded=False,
                decision=record,
            )

        # 5. The broker.
        approved: ApprovedOrder = decision
        try:
            receipt = self._adapter.submit_order(approved)
        except BrokerError as error:
            # Release what the approval reserved. Without this the cash stays committed
            # to an order that does not exist anywhere.
            self._gate.cancel(approved)
            logger.warning(
                "broker refused %s for signal %s: %s",
                decision_id,
                signal.signal_id,
                error,
            )
            rejection = self._audit.record_stage_rejection(
                decision_id,
                RejectedStage.EXECUTION,
                type(error).__name__,
                str(error),
                signal,
                report=report,
                proposal=proposal,
            )
            return PipelineResult(
                decision_id=decision_id,
                signal_id=signal.signal_id,
                stage_reached=str(RejectedStage.EXECUTION),
                traded=False,
                decision=record,
                rejection=rejection,
            )

        self._working[receipt.broker_order_id] = WorkingOrder(
            decision_id=decision_id,
            approved=approved,
            receipt=receipt,
            signal=signal,
            report=report,
            proposal=proposal,
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached="broker",
            traded=True,
            decision=record,
            receipt=receipt,
        )

    # -- order construction ------------------------------------------------------------

    def _build_order(
        self, signal: Signal, report: ResearchReport, proposal: SizedProposal
    ) -> tuple[Optional[EquityBuyOrder], Optional[tuple[str, str]]]:
        """Turn a dollar figure into a concrete order, or explain why not."""
        if report.direction is not Direction.LONG:
            return None, (
                "instrument_not_supported",
                f"direction {report.direction} needs an options chain to express as a "
                f"bought put; no contract-selection source is built, so no order is "
                f"placed. See this module's docstring.",
            )

        if len(report.tickers) != 1:
            # Constraint #6: where a spec admits more than one reading, take the fewer
            # trades and surface the ambiguity rather than silently picking one.
            return None, (
                "ambiguous_instrument",
                f"report names {len(report.tickers)} tickers ({', '.join(report.tickers) or 'none'}); "
                f"one sized proposal cannot be split across them and choosing one would "
                f"be a guess, so no order is placed",
            )

        symbol = report.tickers[0]
        quote = self._prices(symbol)
        if quote is None or quote <= ZERO:
            return None, (
                "no_price",
                f"no usable price for {symbol}; an order priced on a guess is an order "
                f"the gate would cash-secure against a guess",
            )

        # Round the bound UP. It is the worst case the gate reserves against and the
        # limit the broker is sent, so rounding it down would shave the protection.
        limit_price = quote.quantize(CENTS, rounding=ROUND_UP)
        quantity = int((proposal.capital / limit_price).to_integral_value(ROUND_DOWN))
        if quantity < 1:
            return None, (
                "size_below_one_unit",
                f"{proposal.capital} at {limit_price} buys no whole shares of {symbol}",
            )

        return (
            EquityBuyOrder(
                symbol=symbol,
                quantity=quantity,
                execution=LimitExecution(limit_price=limit_price),
                signal_id=signal.signal_id,
                confidence=report.confidence,
            ),
            None,
        )

    # -- settlement --------------------------------------------------------------------

    def reconcile(self) -> list[str]:
        """Poll working orders and settle the ones the broker has finished with.

        Only terminal orders are settled. A partially filled order that is still working
        may yet fill the rest, and settling it early would release a reservation the
        remainder still needs.
        """
        settled: list[str] = []
        for order_id, working in list(self._working.items()):
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                # Not knowing is not the same as nothing having happened. Leave it
                # working and try again next tick.
                logger.warning("could not poll order %s: %s", order_id, error)
                continue
            if not status.is_terminal:
                continue
            self._settle(working, status.status, status.filled_quantity, status.filled_avg_price)
            settled.append(order_id)
        return settled

    def _settle(
        self,
        working: WorkingOrder,
        status: str,
        filled_quantity: Decimal,
        filled_avg_price: Optional[Decimal],
    ) -> None:
        """Book a terminal order and release it from the working set."""
        del self._working[working.receipt.broker_order_id]
        filled = int(filled_quantity)

        if filled <= 0 or filled_avg_price is None:
            self._gate.cancel(working.approved)
            self._audit.record_stage_rejection(
                working.decision_id,
                RejectedStage.EXECUTION,
                status,
                f"order terminated {status} without filling; reservation released",
                working.signal,
                report=working.report,
                proposal=working.proposal,
            )
            return

        # May raise BuyingPowerBreached, which trips the kill switch and is meant to
        # reach a human immediately. The loop lets it out.
        self._gate.record_fill(working.approved, filled_avg_price, filled_units=filled)
        self._audit.record_fill(
            working.decision_id,
            working.receipt.broker_order_id,
            Decimal(filled),
            filled_avg_price,
        )
        if self._fill_sink is not None:
            self._fill_sink(working, filled, filled_avg_price)
        ordered = units_of(working.approved.order)
        if filled < ordered:
            # A partial fill is a fact about the order, and the decision record says a
            # larger quantity was approved. Say so, rather than leaving the difference
            # to be inferred from two records that do not agree.
            self._audit.record_stage_rejection(
                working.decision_id,
                RejectedStage.EXECUTION,
                status,
                f"filled {filled} of {ordered} before "
                f"terminating {status}; the balance was released",
                working.signal,
                report=working.report,
                proposal=working.proposal,
            )

    def cancel_working(self) -> list[str]:
        """Cancel everything still working and account for it. Used on shutdown.

        Leaving a working order behind is not an option, however tempting it looks. The
        reservation protecting it lives in an ``ApprovedOrder``, which is unforgeable
        by design and therefore cannot be reconstructed by the next process — so an
        order left resting would be one no future gate knows it is exposed to. If the
        cancel itself fails, the order is still released here and the next startup
        picks up any fill from the broker's own positions, which is what seeds the
        account state anyway.
        """
        released: list[str] = []
        for order_id, working in list(self._working.items()):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error(
                    "could not cancel working order %s on shutdown: %s. It may still "
                    "fill; the next startup will pick it up from broker positions.",
                    order_id,
                    error,
                )
            status = None
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.error("could not re-poll %s after cancelling: %s", order_id, error)

            if status is not None and status.is_terminal:
                self._settle(
                    working, status.status, status.filled_quantity, status.filled_avg_price
                )
            else:
                self._gate.cancel(working.approved)
                del self._working[order_id]
                self._audit.record_stage_rejection(
                    working.decision_id,
                    RejectedStage.EXECUTION,
                    "released_at_shutdown",
                    "reservation released at shutdown with the order not confirmed "
                    "terminal; broker state is authoritative at next startup",
                    working.signal,
                    report=working.report,
                    proposal=working.proposal,
                )
            released.append(order_id)
        return released

    # -- internals -----------------------------------------------------------------------

    def _stopped(
        self,
        decision_id: str,
        signal: Signal,
        stage: RejectedStage,
        code: str,
        message: str,
        report: Optional[ResearchReport] = None,
        proposal: Optional[SizedProposal] = None,
    ) -> PipelineResult:
        rejection = self._audit.record_stage_rejection(
            decision_id, stage, code, message, signal, report=report, proposal=proposal
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached=str(stage),
            traded=False,
            rejection=rejection,
        )
