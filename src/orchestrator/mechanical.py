"""The mechanical disclosure follower (human ruling 2026-08-27).

A controlled experiment run alongside the judged system: deterministic,
diversified copying of congressional purchase disclosures, equal-weight slices,
long holds, NO LLM anywhere in the path. The thesis under test is that
mechanical portfolio-level copying works; running both arms lets attribution
say which shape produces alpha instead of assuming the judged bar adds value.

Rules of the experiment, all from rulings:

  qualification   DELIBERATELY identical to the judged prefilter — this engine
                  calls the very same ``ResearchPreFilter`` instance the loop
                  uses, so the funnels cannot diverge. On top of it: purchase
                  only, a parseable ticker, and a tradeable-equity check at the
                  venue. The experiment varies judgment and exits, nothing else.
  sizing          equal-weight slices of sleeve NAV / max_positions, capped by
                  slots: total, per filer, and per MAPPED sector (unmapped
                  names are unconstrained singletons, the sectors.yaml
                  convention). One slice per name; re-disclosures in a held
                  name are ignored.
  exits           time only, ``hold_days`` after fill. NO price stop — the
                  stop IS the slice size, and a percentage stop would amputate
                  exactly the winners the strategy exists to hold. The global
                  kill switch still halts entries, and closes remain permitted
                  while halted, like everything else at the gate.
  circuit breaker sleeve value (virtual cash ledger + open mechanical market
                  value) down more than ``drawdown_halt_fraction`` from the
                  sleeve's OWN high-water mark halts new entries. Same
                  discipline as the global kill switch: sticky, surfaced in
                  health, cleared only by a human (``mechanical_halted`` in
                  session_state.json).

Every entry passes the RiskGate like any other order — cash-secured, dust
floor, its own daily deployment budget, its own sector budget, the 25%+drift
allocation ceiling. Records are ordinary DecisionRecords with
``sizing.strategy == "mechanical"`` and no research snapshot, and they never
seal a signal for the judged path: the arms stay independent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Iterable, Optional

from audit.log import AuditLog
from audit.records import AuditTrail, ExitReason, RejectedStage
from execution.base import BrokerAdapter, BrokerError
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.sectors import SectorMap
from risk_gate.state import Sleeve
from signals import Signal, SignalClass

ZERO = Decimal("0")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.mechanical")


@dataclass(slots=True)
class MechanicalPosition:
    """One slice, tracked for the time exit and the sleeve ledger."""

    decision_id: str
    symbol: str
    filer: str
    quantity: Decimal
    entry_cost: Decimal
    proceeds: Decimal
    opened_at: datetime


@dataclass(slots=True)
class _Working:
    approved: ApprovedOrder
    decision_id: str
    signal: Signal
    side: str  # "open" | "close"
    symbol: str
    filer: str


@dataclass(slots=True)
class MechanicalTickReport:
    entries_submitted: int = 0
    settled: int = 0
    exits_started: int = 0
    positions_closed: int = 0


class MechanicalEngine:
    """Owns the mechanical sleeve: qualification, entries, time exits, breaker."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: Callable[[str], Optional[Decimal]],
        limits,  # RiskLimits — mechanical_sleeve + portfolio weights are read
        prefilter,  # THE SAME ResearchPreFilter instance the loop uses
        sectors: Optional[SectorMap] = None,
        clock: Optional[Callable[[], datetime]] = None,
        note: Optional[Callable[[str], None]] = None,
        id_factory: Optional[Callable[[], str]] = None,
        virtual_cash: Optional[Decimal] = None,
        high_water_mark: Optional[Decimal] = None,
        halted: bool = False,
    ) -> None:
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        self._caps = limits.mechanical_sleeve
        self._prefilter = prefilter
        self._sectors = sectors if sectors is not None else SectorMap.load()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._note = note or (lambda message: None)
        import uuid

        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:16])
        self._tracked: dict[str, MechanicalPosition] = {}
        self._working: dict[str, _Working] = {}
        #: Approved entries by signal external id — a re-emitted disclosure the
        #: sleeve already bought is not bought twice. Rejected entries are NOT
        #: added, so they retry at the next restart.
        self._entered_external: set[str] = set()
        #: Signals looked at this process — each is considered exactly once.
        self._considered: set[str] = set()
        self._virtual_cash = virtual_cash
        self._hwm = high_water_mark
        self._halted = halted
        self._halted_recorded_for: set[str] = set()
        #: (decision_id, disclosure external_id) already recorded as filer
        #: events — one filing, one record, however many drains re-emit it.
        self._filer_events_seen: Optional[set[tuple[str, str]]] = None

    # -- introspection -----------------------------------------------------------

    @property
    def tracked(self) -> tuple[MechanicalPosition, ...]:
        return tuple(self._tracked.values())

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def virtual_cash(self) -> Optional[Decimal]:
        return self._virtual_cash

    @property
    def high_water_mark(self) -> Optional[Decimal]:
        return self._hwm

    @property
    def sleeve_value(self) -> Optional[Decimal]:
        """Virtual cash + open mechanical market value; None before first entry."""
        if self._virtual_cash is None:
            return None
        open_value = sum(
            (
                p.market_value
                for key, p in self._gate.state.positions.items()
                if key[0] == "mechanical"
            ),
            ZERO,
        )
        return self._virtual_cash + open_value

    # -- replay --------------------------------------------------------------------

    def replay(self, trails: Iterable[AuditTrail]) -> int:
        """Rebuild open slices and the entered-set from mechanical trails."""
        restored = 0
        for trail in trails:
            decision = trail.decision
            if decision.sizing.strategy != "mechanical":
                continue
            if decision.was_approved and decision.signal.external_id:
                self._entered_external.add(decision.signal.external_id)
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
            order = decision.gate.order or {}
            symbol = str(order.get("symbol", ""))
            gate_position = self._gate.state.position(("mechanical", symbol))
            if gate_position is None or gate_position.quantity <= 0:
                logger.warning(
                    "audit log says mechanical %s holds %s %s but the gate does "
                    "not; not tracking — the broker is authoritative",
                    decision.decision_id,
                    quantity,
                    symbol,
                )
                continue
            mechanical = decision.mechanical
            if self._virtual_cash is None:
                # The ledger anchors at the sleeve allocation on the first
                # settled entry. If the process that filled this one died
                # before settling it, that anchor never happened and startup
                # recovery wrote the fill instead — so reconstruct what the
                # ledger would have held: the allocation, less what was spent.
                self._virtual_cash = self._gate.sleeve_nav(Sleeve.MECHANICAL)
                self._reconstructed_ledger = True
            if getattr(self, "_reconstructed_ledger", False):
                self._virtual_cash -= sum((f.filled_value for f in buys), ZERO)
            self._tracked[decision.decision_id] = MechanicalPosition(
                decision_id=decision.decision_id,
                symbol=symbol,
                filer=(mechanical.filer if mechanical else None) or "",
                quantity=min(quantity, gate_position.quantity),
                entry_cost=sum((f.filled_value for f in buys), ZERO),
                proceeds=sum((f.filled_value for f in sells), ZERO),
                opened_at=buys[0].recorded_at,
            )
            restored += 1
        return restored

    def note_disclosures(self, signals: Iterable[Signal]) -> int:
        """Record new disclosures by originating filers in held names (2026-09-01).

        RECORD ONLY, by ruling: the strategy under test is hold-a-year regardless,
        and reacting to the filer's exit would put judgment in the control arm.
        The ``FilerEventRecord`` exists so attribution can later measure what
        ignoring the filer's exit cost this arm — position untouched, breaker
        untouched, exits untouched. Returns the number of events recorded.
        """
        if self._filer_events_seen is None:
            self._filer_events_seen = self._audit.filer_event_keys()
        noted = 0
        for signal in signals:
            if signal.signal_class not in (
                SignalClass.CLASS_2_MOMENTUM,
                SignalClass.CLASS_3_THESIS,
            ):
                continue
            meta = signal.metadata
            filer = (meta.get("representative") or meta.get("fund") or "").strip()
            ticker = (meta.get("ticker") or "").strip().upper()
            if not filer or not ticker:
                continue
            for position in self._tracked.values():
                if position.symbol.upper() != ticker:
                    continue
                if position.filer.strip().lower() != filer.lower():
                    continue
                key = (position.decision_id, signal.external_id or signal.signal_id)
                if key in self._filer_events_seen:
                    continue
                transaction = meta.get("transaction", "").strip() or "transaction"
                self._audit.record_filer_event(
                    position.decision_id,
                    arm="mechanical",
                    filer=filer,
                    symbol=ticker,
                    transaction=transaction,
                    disclosure_source_id=signal.source_id,
                    disclosure_external_id=signal.external_id,
                    transaction_date=meta.get("transaction_date") or None,
                    report_date=meta.get("report_date") or None,
                    amount_range=meta.get("amount_range") or None,
                    detail=(
                        f"{filer} disclosed a {transaction} of {ticker} while the "
                        f"mechanical sleeve held it; recorded only — the arm "
                        f"holds to its time exit by design"
                    ),
                )
                self._filer_events_seen.add(key)
                noted += 1
        return noted

    # -- qualification + entries ---------------------------------------------------

    def consider(self, signals: Iterable[Signal], now: datetime) -> int:
        """Qualify congressional disclosures and enter the ones with a slot."""
        submitted = 0
        for signal in signals:
            if signal.source_id != "congressional_disclosures":
                continue
            key = signal.external_id or signal.signal_id
            if key in self._considered or key in self._entered_external:
                continue
            self._considered.add(key)

            if self._disqualified(signal, now):
                continue
            block = self._capacity_block(signal)
            if block is not None:
                code, message = block
                # mechanical_capacity / mechanical_halted are excluded from
                # dedup seeding: recorded for the review, never sealing the
                # signal for either arm.
                self._audit.record_stage_rejection(
                    self._id_factory(),
                    RejectedStage.PRE_FILTER,
                    code,
                    message,
                    signal,
                )
                continue
            if not self._headroom_today():
                # Courtesy pre-check to avoid a rejection record per signal on
                # busy days; the gate remains the enforcer. Not entered, not
                # sealed: the signal re-emits at the next startup.
                self._note(
                    "MECH out of daily deployment headroom; remaining qualifiers "
                    "retry at the next startup"
                )
                break
            submitted += self._enter(signal)
        return submitted

    def _disqualified(self, signal: Signal, now: datetime) -> bool:
        """The shared funnel plus the mechanical sleeve's own deterministic
        checks. Quiet by design: everything here is derivable from the judged
        trail or the rule set; only capacity outcomes write records."""
        metadata = signal.metadata
        if "purchase" not in metadata.get("transaction", "").lower():
            return True  # purchase-only, by ruling
        symbol = metadata.get("ticker", "").upper().strip()
        if not symbol:
            return True
        # THE SAME prefilter the judged path runs — identical funnel, by ruling.
        if self._prefilter.skip_verdict(signal, held=(), now=now) is not None:
            return True
        held = {p.symbol for p in self._tracked.values()} | {
            w.symbol for w in self._working.values() if w.side == "open"
        }
        if symbol in held:
            return True  # one slice per name; re-disclosures are ignored
        if not self._adapter.tradeable_equity(symbol):
            self._note(f"MECH {symbol} not tradeable at the venue; skipped")
            return True
        return False

    def _capacity_block(self, signal: Signal) -> Optional[tuple[str, str]]:
        if not self._caps.entries_enabled:
            return (
                "mechanical_disabled",
                "mechanical entries are switched off in risk_limits.yaml "
                "(mechanical_sleeve.entries_enabled); held positions ride and "
                "time exits still fire",
            )
        if self._halted:
            return (
                "mechanical_halted",
                "mechanical sleeve circuit breaker is tripped (drawdown beyond "
                f"{self._caps.drawdown_halt_fraction} from its own high-water "
                "mark); new entries halted pending human review — reset via "
                "mechanical_halted in session_state.json",
            )
        open_slots = len(self._tracked) + sum(
            1 for w in self._working.values() if w.side == "open"
        )
        if open_slots >= self._caps.max_positions:
            return (
                "mechanical_capacity",
                f"all {self._caps.max_positions} mechanical slots are filled; "
                f"qualified but not entered",
            )
        filer = signal.metadata.get("representative", "")
        filer_slots = sum(1 for p in self._tracked.values() if p.filer == filer) + sum(
            1
            for w in self._working.values()
            if w.side == "open" and w.filer == filer
        )
        if filer and filer_slots >= self._caps.max_per_filer:
            return (
                "mechanical_capacity",
                f"{filer} already fills {filer_slots} of "
                f"{self._caps.max_per_filer} per-filer slots",
            )
        symbol = signal.metadata.get("ticker", "").upper().strip()
        sector = self._sectors.sector_of(symbol)
        if not sector.startswith("unmapped:"):
            sector_slots = sum(
                1
                for p in self._tracked.values()
                if self._sectors.sector_of(p.symbol) == sector
            ) + sum(
                1
                for w in self._working.values()
                if w.side == "open" and self._sectors.sector_of(w.symbol) == sector
            )
            if sector_slots >= self._caps.max_per_sector_slots:
                return (
                    "mechanical_capacity",
                    f"sector {sector!r} already fills {sector_slots} of "
                    f"{self._caps.max_per_sector_slots} slots "
                    f"(membership: config/sectors.yaml)",
                )
        return None

    def _headroom_today(self) -> bool:
        slice_size = self._slice()
        cap = (
            self._gate.sleeve_nav(Sleeve.MECHANICAL)
            * self._caps.max_daily_deployment
        )
        return self._gate.state.mechanical_deployed_today + slice_size <= cap

    def _slice(self) -> Decimal:
        return (
            self._gate.sleeve_nav(Sleeve.MECHANICAL)
            / Decimal(self._caps.max_positions)
        ).quantize(CENTS, rounding=ROUND_DOWN)

    def _enter(self, signal: Signal) -> int:
        symbol = signal.metadata.get("ticker", "").upper().strip()
        quote = self._prices(symbol)
        if quote is None or quote <= ZERO:
            self._note(f"MECH no usable quote for {symbol}; not entered")
            return 0
        limit_price = quote.quantize(CENTS, rounding=ROUND_UP)
        slice_size = self._slice()
        step = self._adapter.equity_quantity_step
        quantity = (slice_size / limit_price).quantize(step, rounding=ROUND_DOWN)
        if quantity <= ZERO:
            self._note(f"MECH slice buys no {symbol} at {limit_price}; not entered")
            return 0

        order = EquityBuyOrder(
            symbol=symbol,
            quantity=quantity,
            execution=LimitExecution(limit_price=limit_price),
            signal_id=signal.signal_id,
            sleeve="mechanical",
        )
        decision = self._gate.submit(order)
        record = self._audit.record_mechanical_entry(
            signal,
            decision,
            capital=(quantity * limit_price).quantize(CENTS),
            sleeve_nav=self._gate.sleeve_nav(Sleeve.MECHANICAL),
            ruleset_version=self._caps.ruleset_version,
            max_positions=self._caps.max_positions,
            decision_id=self._id_factory(),
        )
        if not decision.is_approved:
            self._note(
                f"MECH gate rejected {symbol}: {decision.code} — recorded, "
                f"retries at the next startup"
            )
            return 0

        try:
            receipt = self._adapter.submit_order(
                decision, client_reference=record.decision_id
            )
        except BrokerError as error:
            self._gate.cancel(decision)
            self._audit.record_stage_rejection(
                record.decision_id,
                RejectedStage.EXECUTION,
                type(error).__name__,
                str(error),
                signal,
            )
            return 0
        if signal.external_id:
            self._entered_external.add(signal.external_id)
        self._working[receipt.broker_order_id] = _Working(
            approved=decision,
            decision_id=record.decision_id,
            signal=signal,
            side="open",
            symbol=symbol,
            filer=signal.metadata.get("representative", ""),
        )
        return 1

    # -- settlement, marks, breaker, exits ------------------------------------------

    def tick(self, now: datetime) -> MechanicalTickReport:
        report = MechanicalTickReport()
        report.settled = self._reconcile(now)
        self._mark_and_check_breaker()
        report.exits_started = self._check_time_exits(now)
        return report

    def _reconcile(self, now: datetime) -> int:
        settled = 0
        for order_id, working in list(self._working.items()):
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.warning("could not poll mechanical order %s: %s", order_id, error)
                continue
            if not status.is_terminal:
                continue
            del self._working[order_id]
            settled += 1
            filled = status.filled_quantity
            price = status.filled_avg_price
            if filled <= 0 or price is None:
                self._gate.cancel(working.approved)
                self._audit.record_stage_rejection(
                    working.decision_id,
                    RejectedStage.EXECUTION,
                    status.status,
                    f"mechanical order terminated {status.status} without filling; "
                    f"reservation released",
                    working.signal,
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
                side="buy" if working.side == "open" else "sell",
            )
            if working.side == "open":
                if self._virtual_cash is None:
                    # The ledger anchors at the sleeve's allocation the moment
                    # capital first deploys; persisted from here on.
                    self._virtual_cash = self._gate.sleeve_nav(Sleeve.MECHANICAL)
                self._virtual_cash -= value
                self._tracked[working.decision_id] = MechanicalPosition(
                    decision_id=working.decision_id,
                    symbol=working.symbol,
                    filer=working.filer,
                    quantity=filled,
                    entry_cost=value,
                    proceeds=ZERO,
                    opened_at=now,
                )
            else:
                if self._virtual_cash is not None:
                    self._virtual_cash += value
                position = self._tracked.get(working.decision_id)
                if position is None:
                    continue
                position.quantity -= filled
                position.proceeds += value
                if position.quantity <= 0:
                    self._audit.record_outcome(
                        working.decision_id,
                        position.proceeds - position.entry_cost,
                        closed_at=now,
                        note=(
                            f"mechanical time exit after "
                            f"{self._caps.hold_days} days"
                        ),
                    )
                    del self._tracked[working.decision_id]
        return settled

    def _mark_and_check_breaker(self) -> None:
        marks = {}
        for position in self._tracked.values():
            quote = self._prices(position.symbol)
            if quote is not None and quote > ZERO:
                marks[("mechanical", position.symbol)] = quote
        if marks:
            self._gate.mark_to_market(marks)
        value = self.sleeve_value
        if value is None:
            return
        if self._hwm is None or value > self._hwm:
            self._hwm = value
        if self._halted or self._hwm <= ZERO:
            return
        drawdown = (self._hwm - value) / self._hwm
        if drawdown > self._caps.drawdown_halt_fraction:
            self._halted = True
            self._note(
                f"MECHANICAL BREAKER TRIPPED: sleeve value {value} is "
                f"{drawdown:.1%} below its high-water mark {self._hwm} "
                f"(threshold {self._caps.drawdown_halt_fraction}). New "
                f"mechanical entries halted; positions ride per the no-stop "
                f"design. Human review required — reset mechanical_halted in "
                f"session_state.json."
            )

    def _check_time_exits(self, now: datetime) -> int:
        started = 0
        closing = {w.decision_id for w in self._working.values() if w.side == "close"}
        for position in list(self._tracked.values()):
            if position.decision_id in closing:
                continue
            held_days = (now - position.opened_at).days
            if held_days < self._caps.hold_days:
                continue
            quote = self._prices(position.symbol)
            if quote is None or quote <= ZERO:
                self._note(
                    f"MECH time exit due for {position.symbol} but no usable "
                    f"quote; retrying next tick"
                )
                continue
            order = EquitySellToCloseOrder(
                symbol=position.symbol,
                quantity=position.quantity,
                execution=LimitExecution(
                    limit_price=quote.quantize(CENTS, rounding=ROUND_DOWN)
                ),
                sleeve="mechanical",
            )
            decision = self._gate.submit(order)
            submitted = False
            broker_order_id = None
            broker_error = None
            if decision.is_approved:
                try:
                    receipt = self._adapter.submit_order(decision)
                    submitted = True
                    broker_order_id = receipt.broker_order_id
                    self._working[receipt.broker_order_id] = _Working(
                        approved=decision,
                        decision_id=position.decision_id,
                        signal=None,  # type: ignore[arg-type] - exits carry no signal
                        side="close",
                        symbol=position.symbol,
                        filer=position.filer,
                    )
                    started += 1
                except BrokerError as error:
                    self._gate.cancel(decision)
                    broker_error = str(error)
            self._audit.record_exit(
                position.decision_id,
                ExitReason.MECHANICAL_TIME_EXIT,
                f"held {held_days} days, at/past the {self._caps.hold_days}-day "
                f"hold; no price stop by design",
                decision,
                submitted=submitted,
                broker_order_id=broker_order_id,
                broker_error=broker_error,
            )
        return started

    def cancel_working(self) -> list[str]:
        """Shutdown: cancel and release everything still working, like the pipeline."""
        released = []
        for order_id, working in list(self._working.items()):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error(
                    "could not cancel mechanical order %s at shutdown: %s",
                    order_id,
                    error,
                )
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError:
                status = None
            if status is not None and status.is_terminal:
                self._reconcile_one_terminal(order_id, working, status)
            else:
                self._gate.cancel(working.approved)
                del self._working[order_id]
                if working.signal is not None:
                    self._audit.record_stage_rejection(
                        working.decision_id,
                        RejectedStage.EXECUTION,
                        "released_at_shutdown",
                        "mechanical reservation released at shutdown with the "
                        "order not confirmed terminal; broker state is "
                        "authoritative at next startup",
                        working.signal,
                    )
            released.append(order_id)
        return released

    def _reconcile_one_terminal(self, order_id, working, status) -> None:
        # Route through the normal settle path by re-inserting and reconciling.
        self._working[order_id] = working
        self._reconcile(self._clock())
