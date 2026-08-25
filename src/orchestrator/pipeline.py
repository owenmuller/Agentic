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
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Optional, Protocol

from audit.log import AuditLog, AuditLogError
from audit.records import (
    DecisionRecord,
    ExpressionSnapshot,
    NearMissSnapshot,
    RejectedStage,
    StageRejectionRecord,
)
from execution.base import BrokerAdapter, BrokerError, OrderReceipt
from research.reports import Direction, ResearchReport, ResearchUsage
from research.research_pass import ResearchPass
from research.triage import TriagePass


def _combine_usage(
    first: Optional[ResearchUsage], second: Optional[ResearchUsage]
) -> Optional[ResearchUsage]:
    """Sum triage and full-pass usage into one estimate for the record.

    Costs only add when both are priced; one unpriced side keeps the priced
    side's number rather than erasing it.
    """
    if first is None:
        return second
    if second is None:
        return first
    if first.cost_usd is None:
        cost = second.cost_usd
    elif second.cost_usd is None:
        cost = first.cost_usd
    else:
        cost = first.cost_usd + second.cost_usd
    return ResearchUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cost_usd=cost,
    )
from risk_gate.gate import ApprovedOrder, BuyingPowerBreached, RiskGate
from risk_gate.schema import EquityBuyOrder, LimitExecution, OptionBuyToOpenOrder
from risk_gate.state import Sleeve, unit_multiplier, units_of
from sizing.selection import (
    FallbackReason,
    OptionFallback,
    OptionSelector,
    SelectedOption,
)
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


def _selected_snapshot(selection: SelectedOption) -> ExpressionSnapshot:
    quote = selection.quote
    return ExpressionSnapshot(
        considered=True,
        chosen="option",
        contract_symbol=quote.occ_symbol,
        delta=quote.delta,
        iv_percentile=selection.iv_percentile,
        expiration=quote.expiration.isoformat(),
        open_interest=quote.open_interest,
        spread_pct=quote.spread_pct,
    )


def _fallback_snapshot(fallback: OptionFallback, chosen: str) -> ExpressionSnapshot:
    near_miss = fallback.near_miss
    if near_miss is not None and not isinstance(near_miss, NearMissSnapshot):
        near_miss = NearMissSnapshot(
            occ_symbol=near_miss.occ_symbol,
            delta=near_miss.delta,
            open_interest=near_miss.open_interest,
            spread_pct=near_miss.spread_pct,
            killed_by=near_miss.killed_by,
        )
    return ExpressionSnapshot(
        considered=True,
        chosen=chosen,
        fallback_reason=str(fallback.reason),
        detail=fallback.detail,
        near_miss=near_miss,
    )


class SignalPipeline:
    """Runs one signal through every stage, and settles what the broker does next."""

    def __init__(
        self,
        *,
        research: ResearchPass,
        triage: Optional["TriagePass"] = None,
        sizing: SizingEngine,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: PriceSource,
        id_factory: Optional[Callable[[], str]] = None,
        fill_sink: Optional[Callable[["WorkingOrder", Decimal, Decimal], None]] = None,
        options_chain=None,
        option_selector: Optional[OptionSelector] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._research = research
        self._triage = triage
        self._pending_triage_usage: Optional[ResearchUsage] = None
        self._sizing = sizing
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        #: The options expression seam (2026-08-24). Both None = equity-only
        #: pipeline, byte-identical to the pre-options behaviour — which is what
        #: every harness without a chain gets.
        self._options_chain = options_chain
        self._option_selector = option_selector
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:16])
        #: Told about every settled opening fill — this is how the exit engine learns
        #: a position exists without the pipeline knowing what an exit engine is.
        self._fill_sink = fill_sink
        self._working: dict[str, WorkingOrder] = {}

    @property
    def working_orders(self) -> tuple[WorkingOrder, ...]:
        return tuple(self._working.values())

    # -- the pipeline ----------------------------------------------------------------

    def triage_gate(self, signal: Signal) -> Optional[PipelineResult]:
        """Run the cheap triage gate. A no writes the trail and returns the
        result (the full pass never starts, the pass budget is never spent);
        a yes — or any gate failure, which fails open — returns None and the
        caller proceeds exactly as before. The triage call's own cost rides on
        the rejection record (a no) or is folded into the full pass's usage
        (a yes) so the cost meter sees every dollar either way.
        """
        if self._triage is None:
            return None
        outcome = self._triage.run(signal)
        if outcome.proceed:
            self._pending_triage_usage = outcome.usage
            return None
        decision_id = self._id_factory()
        rejection = self._audit.record_stage_rejection(
            decision_id,
            RejectedStage.TRIAGE,
            "triage",
            outcome.reason,
            signal,
            usage=outcome.usage,
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached=str(RejectedStage.TRIAGE),
            traded=False,
            rejection=rejection,
        )

    def record_prefiltered(
        self, signal: Signal, reason: str, code: str = "pre_filter"
    ) -> PipelineResult:
        """Write the trail for a signal the pre-filter kept from research.

        No budget was spent and no model was called; the record is the whole point —
        every post that arrived and was not researched stays readable, with the
        reason, so the filter itself can be audited against what it skipped.
        """
        decision_id = self._id_factory()
        rejection = self._audit.record_stage_rejection(
            decision_id,
            RejectedStage.PRE_FILTER,
            code,
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
        usage = _combine_usage(self._pending_triage_usage, self._research.last_usage)
        self._pending_triage_usage = None
        if not isinstance(outcome, ResearchReport):
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.RESEARCH,
                str(outcome.code),
                outcome.message,
                usage=usage,
            )
        report = outcome

        # 2. Sizing. Sub-floor confidence and a no_position verdict both land here.
        # The table is picked by intended instrument: a catalyst-backed thesis (or
        # any puts thesis — puts are its only expression) sizes on the halved
        # options table; everything else on the full equity table. A later
        # fallback to equity RE-sizes at the full table (ruling 2026-08-24 #2:
        # no phantom half-size penalty for chain illiquidity).
        sleeve_nav = self._gate.sleeve_nav(Sleeve.EQUITY)
        wants_puts = report.direction is Direction.SHORT_VIA_PUTS
        intends_option = self._option_selector is not None and (
            wants_puts or (report.direction is Direction.LONG and report.has_catalyst)
        )
        if intends_option:
            proposal = self._sizing.propose_option(report, sleeve_nav)
        else:
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
                usage=usage,
            )

        # 3. Order construction (expression routing lives inside).
        order, problem, proposal, expression = self._build_order(
            signal, report, proposal
        )
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
                usage=usage,
                expression=expression,
            )

        # 4. The risk gate. Approved or rejected, this writes the full decision record.
        decision = self._gate.submit(order)
        record = self._audit.record_decision(
            signal,
            report,
            proposal,
            decision,
            decision_id=decision_id,
            usage=usage,
            expression=expression,
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
    ) -> tuple[
        Optional[object],
        Optional[tuple[str, str]],
        SizedProposal,
        Optional[ExpressionSnapshot],
    ]:
        """Expression routing + order construction (ruling 2026-08-24).

        Returns ``(order, problem, proposal, expression)``. The proposal comes
        back because a fallback from options to equity RE-sizes at the full
        equity table — a phantom half-size penalty for chain illiquidity would
        be wrong. Every path writes an ExpressionSnapshot when routing ran.
        """
        if len(report.tickers) != 1:
            # Constraint #6: where a spec admits more than one reading, take the fewer
            # trades and surface the ambiguity rather than silently picking one.
            return None, (
                "ambiguous_instrument",
                f"report names {len(report.tickers)} tickers ({', '.join(report.tickers) or 'none'}); "
                f"one sized proposal cannot be split across them and choosing one would "
                f"be a guess, so no order is placed",
            ), proposal, None

        wants_puts = report.direction is Direction.SHORT_VIA_PUTS

        if self._option_selector is None:
            # No expression layer wired: the pre-options pipeline, unchanged.
            if report.direction is not Direction.LONG:
                return None, (
                    "instrument_not_supported",
                    f"direction {report.direction} needs an options chain to express as a "
                    f"bought put; no contract-selection source is built, so no order is "
                    f"placed. See this module's docstring.",
                ), proposal, None
            return self._build_equity_order(signal, report, proposal, expression=None)

        # -- expression routing ---------------------------------------------------
        if not report.has_catalyst:
            if wants_puts:
                fallback = OptionFallback(
                    FallbackReason.NO_CATALYST_FOR_PUTS,
                    "short thesis with no catalyst inside the horizon; puts are its "
                    "only expression and leverage is earned by timing specificity, "
                    "so no order is placed",
                )
                return None, (
                    str(fallback.reason),
                    fallback.detail,
                ), proposal, _fallback_snapshot(fallback, chosen="none")
            fallback = OptionFallback(
                FallbackReason.NO_CATALYST,
                "directional-but-patient thesis: no catalyst inside the horizon, "
                "so the position expresses as stock",
            )
            proposal = self._sizing.propose_equity(report, proposal.sleeve_nav)
            return self._build_equity_order(
                signal, report, proposal,
                expression=_fallback_snapshot(fallback, chosen="equity"),
            )

        # Catalyst present: fetch the chain and select.
        symbol = report.tickers[0]
        today = self._clock().date()
        config = self._option_selector._config  # noqa: SLF001 - same package seam
        min_expiry = today + timedelta(
            days=config.min_expiry_days.for_horizon(str(report.time_horizon))
        )
        chain = (
            self._options_chain.chain_for(symbol, min_expiry=min_expiry)
            if self._options_chain is not None
            else None
        )
        selection = self._option_selector.select(
            direction=str(report.direction),
            time_horizon=str(report.time_horizon),
            confidence=report.confidence,
            chain=chain,
            today=today,
        )

        if isinstance(selection, SelectedOption):
            quote = selection.quote
            limit = quote.mid.quantize(CENTS, rounding=ROUND_UP)  # type: ignore[union-attr]
            contracts = int(
                (proposal.capital / (limit * quote.multiplier)).to_integral_value(
                    ROUND_DOWN
                )
            )
            if contracts >= 1:
                order = OptionBuyToOpenOrder(
                    symbol=quote.occ_symbol,
                    underlying=quote.underlying,
                    right=quote.right,  # type: ignore[arg-type]
                    expiration=quote.expiration,
                    strike=quote.strike,
                    contracts=contracts,
                    multiplier=quote.multiplier,
                    execution=LimitExecution(limit_price=limit),
                    signal_id=signal.signal_id,
                    confidence=report.confidence,
                )
                return order, None, proposal, _selected_snapshot(selection)
            selection = OptionFallback(
                FallbackReason.PREMIUM_EXCEEDS_SIZE,
                f"{proposal.capital} premium at risk buys no whole contract of "
                f"{quote.occ_symbol} at {limit} x {quote.multiplier}",
                near_miss=NearMissSnapshot(
                    occ_symbol=quote.occ_symbol,
                    delta=quote.delta,
                    open_interest=quote.open_interest,
                    spread_pct=quote.spread_pct,
                    killed_by="premium_exceeds_size",
                ),
            )

        # Selector (or affordability) fell back.
        fallback: OptionFallback = selection  # type: ignore[assignment]
        if wants_puts:
            return None, (
                str(fallback.reason),
                f"puts thesis with no valid contract: {fallback.detail}",
            ), proposal, _fallback_snapshot(fallback, chosen="none")
        proposal = self._sizing.propose_equity(report, proposal.sleeve_nav)
        return self._build_equity_order(
            signal, report, proposal,
            expression=_fallback_snapshot(fallback, chosen="equity"),
        )

    def _build_equity_order(
        self,
        signal: Signal,
        report: ResearchReport,
        proposal: SizedProposal,
        expression: Optional[ExpressionSnapshot],
    ) -> tuple[
        Optional[object],
        Optional[tuple[str, str]],
        SizedProposal,
        Optional[ExpressionSnapshot],
    ]:
        symbol = report.tickers[0]
        quote = self._prices(symbol)
        if quote is None or quote <= ZERO:
            return None, (
                "no_price",
                f"no usable price for {symbol}; an order priced on a guess is an order "
                f"the gate would cash-secure against a guess",
            ), proposal, expression

        # Round the bound UP. It is the worst case the gate reserves against and the
        # limit the broker is sent, so rounding it down would shave the protection.
        limit_price = quote.quantize(CENTS, rounding=ROUND_UP)
        # Fractional shares (2026-08-20): round DOWN to the venue's quantity step —
        # rounding must never increase exposure. Whole-share venues keep step 1, so
        # this is the old behaviour wherever fractional is unproven.
        step = self._adapter.equity_quantity_step
        quantity = (proposal.capital / limit_price).quantize(step, rounding=ROUND_DOWN)
        floor = self._gate.limits.equity_sleeve.min_order_notional_usd
        if quantity <= ZERO or quantity * limit_price < floor:
            return None, (
                "below_min_notional",
                f"{proposal.capital} at {limit_price} rounds to {quantity} shares "
                f"of {symbol} ({(quantity * limit_price).quantize(CENTS)} notional), "
                f"below the {floor} minimum — dust, not a position",
            ), proposal, expression

        return (
            EquityBuyOrder(
                symbol=symbol,
                quantity=quantity,
                execution=LimitExecution(limit_price=limit_price),
                signal_id=signal.signal_id,
                confidence=report.confidence,
            ),
            None,
            proposal,
            expression,
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
        filled = filled_quantity

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
            # True cash committed: contracts carry a share multiplier the raw
            # quantity x price product misses (equity multiplier is 1).
            filled_value=filled
            * filled_avg_price
            * unit_multiplier(working.approved.order),
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
        usage: Optional["ResearchUsage"] = None,
        expression: Optional[ExpressionSnapshot] = None,
    ) -> PipelineResult:
        rejection = self._audit.record_stage_rejection(
            decision_id,
            stage,
            code,
            message,
            signal,
            report=report,
            proposal=proposal,
            usage=usage,
            expression=expression,
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached=str(stage),
            traded=False,
            rejection=rejection,
        )
