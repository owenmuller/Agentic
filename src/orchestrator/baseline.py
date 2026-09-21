"""The baseline sleeve (AGGRESSION RULING 2026-09-18, lever 4; kill-switch
semantics revised the same day).

A deterministic market-beta sleeve: a target fraction of NAV held in one broad
index ETF (SPY), rebalanced WEEKLY to target when it drifts outside a band. No
LLM, no thesis, no research spend, no signal. Its whole purpose is to stop the
account paying an idleness tax on cash it has decided not to deploy through
judgment — and to be measured APART, so that beta can never be reported as
alpha: attribution renders the sleeve on its own line and partitions it out of
every alpha calculation before the headline (beta-adjusted excess) is computed.

The rules, all from the ruling
------------------------------
  target        ``portfolio.sleeves.baseline`` x NAV (30% — human ruling
                2026-09-21 at shipping; the 2026-09-18 draft said 40%).
  cadence       one check per ISO week, at the first tick of the week the loop
                runs with a usable quote. Outside ``rebalance_band`` of NAV
                (+/-5 percentage points of NAV — Constraint #6 reads "+/-5%"
                as the WIDER band, fewer trades; approved 2026-09-21) the
                sleeve trades TO TARGET
                over the following ticks; inside it, nothing happens until
                next week. The first check after the sleeve is switched on is
                its initial build, from 0% — one rebalance, not a staged one.
  funding       a buy spends only cash above the liquidity floor (the cash
                sweep's own buffer, ``orchestrator.sweep.liquidity_buffer``).
                What it still owes is exposed as ``funding_need`` and the
                sweeper adds that to its buffer, so SGOV unsweeps supply it a
                lot at a time; the buy resumes as the cash lands. SGOV holds
                what the judged sleeve has not deployed — never the baseline's
                allotment.
  kill switch   while tripped the sleeve NEITHER buys NOR sells: no rebalance,
                no drift correction, no unwind — frozen at current holdings
                until a human resets the switch. The gate already refuses the
                buys; the sells are risk-reducing and the gate would pass them,
                so the freeze on the sell side lives HERE, by ruling. The
                position stays inside NAV, drawdown and the 12% trip.
  gate          every order passes the RiskGate: cash-secured like everything
                else, the dust floor, the sleeve's own allocation ceiling
                (target + drift tolerance); exempt from the alpha caps (single
                position, sector, daily deployment) exactly like the sweep —
                those bound concentration risk, and a 30% index position IS
                the ruling.

Lots and the audit story mirror the sweep: each buy is a DecisionRecord
(``strategy="baseline"``, synthetic signal, no research); sells relieve lots
oldest first, one lot per tick, writing ExitRecords (``baseline_rebalance``)
and fills against the lot's decision id; a lot sold flat writes its
OutcomeRecord. One working order at a time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Iterable, Optional

from audit.log import AuditLog
from audit.records import ExitReason
from execution.base import BrokerAdapter, BrokerError
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.limits import BaselineSleeveLimits
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import Sleeve

ZERO = Decimal("0")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.baseline")


def iso_week(now: datetime) -> str:
    """``2026-W39``: the label the weekly check is keyed on."""
    year, week, _ = now.date().isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(slots=True)
class BaselineLot:
    """One baseline buy, tracked until sold flat."""

    decision_id: str
    quantity: Decimal
    entry_cost: Decimal
    proceeds: Decimal
    opened_at: datetime


@dataclass(slots=True)
class _Working:
    approved: ApprovedOrder
    decision_id: str
    side: str  # "buy" | "sell"


class BaselineSleeve:
    """Owns the baseline sleeve: the weekly check, the rebalance, settlement."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: Callable[[str], Optional[Decimal]],
        config: BaselineSleeveLimits,
        weight: Decimal,
        liquidity_floor: Callable[[], Decimal],
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        note: Optional[Callable[[str], None]] = None,
        bids: Optional[Callable[[str], Optional[Decimal]]] = None,
        week_checked: Optional[str] = None,
        rebalancing: bool = False,
    ) -> None:
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        self._bids = bids
        self._config = config
        self._weight = weight
        #: The cash the sweep defends. A baseline buy never dips below it.
        self._liquidity_floor = liquidity_floor
        self._clock = clock
        self._id_factory = id_factory
        self._note = note or (lambda message: None)
        self._lots: dict[str, BaselineLot] = {}
        self._working: dict[str, _Working] = {}
        #: Persisted in session_state.json so a restart neither re-runs a
        #: week's check nor forgets a rebalance it was in the middle of.
        self._week_checked = week_checked
        self._rebalancing = rebalancing
        self._warned_frozen = False

    # -- introspection ---------------------------------------------------------------

    @property
    def symbol(self) -> str:
        return self._config.symbol

    @property
    def weight(self) -> Decimal:
        return self._weight

    @property
    def lots(self) -> tuple[BaselineLot, ...]:
        return tuple(self._lots.values())

    @property
    def week_checked(self) -> Optional[str]:
        return self._week_checked

    @property
    def rebalancing(self) -> bool:
        return self._rebalancing

    @property
    def frozen(self) -> bool:
        """The ruling's freeze: tripped kill switch = no buys, no sells."""
        return self._gate.kill_switch_tripped

    def target_value(self) -> Decimal:
        return self._gate.state.nav * self._weight

    def held_value(self) -> Decimal:
        """Market value plus cash committed to a working buy."""
        position = self._gate.state.position((Sleeve.BASELINE.value, self.symbol))
        return position.exposure if position is not None else ZERO

    def band_value(self) -> Decimal:
        return self._gate.state.nav * self._config.rebalance_band

    def funding_need(self) -> Decimal:
        """Cash this sleeve still has to buy in the rebalance it is running —
        what the sweeper adds to its buffer so SGOV unsweeps supply it. Zero
        between rebalances, zero on the sell side, zero while frozen (a frozen
        sleeve buys nothing, so nothing should be unparked for it)."""
        if not self._rebalancing or self.frozen:
            return ZERO
        gap = self.target_value() - self.held_value()
        return gap if gap >= self._config.min_order_notional_usd else ZERO

    # -- replay ------------------------------------------------------------------------

    def replay(self, trails) -> int:
        """Rebuild open lots from baseline trails, clamped to the gate."""
        restored = 0
        for trail in trails:
            decision = trail.decision
            if decision.sizing.strategy != "baseline":
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
            position = self._gate.state.position((Sleeve.BASELINE.value, self.symbol))
            if position is None or position.quantity <= 0:
                logger.warning(
                    "audit log says baseline lot %s holds %s %s but the gate does "
                    "not; not tracking — the broker is authoritative",
                    decision.decision_id,
                    quantity,
                    self.symbol,
                )
                continue
            self._lots[decision.decision_id] = BaselineLot(
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
        """Settle, mark, run the weekly check when due, trade toward target
        while a rebalance is open. Returns orders placed (0 or 1)."""
        self._reconcile(now)
        quote = self._quote()
        if quote is not None:
            self._gate.mark_to_market({(Sleeve.BASELINE.value, self.symbol): quote})
        if self._working:
            return 0  # one working order at a time
        if self.frozen:
            if not self._warned_frozen:
                self._warned_frozen = True
                self._note(
                    "BASELINE frozen: the kill switch is tripped — no rebalance, "
                    "no drift correction, no unwind until a human resets it "
                    "(ruling 2026-09-18)"
                )
            return 0
        self._warned_frozen = False
        if quote is None:
            return 0  # no price, no check, no order — never trade on a guess
        nav = self._gate.state.nav
        if nav <= ZERO:
            return 0

        week = iso_week(now)
        if week != self._week_checked:
            self._week_checked = week
            held = self.held_value()
            target = self.target_value()
            outside = abs(held - target) > self.band_value()
            self._rebalancing = outside
            self._note(
                f"BASELINE weekly check {week}: {self.symbol} {held:.2f} = "
                f"{(held / nav):.1%} of NAV against target {self._weight:.0%} "
                f"+/-{self._config.rebalance_band:.0%} — "
                + ("rebalancing to target" if outside else "inside the band, no trade")
            )
        if not self._rebalancing:
            return 0

        gap = self.target_value() - self.held_value()
        if abs(gap) < self._config.min_order_notional_usd:
            self._rebalancing = False
            self._note(
                f"BASELINE at target: {self.symbol} {self.held_value():.2f} = "
                f"{(self.held_value() / nav):.1%} of NAV; next check next week"
            )
            return 0
        if gap > ZERO:
            return self._buy(gap, quote)
        return self._sell(-gap, quote)

    def _buy(self, gap: Decimal, quote: Decimal) -> int:
        state = self._gate.state
        spendable = state.cash - self._liquidity_floor()
        amount = min(gap, spendable)
        if amount < self._config.min_order_notional_usd:
            # The liquidity buffer holds the rest; the sweeper is unparking
            # SGOV for it (funding_need) and the buy resumes as cash lands.
            return 0
        limit = quote.quantize(CENTS, rounding=ROUND_UP)
        step = self._adapter.equity_quantity_step
        quantity = (amount / limit).quantize(step, rounding=ROUND_DOWN)
        if quantity <= ZERO:
            return 0
        detail = (
            f"baseline sleeve below target: holds {self.held_value():.2f} of "
            f"{self.symbol} against a {self.target_value():.2f} target "
            f"({self._weight:.0%} of NAV {state.nav:.2f}); buying {quantity} at "
            f"limit {limit} — {amount:.2f} of the {gap:.2f} gap, the rest as the "
            f"liquidity buffer refills"
        )
        order = EquityBuyOrder(
            symbol=self.symbol,
            quantity=quantity,
            execution=LimitExecution(limit_price=limit),
            sleeve="baseline",
        )
        decision = self._gate.submit(order)
        record = self._audit.record_baseline(
            side="buy",
            detail=detail,
            gate_decision=decision,
            capital=(quantity * limit).quantize(CENTS),
            decision_id=self._id_factory(),
        )
        if not decision.is_approved:
            self._note(f"BASELINE gate rejected buy: {decision.code} — recorded")
            return 0
        try:
            receipt = self._adapter.submit_order(
                decision, client_reference=record.decision_id
            )
        except BrokerError as error:
            self._gate.cancel(decision)
            logger.warning("broker refused baseline buy: %s; retrying next tick", error)
            return 0
        self._working[receipt.broker_order_id] = _Working(
            approved=decision, decision_id=record.decision_id, side="buy"
        )
        return 1

    def _sell(self, excess: Decimal, quote: Decimal) -> int:
        if not self._lots:
            # The gate holds it but no lot in the log claims it: a human's
            # problem (health lists the mismatch), not a sale.
            return 0
        lot = min(self._lots.values(), key=lambda item: item.opened_at)
        limit = self._sell_limit(quote)
        step = self._adapter.equity_quantity_step
        wanted = (excess / limit).quantize(step, rounding=ROUND_DOWN)
        quantity = min(lot.quantity, wanted)
        if quantity <= ZERO:
            return 0
        order = EquitySellToCloseOrder(
            symbol=self.symbol,
            quantity=quantity,
            execution=LimitExecution(limit_price=limit),
            sleeve="baseline",
        )
        decision = self._gate.submit(order)
        detail = (
            f"baseline sleeve above target: holds {self.held_value():.2f} of "
            f"{self.symbol} against a {self.target_value():.2f} target "
            f"({self._weight:.0%} of NAV {self._gate.state.nav:.2f}); selling "
            f"{quantity} at limit {limit} toward target (weekly rebalance)"
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
                    approved=decision, decision_id=lot.decision_id, side="sell"
                )
            except BrokerError as error:
                self._gate.cancel(decision)
                broker_error = str(error)
        self._audit.record_exit(
            lot.decision_id,
            ExitReason.BASELINE_REBALANCE,
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
                logger.warning("could not poll baseline order %s: %s", order_id, error)
                continue
            if not status.is_terminal:
                continue
            del self._working[order_id]
            settled += 1
            filled = status.filled_quantity
            price = status.filled_avg_price
            if filled <= 0 or price is None:
                self._gate.cancel(working.approved)
                self._audit.record_unfilled_order(
                    working.decision_id,
                    order_id,
                    status.status,
                    f"baseline {working.side} order {order_id} terminated "
                    f"{status.status} without filling; reservation released, the "
                    f"gap is re-measured next pass",
                )
                continue
            self._gate.record_fill(working.approved, price, filled_units=filled)
            value = filled * price
            self._audit.record_fill(
                working.decision_id,
                order_id,
                filled,
                price,
                filled_value=value,
                side=working.side,
            )
            if working.side == "buy":
                self._lots[working.decision_id] = BaselineLot(
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
                    # Realised market beta, on its own line — never alpha.
                    self._audit.record_outcome(
                        working.decision_id,
                        lot.proceeds - lot.entry_cost,
                        closed_at=now,
                        note="baseline lot sold flat (weekly rebalance)",
                    )
                    del self._lots[working.decision_id]
        return settled

    def cancel_working(self) -> list[str]:
        """Shutdown or halt: cancel and account for outstanding orders."""
        released = []
        for order_id, working in list(self._working.items()):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error("could not cancel baseline order %s: %s", order_id, error)
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

    def _sell_limit(self, quote: Decimal) -> Decimal:
        """Marketable: the bid rounded down, else a cent under the ask."""
        bid = self._bid()
        if bid is not None:
            limit = bid.quantize(CENTS, rounding=ROUND_DOWN)
        else:
            limit = quote.quantize(CENTS, rounding=ROUND_DOWN) - CENTS
        return max(limit, CENTS)

    def _bid(self) -> Optional[Decimal]:
        if self._bids is None:
            return None
        try:
            bid = self._bids(self.symbol)
        except Exception:  # noqa: BLE001 - a price bug must not kill the loop
            logger.exception("baseline bid failed for %s", self.symbol)
            return None
        if bid is None or bid <= ZERO:
            return None
        return bid

    def _quote(self) -> Optional[Decimal]:
        try:
            quote = self._prices(self.symbol)
        except Exception:  # noqa: BLE001
            logger.exception("baseline quote failed for %s", self.symbol)
            return None
        if quote is None or quote <= ZERO:
            return None
        return quote
