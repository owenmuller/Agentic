"""The momentum sleeve's in-session engine (human ruling 2026-10-13).

v1 of the momentum book runs inside the main paper account as its own sleeve.
Its JUDGEMENT-FREE half lives here: the monthly plan - which names, at what
weight - is computed by the prep job (``python -m orchestrator momentum-prep``,
v1's frozen rules, the AI veto of new entrants, the shadows) and written to
``data/momentum/plan-YYYY-MM.json``. This engine only executes it.

The rules
---------
  when          from ``trade_after`` (10:00 ET) on the plan's rebalance day -
                the first trading day of the month - until ``trade_before``;
                a rebalance that cannot finish (funds, T+1, the gate) carries
                into the next session, at most ``max_rebalance_sessions``,
                then closes for the month and says what it left undone. A plan
                not started within ``plan_valid_days`` of its date is skipped,
                never chased.
  order         sells first (names that left the book, then trims back to
                target), then buys (new names and top-ups) in rank order. One
                working order at a time; marketable limits; an order still
                working after ``fill_wait_seconds`` is cancelled and re-priced.
  target        weight x the sleeve's NAV (its allocation of the book). A name
                within ``drift_tolerance`` of its target is left alone.
  funding       a buy spends settled cash above every OTHER sleeve's part of
                the sweep's floor (``sweep.momentum_spendable``) - never
                today's sale proceeds (T+1, enforced again by the gate).
  kill switch   the book-level switch applies: the gate refuses the buys, so
                none are sent; the sells are risk-reducing and run.
  entries off   ``risk_limits.yaml momentum_sleeve.entries_enabled: false``:
                no buys; held names are still sold out of the book by rule.

No LLM, no stop, no review: v1's monthly rules are the only exit. Every order
passes the RiskGate. Each buy is a DecisionRecord (``strategy="momentum"``);
sells relieve lots oldest first and write ExitRecords (``momentum_rebalance``);
a lot sold flat writes its OutcomeRecord. Its own attribution bucket, outside
every alpha line.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from audit.log import AuditLog
from audit.records import ExitReason
from execution.base import BrokerAdapter, BrokerError
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.limits import MomentumSleeveLimits
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import Sleeve

from orchestrator.config import MomentumSleeveConfig

ZERO = Decimal("0")
CENTS = Decimal("0.01")
NY = ZoneInfo("America/New_York")
PLAN_VERSION = 1
SLEEVE = Sleeve.MOMENTUM.value

logger = logging.getLogger("orchestrator.momentum_sleeve")


# -- the plan -------------------------------------------------------------------------


class PlanError(ValueError):
    """A plan file that is missing a field or breaks the sleeve's caps."""


@dataclass(frozen=True, slots=True)
class PlanTarget:
    symbol: str
    weight: Decimal
    rank: Optional[int] = None
    sector: str = ""


@dataclass(frozen=True, slots=True)
class Plan:
    month: str
    rebalance_date: date
    data_through: str
    computed_at: str
    buys_allowed: bool
    exposure: Decimal
    targets: tuple[PlanTarget, ...]
    #: symbol -> why it left the book (from v1's selection), for the record.
    sold: dict = field(default_factory=dict)
    #: symbol -> the veto's reason (the AI leg; v1 never changes).
    vetoed: dict = field(default_factory=dict)

    def weight(self, symbol: str) -> Decimal:
        for target in self.targets:
            if target.symbol == symbol:
                return target.weight
        return ZERO


def plan_path(directory: Path, month: str) -> Path:
    return directory / f"plan-{month}.json"


def load_plan(path: Path, limits: MomentumSleeveLimits) -> Plan:
    """Parse and validate a plan against the sleeve's cap table. Any breach
    rejects the whole plan - a plan the gate would half-refuse is a broken
    prep job, not something to execute in part."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PlanError(f"unreadable plan {path.name}: {error}") from error
    if raw.get("version") != PLAN_VERSION:
        raise PlanError(f"{path.name}: version {raw.get('version')!r}, expected {PLAN_VERSION}")
    try:
        month = str(raw["month"])
        rebalance_date = date.fromisoformat(str(raw["rebalance_date"]))
        targets = tuple(
            PlanTarget(
                symbol=str(item["symbol"]).upper(),
                weight=Decimal(str(item["weight"])),
                rank=int(item["rank"]) if item.get("rank") is not None else None,
                sector=str(item.get("sector", "")),
            )
            for item in raw["targets"]
        )
        plan = Plan(
            month=month,
            rebalance_date=rebalance_date,
            data_through=str(raw.get("data_through", "")),
            computed_at=str(raw.get("computed_at", "")),
            buys_allowed=bool(raw["buys_allowed"]),
            exposure=Decimal(str(raw["exposure"])),
            targets=targets,
            sold={str(k).upper(): str(v) for k, v in (raw.get("sold") or {}).items()},
            vetoed={str(k).upper(): str(v) for k, v in (raw.get("vetoed") or {}).items()},
        )
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise PlanError(f"{path.name}: malformed ({error})") from error
    if rebalance_date.strftime("%Y-%m") != month:
        raise PlanError(f"{path.name}: rebalance date {rebalance_date} is not in {month}")
    symbols = [t.symbol for t in targets]
    if len(set(symbols)) != len(symbols):
        raise PlanError(f"{path.name}: a symbol appears twice")
    if len(targets) > limits.max_positions:
        raise PlanError(f"{path.name}: {len(targets)} names, the cap is {limits.max_positions}")
    for target in targets:
        if not target.symbol or not (ZERO < target.weight <= limits.max_single_position):
            raise PlanError(
                f"{path.name}: {target.symbol or '?'} weight {target.weight} outside "
                f"(0, {limits.max_single_position}]"
            )
    if sum((t.weight for t in targets), ZERO) > Decimal("1.000001"):
        raise PlanError(f"{path.name}: weights sum past 1")
    if not (ZERO <= plan.exposure <= Decimal("1")):
        raise PlanError(f"{path.name}: exposure {plan.exposure} outside [0, 1]")
    return plan


# -- the engine -----------------------------------------------------------------------


@dataclass(slots=True)
class MomentumLot:
    """One momentum buy, tracked until sold flat."""

    decision_id: str
    symbol: str
    quantity: Decimal
    entry_cost: Decimal
    proceeds: Decimal
    opened_at: datetime


@dataclass(slots=True)
class _Working:
    approved: ApprovedOrder
    decision_id: str
    side: str  # "buy" | "sell"
    symbol: str
    placed_at: datetime
    cancel_requested: bool = False


@dataclass(slots=True)
class MonthState:
    """Persisted in ``momentum_state.json``: a restart neither re-runs a
    finished month nor forgets one it was in the middle of."""

    month: Optional[str] = None
    sessions: list = field(default_factory=list)
    done: bool = False
    note: str = ""


def _clock_time(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


class MomentumSleeve:
    """Executes v1's monthly plan through the gate."""

    def __init__(
        self,
        *,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        prices: Callable[[str], Optional[Decimal]],
        limits: MomentumSleeveLimits,
        config: MomentumSleeveConfig,
        plan_dir: Path,
        state_path: Path,
        spendable: Callable[[], Decimal],
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        note: Optional[Callable[[str], None]] = None,
        bids: Optional[Callable[[str], Optional[Decimal]]] = None,
    ) -> None:
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._prices = prices
        self._bids = bids
        self._limits = limits
        self._config = config
        self._plan_dir = plan_dir
        self._state_path = state_path
        self._spendable = spendable
        self._clock = clock
        self._id_factory = id_factory
        self._note = note or (lambda message: None)
        self._lots: dict[str, MomentumLot] = {}
        self._working: dict[str, _Working] = {}
        self._state = self._load_state()
        self._plan: Optional[Plan] = None
        self._plan_month: Optional[str] = None
        self._plan_seen: Optional[int] = None
        #: Names the gate refused today: not resubmitted until tomorrow.
        self._refused: set[str] = set()
        self._refused_day: Optional[date] = None
        self._last_mark: Optional[datetime] = None
        self._warned: set[str] = set()

    # -- introspection ---------------------------------------------------------------

    @property
    def lots(self) -> tuple[MomentumLot, ...]:
        return tuple(self._lots.values())

    @property
    def month_state(self) -> MonthState:
        return self._state

    @property
    def working(self) -> int:
        return len(self._working)

    def held(self) -> dict[str, Decimal]:
        """symbol -> quantity the gate holds in this sleeve."""
        return {
            p.key[1]: p.quantity
            for p in self._gate.state.positions.values()
            if p.sleeve is Sleeve.MOMENTUM and p.quantity > 0
        }

    # -- replay ------------------------------------------------------------------------

    def replay(self, trails) -> int:
        """Rebuild open lots from momentum trails, clamped to what the gate
        holds per symbol (oldest lots first) - the broker is authoritative."""
        candidates = []
        for trail in trails:
            decision = trail.decision
            if decision.sizing.strategy != "momentum":
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
            symbol = str((decision.gate.order or {}).get("symbol", ""))
            candidates.append(
                MomentumLot(
                    decision_id=decision.decision_id,
                    symbol=symbol,
                    quantity=quantity,
                    entry_cost=sum((f.filled_value for f in buys), ZERO),
                    proceeds=sum((f.filled_value for f in sells), ZERO),
                    opened_at=buys[0].recorded_at,
                )
            )
        remaining = self.held()
        restored = 0
        for lot in sorted(candidates, key=lambda item: item.opened_at):
            available = remaining.get(lot.symbol, ZERO)
            if available <= 0:
                logger.warning(
                    "audit log says momentum lot %s holds %s %s but the gate does "
                    "not; not tracking - the broker is authoritative",
                    lot.decision_id,
                    lot.quantity,
                    lot.symbol,
                )
                continue
            lot.quantity = min(lot.quantity, available)
            remaining[lot.symbol] = available - lot.quantity
            self._lots[lot.decision_id] = lot
            restored += 1
        return restored

    # -- one pass ----------------------------------------------------------------------

    def tick(self, now: datetime) -> int:
        """Settle, mark, and - on a rebalance day inside the window - place at
        most one order of the plan. Returns orders placed (0 or 1)."""
        self._reconcile(now)
        local = now.astimezone(NY)
        if self._refused_day != local.date():
            self._refused_day = local.date()
            self._refused.clear()
        month = local.strftime("%Y-%m")
        active = self._state.month == month and not self._state.done
        self._mark(now, force=active)
        if self._working:
            return 0  # one working order at a time
        if self._state.month == month and self._state.done:
            return 0
        if not (_clock_time(self._config.trade_after) <= local.time() < _clock_time(self._config.trade_before)):
            return 0
        plan = self._current_plan(month, local.date())
        if plan is None:
            return 0
        if self._state.month != month:
            if local.date() > plan.rebalance_date + timedelta(days=self._config.plan_valid_days):
                self._close(
                    month,
                    f"MOMENTUM plan {month} skipped: not started within "
                    f"{self._config.plan_valid_days} days of {plan.rebalance_date} - "
                    f"a stale plan is not chased (holdings unchanged)",
                    error=True,
                )
                return 0
            self._state = MonthState(month=month)
            self._note(
                f"MOMENTUM rebalance {month} starts: {len(plan.targets)} names at "
                f"exposure {plan.exposure:.2f}"
                + ("" if plan.buys_allowed else "; market filter on - no new names")
                + (f"; vetoed {', '.join(sorted(plan.vetoed))}" if plan.vetoed else "")
            )
        today = local.date().isoformat()
        if today not in self._state.sessions:
            if len(self._state.sessions) >= self._config.max_rebalance_sessions:
                self._close(month, self._summary(plan, "closed after "
                            f"{len(self._state.sessions)} sessions with work left undone"), error=True)
                return 0
            self._state.sessions.append(today)
            self._save_state()

        placed, sells_left = self._next_sell(plan, now)
        if placed:
            return 1
        if sells_left:
            return 0  # a held name is still to go; buys after it
        placed, buys_left = self._next_buy(plan, now)
        if placed:
            return 1
        if not buys_left:
            self._close(month, self._summary(plan, "complete"))
        return 0

    # -- sells -------------------------------------------------------------------------

    def _next_sell(self, plan: Plan, now: datetime) -> tuple[bool, bool]:
        """(placed, a sell is still owed but could not be placed this tick)."""
        nav = self._gate.sleeve_nav(Sleeve.MOMENTUM)
        owed = False
        for symbol in sorted(self.held()):
            position = self._gate.state.position((SLEEVE, symbol))
            if position is None or position.quantity <= 0:
                continue
            target = plan.weight(symbol) * nav
            if target > 0 and position.market_value - target <= target * self._config.drift_tolerance:
                continue
            lots = sorted(
                (lot for lot in self._lots.values() if lot.symbol == symbol),
                key=lambda item: item.opened_at,
            )
            if not lots:
                self._warn_once(
                    f"unclaimed:{symbol}",
                    f"MOMENTUM holds {position.quantity} {symbol} that no momentum lot "
                    f"in the audit log claims - not sold; a human's question",
                )
                continue
            if symbol in self._refused:
                continue
            quote = self._quote(symbol)
            if quote is None:
                owed = True
                continue
            limit = self._sell_limit(symbol, quote)
            lot = lots[0]
            if target <= 0:
                quantity = lot.quantity
                why = plan.sold.get(symbol, "not in this month's book")
                detail = f"{symbol} left v1's book ({why}): selling {quantity} at limit {limit}"
            else:
                step = self._adapter.equity_quantity_step
                wanted = ((position.market_value - target) / limit).quantize(step, rounding=ROUND_DOWN)
                quantity = min(lot.quantity, wanted)
                detail = (
                    f"{symbol} above target: holds {position.market_value:.2f} against "
                    f"{target:.2f} ({plan.weight(symbol):.4f} of sleeve NAV {nav:.2f}); "
                    f"selling {quantity} at limit {limit}"
                )
            if quantity <= 0:
                continue
            order = EquitySellToCloseOrder(
                symbol=symbol,
                quantity=quantity,
                execution=LimitExecution(limit_price=limit),
                sleeve=SLEEVE,
            )
            decision = self._gate.submit(order)
            submitted, broker_order_id, broker_error = False, None, None
            if decision.is_approved:
                try:
                    receipt = self._adapter.submit_order(decision)
                    submitted = True
                    broker_order_id = receipt.broker_order_id
                    self._working[receipt.broker_order_id] = _Working(
                        approved=decision,
                        decision_id=lot.decision_id,
                        side="sell",
                        symbol=symbol,
                        placed_at=now,
                    )
                except BrokerError as error:
                    self._gate.cancel(decision)
                    broker_error = str(error)
            self._audit.record_exit(
                lot.decision_id,
                ExitReason.MOMENTUM_REBALANCE,
                f"momentum rebalance {plan.month}: {detail}",
                decision,
                submitted=submitted,
                broker_order_id=broker_order_id,
                broker_error=broker_error,
            )
            if submitted:
                return True, owed
            if not decision.is_approved:
                self._refused.add(symbol)
                self._note(f"MOMENTUM gate refused the {symbol} sell: {decision.code} - recorded")
            owed = True
        return False, owed

    # -- buys --------------------------------------------------------------------------

    def _buys_blocked(self) -> Optional[str]:
        if not self._limits.entries_enabled:
            return "entries switched off (risk_limits.yaml momentum_sleeve)"
        if self._gate.kill_switch_tripped:
            return "the kill switch is tripped"
        return None

    def _next_buy(self, plan: Plan, now: datetime) -> tuple[bool, bool]:
        """(placed, a buy is still owed and may become possible this month)."""
        blocked = self._buys_blocked()
        if blocked is not None:
            self._warn_once(f"blocked:{plan.month}", f"MOMENTUM buys held back: {blocked}")
            return False, False
        nav = self._gate.sleeve_nav(Sleeve.MOMENTUM)
        owed = False
        ordered = sorted(plan.targets, key=lambda t: (t.rank if t.rank is not None else 10**6, t.symbol))
        for target in ordered:
            symbol = target.symbol
            goal = target.weight * nav
            position = self._gate.state.position((SLEEVE, symbol))
            held = position.exposure if position is not None else ZERO
            gap = goal - held
            if gap <= goal * self._config.drift_tolerance:
                continue
            if symbol in self._refused:
                owed = True
                continue
            quote = self._quote(symbol)
            if quote is None:
                owed = True
                continue
            limit = (quote * (1 + self._config.limit_buffer)).quantize(CENTS, rounding=ROUND_UP)
            step = self._adapter.equity_quantity_step
            if (gap / limit).quantize(step, rounding=ROUND_DOWN) <= 0:
                continue  # the gap is under one tradeable unit: at target
            amount = min(gap, self._spendable())
            quantity = (amount / limit).quantize(step, rounding=ROUND_DOWN)
            if amount < self._limits.min_order_notional_usd or quantity <= 0:
                owed = True  # settled cash arrives tomorrow, or the sweep is unparking it
                continue
            capital = (quantity * limit).quantize(CENTS)
            detail = (
                f"{symbol} below target (rank {target.rank}): holds {held:.2f} against "
                f"{goal:.2f} ({target.weight:.4f} of sleeve NAV {nav:.2f}); buying "
                f"{quantity} at limit {limit}"
            )
            order = EquityBuyOrder(
                symbol=symbol,
                quantity=quantity,
                execution=LimitExecution(limit_price=limit),
                sleeve=SLEEVE,
            )
            decision = self._gate.submit(order)
            record = self._audit.record_momentum(
                detail=detail,
                gate_decision=decision,
                capital=capital,
                sleeve_nav=nav,
                plan_month=plan.month,
                decision_id=self._id_factory(),
            )
            if not decision.is_approved:
                self._refused.add(symbol)
                self._note(f"MOMENTUM gate refused the {symbol} buy: {decision.code} - recorded")
                owed = True
                continue
            try:
                receipt = self._adapter.submit_order(decision, client_reference=record.decision_id)
            except BrokerError as error:
                self._gate.cancel(decision)
                logger.warning("broker refused momentum buy %s: %s; retrying next tick", symbol, error)
                return False, True
            self._working[receipt.broker_order_id] = _Working(
                approved=decision,
                decision_id=record.decision_id,
                side="buy",
                symbol=symbol,
                placed_at=now,
            )
            return True, True
        return False, owed

    # -- settlement --------------------------------------------------------------------

    def _reconcile(self, now: datetime) -> int:
        settled = 0
        for order_id, working in list(self._working.items()):
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.warning("could not poll momentum order %s: %s", order_id, error)
                continue
            if not status.is_terminal:
                waited = (now - working.placed_at).total_seconds()
                if not working.cancel_requested and waited >= self._config.fill_wait_seconds:
                    working.cancel_requested = True
                    try:
                        self._adapter.cancel_order(order_id)
                    except BrokerError as error:
                        logger.warning("could not cancel stale momentum order %s: %s", order_id, error)
                continue
            del self._working[order_id]
            settled += 1
            self._settle(order_id, working, status, now)
        return settled

    def _settle(self, order_id: str, working: _Working, status, now: datetime) -> None:
        filled = status.filled_quantity
        price = status.filled_avg_price
        if filled <= 0 or price is None:
            self._gate.cancel(working.approved)
            self._audit.record_unfilled_order(
                working.decision_id,
                order_id,
                status.status,
                f"momentum {working.side} {working.symbol} order {order_id} terminated "
                f"{status.status} without filling; reservation released, re-priced next pass",
            )
            return
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
            self._lots[working.decision_id] = MomentumLot(
                decision_id=working.decision_id,
                symbol=working.symbol,
                quantity=filled,
                entry_cost=value,
                proceeds=ZERO,
                opened_at=now,
            )
            return
        lot = self._lots.get(working.decision_id)
        if lot is None:
            return
        lot.quantity -= filled
        lot.proceeds += value
        if lot.quantity <= 0:
            self._audit.record_outcome(
                working.decision_id,
                lot.proceeds - lot.entry_cost,
                closed_at=now,
                note="momentum lot sold flat (monthly rebalance)",
            )
            del self._lots[working.decision_id]

    def cancel_working(self) -> list[str]:
        """Shutdown or halt: cancel and account for outstanding orders."""
        released = []
        for order_id, working in list(self._working.items()):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error("could not cancel momentum order %s: %s", order_id, error)
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError:
                status = None
            self._working.pop(order_id, None)
            if status is not None and status.is_terminal:
                self._settle(order_id, working, status, self._clock())
            else:
                self._gate.cancel(working.approved)
            released.append(order_id)
        return released

    # -- the plan and the month ----------------------------------------------------------

    def _current_plan(self, month: str, today: date) -> Optional[Plan]:
        # Looked for on every tick until a usable plan is loaded (2026-10-14:
        # a plan written after 10:00 ET - a late catch-up - was cached as
        # missing for the whole month). A rejected file is re-read only when
        # it changes, so a corrected rerun is picked up without a restart.
        path = plan_path(self._plan_dir, month)
        if self._plan_month != month:
            self._plan_month, self._plan, self._plan_seen = month, None, None
        if self._plan is None and path.exists():
            stamp = path.stat().st_mtime_ns
            if stamp != self._plan_seen:
                self._plan_seen = stamp
                try:
                    self._plan = load_plan(path, self._limits)
                except PlanError as error:
                    logger.error("MOMENTUM plan rejected, nothing traded: %s", error)
        if self._plan is None:
            # Owed only when the sleeve can act: entries on, or names held.
            if today.day <= 7 and (self._limits.entries_enabled or self.held()):
                self._warn_once(
                    f"noplan:{today.isoformat()}",
                    f"MOMENTUM has no usable plan for {month} "
                    f"({plan_path(self._plan_dir, month).name}); nothing traded",
                    error=True,
                )
            return None
        if today < self._plan.rebalance_date:
            return None
        return self._plan

    def _close(self, month: str, message: str, *, error: bool = False) -> None:
        self._state.month = month
        self._state.done = True
        self._state.note = message
        self._save_state()
        if error:
            logger.error(message)
        self._note(message)

    def _summary(self, plan: Plan, how: str) -> str:
        nav = self._gate.sleeve_nav(Sleeve.MOMENTUM)
        held = self.held()
        invested = sum(
            (p.market_value for p in self._gate.state.positions.values() if p.sleeve is Sleeve.MOMENTUM),
            ZERO,
        )
        missing = [t.symbol for t in plan.targets if t.symbol not in held]
        extra = [s for s in held if plan.weight(s) == 0]
        text = (
            f"MOMENTUM rebalance {plan.month} {how}: {len(held)} names, "
            f"{invested:.2f} invested of sleeve NAV {nav:.2f}"
        )
        if missing:
            text += f"; not bought: {', '.join(missing)}"
        if extra:
            text += f"; still held outside the book: {', '.join(sorted(extra))}"
        return text

    # -- state ---------------------------------------------------------------------------

    def _load_state(self) -> MonthState:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return MonthState()
        except (OSError, ValueError) as error:
            # Constraint #6: an unreadable state could mean a month already
            # traded; do not re-run it blind.
            logger.error("momentum state unreadable (%s); this month stays closed", error)
            return MonthState(month=datetime.now(NY).strftime("%Y-%m"), done=True, note="state unreadable")
        return MonthState(
            month=raw.get("month"),
            sessions=list(raw.get("sessions") or []),
            done=bool(raw.get("done", False)),
            note=str(raw.get("note", "")),
        )

    def _save_state(self) -> None:
        payload = {
            "month": self._state.month,
            "sessions": self._state.sessions,
            "done": self._state.done,
            "note": self._state.note,
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self._state_path)

    # -- prices --------------------------------------------------------------------------

    def _mark(self, now: datetime, *, force: bool) -> None:
        held = self.held()
        if not held:
            return
        due = (
            force
            or self._last_mark is None
            or (now - self._last_mark).total_seconds() >= self._config.mark_interval_seconds
        )
        if not due:
            return
        self._last_mark = now
        marks = {}
        for symbol in held:
            quote = self._quote(symbol)
            if quote is not None:
                marks[(SLEEVE, symbol)] = quote
        if marks:
            self._gate.mark_to_market(marks)

    def _sell_limit(self, symbol: str, quote: Decimal) -> Decimal:
        """Marketable: the bid rounded down, else the quote less the buffer."""
        bid = None
        if self._bids is not None:
            try:
                bid = self._bids(symbol)
            except Exception:  # noqa: BLE001 - a price bug must not kill the loop
                logger.exception("momentum bid failed for %s", symbol)
        if bid is not None and bid > ZERO:
            limit = bid.quantize(CENTS, rounding=ROUND_DOWN)
        else:
            limit = (quote * (1 - self._config.limit_buffer)).quantize(CENTS, rounding=ROUND_DOWN)
        return max(limit, CENTS)

    def _quote(self, symbol: str) -> Optional[Decimal]:
        try:
            quote = self._prices(symbol)
        except Exception:  # noqa: BLE001
            logger.exception("momentum quote failed for %s", symbol)
            return None
        if quote is None or quote <= ZERO:
            return None
        return quote

    def _warn_once(self, key: str, message: str, *, error: bool = False) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        if error:
            logger.error(message)
        self._note(message)
