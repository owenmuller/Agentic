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
from typing import Callable, Collection, Optional, Protocol

from audit.log import AuditLog, AuditLogError
from audit.records import (
    AddSnapshot,
    BoundaryConfirmationSnapshot,
    DecisionRecord,
    ExpressionSnapshot,
    NearMissSnapshot,
    RejectedStage,
    StageRejectionRecord,
)
from execution.base import BrokerAdapter, BrokerError, OrderReceipt
from research.add_decision import ADD_HOLD_CODES, HeldPositionContext
from research.reports import Direction, ResearchReport, ResearchUsage
from signals.themes import THEME_KEY, shortlist_of
from research.research_pass import ResearchPass
from research.triage import TriagePass


@dataclass(frozen=True, slots=True)
class _Confirmation:
    """What the boundary confirmation decided: the report to size (None = not
    confirmed, with ``failure`` saying why), the snapshot for the decision record
    when a second pass ran, and that pass's usage so the record's cost is honest."""

    report: Optional[ResearchReport]
    failure: Optional[str] = None
    snapshot: Optional[BoundaryConfirmationSnapshot] = None
    usage: Optional[ResearchUsage] = None


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
from risk_gate.state import Sleeve, is_option, unit_multiplier, units_of
from sizing.selection import (
    FallbackReason,
    OptionFallback,
    OptionSelector,
    SelectedOption,
)
from signals import Signal
from sizing.engine import InstrumentKind, SizedProposal, SizingEngine

ZERO = Decimal("0")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.pipeline")


class HeldPositions(Protocol):
    """The exit engine's add-decision seam (ruling 2026-09-16): which held
    position a signal names, and where a non-add outcome lands as convergence.
    A pipeline wired without one never routes an add — the pre-ruling path."""

    def context_for(self, signal: Signal) -> Optional[HeldPositionContext]: ...

    def context_for_symbol(
        self, symbol: str, signal: Optional[Signal] = None
    ) -> Optional[HeldPositionContext]: ...

    def note_add_signal(
        self,
        position_decision_id: str,
        signal: Signal,
        report: Optional[ResearchReport],
        verdict: str,
        code: str,
        decision_id: str,
    ) -> None: ...


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
    #: Execution fidelity (ruling 2026-09-02): when the broker accepted the
    #: order, and the quoted spread at that moment — the raw material for
    #: intended-vs-fill and time-to-fill on the fill record. None on records
    #: from before the ruling or when the quote was unavailable.
    submitted_at: Optional[datetime] = None
    spread_pct_at_submission: Optional[Decimal] = None
    #: Add decision (ruling 2026-09-16): the ORIGINATING decision id of the
    #: position this fill joins as a new lot. None = opens a position.
    add_to: Optional[str] = None


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


def _selected_snapshot(
    selection: SelectedOption,
    door: Optional[str] = None,
    tag: Optional[str] = None,
    underlying_price: Optional[Decimal] = None,
) -> ExpressionSnapshot:
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
        door=door,
        tag=tag,
        underlying_price=underlying_price,
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
        convergence_snapshot: Optional[Callable] = None,
        options_chain=None,
        option_selector: Optional[OptionSelector] = None,
        themes: Optional[object] = None,
        clock: Optional[Callable[[], datetime]] = None,
        probation_sources: Collection[str] = (),
        scalars: Optional["SizingScalars"] = None,
        atr_fraction: Optional[Callable[[str], Optional[Decimal]]] = None,
        atr_config: Optional["AtrSizingConfig"] = None,
        spread_pct: Optional[Callable[[str], Optional[Decimal]]] = None,
        reward_risk: Optional["RewardRiskConfig"] = None,
        boundary: Optional["BoundaryConfirmationConfig"] = None,
        sizing_floor: int = 50,
        adds: Optional[HeldPositions] = None,
        add_config: Optional["AddDecisionsConfig"] = None,
    ) -> None:
        self._research = research
        #: Add decisions (ruling 2026-09-16): the exit engine's view of held
        #: names. None = never route an add (harnesses without an exit engine).
        self._adds = adds
        self._add_config = add_config
        #: The add decision in flight for the signal being processed, so the
        #: outer wrapper can hand a non-add outcome back as convergence
        #: whichever stage it stopped at.
        self._add_in_flight: Optional[dict] = None
        self._triage = triage
        self._pending_triage_usage: Optional[ResearchUsage] = None
        self._sizing = sizing
        #: Post-table risk scalars (rulings 2026-09-01/02): drawdown ladder x
        #: regime, both ≤1.0, applied to every judged proposal this pipeline
        #: sizes — the ONE composition point. None = multiplier 1.0 forever.
        self._scalars = scalars
        #: ATR sizing (ruling 2026-09-02): equity proposals only. Either piece
        #: absent = the fixed-15% regime, exactly as before the ruling.
        self._atr_fraction = atr_fraction
        self._atr_config = atr_config
        #: Execution fidelity (ruling 2026-09-02): the quoted spread at order
        #: submission, recorded on the fill. None = not measured, never zero.
        self._spread_pct = spread_pct
        #: The reward:risk gate (ruling 2026-09-02): veto-only, equity longs.
        self._rr_config = reward_risk
        #: Boundary confirmation (ruling 2026-09-02): the sizing floor's noise
        #: band demands a second independent pass; the lower confidence sizes.
        self._boundary = boundary
        self._sizing_floor = sizing_floor
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        #: Convergence state at dispatch (2026-09-02), stamped on decision
        #: records so band-upgrade evidence accumulates. None = no registry.
        self._convergence_snapshot = convergence_snapshot
        #: The options expression seam (2026-08-24). Both None = equity-only
        #: pipeline, byte-identical to the pre-options behaviour — which is what
        #: every harness without a chain gets.
        self._options_chain = options_chain
        self._option_selector = option_selector
        #: Theme -> ETF map (ruling 2026-09-15); None when no source configures one.
        self._themes = themes
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        #: Probation sources (human ruling 2026-08-25): researched and
        #: credibility-tracked as normal, sized to zero at this stage.
        self._probation = frozenset(probation_sources)
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
        self._add_in_flight = None
        try:
            result = self._process(decision_id, signal)
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
            result = PipelineResult(
                decision_id=decision_id,
                signal_id=signal.signal_id,
                stage_reached=str(RejectedStage.INTERNAL_ERROR),
                traded=False,
                rejection=rejection,
            )
        self._settle_add_outcome(signal, result)
        return result

    def _settle_add_outcome(self, signal: Signal, result: PipelineResult) -> None:
        """An add decision that did not add — a hold, no headroom, a research
        failure, a gate or broker refusal — is convergence on the position and
        owes it a review (ruling 2026-09-16, rule 4). Whichever stage it
        stopped at, it lands here once."""
        in_flight = self._add_in_flight
        self._add_in_flight = None
        if in_flight is None or result.traded or self._adds is None:
            return
        context: HeldPositionContext = in_flight["context"]
        if result.rejection is not None:
            code = result.rejection.code
        elif result.decision is not None:
            code = result.decision.gate.rejection_code or "gate_rejected"
        else:
            code = "unknown"
        verdict = in_flight.get("verdict") or (
            "hold" if code in ADD_HOLD_CODES else code
        )
        try:
            self._adds.note_add_signal(
                context.position_decision_id,
                signal,
                in_flight.get("report"),
                verdict=verdict,
                code=code,
                decision_id=result.decision_id,
            )
        except Exception:  # noqa: BLE001 - a note must never undo a recorded verdict
            logger.exception("could not note the add decision on %s", context.symbol)

    def _process(self, decision_id: str, signal: Signal) -> PipelineResult:
        # 0. Theme -> ETF proposal (ruling 2026-09-15): a no-ticker Class 1 post
        # matching exactly one configured theme carries its mapped ETF into the
        # prompt. Deterministic; the model may decline.
        if self._themes is not None:
            signal = self._themes.apply(signal)
        # 0b. Add decision (ruling 2026-09-16): a signal naming a name the judged
        # sleeve already holds is researched as an ADD DECISION — the position
        # stated in the prompt, the verdict add/hold with a fraction — never as
        # a second position. Deterministic routing from the scanner's tickers.
        add_context: Optional[HeldPositionContext] = None
        second_pass = False
        if self._adds_enabled:
            add_context = self._adds.context_for(signal)  # type: ignore[union-attr]
            if add_context is not None:
                self._add_in_flight = {"context": add_context, "report": None}
        # 1. Research.
        outcome = self._research.run(signal, add_context=add_context)
        usage = _combine_usage(self._pending_triage_usage, self._research.last_usage)
        self._pending_triage_usage = None
        screen_report = self._research.last_screen
        screen_usage = self._research.last_screen_usage
        if not isinstance(outcome, ResearchReport):
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.RESEARCH,
                str(outcome.code),
                outcome.message,
                usage=usage,
                screen_report=screen_report,
                screen_usage=screen_usage,
                add=self._add_stub(add_context, "research_failed", second_pass),
            )
        report = outcome
        # 1b. The model named a held symbol WITHOUT having been asked the add
        # question (the signal carried no ticker — a theme post, a bare call).
        # One position per symbol still holds: buy the add-decision pass now,
        # with the position in view, and let ITS verdict stand.
        if (
            add_context is None
            and self._adds_enabled
            and not report.recommends_no_position
            and len(report.tickers) == 1
        ):
            held = self._adds.context_for_symbol(report.tickers[0], signal)  # type: ignore[union-attr]
            if held is not None:
                logger.info(
                    "%s named held %s without the add question; running the add "
                    "decision pass (ruling 2026-09-16)",
                    signal.signal_id,
                    held.symbol,
                )
                add_context = held
                second_pass = True
                self._add_in_flight = {"context": held, "report": report}
                second = self._research.run(signal, add_context=held)
                usage = _combine_usage(usage, self._research.last_usage)
                if self._research.last_screen is not None:
                    screen_report = self._research.last_screen
                    screen_usage = self._research.last_screen_usage
                if not isinstance(second, ResearchReport):
                    return self._stopped(
                        decision_id,
                        signal,
                        RejectedStage.RESEARCH,
                        str(second.code),
                        f"add decision pass on held {held.symbol} failed: {second.message}",
                        usage=usage,
                        screen_report=screen_report,
                        screen_usage=screen_usage,
                        add=self._add_stub(held, "research_failed", second_pass),
                    )
                report = second
        is_add = add_context is not None
        if is_add:
            self._add_in_flight = {"context": add_context, "report": report}
            problem = self._add_problem(report, add_context)
            if problem is not None:
                verdict, message = problem
                self._add_in_flight["verdict"] = verdict
                return self._stopped(
                    decision_id,
                    signal,
                    RejectedStage.SIZING,
                    "already_held_no_add",
                    message,
                    report=report,
                    usage=usage,
                    screen_report=screen_report,
                    screen_usage=screen_usage,
                    add=self._add_stub(add_context, verdict, second_pass),
                )

        # 2. Sizing. Sub-floor confidence and a no_position verdict both land here.
        # The table is picked by intended instrument: a catalyst-backed thesis (or
        # any puts thesis — puts are its only expression) sizes on the halved
        # options table; everything else on the full equity table. A later
        # fallback to equity RE-sizes at the full table (ruling 2026-08-24 #2:
        # no phantom half-size penalty for chain illiquidity). Adds are equity
        # only (ruling 2026-09-16): they join an equity position's lots.
        sleeve_nav = self._gate.sleeve_nav(Sleeve.EQUITY)
        wants_puts = report.direction is Direction.SHORT_VIA_PUTS
        intends_option = (
            not is_add
            and self._option_selector is not None
            and (
                wants_puts
                or (report.direction is Direction.LONG and self._option_door(report) is not None)
            )
        )
        # 2a-0. Boundary confirmation (ruling 2026-09-02, post-diagnosis): a
        # tradeable verdict in the sizing floor's noise band must be confirmed
        # by a second independent pass, and the LOWER confidence sizes.
        boundary = None
        if not report.recommends_no_position:
            confirmation = self._confirm_boundary(signal, report, add_context)
            if confirmation.usage is not None:
                # The second pass is real spend on THIS decision (2026-09-15:
                # RWT's record carried one pass's cost for two passes' calls).
                usage = _combine_usage(usage, confirmation.usage)
            if confirmation.report is None:
                return self._stopped(
                    decision_id,
                    signal,
                    RejectedStage.SIZING,
                    "unconfirmed_boundary",
                    confirmation.failure or "boundary confirmation failed",
                    report=report,
                    usage=usage,
                    screen_report=screen_report,
                    screen_usage=screen_usage,
                    add=self._add_stub(add_context, "unconfirmed", second_pass),
                )
            report = confirmation.report
            boundary = confirmation.snapshot
            if is_add:
                self._add_in_flight = {"context": add_context, "report": report}
        # 2a. The reward:risk gate (ruling 2026-09-02): equity longs must clear
        # (target - entry) / (entry x stop) >= min_ratio before a dollar is
        # sized. Veto-only — the model's target claim can block an entry, never
        # enlarge one. Adds clear it too: more of a position is a new dollar.
        if not intends_option and report.direction is Direction.LONG:
            failed = self._reward_risk_reason(report)
            if failed is not None:
                return self._stopped(
                    decision_id,
                    signal,
                    RejectedStage.SIZING,
                    "insufficient_reward_risk",
                    failed,
                    report=report,
                    usage=usage,
                    screen_report=screen_report,
                    screen_usage=screen_usage,
                    add=self._add_stub(add_context, "insufficient_reward_risk", second_pass),
                )
        add_snapshot: Optional[AddSnapshot] = None
        if is_add:
            proposal, add_snapshot = self._propose_add(
                report, sleeve_nav, add_context, second_pass  # type: ignore[arg-type]
            )
        elif intends_option:
            proposal = self._propose_option(report, sleeve_nav)
        else:
            proposal = self._propose_equity(report, sleeve_nav)
        if not proposal.is_tradeable:
            if is_add:
                code = "below_floor" if add_snapshot.combined_cap_fraction == ZERO else "add_no_headroom"  # type: ignore[union-attr]
                self._add_in_flight["verdict"] = (  # type: ignore[index]
                    "no_headroom" if code == "add_no_headroom" else code
                )
                add_snapshot = add_snapshot.model_copy(  # type: ignore[union-attr]
                    update={"verdict": self._add_in_flight["verdict"]}  # type: ignore[index]
                )
            else:
                code = "no_position" if report.recommends_no_position else "below_floor"
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.SIZING,
                code,
                proposal.rationale,
                report=report,
                proposal=proposal,
                usage=usage,
                screen_report=screen_report,
                screen_usage=screen_usage,
                add=add_snapshot,
            )

        # 2b. Probation (human ruling 2026-08-25, first source: optionshawk).
        # The research and the credibility record are real; the position is
        # not. Checked AFTER the honest verdicts so a no_position or
        # below_floor from a probation source keeps its own code — probation
        # records exactly the trades that WOULD have happened, which is what
        # the 60-90 day promote-or-drop review counts. The proposal rides on
        # the record so the review sees the size each one would have taken.
        if signal.source_id in self._probation:
            return self._stopped(
                decision_id,
                signal,
                RejectedStage.SIZING,
                "probation",
                f"{signal.source_id} is on probation: report and credibility "
                f"accrue normally, sizing short-circuits to zero. Would have "
                f"deployed {proposal.capital} at confidence {report.confidence}.",
                report=report,
                proposal=proposal,
                usage=usage,
                screen_report=screen_report,
                screen_usage=screen_usage,
                add=add_snapshot,
            )

        # 3. Order construction (expression routing lives inside). An add is
        # always stock: it joins the lots of an equity position.
        if is_add:
            order, problem, proposal, expression = self._build_equity_order(
                signal, report, proposal, expression=None
            )
        else:
            order, problem, proposal, expression = self._build_order(
                signal, report, proposal
            )
        if order is not None:
            expression = self._with_theme(signal, report, expression)
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
                screen_report=screen_report,
                screen_usage=screen_usage,
                add=add_snapshot,
            )

        # 4. The risk gate. Approved or rejected, this writes the full decision record.
        decision = self._gate.submit(order)
        convergence = None
        if self._convergence_snapshot is not None:
            try:
                convergence = self._convergence_snapshot(signal)
            except Exception:  # noqa: BLE001 - a stamp must never block a record
                logger.exception("convergence snapshot failed; recording without it")
        record = self._audit.record_decision(
            signal,
            report,
            proposal,
            decision,
            decision_id=decision_id,
            usage=usage,
            expression=expression,
            screen_report=screen_report,
            screen_usage=screen_usage,
            convergence=convergence,
            boundary=boundary,
            add=add_snapshot,
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
            receipt = self._adapter.submit_order(
                approved, client_reference=decision_id
            )
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
                add=add_snapshot,
            )
            return PipelineResult(
                decision_id=decision_id,
                signal_id=signal.signal_id,
                stage_reached=str(RejectedStage.EXECUTION),
                traded=False,
                decision=record,
                rejection=rejection,
            )

        spread = None
        if self._spread_pct is not None:
            try:
                # Equity orders quote the symbol itself; an option order's
                # chain spread is already on the decision's expression
                # snapshot, so it is not re-measured here.
                if not is_option(approved.order):
                    spread = self._spread_pct(approved.order.symbol)
            except Exception:  # noqa: BLE001 - fidelity metadata, never blocking
                spread = None
        self._working[receipt.broker_order_id] = WorkingOrder(
            decision_id=decision_id,
            approved=approved,
            receipt=receipt,
            signal=signal,
            report=report,
            proposal=proposal,
            submitted_at=self._clock(),
            spread_pct_at_submission=spread,
            add_to=(add_context.position_decision_id if add_context is not None else None),
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached="broker",
            traded=True,
            decision=record,
            receipt=receipt,
        )

    # -- add decisions (human ruling 2026-09-16) -----------------------------------------

    @property
    def _adds_enabled(self) -> bool:
        return self._adds is not None and (
            self._add_config is None or self._add_config.enabled
        )

    def _add_problem(
        self, report: ResearchReport, context: HeldPositionContext
    ) -> Optional[tuple[str, str]]:
        """Why this add-decision report does NOT add: ``(verdict, message)`` for
        the already_held_no_add record, or None when it is a qualifying add.
        Every non-qualifying reading resolves to a hold (Constraint #6)."""
        symbol = context.symbol
        if context.instrument_kind == "option":
            return "not_built", (
                f"{symbol} is held as an option; adds to option positions are not "
                f"built (ruling 2026-09-16) — the signal is recorded as convergence "
                f"and a review is triggered"
            )
        verdict = str(report.add_verdict) if report.add_verdict is not None else "unstated"
        if report.recommends_no_position or report.add_verdict is None or verdict == "hold":
            return "hold", (
                f"add decision on held {symbol}: verdict {verdict}"
                f"{' (direction no_position)' if report.recommends_no_position else ''} "
                f"at confidence {report.confidence} — nothing bought; recorded as "
                f"convergence on the position, review triggered (ruling 2026-09-16)"
            )
        if report.direction is not Direction.LONG:
            return "hold", (
                f"add decision on held {symbol}: verdict add with direction "
                f"{report.direction} — only a long adds to a long position; read as "
                f"a hold (Constraint #6), recorded as convergence, review triggered"
            )
        if [t.upper() for t in report.tickers] != [symbol.upper()]:
            return "hold", (
                f"add decision on held {symbol}: the report names "
                f"{', '.join(report.tickers) or 'nothing'} — an add must name exactly "
                f"the held symbol; read as a hold, recorded as convergence, review "
                f"triggered"
            )
        if report.add_fraction is None:
            return "hold", (
                f"add decision on held {symbol}: verdict add without an add_fraction "
                f"— read as a hold (Constraint #6), recorded as convergence, review "
                f"triggered"
            )
        return None

    def _combined_cap(self, confidence: int, independent_family: bool) -> tuple[Decimal, bool]:
        """The combined-position cap (rule 2): the band of the NEW verdict's
        confidence, one band wider for an independent family, never past the
        hard cap. Returns ``(fraction, bumped)``."""
        sizing = self._gate.limits.sizing
        band = sizing.size_for(confidence)
        bumped = False
        bump_bands = self._add_config.family_bump_bands if self._add_config else 1
        if band > ZERO and independent_family and bump_bands > 0:
            wider = sorted({b.size for b in sizing.bands if b.size > band})
            if wider:
                band = wider[0]
                bumped = True
        return min(band, sizing.hard_cap), bumped

    def _propose_add(
        self,
        report: ResearchReport,
        sleeve_nav: Decimal,
        context: HeldPositionContext,
        second_pass: bool,
    ) -> tuple[SizedProposal, AddSnapshot]:
        """Size an add (ruling 2026-09-16): headroom under the combined cap x
        the model's add_fraction, then the same ATR risk-parity and post-table
        scalars every judged entry gets. The held value is max(cost, market),
        so a drawdown cannot manufacture headroom (Constraint #6)."""
        combined_fraction, bumped = self._combined_cap(
            report.confidence, context.independent_family
        )
        combined_capital = (sleeve_nav * combined_fraction).quantize(
            CENTS, rounding=ROUND_DOWN
        )
        held_value = context.held_value
        headroom = combined_capital - held_value
        add_fraction = report.add_fraction or ZERO
        capital = ZERO
        if combined_fraction > ZERO and headroom > ZERO:
            capital = (headroom * add_fraction).quantize(CENTS, rounding=ROUND_DOWN)
        if combined_fraction == ZERO:
            rationale = (
                f"add decision: confidence {report.confidence} is below the "
                f"{self._gate.limits.sizing.no_trade_below} floor; no add"
            )
        elif headroom <= ZERO:
            rationale = (
                f"add decision: confidence {report.confidence} caps the COMBINED "
                f"position at {combined_fraction:.2%} of sleeve NAV = {combined_capital}"
                f"{' (one band up: ' + context.signal_family + ' is independent of ' + context.originating_family + ')' if bumped else ''}; "
                f"already holding {held_value:.2f} — no headroom, no add"
            )
        else:
            rationale = (
                f"add decision: confidence {report.confidence} caps the COMBINED "
                f"position at {combined_fraction:.2%} of sleeve NAV = {combined_capital}"
                f"{' (one band up: ' + context.signal_family + ' is independent of ' + context.originating_family + ')' if bumped else ''}; "
                f"holding {held_value:.2f}, headroom {headroom:.2f} x add_fraction "
                f"{add_fraction} = {capital}"
            )
        proposal = SizedProposal(
            instrument=InstrumentKind.EQUITY,
            sleeve=Sleeve.EQUITY,
            confidence=report.confidence,
            sleeve_nav=sleeve_nav,
            fraction_of_sleeve_nav=(capital / sleeve_nav if sleeve_nav > ZERO else ZERO),
            capital=capital,
            rationale=rationale,
        )
        proposal = self._apply_atr(proposal, report)
        if self._scalars:
            proposal = self._scalars.scale(proposal)
        snapshot = AddSnapshot(
            position_decision_id=context.position_decision_id,
            symbol=context.symbol,
            verdict="add",
            signal_family=context.signal_family,
            originating_family=context.originating_family,
            family_bump=bumped,
            add_fraction=report.add_fraction,
            combined_cap_fraction=combined_fraction,
            combined_cap_capital=combined_capital,
            held_value=held_value,
            headroom=headroom,
            lots_before=max(1, len(context.lots)),
            second_pass=second_pass,
        )
        return proposal, snapshot

    @staticmethod
    def _add_stub(
        context: Optional[HeldPositionContext], verdict: str, second_pass: bool
    ) -> Optional[AddSnapshot]:
        """The add snapshot for a decision that stopped before sizing."""
        if context is None:
            return None
        return AddSnapshot(
            position_decision_id=context.position_decision_id,
            symbol=context.symbol,
            verdict=verdict,
            signal_family=context.signal_family,
            originating_family=context.originating_family,
            held_value=context.held_value,
            lots_before=max(1, len(context.lots)),
            second_pass=second_pass,
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
        door = self._option_door(report)
        if door is None:
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
                "directional-but-patient thesis: no catalyst inside the horizon "
                "and confidence below the conviction door, so the position "
                "expresses as stock",
            )
            proposal = self._propose_equity(report, proposal.sleeve_nav)
            return self._build_equity_order(
                signal, report, proposal,
                expression=_fallback_snapshot(fallback, chosen="equity"),
            )

        # A door is open (catalyst, or conviction — ruling 2026-09-15): fetch
        # the chain and select. The same gates apply whichever door it was.
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
                # Measurement tags (ruling 2026-09-15): short-dated wins the
                # tag when both apply; the door is recorded either way.
                short_dated = (quote.expiration - today).days < config.short_dated_dte
                tag = (
                    "short_dated_option"
                    if short_dated
                    else ("conviction_option" if door == "conviction" else None)
                )
                return order, None, proposal, _selected_snapshot(
                    selection, door=door, tag=tag,
                    underlying_price=self._safe_price(symbol),
                )
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
        proposal = self._propose_equity(report, proposal.sleeve_nav)
        return self._build_equity_order(
            signal, report, proposal,
            expression=_fallback_snapshot(fallback, chosen="equity"),
        )

    def _option_door(self, report: ResearchReport) -> Optional[str]:
        """Which door, if any, admits this report to an options expression
        (ruling 2026-09-15): "catalyst" (unchanged) or "conviction" (confidence at
        or above the configured floor; every report states a time_horizon by
        schema). None = stock. Requires a wired selector."""
        if self._option_selector is None:
            return None
        if report.has_catalyst:
            return "catalyst"
        config = self._option_selector._config  # noqa: SLF001 - same package seam
        if report.confidence >= config.conviction_min_confidence:
            return "conviction"
        return None

    def _safe_price(self, symbol: str) -> Optional[Decimal]:
        try:
            price = self._prices(symbol)
        except Exception:  # noqa: BLE001 - a quote outage is not a verdict
            return None
        return price if price is not None and price > ZERO else None

    def _with_theme(
        self, signal: Signal, report: ResearchReport, expression: Optional[ExpressionSnapshot]
    ) -> Optional[ExpressionSnapshot]:
        """Tag a decision that expressed a no-ticker post through its mapped ETF
        (ruling 2026-09-15). Only when the report actually named the ETF."""
        shortlist = shortlist_of(signal)
        if not shortlist or len(report.tickers) != 1 or report.tickers[0].upper() not in shortlist:
            return expression
        theme = signal.metadata.get(THEME_KEY)
        if expression is None:
            return ExpressionSnapshot(
                considered=False,
                chosen="equity",
                tag="theme_etf",
                theme=theme,
                underlying_price=self._safe_price(report.tickers[0]),
            )
        return expression.model_copy(
            update={"tag": expression.tag or "theme_etf", "theme": theme}
        )

    def _confirm_boundary(
        self,
        signal: Signal,
        report: ResearchReport,
        add_context: Optional[HeldPositionContext] = None,
    ) -> _Confirmation:
        """Boundary confirmation (ruling 2026-09-02, diagnosis: five
        identical-input replays of a floor-band case spanned long/38-54 and
        no_position/30-72 — the band admits stochastic noise).

        A tradeable verdict with confidence in [floor, floor + band) runs a
        SECOND independent pass, same tier and fresh context. Confirmed = same
        direction at or above the floor; the LOWER-confidence report is the one
        that sizes (the second pass can only block or shrink, never enlarge).
        Returns a confirmation carrying the report to size, or none plus why
        for the typed ``unconfirmed_boundary`` rejection. A second pass that
        errors outright does not confirm — an unconfirmable boundary verdict is
        not sized (Constraint #6). Whenever a second pass ran, both verdicts and
        its usage come back for the record (2026-09-15).
        """
        if self._boundary is None or not self._boundary.enabled:
            return _Confirmation(report)
        floor = self._sizing_floor
        if not floor <= report.confidence < floor + self._boundary.band_width:
            return _Confirmation(report)
        logger.info(
            "boundary confirmation on %s: %s/%d sits in the floor band "
            "[%d, %d); buying a second independent pass",
            signal.signal_id,
            report.direction,
            report.confidence,
            floor,
            floor + self._boundary.band_width,
        )
        second = self._research.run(signal, add_context=add_context)
        second_usage = self._research.last_usage
        if not isinstance(second, ResearchReport):
            return _Confirmation(
                None,
                f"boundary verdict {report.direction}/{report.confidence} could "
                f"not be confirmed: the second pass failed "
                f"({getattr(second, 'code', 'error')}) — an unconfirmable "
                f"floor-band verdict is not sized (ruling 2026-09-02)",
                usage=second_usage,
            )
        if add_context is not None and not second.is_add:
            # An add decision must REPLICATE as an add (ruling 2026-09-16): a
            # second pass that holds is a hold, whatever the first said.
            return _Confirmation(
                None,
                f"boundary add verdict NOT confirmed: first pass add/"
                f"{report.confidence}, second independent pass "
                f"{second.add_verdict or 'unstated'}/{second.direction}/"
                f"{second.confidence} — an add that does not replicate is not "
                f"sized (rulings 2026-09-02, 2026-09-16)",
                usage=second_usage,
            )
        if second.direction is not report.direction or second.confidence < floor:
            return _Confirmation(
                None,
                f"boundary verdict NOT confirmed: first pass "
                f"{report.direction}/{report.confidence}, second independent "
                f"pass {second.direction}/{second.confidence} — the floor band "
                f"is stochastic there, and a verdict that does not replicate "
                f"is not sized (ruling 2026-09-02)",
                usage=second_usage,
            )
        sized_from = "second" if second.confidence < report.confidence else "first"
        logger.info(
            "boundary confirmed on %s: first %s/%d, second %s/%d; the %s pass sizes",
            signal.signal_id,
            report.direction,
            report.confidence,
            second.direction,
            second.confidence,
            sized_from,
        )
        return _Confirmation(
            second if sized_from == "second" else report,
            snapshot=BoundaryConfirmationSnapshot(
                floor=floor,
                band_width=self._boundary.band_width,
                first_direction=str(report.direction),
                first_confidence=report.confidence,
                second_direction=str(second.direction),
                second_confidence=second.confidence,
                sized_from=sized_from,
                second_est_cost_usd=(second_usage.cost_usd if second_usage else None),
            ),
            usage=second_usage,
        )

    def _reward_risk_reason(self, report) -> Optional[str]:
        """Why this equity long fails the reward:risk gate, or None to proceed.

        Deterministic: the model's own target claim over the stop distance the
        position would actually get (ATR-derived, or the fixed fallback). A
        missing QUOTE fails open — order construction already refuses to price
        without one; a missing TARGET fails closed (Constraint #6): a long
        without a defensible level is not a sized trade.
        """
        config = self._rr_config
        if config is None or not config.enabled or not report.tickers:
            return None
        if report.target_price is None:
            return (
                "long verdict with no target_price: the reward:risk gate "
                "cannot price the claim, and an unpriced long is not sized "
                "(ruling 2026-09-02)"
            )
        symbol = report.tickers[0]
        try:
            entry = self._prices(symbol)
        except Exception:  # noqa: BLE001 - a quote outage is not a verdict
            entry = None
        if entry is None or entry <= 0:
            return None  # fail open: the no-quote path blocks downstream anyway
        stop_fraction = config.fallback_stop_fraction
        if self._atr_config is not None and self._atr_config.enabled and self._atr_fraction:
            atr = self._atr_fraction(symbol)
            if atr is not None and atr > 0:
                stop_fraction = min(
                    max(self._atr_config.k * atr, self._atr_config.stop_floor),
                    self._atr_config.stop_ceiling,
                )
        reward = report.target_price - entry
        risk = entry * stop_fraction
        if risk <= 0:
            return None
        ratio = reward / risk
        if ratio >= config.min_ratio:
            return None
        return (
            f"reward:risk {ratio:.2f} below the {config.min_ratio} floor: "
            f"target {report.target_price} vs entry {entry} with a "
            f"{stop_fraction:.2%} stop risks {risk:.2f} to make {reward:.2f} "
            f"(ruling 2026-09-02)"
        )

    def _propose_equity(self, report, sleeve_nav) -> SizedProposal:
        """The confidence table, then ATR risk-parity (per-name, ruling
        2026-09-02), then the post-table risk scalars (book-level, rulings
        2026-09-01/02) — in that order, each step only ever shrinking. EVERY
        judged proposal — the initial sizing and the option-to-equity fallback
        re-sizings — reaches the table through these two helpers, so there is
        no path on which either mechanism is skipped."""
        proposal = self._sizing.propose_equity(report, sleeve_nav)
        proposal = self._apply_atr(proposal, report)
        return self._scalars.scale(proposal) if self._scalars else proposal

    def _propose_option(self, report, sleeve_nav) -> SizedProposal:
        # Options are EXCLUDED from ATR sizing by ruling: the premium is the
        # stop, and the halved table already prices the leverage.
        proposal = self._sizing.propose_option(report, sleeve_nav)
        return self._scalars.scale(proposal) if self._scalars else proposal

    def _apply_atr(self, proposal: SizedProposal, report) -> SizedProposal:
        """Equalized dollar risk inside the band (ruling 2026-09-02).

        stop = clamp(k x ATR(14)/price, floor, ceiling), frozen into the
        proposal for the exit engine to arm at fill; size = min(band capital,
        band capital x risk_budget_fraction / stop) — one-sided by the
        arithmetic: quiet names size at the band cap, volatile names shade
        down. Missing ATR data returns the proposal untouched, which IS the
        fixed-15% regime this ruling replaced.
        """
        config = self._atr_config
        if (
            config is None
            or not config.enabled
            or self._atr_fraction is None
            or not proposal.is_tradeable
            or proposal.instrument is not InstrumentKind.EQUITY
            or not report.tickers
        ):
            return proposal
        atr = self._atr_fraction(report.tickers[0])
        if atr is None or atr <= 0:
            return proposal
        stop = min(max(config.k * atr, config.stop_floor), config.stop_ceiling)
        budget = proposal.capital * config.risk_budget_fraction
        capital = min(proposal.capital, budget / stop).quantize(
            CENTS, rounding=ROUND_DOWN
        )
        fraction = (
            proposal.fraction_of_sleeve_nav * capital / proposal.capital
            if proposal.capital > 0
            else proposal.fraction_of_sleeve_nav
        )
        return SizedProposal(
            instrument=proposal.instrument,
            sleeve=proposal.sleeve,
            confidence=proposal.confidence,
            sleeve_nav=proposal.sleeve_nav,
            fraction_of_sleeve_nav=fraction,
            capital=capital,
            rationale=(
                f"{proposal.rationale}; ATR stop {stop:.2%} "
                f"(k={config.k} x ATR {atr:.2%}, clamped "
                f"[{config.stop_floor:%}, {config.stop_ceiling:%}]), "
                f"risk-parity size {capital}"
            ),
            strategy=proposal.strategy,
            atr_fraction=atr,
            stop_fraction=stop,
            counterfactual_fixed_capital=proposal.capital,
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
        execution = getattr(working.approved.order, "execution", None)
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
            # Execution fidelity (ruling 2026-09-02): what was intended, what
            # the market looked like at submission, how long the fill took.
            intended_price=getattr(execution, "limit_price", None),
            spread_pct_at_submission=working.spread_pct_at_submission,
            seconds_to_fill=(
                Decimal(
                    int(
                        (self._clock() - working.submitted_at).total_seconds()
                    )
                )
                if working.submitted_at is not None
                else None
            ),
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
        screen_report=None,
        screen_usage=None,
        add: Optional[AddSnapshot] = None,
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
            screen_report=screen_report,
            screen_usage=screen_usage,
            add=add,
        )
        return PipelineResult(
            decision_id=decision_id,
            signal_id=signal.signal_id,
            stage_reached=str(stage),
            traded=False,
            rejection=rejection,
        )
