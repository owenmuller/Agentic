"""The idle-cash yield sweep (human ruling 2026-09-02).

Idle cash above a deterministic liquidity buffer parks in a T-bill ETF (SGOV);
cash below the buffer unswepts back out. No LLM anywhere, no alpha claim, no
new risk: the ETF is bought only with cash on hand through the ordinary gate
(cash-secured reservation and all), it is NEVER buying power, and its whole
job is to stop the account paying an idleness tax while it waits for signals.

The buffer, per the ruling
--------------------------
    buffer = judged sleeve's FULL daily deployment cap
           + mechanical sleeve's FULL daily deployment cap
           + working-order reservations (gate.reserved_cash)
           + a configured margin

Full caps, not remaining: on the day the system wants to deploy maximally, the
cash must already be sitting there — the conservative sweep design costs almost
nothing precisely because the buffer is sized to the worst deployment day.

Kill switch: sweep BUYS are opening orders and stop while halted (checked here
so a halt does not accumulate one rejection record per tick, and enforced by
the gate regardless). Unsweep SELLS are risk-reducing and stay permitted, like
every close — cash stays cash in an emergency.

Lots and the audit story
------------------------
Each sweep buy is its own DecisionRecord (``strategy="cash_sweep"``, synthetic
signal, no research). Unsweeps close lots OLDEST FIRST, one lot per tick at
most, writing ExitRecords (``cash_unsweep``) and fills against the lot's
decision_id; a lot sold flat writes its OutcomeRecord, whose realised P&L is
captured yield. Attribution reads the accrual off these trails as its own line
— never a signal class, never alpha.

One working order at a time, by design: sweeping is a slow correction loop,
and a second order racing the first is how a buffer overshoots.

PDT note: a sweep buy and an unsweep sell of the merged position on the same
day counts a day trade in the gate's ledger. In the preferred cash account the
count is not enforced; the buffer's margin exists partly to make same-day
round trips rare. Settled/unsettled cash split is deferred to the live-gate
review — paper settles instantly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Optional

from audit.log import AuditLog
from audit.records import ExitReason
from execution.base import BrokerAdapter, BrokerError
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.limits import CashManagementLimits
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import Sleeve

ZERO = Decimal("0")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.sweep")


@dataclass(slots=True)
class SweepLot:
    """One sweep buy, tracked until sold flat."""

    decision_id: str
    quantity: Decimal
    entry_cost: Decimal
    proceeds: Decimal
    opened_at: datetime


@dataclass(slots=True)
class _Working:
    approved: ApprovedOrder
    decision_id: str
    side: str  # "sweep" | "unsweep"


class CashSweeper:
    """Owns the sweep loop: buffer arithmetic, lot tracking, settlement."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: Callable[[str], Optional[Decimal]],
        config: CashManagementLimits,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        note: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        self._config = config
        self._clock = clock
        self._id_factory = id_factory
        self._note = note or (lambda message: None)
        self._lots: dict[str, SweepLot] = {}
        self._working: dict[str, _Working] = {}
        self._warned_halted = False

    # -- introspection ---------------------------------------------------------------

    @property
    def lots(self) -> tuple[SweepLot, ...]:
        return tuple(self._lots.values())

    @property
    def symbol(self) -> str:
        return self._config.symbol

    def buffer(self) -> Decimal:
        """The cash floor the sweeper defends. Deterministic, per the ruling."""
        limits = self._gate.limits
        return (
            self._gate.sleeve_nav(Sleeve.EQUITY)
            * limits.equity_sleeve.max_daily_deployment
            + self._gate.sleeve_nav(Sleeve.MECHANICAL)
            * limits.mechanical_sleeve.max_daily_deployment
            + self._gate.state.reserved_cash
            + self._config.buffer_margin_usd
        )

    # -- replay ------------------------------------------------------------------------

    def replay(self, trails) -> int:
        """Rebuild open lots from cash_sweep trails, clamped to the gate."""
        restored = 0
        for trail in trails:
            decision = trail.decision
            if decision.sizing.strategy != "cash_sweep":
                continue
            if not decision.was_approved or trail.outcome is not None:
                continue
            buys = [f for f in trail.fills if f.side == "buy"]
            if not buys:
                continue
            sells = [f for f in trail.fills if f.side == "sell"]
            quantity = sum((f.filled_quantity for f in buys), ZERO) - sum(
                (f.filled_quantity for f in sells), ZERO
            )
            if quantity <= 0:
                continue
            position = self._gate.state.position(
                (Sleeve.CASH_MANAGEMENT.value, self._config.symbol)
            )
            if position is None or position.quantity <= 0:
                logger.warning(
                    "audit log says sweep lot %s holds %s %s but the gate does "
                    "not; not tracking — the broker is authoritative",
                    decision.decision_id,
                    quantity,
                    self._config.symbol,
                )
                continue
            self._lots[decision.decision_id] = SweepLot(
                decision_id=decision.decision_id,
                quantity=min(quantity, position.quantity),
                entry_cost=sum((f.filled_value for f in buys), ZERO),
                proceeds=sum((f.filled_value for f in sells), ZERO),
                opened_at=buys[0].recorded_at,
            )
            restored += 1
        return restored

    # -- one pass ----------------------------------------------------------------------

    def tick(self, now: datetime) -> int:
        """Settle, mark, then correct toward the buffer. Returns orders placed."""
        self._reconcile(now)
        quote = self._quote()
        if quote is not None:
            self._gate.mark_to_market(
                {(Sleeve.CASH_MANAGEMENT.value, self._config.symbol): quote}
            )
        if self._working:
            return 0  # one working order at a time; correct again next tick
        if quote is None:
            return 0  # no price, no order — never trade on a guess

        cash = self._gate.state.cash
        buffer = self.buffer()
        excess = cash - buffer
        if excess >= self._config.min_order_notional_usd:
            return self._sweep(excess, quote)
        deficit = buffer - cash
        if deficit >= self._config.min_order_notional_usd and self._lots:
            return self._unsweep(deficit, quote)
        return 0

    def _sweep(self, excess: Decimal, quote: Decimal) -> int:
        if self._gate.kill_switch_tripped:
            # The gate would reject anyway; skipping avoids a record per tick.
            if not self._warned_halted:
                self._warned_halted = True
                self._note(
                    "SWEEP paused: kill switch is tripped, and parking cash is "
                    "an opening order; unsweeps remain available"
                )
            return 0
        self._warned_halted = False
        limit = quote.quantize(CENTS, rounding=ROUND_UP)
        step = self._adapter.equity_quantity_step
        quantity = (excess / limit).quantize(step, rounding=ROUND_DOWN)
        if quantity <= ZERO:
            return 0
        detail = (
            f"cash {self._gate.state.cash:.2f} exceeds the {self.buffer():.2f} "
            f"liquidity buffer; parking {quantity} {self._config.symbol} at "
            f"limit {limit}"
        )
        order = EquityBuyOrder(
            symbol=self._config.symbol,
            quantity=quantity,
            execution=LimitExecution(limit_price=limit),
            sleeve="cash_management",
        )
        decision = self._gate.submit(order)
        record = self._audit.record_sweep(
            side="sweep",
            detail=detail,
            gate_decision=decision,
            capital=(quantity * limit).quantize(CENTS),
            decision_id=self._id_factory(),
        )
        if not decision.is_approved:
            self._note(f"SWEEP gate rejected: {decision.code} — recorded")
            return 0
        try:
            receipt = self._adapter.submit_order(decision)
        except BrokerError as error:
            self._gate.cancel(decision)
            logger.warning("broker refused sweep buy: %s; retrying next tick", error)
            return 0
        self._working[receipt.broker_order_id] = _Working(
            approved=decision, decision_id=record.decision_id, side="sweep"
        )
        return 1

    def _unsweep(self, deficit: Decimal, quote: Decimal) -> int:
        # Oldest lot first, one lot per tick: deterministic, and a deficit that
        # spans lots resolves over the next few ticks rather than in one racing
        # batch of orders.
        lot = min(self._lots.values(), key=lambda item: item.opened_at)
        limit = quote.quantize(CENTS, rounding=ROUND_DOWN)
        limit = max(limit, CENTS)
        step = self._adapter.equity_quantity_step
        wanted = (deficit / limit).quantize(step, rounding=ROUND_UP)
        quantity = min(lot.quantity, wanted)
        if quantity <= ZERO:
            return 0
        order = EquitySellToCloseOrder(
            symbol=self._config.symbol,
            quantity=quantity,
            execution=LimitExecution(limit_price=limit),
            sleeve="cash_management",
        )
        decision = self._gate.submit(order)
        detail = (
            f"cash is {deficit:.2f} below the liquidity buffer; unsweeping "
            f"{quantity} {self._config.symbol} at limit {limit} "
            f"(risk-reducing; permitted under a halt)"
        )
        submitted = False
        broker_order_id = None
        broker_error = None
        if decision.is_approved:
            try:
                receipt = self._adapter.submit_order(decision)
                submitted = True
                broker_order_id = receipt.broker_order_id
                self._working[receipt.broker_order_id] = _Working(
                    approved=decision, decision_id=lot.decision_id, side="unsweep"
                )
            except BrokerError as error:
                self._gate.cancel(decision)
                broker_error = str(error)
        self._audit.record_exit(
            lot.decision_id,
            ExitReason.CASH_UNSWEEP,
            detail,
            decision,
            submitted=submitted,
            broker_order_id=broker_order_id,
            broker_error=broker_error,
        )
        return 1 if submitted else 0

    # -- settlement ----------------------------------------------------------------------

    def _reconcile(self, now: datetime) -> int:
        settled = 0
        for order_id, working in list(self._working.items()):
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.warning("could not poll sweep order %s: %s", order_id, error)
                continue
            if not status.is_terminal:
                continue
            del self._working[order_id]
            settled += 1
            filled = status.filled_quantity
            price = status.filled_avg_price
            if filled <= 0 or price is None:
                self._gate.cancel(working.approved)
                continue  # the buffer imbalance is still there; next tick retries
            self._gate.record_fill(working.approved, price, filled_units=filled)
            value = filled * price
            self._audit.record_fill(
                working.decision_id,
                order_id,
                filled,
                price,
                filled_value=value,
                side="buy" if working.side == "sweep" else "sell",
            )
            if working.side == "sweep":
                self._lots[working.decision_id] = SweepLot(
                    decision_id=working.decision_id,
                    quantity=filled,
                    entry_cost=value,
                    proceeds=ZERO,
                    opened_at=now,
                )
            else:
                lot = self._lots.get(working.decision_id)
                if lot is None:
                    continue
                lot.quantity -= filled
                lot.proceeds += value
                if lot.quantity <= 0:
                    # The lot's realised P&L IS captured yield: what the parked
                    # cash earned over idleness. Its own attribution line, never
                    # a signal class.
                    self._audit.record_outcome(
                        working.decision_id,
                        lot.proceeds - lot.entry_cost,
                        closed_at=now,
                        note="cash-management lot sold flat (unsweep)",
                    )
                    del self._lots[working.decision_id]
        return settled

    def cancel_working(self) -> list[str]:
        """Shutdown: cancel and account for outstanding sweep orders."""
        released = []
        for order_id, working in list(self._working.items()):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error("could not cancel sweep order %s: %s", order_id, error)
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError:
                status = None
            if status is not None and status.is_terminal:
                self._working[order_id] = working
                self._reconcile(self._clock())
            else:
                self._gate.cancel(working.approved)
                self._working.pop(order_id, None)
            released.append(order_id)
        return released

    # -- internals ---------------------------------------------------------------------

    def _quote(self) -> Optional[Decimal]:
        try:
            quote = self._prices(self._config.symbol)
        except Exception:  # noqa: BLE001 - a price bug must not kill the loop
            logger.exception("sweep quote failed for %s", self._config.symbol)
            return None
        if quote is None or quote <= ZERO:
            return None
        return quote
