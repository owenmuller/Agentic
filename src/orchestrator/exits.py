"""Exit logic: deterministic guardrails underneath, thesis review on top.

Two layers, deliberately unequal
--------------------------------
Layer 1 is deterministic Python — a max-loss stop and a time stop, both frozen per
position at entry from ``config/orchestrator.yaml``, checked against live prices every
loop cycle. No LLM anywhere in it.

Layer 2 is the thesis review: a periodic LLM re-research of each open position
(``research.exit_review``), which receives the original thesis, the invalidation
condition, and current context, and returns hold or close through a closed schema.

The asymmetry is the design. The review layer decides *well*; the guardrail layer
decides *always*. A failed or malformed review is a hold — closing on bad data is
trading on bad data — and that default is only safe because the guardrails do not care
whether the review layer works. A position can never become unexitable because the
LLM is down; the worst a dead review layer costs is the difference between a
thoughtful exit and a mechanical one.

The one dependency both layers share is a price: an exit order needs a limit, and a
limit needs a quote. A dead price source therefore does block exits — an unpriced
sell order cannot be constructed, and the schema is right to refuse one — so a
guardrail breach with no quote is logged and retried every cycle until a quote
returns.

Exits are decisions too
-----------------------
Every close attempt routes through ``RiskGate.submit`` sell-to-close validation like
any other order — never beyond held quantity, permitted while the kill switch is
halted (a halt stops exposure growing; it does not trap the account in its
positions). Every attempt writes an ``ExitRecord`` under the entry's ``decision_id``,
every review writes a ``ThesisReviewRecord``, the closing fill writes a sell-side
``FillRecord``, and a fully-closed position writes the ``OutcomeRecord`` that finally
turns the source's hit rate from "not yet available" into a number — via
``AuditLog.record_outcome``, which credits the ``CredibilityTracker`` directly.

Restarts
--------
Tracked positions are rebuilt from the audit log at startup (``replay``): a decision
that was approved and filled, with no outcome, is an open position, and its thesis,
invalidation condition, entry cost and fills are all in the trail. Stops are re-derived
from config at replay (a mid-position config change moves them; the config is the
human-owned statement of intent, so it wins). A close verdict from a previous session's
review is restored from the trail too, so a "close" the process died before executing
is not forgotten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Iterable, Optional

from audit.log import AuditLog
from audit.records import AuditTrail, ExitReason, ReviewOutcome
from execution.base import BrokerAdapter, BrokerError
from research.exit_review import ExitReview, ExitReviewPass, PositionUnderReview
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.rejections import Rejection, RejectionCode
from risk_gate.schema import (
    EquityBuyOrder,
    EquitySellToCloseOrder,
    LimitExecution,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
    parse_order,
)

from orchestrator.budget import ResearchBudget
from orchestrator.config import ExitsConfig
from orchestrator.pipeline import PriceSource, WorkingOrder

ZERO = Decimal("0")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.exits")


@dataclass(slots=True)
class TrackedPosition:
    """An open position and everything needed to close it well.

    The gate's own ``Position`` holds the money arithmetic; this holds the *story* —
    which decision opened it, on what thesis, with what invalidation condition — plus
    the two stops frozen at entry. One tracked position per decision, even when the
    gate has merged same-symbol holdings, so P&L and credibility resolve back to the
    signal that actually called it.
    """

    decision_id: str
    symbol: str
    #: Units this decision still holds (the gate's merged position may hold more).
    #: Decimal: equity positions may be fractional.
    quantity: Decimal
    entry_quantity: Decimal
    entry_price: Decimal
    #: Total cash the entry fills committed. P&L closes against this.
    entry_cost: Decimal
    opened_at: datetime
    signal_id: str
    source_id: str
    #: Verbatim original signal content, for the review prompt's fenced block.
    content: str
    thesis: str
    invalidation_condition: str
    time_horizon: str
    confidence: int
    #: Layer-1 stops, frozen when tracking starts.
    stop_price: Decimal
    leash_days: int
    #: Accumulated proceeds from closing fills.
    proceeds: Decimal = ZERO
    last_review_at: Optional[datetime] = None
    #: A review said close (or its invalidation triggered) but the order has not
    #: completed yet. Durable intent: re-attempted every cycle until the position is
    #: flat, and restored from the audit trail after a restart.
    close_verdict: bool = False
    close_detail: str = ""
    #: Broker id of a working exit order, if one is out. Blocks duplicate exits.
    pending_exit: Optional[str] = None
    #: "equity" or "option" — options carry an expiration and a share multiplier,
    #: and their close orders need the full contract identity (entry_order).
    instrument_kind: str = "equity"
    expiration: Optional[date] = None
    multiplier: int = 1
    #: The parsed opening order, kept so an option close can be built with the
    #: exact contract identity the entry carried. None for equity.
    entry_order: Optional[object] = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.instrument_kind, self.symbol)

    @property
    def is_option(self) -> bool:
        return self.instrument_kind == "option"

    def days_held(self, now: datetime) -> int:
        return (now.date() - self.opened_at.date()).days


@dataclass(frozen=True, slots=True)
class _WorkingExit:
    """A sell-to-close the broker has accepted and not yet finished with."""

    broker_order_id: str
    approved: ApprovedOrder
    position: TrackedPosition
    reason: ExitReason
    detail: str


class ExitEngine:
    """Owns open-position tracking, both exit layers, and exit settlement."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: PriceSource,
        review_pass: ExitReviewPass,
        budget: ResearchBudget,
        config: ExitsConfig,
        clock,
        credibility=None,
        cost_sink=None,
        option_prices=None,
        close_before_expiry_days: Optional[int] = None,
    ) -> None:
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        #: Called with each review's estimated cost (None = unpriced/no call),
        #: so the daily cost tripwire sees reviews as well as entry passes.
        self._cost_sink = cost_sink
        self._reviews = review_pass
        self._budget = budget
        self._config = config
        self._clock = clock
        self._credibility = credibility
        #: Premium marks for OCC symbols (the chain source's option_mid). None =
        #: no options venue wired; option positions then mark stale and reviews
        #: run without a current price — degraded, never invented.
        self._option_prices = option_prices
        self._close_before_expiry_days = close_before_expiry_days
        self._tracked: dict[str, TrackedPosition] = {}
        self._working: dict[str, _WorkingExit] = {}

    @property
    def tracked(self) -> tuple[TrackedPosition, ...]:
        return tuple(self._tracked.values())

    @property
    def working_exits(self) -> tuple[str, ...]:
        return tuple(self._working)

    # -- learning about positions ----------------------------------------------------

    def held_symbols(self) -> frozenset[str]:
        """Symbols with a tracked open position — the pre-filter's answer to
        "is this a sale in a name we hold". Deterministic and local."""
        return frozenset(
            position.symbol.upper() for position in self._tracked.values()
        )

    def track_fill(self, working: WorkingOrder, filled: Decimal, price: Decimal) -> None:
        """Called by the pipeline's fill sink when an entry order settles with a fill."""
        order = working.approved.order
        if not isinstance(order, (EquityBuyOrder, OptionBuyToOpenOrder)) or filled <= 0:
            return
        is_option = isinstance(order, OptionBuyToOpenOrder)
        multiplier = order.multiplier if is_option else 1
        cost = price * filled * multiplier
        existing = self._tracked.get(working.decision_id)
        if existing is not None:
            # A second terminal fill against the same decision should not happen, but
            # accumulating is strictly more correct than overwriting if it does.
            existing.quantity += filled
            existing.entry_quantity += filled
            existing.entry_cost += cost
            return

        report = working.report
        self._tracked[working.decision_id] = TrackedPosition(
            decision_id=working.decision_id,
            symbol=order.symbol,
            quantity=filled,
            entry_quantity=filled,
            entry_price=price,
            entry_cost=cost,
            opened_at=self._clock(),
            signal_id=working.signal.signal_id,
            source_id=working.signal.source_id,
            # Sanitized: this string re-enters the review prompt every cycle.
            content=working.signal.content,
            thesis=report.thesis,
            invalidation_condition=report.invalidation_condition,
            time_horizon=str(report.time_horizon),
            confidence=report.confidence,
            stop_price=self._stop_for(price),
            leash_days=self._config.time_stop_days.for_horizon(
                str(report.time_horizon)
            ),
            instrument_kind="option" if is_option else "equity",
            expiration=order.expiration if is_option else None,
            multiplier=multiplier,
            entry_order=order if is_option else None,
        )

    def replay(self, trails: Iterable[AuditTrail]) -> int:
        """Rebuild tracked positions from the audit log after a restart.

        Open means: approved, at least one buy fill, no outcome, and net quantity
        still positive after any sell fills. The broker remains authoritative on what
        is actually held — the gate was seeded from it — so a trail whose position the
        gate does not hold is skipped with a warning, and quantities are clamped to
        what the gate can see.
        """
        restored = 0
        for trail in trails:
            decision = trail.decision
            if decision.sizing.strategy == "mechanical":
                # The mechanical engine replays its own positions: it has no
                # stops to arm and a different exit regime entirely.
                continue
            if not decision.was_approved or trail.outcome is not None:
                continue
            buys = [f for f in trail.fills if f.side == "buy"]
            sells = [f for f in trail.fills if f.side == "sell"]
            if not buys:
                continue
            order = decision.gate.order or {}
            if order.get("kind") not in ("equity_buy", "option_buy_to_open"):
                continue
            is_option = order.get("kind") == "option_buy_to_open"
            multiplier = int(order.get("multiplier", 100)) if is_option else 1

            entry_quantity = sum((f.filled_quantity for f in buys), ZERO)
            quantity = entry_quantity - sum((f.filled_quantity for f in sells), ZERO)
            if quantity <= 0:
                continue

            symbol = str(order["symbol"])
            kind = "option" if is_option else "equity"
            gate_position = self._gate.state.position((kind, symbol))
            if gate_position is None or gate_position.quantity <= 0:
                logger.warning(
                    "audit log says %s holds %s %s but the broker does not; "
                    "not tracking — the broker is authoritative",
                    decision.decision_id,
                    quantity,
                    symbol,
                )
                continue
            quantity = min(quantity, gate_position.quantity)

            entry_cost = sum((f.filled_value for f in buys), ZERO)
            # Per-unit premium/price: filled_value carries the multiplier for
            # options, so divide it back out to compare against per-unit marks.
            entry_price = entry_cost / entry_quantity / multiplier
            research = decision.research

            # A close verdict the previous process recorded but died before executing
            # must survive the restart — reviews are budgeted, and re-earning a verdict
            # already paid for wastes one.
            last_review = trail.reviews[-1] if trail.reviews else None
            close_verdict = (
                last_review is not None and last_review.outcome is ReviewOutcome.CLOSE
            )

            self._tracked[decision.decision_id] = TrackedPosition(
                decision_id=decision.decision_id,
                symbol=symbol,
                quantity=quantity,
                entry_quantity=entry_quantity,
                entry_price=entry_price,
                entry_cost=entry_cost,
                opened_at=buys[0].recorded_at,
                signal_id=decision.signal.signal_id,
                source_id=decision.signal.source_id,
                content=decision.signal.content,
                thesis=research.thesis,
                invalidation_condition=research.invalidation_condition,
                time_horizon=research.time_horizon,
                confidence=research.confidence,
                stop_price=self._stop_for(entry_price),
                leash_days=self._config.time_stop_days.for_horizon(
                    research.time_horizon
                ),
                proceeds=sum((f.filled_value for f in sells), ZERO),
                instrument_kind=kind,
                expiration=(
                    date.fromisoformat(str(order["expiration"])) if is_option else None
                ),
                multiplier=multiplier,
                entry_order=parse_order(order) if is_option else None,
                last_review_at=(last_review.recorded_at if last_review else None),
                close_verdict=close_verdict,
                close_detail=(
                    (last_review.assessment or "")[:200] if close_verdict else ""
                ),
            )
            restored += 1
        if restored:
            logger.info("restored %d open positions from the audit log", restored)
        return restored

    def _stop_for(self, entry_price: Decimal) -> Decimal:
        return entry_price * (Decimal("1") - self._config.max_loss_fraction)

    # -- layer 1: deterministic guardrails ---------------------------------------------

    def check_guardrails(self, now: Optional[datetime] = None) -> list[str]:
        """Mark positions to market, then close anything past a stop.

        Returns the decision ids for which an exit order went out. Runs every cycle,
        needs nothing from the LLM layer, and also re-fires pending close verdicts —
        the durable half of layer 2 — so a "close" survives a price outage or a broker
        refusal by being retried here.

        Boundary comparisons trigger (``<=`` the stop, ``>=`` the leash): a boundary
        is ambiguous, and the exit is the smaller position (Constraint #6).
        """
        moment = now or self._clock()
        marks: dict[tuple[str, ...], Decimal] = {}
        for position in self._tracked.values():
            price = self._mark_for(position)
            if price is not None:
                marks[position.key] = price
        if marks:
            # Live marks keep NAV, drawdown, and therefore the kill switch honest.
            self._gate.mark_to_market(marks)

        exited: list[str] = []
        for position in list(self._tracked.values()):
            if position.pending_exit is not None:
                continue
            price = marks.get(position.key)

            if price is not None and price <= position.stop_price:
                reason: Optional[ExitReason] = ExitReason.MAX_LOSS_STOP
                detail = (
                    f"{position.symbol} at {price} is at or below the "
                    f"{position.stop_price} stop set at entry "
                    f"({self._config.max_loss_fraction:%} below entry "
                    f"{position.entry_price})"
                )
            elif (
                position.expiration is not None
                and self._close_before_expiry_days is not None
                and (position.expiration - moment.date()).days
                <= self._close_before_expiry_days
            ):
                # Deliberately quote-independent: an option this close to expiry
                # exits whether or not a mark arrived this cycle.
                reason = ExitReason.EXPIRY_CLOSE
                detail = (
                    f"{position.symbol} expires {position.expiration}, inside the "
                    f"{self._close_before_expiry_days}-day pre-expiry window; theta "
                    f"endgame is not a place this system holds"
                )
            elif position.days_held(moment) >= position.leash_days:
                reason = ExitReason.TIME_STOP
                detail = (
                    f"held {position.days_held(moment)} days, at or past the "
                    f"{position.leash_days}-day leash for a "
                    f"{position.time_horizon} horizon"
                )
            elif position.close_verdict:
                reason = ExitReason.THESIS_INVALIDATED
                detail = position.close_detail or "thesis review returned close"
            else:
                continue

            if self._initiate_exit(position, reason, detail, price):
                exited.append(position.decision_id)
        return exited

    # -- layer 2: thesis review ---------------------------------------------------------

    def review_theses(self, now: Optional[datetime] = None) -> tuple[int, int]:
        """Re-research open positions on the configured cadence.

        Returns ``(reviews_run, closes_initiated)``. Each review spends one pass from
        the shared daily research budget; when the budget is exhausted, reviews wait —
        the guardrails do not.
        """
        moment = now or self._clock()
        interval = timedelta(hours=self._config.thesis_review_interval_hours)
        reviews_run = 0
        closes = 0

        for position in list(self._tracked.values()):
            if position.pending_exit is not None or position.close_verdict:
                continue
            since = position.last_review_at or position.opened_at
            if moment - since < interval:
                continue
            if not self._budget.try_spend():
                break

            price = self._mark_for(position)
            outcome = self._reviews.run(
                PositionUnderReview(
                    symbol=position.symbol,
                    entry_price=position.entry_price,
                    current_price=price,
                    opened_at=position.opened_at,
                    days_held=position.days_held(moment),
                    time_horizon=position.time_horizon,
                    confidence_at_entry=position.confidence,
                    source_id=position.source_id,
                    thesis=position.thesis,
                    invalidation_condition=position.invalidation_condition,
                    original_content=position.content,
                )
            )
            position.last_review_at = moment
            reviews_run += 1

            if not isinstance(outcome, ExitReview):
                # No verdict is a HOLD, logged as its own outcome. Never a close on
                # bad data; the guardrails above still apply to this position.
                usage = self._reviews.last_usage
                self._audit.record_thesis_review(
                    position.decision_id,
                    ReviewOutcome.REVIEW_FAILED,
                    code=str(outcome.code),
                    message=outcome.message,
                    usage=usage,
                )
                if self._cost_sink is not None:
                    self._cost_sink(usage.cost_usd if usage else None)
                logger.warning(
                    "thesis review of %s failed (%s); holding — guardrails still "
                    "apply",
                    position.decision_id,
                    outcome.code,
                )
                continue

            usage = self._reviews.last_usage
            self._audit.record_thesis_review(
                position.decision_id,
                ReviewOutcome.CLOSE if outcome.should_close else ReviewOutcome.HOLD,
                assessment=outcome.assessment,
                invalidation_triggered=outcome.invalidation_triggered,
                usage=usage,
            )
            if self._cost_sink is not None:
                self._cost_sink(usage.cost_usd if usage else None)
            if not outcome.should_close:
                continue

            # Durable intent first, attempt second: if the order cannot go out right
            # now (no quote, broker down), check_guardrails re-fires it every cycle.
            position.close_verdict = True
            position.close_detail = outcome.assessment[:300]
            if self._initiate_exit(
                position,
                ExitReason.THESIS_INVALIDATED,
                position.close_detail,
                price,
            ):
                closes += 1
        return reviews_run, closes

    # -- placing and settling exits ------------------------------------------------------

    def _initiate_exit(
        self,
        position: TrackedPosition,
        reason: ExitReason,
        detail: str,
        price: Optional[Decimal],
    ) -> bool:
        """Build and submit one sell-to-close. True if the broker accepted it."""
        if price is None:
            price = self._mark_for(position)
        if price is None or price <= ZERO:
            logger.warning(
                "cannot exit %s (%s): no usable quote for %s; will retry next cycle",
                position.decision_id,
                reason,
                position.symbol,
            )
            return False

        gate_position = self._gate.state.position(position.key)
        available = gate_position.available_to_close if gate_position else ZERO
        quantity = min(position.quantity, available)
        if quantity <= ZERO:
            logger.error(
                "wanted to exit %s but the gate shows %s units available for %s; "
                "dropping tracking — the broker is authoritative",
                position.decision_id,
                available,
                position.symbol,
            )
            self._audit.record_exit(
                position.decision_id,
                reason,
                f"{detail} — but no units were available to close; position "
                f"presumed gone",
                gate_decision=_phantom_rejection(position),
            )
            del self._tracked[position.decision_id]
            return False

        # Rounded DOWN: the limit is the worst proceeds the order may accept, and for
        # a risk-reducing exit a marginally worse floor beats resting unfilled.
        order = self._order_for(position, quantity, price.quantize(CENTS, ROUND_DOWN))
        decision = self._gate.submit(order)

        if not decision.is_approved:
            self._audit.record_exit(
                position.decision_id, reason, detail, gate_decision=decision
            )
            if decision.code is RejectionCode.POSITION_NOT_HELD:
                logger.error(
                    "gate rejected exit of %s: position not held; dropping tracking",
                    position.decision_id,
                )
                del self._tracked[position.decision_id]
            else:
                logger.warning(
                    "gate rejected exit of %s (%s); will retry",
                    position.decision_id,
                    decision.code,
                )
            return False

        try:
            receipt = self._adapter.submit_order(decision)
        except BrokerError as error:
            self._gate.cancel(decision)
            self._audit.record_exit(
                position.decision_id,
                reason,
                detail,
                gate_decision=decision,
                submitted=False,
                broker_error=str(error),
            )
            logger.warning(
                "broker refused exit of %s: %s; will retry",
                position.decision_id,
                error,
            )
            return False

        self._audit.record_exit(
            position.decision_id,
            reason,
            detail,
            gate_decision=decision,
            submitted=True,
            broker_order_id=receipt.broker_order_id,
        )
        position.pending_exit = receipt.broker_order_id
        self._working[receipt.broker_order_id] = _WorkingExit(
            broker_order_id=receipt.broker_order_id,
            approved=decision,
            position=position,
            reason=reason,
            detail=detail,
        )
        return True

    def _order_for(self, position: TrackedPosition, quantity: Decimal, limit: Decimal):
        # A deep-loss premium can round to zero; the schema (rightly) refuses a
        # zero price, and a floor of one cent only LOWERS the proceeds floor of
        # a risk-reducing exit — the safe direction.
        limit = max(limit, CENTS)
        if position.is_option:
            entry = position.entry_order
            assert isinstance(entry, OptionBuyToOpenOrder)
            return OptionSellToCloseOrder(
                symbol=entry.symbol,
                underlying=entry.underlying,
                right=entry.right,
                expiration=entry.expiration,
                strike=entry.strike,
                contracts=int(quantity),
                multiplier=entry.multiplier,
                execution=LimitExecution(limit_price=limit),
                signal_id=position.signal_id,
                confidence=position.confidence,
            )
        return EquitySellToCloseOrder(
            symbol=position.symbol,
            quantity=quantity,
            execution=LimitExecution(limit_price=limit),
            signal_id=position.signal_id,
            confidence=position.confidence,
        )

    def reconcile(self) -> list[str]:
        """Settle terminal exit orders. Returns decision ids of fully closed positions."""
        closed: list[str] = []
        for order_id, working in list(self._working.items()):
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.warning("could not poll exit order %s: %s", order_id, error)
                continue
            if not status.is_terminal:
                continue
            if self._settle(
                working, status.status, status.filled_quantity, status.filled_avg_price
            ):
                closed.append(working.position.decision_id)
        return closed

    def _settle(
        self,
        working: _WorkingExit,
        status: str,
        filled_quantity: Decimal,
        filled_avg_price: Optional[Decimal],
    ) -> bool:
        """Book a terminal exit order. True if the position is now fully closed."""
        del self._working[working.broker_order_id]
        position = working.position
        position.pending_exit = None
        filled = filled_quantity

        if filled <= 0 or filled_avg_price is None:
            # Nothing printed. Release the close reservation; the breach (or the
            # close verdict) is still standing, so the next cycle re-fires.
            self._gate.cancel(working.approved)
            logger.info(
                "exit order for %s terminated %s unfilled; retrying next cycle",
                position.decision_id,
                status,
            )
            return False

        self._gate.record_fill(working.approved, filled_avg_price, filled_units=filled)
        self._audit.record_fill(
            position.decision_id,
            working.broker_order_id,
            Decimal(filled),
            filled_avg_price,
            filled_value=filled_avg_price * filled * position.multiplier,
            side="sell",
        )
        position.quantity -= filled
        position.proceeds += filled_avg_price * filled * position.multiplier

        if position.quantity > 0:
            # Partial: the remainder is still held, still tracked, still stopped.
            logger.info(
                "exit of %s filled %s, %s still held; will re-close next cycle",
                position.decision_id,
                filled,
                position.quantity,
            )
            return False

        realised = position.proceeds - position.entry_cost
        self._audit.record_outcome(
            position.decision_id,
            realised,
            closed_at=self._clock(),
            note=f"closed by exit engine: {working.reason} — {working.detail}",
            credibility=self._credibility,
        )
        del self._tracked[position.decision_id]
        logger.info(
            "position %s closed: %s realised (%s)",
            position.decision_id,
            realised,
            working.reason,
        )
        return True

    def cancel_working(self) -> list[str]:
        """Cancel outstanding exit orders and account for them. Used on shutdown.

        Same reasoning as the pipeline's: an ``ApprovedOrder`` cannot outlive its
        process, so nothing may be left resting. The positions themselves stay held —
        they are replayed from the log at the next startup, stops re-armed.
        """
        released: list[str] = []
        for order_id, working in list(self._working.items()):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error("could not cancel exit order %s: %s", order_id, error)
            status = None
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.error("could not re-poll exit %s after cancel: %s", order_id, error)

            if status is not None and status.is_terminal:
                self._settle(
                    working, status.status, status.filled_quantity, status.filled_avg_price
                )
            else:
                self._gate.cancel(working.approved)
                del self._working[order_id]
                working.position.pending_exit = None
            released.append(order_id)
        return released

    # -- internals -------------------------------------------------------------------------

    def _mark_for(self, position: TrackedPosition) -> Optional[Decimal]:
        """Per-unit mark: premium mid for options, the price source for equity."""
        if position.is_option:
            if self._option_prices is None:
                return None
            try:
                return self._option_prices(position.symbol)
            except Exception:  # noqa: BLE001 - degrade, never crash the cycle
                logger.exception("option mark failed for %s", position.symbol)
                return None
        return self._price_for(position.symbol)

    def _price_for(self, symbol: str) -> Optional[Decimal]:
        """A quote, or None. A price-source bug must not kill the loop."""
        try:
            return self._prices(symbol)
        except Exception:  # noqa: BLE001 - degrade, never crash the cycle
            logger.exception("price source failed for %s", symbol)
            return None


def unmanaged_exposure(
    gate: RiskGate, tracked: "Iterable[TrackedPosition]"
) -> dict[str, int]:
    """Equity units the gate holds that no tracked position accounts for.

    Nonzero means something is held with NO STOPS ARMED — typically a fill from a
    crashed process that never reached the audit log, or a manual trade in the same
    account. The exit engine will not invent a thesis for it, so it is surfaced for a
    human instead: close it manually, or accept that it is unprotected.
    """
    covered: dict[str, int] = {}
    for position in tracked:
        covered[position.symbol] = covered.get(position.symbol, 0) + position.quantity

    unmanaged: dict[str, int] = {}
    for key, held in gate.state.positions.items():
        # Options included (2026-08-24): an untracked option is the WORSE kind of
        # unmanaged — it decays while nobody's stops are armed.
        if key[0] not in ("equity", "option") or held.quantity <= 0:
            continue
        symbol = key[1]
        excess = held.quantity - covered.get(symbol, 0)
        if excess > 0:
            unmanaged[symbol] = excess
    return unmanaged


def _phantom_rejection(position: TrackedPosition) -> Rejection:
    """A rejection-shaped value for the record when there is nothing to submit."""
    return Rejection(
        code=RejectionCode.POSITION_NOT_HELD,
        message=(
            f"no units of {position.symbol} available to close for "
            f"{position.decision_id}; the broker no longer shows the position"
        ),
        limit=ZERO,
        observed=Decimal(position.quantity),
    )
