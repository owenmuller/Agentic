"""2b. THE AUTONOMOUS AI TRADER (PAPER PUSH 2026-10-09; built per ruling
2026-10-10, item 5), in its own paper account.

"The agent may trade stocks, ETFs, day trades and long options on its own
ideas, any source. Risk budget 2% of the account per trade, max 5 open
positions, 0DTE allowed. Rules engine enforces stops, sizing and close-outs;
the LLM decides what and why. Constraint #6 holds: nothing sized off P&L or
targets. Daily LLM spend cap $3; report actual."

Division of labour
------------------
THE MODEL (``lab.ai_prompts``, ``lab.llm``) proposes ideas at the scheduled
scans: an underlying, an instrument (stock / etf / call / put), a horizon
(intraday / days / weeks), a stop and a target as PERCENTAGE DISTANCES from entry
(ruling 2026-10-10; never prices), an optional price_seen, a
confidence, a thesis, a source, an invalidation. That is all it can do.

THE RULES ENGINE (this module) decides everything else, deterministically:
  validate  the instrument and horizon are known; the symbol is not held or
            already traded today; a slot is free; confidence >= the floor;
            a live quote exists; the stop distance is 1%-25% (1% by ruling
            2026-10-10 until stop slippage is measured); target distance >=
            min_reward_risk x stop distance; an intraday idea arrives before
            15:15 ET; no new entry after 15:30 ET; kill switch and halt clear.
  levels    stop and target are set from the LIVE price at order time, at the
            stated distances; the gap between that price and any price the
            model cites (price_seen, $-figures in its thesis) is logged
            (``anchor_check``), never used. The live price is read by the main
            book's quote rule (human ruling 2026-11-12, item 1;
            ``execution.quotes``): the mid of a quote no wider than 2%, else
            the last trade, never a wide quote's mid. Neither: no entry.
  one bet   no new position whose sector (the gate's map) or theme (the
            model's tag, from a configured list) is already held or already
            proposed earlier in the same pass; at most one new option per
            scan (human ruling 2026-11-12, item 2).
  size      2% of the account's NAV at risk: shares = budget / (limit - stop)
            for a stock or ETF (capped by the gate's single-position cap and
            the cash), contracts = floor(budget / premium) for an option -
            whose whole premium is the risk. Never a function of P&L.
  pick      the contract for a call/put: the nearest expiry inside the
            horizon's DTE window (0DTE allowed intraday), |delta| in the band
            closest to the target delta, open interest and spread floors, IV
            percentile ceiling. Never the model's choice of strike.
  exit      the stop or target touched on the underlying, every tick, by the
            quote rule: a long's stop on the bid of a tight quote, else the
            last trade; anything else on the mid of a tight quote, else the
            last trade (human ruling 2026-11-12, item 1: 9 of 17 exits in the
            review fired on a wide IEX quote's mid at a price that never
            traded); the horizon ended (intraday at 15:50 ET; days/weeks after
            5/20 sessions, at 15:50); an option on its expiry day or the
            session before it, at 15:45 ET. Marketable limits, re-priced
            until filled. Through ``exit_shadow_until`` the old rule's verdict
            is logged wherever it disagrees (``exit_shadow``).
  logged    beside the rules, never deciding (same ruling, items 2-4): the
            contract the old DTE window would have bought, priced at entry
            and at exit (``expiry_counterfactual``); each idea's move since
            its catalyst went public (``move_since_public``).
  refuse    every order passes this account's own RiskGate first.

Spend: every pass is metered (tokens priced from research.yaml + $0.01 per
search). A pass starts only if today's spend plus the reserve stays inside
the $3 cap; the actual figure is in the ledger and in ``lab status``.
"""
from __future__ import annotations

import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo


import datahealth
from execution import BrokerError
from execution.base import OrderStatus
from execution.market_calendar import MarketCalendar
from execution.quotes import QuoteReader, Reading
from lab.account import LabAccount
from lab.ai_prompts import (
    HORIZONS,
    INSTRUMENTS,
    ConfirmItem,
    HeldView,
    build_user,
    confirm_system,
    confirm_tool,
    confirm_user,
    report_tool,
    system_prompt,
)
from lab.brief import BEGIN as BRIEF_BEGIN, END as BRIEF_END, Brief
from lab import leverage
from lab.config import SETUPS, AiTraderConfig
from lab.intraday import Context, pullback_level
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.sectors import SectorMap
from risk_gate.schema import (
    EquityBuyOrder,
    EquitySellToCloseOrder,
    LimitExecution,
    OptionBuyToOpenOrder,
    OptionSellToCloseOrder,
)
from risk_gate.state import Sleeve

ZERO = Decimal("0")
CENTS = Decimal("0.01")
STEP = Decimal("0.000001")
NY = ZoneInfo("America/New_York")
_SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
#: A price cited in a thesis: "$123.45", "$1,234" (logged against the live price).
_CITED = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
OPTIONS = ("call", "put")

logger = logging.getLogger("lab.ai_trader")


def bullish(instrument: str) -> bool:
    return instrument != "put"


def at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, dtime.fromisoformat(hhmm), NY)


def sessions_between(start: date, end: date) -> int:
    """Weekday sessions from ``start`` to ``end`` inclusive (holidays count as
    sessions: a horizon then ends a session EARLY - the less-risk error).
    The trader itself counts on its calendar (``AiTrader.cut``)."""
    return MarketCalendar.weekdays().sessions_between(start, end)


@dataclass
class Position:
    """An AI-trader position: the idea's plan plus what actually filled."""

    pid: str
    symbol: str
    instrument: str
    horizon: str
    stop: Decimal
    target: Decimal
    thesis: str
    opened_at: str
    occ: Optional[str] = None
    expiry: Optional[date] = None
    right: Optional[str] = None
    strike: Optional[Decimal] = None
    quantity: Decimal = ZERO
    #: the theme it was opened under (ruling 2026-11-12, item 2); None before
    theme: Optional[str] = None

    @property
    def trade_symbol(self) -> str:
        return self.occ or self.symbol

    @property
    def opened_day(self) -> date:
        return datetime.fromisoformat(self.opened_at).astimezone(NY).date()


def replay_positions(events: list[dict]) -> dict[str, Position]:
    """Open positions from the ledger: plans opened, quantities from settled
    fills, closed when they reach zero."""
    positions: dict[str, Position] = {}
    for event in events:
        kind = event.get("event")
        if kind == "position_opened":
            positions[event["pid"]] = Position(
                pid=event["pid"], symbol=event["symbol"], instrument=event["instrument"],
                horizon=event["horizon"], stop=Decimal(event["stop"]), target=Decimal(event["target"]),
                thesis=event.get("thesis", ""), opened_at=event["at"], occ=event.get("occ"),
                expiry=date.fromisoformat(event["expiry"]) if event.get("expiry") else None,
                right=event.get("right"), strike=Decimal(event["strike"]) if event.get("strike") else None,
                theme=event.get("theme"),
            )
        elif kind == "settled" and event.get("pid") in positions:
            filled = Decimal(event["filled_qty"])
            sign = Decimal(1) if event["side"] == "buy" else Decimal(-1)
            positions[event["pid"]].quantity += sign * filled
    return {pid: p for pid, p in positions.items() if p.quantity > ZERO}


def price_exit(position: Position, reading: Reading) -> Optional[str]:
    """The stop or the target by the quote rule (human ruling 2026-11-12,
    item 1; the main book's rule of 2026-11-04): a long's stop compares the
    TRIGGER (the bid of a tight quote, else the last trade); everything else
    compares the mid of a tight quote, else the last trade. A wide quote's
    mid never counts; with no usable price nothing fires this tick."""
    price = reading.value if reading.value is not None else reading.last
    if bullish(position.instrument):
        if reading.trigger is not None and reading.trigger <= position.stop:
            return "stop"
        if price is not None and price >= position.target:
            return "target"
    else:
        if price is not None and price >= position.stop:
            return "stop"
        if price is not None and price <= position.target:
            return "target"
    return None


def old_price_exit(position: Position, reading: Reading) -> Optional[str]:
    """The rule before 2026-11-12, for the "would exit" shadow only: the mid
    of any quote, however wide."""
    if reading.bid is None or reading.ask is None:
        return None
    mid = (reading.bid + reading.ask) / 2
    if bullish(position.instrument):
        return "stop" if mid <= position.stop else "target" if mid >= position.target else None
    return "stop" if mid >= position.stop else "target" if mid <= position.target else None


def usable_price(reading: Reading) -> Optional[Decimal]:
    """What a level is set from: the mid of a tight quote, else the last trade."""
    return reading.value if reading.value is not None else reading.last


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


@dataclass
class _Working:
    approved: ApprovedOrder
    order_id: str
    side: str
    limit: Decimal
    modelled: Decimal


@dataclass
class ScanOutcome:
    ran: bool
    cost_usd: Decimal = ZERO
    input_tokens: int = 0
    #: the research brief's A/B arm of this pass: None (off), "brief", "control"
    brief_arm: Optional[str] = None
    ideas: list[dict] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""


class AiTrader:
    """The rules engine around the model, for one paper account."""

    def __init__(
        self,
        account: LabAccount,
        config: AiTraderConfig,
        *,
        llm: Any,
        quotes: Callable[[str], Optional[tuple[Decimal, Decimal]]],
        chain: Any,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        alert: Optional[Callable[[str, str], None]] = None,
        leverage_alert: Optional[Callable[[Decimal, Decimal, list], None]] = None,
        brief: Optional[Callable[[], Brief]] = None,
        coin: Callable[[], float] = random.random,
        calendar: Optional[MarketCalendar] = None,
        reader: Optional[QuoteReader] = None,
        sectors: Optional[SectorMap] = None,
        catalyst_clock: Any = None,
        intraday: Any = None,
    ) -> None:
        self.account = account
        #: Intraday context (ruling 2026-11-16, items 1-3; ``lab.intraday``),
        #: read only while ``config.day_trader.enabled``. None there: every
        #: idea's extension is unknown, so nothing is entered (Constraint #6).
        self._intraday = intraday
        #: this scan's move-since-public rows, measured once per symbol
        self._moves: dict[str, dict] = {}
        #: this scan's anti-chase measurements, by symbol, for the plan
        self._scan_context: dict[str, dict] = {}
        #: The live round trip's size cap (shares or contracts); None in production.
        #: It can only make an entry smaller.
        self._max_units: Optional[Decimal] = None
        #: Prices by the main book's quote rule (human ruling 2026-11-12, item
        #: 1): stops, targets and the levels. ``quotes`` (raw bid/ask) still
        #: prices the orders themselves. Default: the rule over ``quotes``
        #: with no last trade (a wide or missing quote then fires nothing).
        self._reader = reader or QuoteReader(lambda symbol: None, sides=quotes, engine="AI trader")
        #: The gate's sector map: the concentration cap's sector (ruling
        #: 2026-11-12, item 2); an unmapped name is its own sector.
        self._sectors = sectors or SectorMap.load()
        #: Move since public (ruling 2026-11-12, item 4), logged only; None: off.
        self._catalyst_clock = catalyst_clock
        self._shadowed: set[tuple] = set()
        self._options_this_scan = 0
        self._opened_pids: dict[str, str] = {}
        #: symbols whose entry order reached the broker in this scan
        self._placed: set[str] = set()
        #: Sessions and closes (sweep 2026-11-10): every cutoff below moves with
        #: the day's own close. None: every weekday a regular session (tests).
        self.calendar = calendar or MarketCalendar.weekdays()
        #: The research brief's renderer (ruling 2026-10-15); used only while
        #: ``config.brief_mode`` is not "off"; ``coin`` draws the A/B arm.
        self._brief = brief
        self._coin = coin
        self._arm: Optional[str] = None
        self.config = config
        self.llm = llm
        self._quotes = quotes
        self.chain = chain
        self._sleep = sleep
        self._monotonic = monotonic
        self._alert = alert
        #: The leverage watch's card (ruling 2026-10-14, third).
        self._leverage_alert = leverage_alert
        self.gate: Optional[RiskGate] = None
        self.notes: list[str] = []

    # -- state ---------------------------------------------------------------

    def positions(self) -> dict[str, Position]:
        return replay_positions(self.account.ledger.events())

    def leverage_factor(self, symbol: str, instrument: str) -> Decimal:
        """The signed leverage of a stock/ETF entry: the configured table,
        else the fund's name at the broker, else 1. Options: 1 (premium)."""
        if instrument not in ("stock", "etf"):
            return leverage.ONE
        table = self.config.leverage_factors
        name = None
        lookup = getattr(self.account.adapter, "asset_name", None)
        if symbol.upper() not in table and lookup is not None:
            name = lookup(symbol)
        return leverage.factor(symbol, "etf", table, name)

    def gross(self) -> tuple[Decimal, Decimal]:
        """(face, leverage-adjusted) gross exposure from the gate: each
        position's exposure, and that times |its leverage factor| (as recorded
        on its plan; for one opened before the factor was recorded, the
        configured table)."""
        recorded: dict[str, Decimal] = {}
        for event in self.account.ledger.events(["position_opened"]):
            if event.get("leverage"):
                recorded[event.get("occ") or event["symbol"]] = Decimal(str(event["leverage"]))
        face = adjusted = ZERO
        for position in self.gate.state.positions.values():
            symbol = position.key[1]
            factor = recorded.get(symbol) or leverage.factor(symbol, "etf", self.config.leverage_factors)
            face += position.exposure
            adjusted += position.exposure * abs(factor)
        for row in self.pending().values():   # a resting pullback limit counts as the exposure it may become
            notional = self._pending_notional(row)
            face += notional
            adjusted += notional * abs(Decimal(str((row.get("plan") or {}).get("leverage") or "1")))
        return face, adjusted

    def spent_today(self, day: date) -> Decimal:
        """Every model call today: the passes and (ruling 2026-11-16) the
        day-trader confirm steps."""
        return sum(
            (Decimal(str(e["cost_usd"])) for e in self.account.ledger.events(["llm_pass", "context_pass"])
             if datetime.fromisoformat(e["at"]).astimezone(NY).date() == day),
            ZERO,
        )

    def traded_today(self, day: date) -> list[str]:
        """Symbols opened today, and (ruling 2026-11-16) symbols that had a
        pullback entry placed today, filled or not (the reading that trades less)."""
        return [e["symbol"] for e in self.account.ledger.events(["position_opened", "pullback_placed"])
                if datetime.fromisoformat(e["at"]).astimezone(NY).date() == day]

    def pending(self) -> dict[str, dict]:
        """Pullback entries placed and not yet done (ruling 2026-11-16, item 2),
        from the ledger: a restart resumes them. Each holds a slot."""
        out: dict[str, dict] = {}
        for event in self.account.ledger.events(["pullback_placed", "pullback_order", "pullback_done"]):
            kind, pid = event.get("event"), event.get("pid")
            if kind == "pullback_placed":
                out[pid] = dict(event)
            elif kind == "pullback_order" and pid in out:
                out[pid]["order_id"] = event.get("order_id")
            elif kind == "pullback_done":
                out.pop(pid, None)
        return out

    @staticmethod
    def _pending_notional(row: dict) -> Decimal:
        if row.get("kind") != "limit":
            return ZERO
        return Decimal(str(row.get("quantity") or 0)) * Decimal(str(row.get("level") or 0))

    def rebuild_gate(self, day: date) -> RiskGate:
        """The account's RiskGate, seeded from the broker every tick (orders
        are worked to a terminal state inside a tick, so nothing is pending
        between ticks), with today's deployment and unsettled proceeds
        replayed from the ledger so a rebuild can never refill a daily cap."""
        adapter = self.account.adapter
        gate = self.account.build_gate(adapter.get_buying_power(), adapter.get_positions(), Sleeve.AGGRESSIVE,
                                       allow_options=True)
        state = gate.state
        state.deployment_date = day
        for event in self.account.ledger.events(["settled"]):
            if datetime.fromisoformat(event["at"]).astimezone(NY).date() != day:
                continue
            value = Decimal(event["filled_qty"]) * Decimal(event["avg_price"]) * Decimal(event.get("multiplier", 1))
            if event["side"] == "buy":
                if event.get("instrument") in OPTIONS:
                    state.deployed_today += value
                else:
                    state.aggressive_deployed_today += value
            else:
                state.unsettled_proceeds += value
        # A resting pullback limit (ruling 2026-11-16, item 2) is not in the
        # broker's cash figure: its worst-case cost is reserved again every
        # rebuild and counted as deployed, so no later order can spend it.
        for row in self.pending().values():
            notional = self._pending_notional(row)
            state.reserved_cash += notional
            state.aggressive_deployed_today += notional
        marks = {}
        for position in state.positions.values():
            if position.quantity:
                marks[position.key] = position.market_value / (position.quantity * position.unit_multiplier)
        gate.mark_to_market(marks)
        halt = self.account.halted()
        if halt and not gate.kill_switch_tripped:
            gate.trip_kill_switch(f"operator halt: {halt}")
            self.account.persist_gate(gate, reason=f"operator halt marker: {halt}")
        tripped_before = self.account.state.kill_switch_tripped
        self.account.persist_gate(gate)
        # the gate is in place BEFORE the card: the card reads it (2026-11-16:
        # the trip at 09:30:01 found ``self.gate`` still None, the card raised,
        # and the kill-switch card was never sent)
        self.gate = gate
        if gate.kill_switch_tripped and not tripped_before and self._alert is not None:
            try:
                self._alert(f"LAB {self.account.name} KILL SWITCH", self.account.state.trip_reason or "tripped")
            except Exception as error:  # noqa: BLE001 - a failed card must not cost the tick its exits
                self._fault(f"the kill-switch card failed ({type(error).__name__}: {str(error)[:200]}); "
                            f"the switch itself is tripped: {self.account.state.trip_reason}")
        return gate

    # -- the tick ----------------------------------------------------------

    def tick(self, now: datetime) -> list[str]:
        self.notes = []
        day = now.astimezone(NY).date()
        self._recover()
        self.rebuild_gate(day)
        self._leverage_watch()
        self._work_pullbacks(now)
        for position in list(self.positions().values()):
            reason = self.exit_reason(position, now)
            if reason:
                self.close(position, reason, now)
        self._counterfactual_expiries(now)
        due = self._due_scan(now)
        if due is not None:
            self.scan(now, label=due)
        return self.notes

    def _leverage_watch(self) -> None:
        """Ruling 2026-10-14 (third): an URGENT alert when leverage-adjusted
        gross passes ``leverage_alert_above`` of NAV, once per crossing,
        re-armed only after it falls below ``leverage_alert_rearm_below``.
        Information only - nothing is blocked on this figure."""
        nav = self.gate.state.nav
        if nav <= ZERO:
            return
        face, adjusted = self.gross()
        ratio, face_ratio = adjusted / nav, face / nav
        engine = self.account.state.engine
        armed = engine.get("leverage_alert_armed", True)
        if armed and ratio > self.config.leverage_alert_above:
            contributors = self._leverage_contributors(nav)
            engine["leverage_alert_armed"] = False
            self.account.state.save(self.account.state_path)
            self.account.ledger.append("leverage_alert", leverage_adjusted=str(round(ratio, 4)),
                                       face=str(round(face_ratio, 4)),
                                       threshold=str(self.config.leverage_alert_above))
            self.notes.append(f"LEVERAGE ALERT: leverage-adjusted gross {ratio:.0%} of NAV (face {face_ratio:.0%})")
            if self._leverage_alert is not None:
                self._leverage_alert(ratio, face_ratio, contributors)
        elif not armed and ratio < self.config.leverage_alert_rearm_below:
            engine["leverage_alert_armed"] = True
            self.account.state.save(self.account.state_path)
            self.account.ledger.append("leverage_alert_rearmed", leverage_adjusted=str(round(ratio, 4)))

    def _leverage_contributors(self, nav: Decimal) -> list:
        """(symbol, leverage factor, leverage-adjusted share of NAV), largest first."""
        recorded = {(e.get("occ") or e["symbol"]): Decimal(str(e["leverage"]))
                    for e in self.account.ledger.events(["position_opened"]) if e.get("leverage")}
        rows = []
        for position in self.gate.state.positions.values():
            symbol = position.key[1]
            factor = recorded.get(symbol) or leverage.factor(symbol, "etf", self.config.leverage_factors)
            rows.append((symbol, factor, position.exposure * abs(factor) / nav))
        return sorted(rows, key=lambda row: -row[2])

    def _due_scan(self, now: datetime) -> Optional[str]:
        """The latest scan time that has passed today and not yet run; any
        earlier missed scan is skipped (a late start runs one scan, not a burst)."""
        local = now.astimezone(NY)
        day = local.date().isoformat()
        done = self.account.state.engine.setdefault("scans", {}).get(day, [])
        session = self.calendar.session(local.date())
        if session is None:
            return None
        # a scan time at or after an early close is not a scan that day
        passed = [t for t in self.config.scan_times
                  if local.time() >= dtime.fromisoformat(t) and at(local.date(), t) < session.close]
        if not passed or passed[-1] in done:
            return None
        return passed[-1]

    def _mark_scan(self, now: datetime, label: str) -> None:
        scans = self.account.state.engine.setdefault("scans", {})
        day = now.astimezone(NY).date().isoformat()
        scans[day] = sorted(set(scans.get(day, [])) | {label})
        for old in [d for d in scans if d < (now.astimezone(NY).date() - timedelta(days=10)).isoformat()]:
            del scans[old]
        self.account.state.save(self.account.state_path)

    # -- exits ---------------------------------------------------------------

    def cut(self, day: date, hhmm: str) -> datetime:
        """A time configured for a regular 16:00 close (15:50, 15:45 ...) on
        ``day``'s own session: 12:50 for 15:50 on a 13:00 early close. A closed
        day has no time left at all."""
        session = self.calendar.session(day)
        return session.at(hhmm) if session is not None else datetime.combine(day, dtime.min, NY)

    def exit_reason(self, position: Position, now: datetime) -> Optional[str]:
        local = now.astimezone(NY)
        today = local.date()
        cfg = self.config
        if position.occ and position.expiry is not None:
            close_day = position.expiry
            if position.expiry > position.opened_day:
                # a multi-day contract closes on the session before expiry
                close_day = self.calendar.previous_session(position.expiry)
            if today > close_day or (today == close_day and local >= self.cut(today, cfg.options.expiry_close)):
                return "option_expiry_close"
        # an intraday position from an EARLIER session (a crash, an early close
        # passed by) is overdue: it closes at the first tick
        if position.horizon == "intraday" and (position.opened_day < today or
                                               local >= self.cut(today, cfg.intraday_close)):
            return "intraday_close"
        if position.horizon in cfg.horizon_sessions:
            held = self.calendar.sessions_between(position.opened_day, today)
            if held > cfg.horizon_sessions[position.horizon] or (
                held == cfg.horizon_sessions[position.horizon] and local >= self.cut(today, cfg.intraday_close)
            ):
                return "horizon_end"
        reading = self._reader.quote(position.symbol)
        reason = price_exit(position, reading)
        self._exit_shadow(position, reading, reason, local)
        return reason

    def _exit_shadow(self, position: Position, reading: Reading, new: Optional[str], local: datetime) -> None:
        """The "would exit" shadow (human ruling 2026-11-12, item 1): through
        ``exit_shadow_until``, where the OLD rule (a wide quote's mid) and the
        new one disagree, one row per position, pair and day - what the old
        rule would have done and the quote that would have done it."""
        until = self.config.exit_shadow_until
        if until is None or local.date() > until:
            return
        old = old_price_exit(position, reading)
        if old == new:
            return
        key = (position.pid, old, new, local.date())
        if key in self._shadowed:
            return
        self._shadowed.add(key)
        spread = None
        if reading.bid is not None and reading.ask is not None and reading.bid + reading.ask > 0:
            spread = str(round((reading.ask - reading.bid) / ((reading.ask + reading.bid) / 2), 5))
        self.account.ledger.append(
            "exit_shadow", pid=position.pid, symbol=position.symbol, instrument=position.instrument,
            old_rule=old, new_rule=new, bid=reading.bid, ask=reading.ask, spread=spread, value=reading.value,
            trigger=reading.trigger, last=reading.last, wide=reading.wide, stop=position.stop, target=position.target)

    def close(self, position: Position, reason: str, now: datetime) -> None:
        deadline = self._monotonic() + self.config.fill_wait_seconds
        remaining = position.quantity
        while remaining > ZERO and self._monotonic() < deadline:
            quote = self._trade_quote(position)
            if quote is None:
                self._fault(f"{reason} exit of {position.trade_symbol}: no quote; retried next tick")
                return
            bid, ask = quote
            limit = max(CENTS, (bid * (1 - self.config.limit_buffer)).quantize(CENTS, rounding=ROUND_DOWN))
            if position.occ:
                order = OptionSellToCloseOrder(symbol=position.occ, underlying=position.symbol,
                                               right=position.right, expiration=position.expiry,
                                               strike=position.strike, contracts=int(remaining),
                                               execution=LimitExecution(limit_price=limit))
            else:
                order = EquitySellToCloseOrder(symbol=position.symbol, quantity=remaining,
                                               execution=LimitExecution(limit_price=limit), sleeve="aggressive")
            filled = self._work(order, position, "sell", limit, (bid + ask) / 2, reason=reason,
                                wait=min(30, self.config.fill_wait_seconds))
            if filled is None:
                return
            remaining -= filled
        if remaining <= ZERO:
            self.account.ledger.append("position_closed", pid=position.pid, symbol=position.trade_symbol,
                                       reason=reason)
            self.notes.append(f"closed {position.trade_symbol} ({reason})")
            self._counterfactual_mark(position.pid, f"exit: {reason}")
        else:
            self._fault(f"{reason} exit of {position.trade_symbol}: {remaining} left unfilled; retried next tick")

    def _trade_quote(self, position: Position) -> Optional[tuple[Decimal, Decimal]]:
        if position.occ:
            return self.chain.option_bid_ask(position.occ)
        return self._quotes(position.symbol)

    # -- the scan ------------------------------------------------------------

    def scan(self, now: datetime, *, label: str, dry_run: bool = False) -> ScanOutcome:
        """One model pass and the rules engine's verdict on every idea. A dry
        run (the golden replay) validates and sizes but places nothing."""
        cfg = self.config
        day = now.astimezone(NY).date()
        if not dry_run:
            self._mark_scan(now, label)
        gate = self.gate
        held = list(self.positions().values())
        waiting = list(self.pending().values())   # pullback entries hold slots (ruling 2026-11-16)
        entries_open = (now.astimezone(NY) < self.cut(day, cfg.no_entries_after)
                        and not (gate is not None and gate.kill_switch_tripped))
        if not dry_run:
            if not entries_open:
                return ScanOutcome(False, note="entries closed (time, kill switch or halt); no pass bought")
            if len(held) + len(waiting) >= cfg.max_positions:
                return ScanOutcome(False, note="all slots full; no pass bought")
            spent = self.spent_today(day)
            if spent + cfg.pass_cost_reserve_usd > cfg.daily_spend_cap_usd:
                note = (f"spend cap: {spent:.2f} spent today, a pass needs {cfg.pass_cost_reserve_usd} of "
                        f"headroom under {cfg.daily_spend_cap_usd}; no pass bought")
                self.account.ledger.append("spend_cap", spent=spent, cap=cfg.daily_spend_cap_usd, scan=label)
                self.notes.append(note)
                return ScanOutcome(False, note=note)
        # the research brief's A/B (ruling 2026-10-15, second): the arm is drawn
        # per pass; the brief is rendered in BOTH arms (each idea is logged on or
        # off its shortlist) and SENT only in the brief arm
        rendered = self.current_brief()
        arm = self._arm = self.brief_arm()
        brief = rendered if arm == "brief" else None
        user = self.user_prompt(now, held, brief=brief)
        day_on = cfg.day_trader.enabled
        try:
            result = self.llm.scan(system=system_prompt(cfg, brief=brief is not None), user=user,
                                   tool=report_tool(brief is not None, list(cfg.concentration.themes),
                                                    SETUPS if day_on else None))
        except Exception as error:  # noqa: BLE001 - a failed pass is a fault, never a trade
            self._fault(f"scan {label}: the model pass failed ({type(error).__name__}: {str(error)[:200]}); "
                        f"nothing traded")
            return ScanOutcome(False, note="pass failed")
        structured = result.structured or {}
        ideas = list(structured.get("ideas") or [])[: cfg.max_ideas_per_scan]
        outcome = ScanOutcome(True, cost_usd=result.cost_usd, ideas=ideas, input_tokens=result.input_tokens or 0,
                              brief_arm=arm)
        if not dry_run:
            self.account.ledger.append(
                "llm_pass", scan=label, model=result.model, cost_usd=result.cost_usd,
                token_cost_usd=result.token_cost_usd, searches=result.searches,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                transcript_hash=result.transcript_hash, reported=result.structured is not None,
                market_view=structured.get("market_view"), no_trade_reason=structured.get("no_trade_reason"),
                ideas=ideas,
                # which brief this pass saw (ruling 2026-10-15): the learning
                # loop measures whether it helps
                brief_arm=arm,
                brief=(dict(sha=rendered.sha, chars=rendered.chars, tokens_estimate=rendered.tokens_estimate,
                            sections=rendered.sections, sent=brief is not None) if rendered is not None else None),
                ideas_on_shortlist=([str(i.get("symbol", "")).upper().strip() in rendered.symbols for i in ideas]
                                    if rendered is not None else None),
            )
        if result.structured is None:
            self._fault(f"scan {label}: the model returned no report ({result.stop_reason}); nothing traded")
            return outcome
        taken: list[str] = [p.symbol for p in held] + self.traded_today(day)
        # one bet per sector or theme (ruling 2026-11-12, item 2): what is held,
        # and what earlier ideas in this pass PLACED an entry order (readings
        # 2026-11-12, later: a refused idea never blocks a later one). The
        # stricter reading - every earlier idea counts - runs as a logged shadow.
        held_keys: dict[tuple[str, str], str] = {}
        for position in held:
            for key in self.concentration_keys(position.symbol, position.theme):
                held_keys.setdefault(key, position.symbol)
        for row in waiting:   # a placed pullback entry occupies its sector and theme like a position
            for key in self.concentration_keys(row["symbol"], (row.get("plan") or {}).get("theme")):
                held_keys.setdefault(key, row["symbol"])
        pass_keys: dict[tuple[str, str], str] = {}
        strict_keys: dict[tuple[str, str], str] = {}
        self._options_this_scan = 0
        self._opened_pids = {}
        self._placed = set()
        self._moves = {}
        self._scan_context = {}
        anchors: dict[str, Optional[Decimal]] = {}
        # Stop chasing (ruling 2026-11-16): measure, refuse what cannot be
        # entered, ask the model about the rest with its context in front of it
        routes: dict[int, tuple] = {}
        if day_on and not dry_run:
            routes = self._day_trader_routes(ideas, now, label, taken, held, waiting, anchors)
        for index, idea in enumerate(ideas):
            symbol = str(idea.get("symbol", "")).upper().strip()
            keys = self.concentration_keys(symbol, idea.get("theme")) if _SYMBOL.match(symbol) else set()
            if not dry_run and _SYMBOL.match(symbol) and symbol not in anchors:
                anchors[symbol] = usable_price(self._reader.quote(symbol))
                if anchors[symbol] is not None:
                    self.account.ledger.append("anchor_check", scan=label, symbol=idea.get("symbol"),
                                               **self.anchor_check(idea, anchors[symbol]))
            verdict = self.validate(idea, now, taken,
                                    slots=cfg.max_positions - len(held) - len(waiting) - len(outcome.opened),
                                    live=not dry_run)
            route = routes.get(index)
            if verdict.ok and route is not None and route[0] in ("refuse", "drop"):
                verdict = Verdict(False, route[1])
            strict: Optional[Verdict] = None
            if verdict.ok:
                verdict = self.concentration(idea, keys, held_keys, pass_keys)
                if verdict.ok:
                    strict = self.concentration(idea, keys, held_keys, strict_keys)
            for key in keys:    # the strict shadow: every proposed idea counts
                strict_keys.setdefault(key, symbol)
            if not verdict.ok:
                outcome.rejected.append((str(idea.get("symbol")), verdict.reason))
                if not dry_run:
                    extra = {}
                    if verdict.reason.startswith("concentration"):
                        # the levels it would have had, so the replay shadow can score it
                        extra = dict(code="concentration", levels=self._shadow_levels(idea, anchors.get(symbol)))
                    elif route is not None and route[0] in ("refuse", "drop") and verdict.reason == route[1]:
                        # the chase it would have been (ruling 2026-11-16, item 5): scored by the shadow
                        extra = dict(code=route[2], levels=self._shadow_levels(idea, anchors.get(symbol)),
                                     context=self._scan_context.get(symbol))
                    self.account.ledger.append("idea_rejected", scan=label, symbol=idea.get("symbol"),
                                               reason=verdict.reason, idea=idea, **extra)
                continue
            if dry_run:
                outcome.opened.append(symbol)
                taken.append(symbol)
                if idea.get("instrument") in OPTIONS:
                    self._options_this_scan += 1
                for key in keys:
                    pass_keys.setdefault(key, symbol)
                continue
            if route is not None and route[0] == "pullback":
                opened = self._place_pullback(idea, now, label, route[1], anchors.get(symbol))
            else:
                opened = self._enter(idea, now, label)
            if symbol in self._placed:  # placed: it now counts against every later idea
                for key in keys:
                    pass_keys.setdefault(key, symbol)
            if strict is not None and not strict.ok:
                # the stricter reading would have refused it: logged for the comparison
                self.account.ledger.append(
                    "concentration_strict_shadow", scan=label, symbol=symbol, reason=strict.reason,
                    pid=self._opened_pids.get(symbol), placed=symbol in self._placed, idea=idea,
                    levels=self._shadow_levels(idea, anchors.get(symbol)))
            if opened is None:
                outcome.rejected.append((str(idea.get("symbol")), "entry not filled or refused (see ledger)"))
                continue
            outcome.opened.append(opened)
            taken.append(symbol)
        if not dry_run:
            self._log_moves_since_public(ideas, label, now, anchors)
        self.notes.append(
            f"scan {label}: {len(ideas)} idea(s), opened {outcome.opened or 'none'}, rejected "
            f"{[f'{s}: {r}' for s, r in outcome.rejected] or 'none'}; pass cost {result.cost_usd:.3f} "
            f"({result.searches} searches)"
        )
        return outcome

    def brief_arm(self) -> Optional[str]:
        """This pass's arm: None while the brief is off; "brief" or "control"
        under the A/B (drawn at random, ``brief_ab_share`` to the brief arm);
        always "brief" under "always"."""
        mode = self.config.brief_mode
        if mode == "off":
            return None
        if mode == "always":
            return "brief"
        return "brief" if self._coin() < self.config.brief_ab_share else "control"

    def current_brief(self) -> Optional[Brief]:
        """The research brief for this scan, or None while it is switched off.
        A renderer failure still gives a (empty) fenced brief, so a brief-arm
        prompt keeps the shape the system prompt describes; the fault is logged."""
        if self.config.brief_mode == "off":
            return None
        try:
            if self._brief is None:
                raise RuntimeError("no brief renderer wired")
            return self._brief()
        except Exception as error:  # noqa: BLE001 - reference data never stops a scan
            logger.warning("research brief unavailable: %s: %s", type(error).__name__, error)
            text = f"{BRIEF_BEGIN}\n(unavailable this scan)\n{BRIEF_END}"
            return Brief(text, "unavailable", len(text), {"all": f"missing: {type(error).__name__}"})

    def user_prompt(self, now: datetime, held: list[Position], brief: Optional[Brief] = None) -> str:
        cfg = self.config
        day = now.astimezone(NY).date()
        views = [HeldView(p.symbol, p.instrument + (f" {p.occ}" if p.occ else ""), p.horizon,
                          p.opened_day.isoformat(), str(p.stop), str(p.target), p.thesis[:300],
                          theme=p.theme or "not recorded") for p in held]
        local = now.astimezone(NY)
        waiting = [row["symbol"] for row in self.pending().values()]
        return build_user(
            now, held=views, traded_today=self.traded_today(day),
            max_positions=cfg.max_positions,
            max_ideas=cfg.max_ideas_per_scan,
            entries_open=local < self.cut(day, cfg.no_entries_after),
            intraday_open=local < self.cut(day, cfg.no_intraday_entries_after),
            brief=brief.text if brief is not None else None,
            pending=waiting,
        )

    # -- validation --------------------------------------------------------

    def validate(self, idea: dict, now: datetime, taken: list[str], *, slots: int, live: bool = True) -> Verdict:
        """The deterministic verdict on one idea. Stop and target are DISTANCES
        (percent of the entry price, human ruling 2026-10-10): the levels are
        set from the live quote at order time, so a model's stale or invented
        price can no longer misplace them. ``live`` = False (the golden
        replay) skips the live-quote requirement."""
        cfg = self.config
        local = now.astimezone(NY)
        day = local.date()
        symbol = str(idea.get("symbol", "")).upper().strip()
        instrument = idea.get("instrument")
        horizon = idea.get("horizon")
        if not _SYMBOL.match(symbol):
            return Verdict(False, f"malformed symbol {symbol!r}")
        if instrument not in INSTRUMENTS or horizon not in HORIZONS:
            return Verdict(False, f"unknown instrument/horizon {instrument!r}/{horizon!r}")
        if idea.get("theme") not in cfg.concentration.themes:
            # the cap reads it (ruling 2026-11-12, item 2): no theme, no entry
            return Verdict(False, f"theme {idea.get('theme')!r} is not one of the configured themes")
        if cfg.day_trader.enabled and idea.get("setup") not in SETUPS:
            # measured by setup (ruling 2026-11-16, items 4-5): no setup, no entry
            return Verdict(False, f"setup {idea.get('setup')!r} is not one of the fixed setup types")
        if slots <= 0:
            return Verdict(False, "no free slot")
        if symbol in {t.upper() for t in taken}:
            return Verdict(False, "already held or traded today")
        if local >= self.cut(day, cfg.no_entries_after):
            return Verdict(False, f"no new entries after {self.cut(day, cfg.no_entries_after):%H:%M} ET")
        if horizon == "intraday" and local >= self.cut(day, cfg.no_intraday_entries_after):
            return Verdict(False, f"no intraday entries after "
                                  f"{self.cut(day, cfg.no_intraday_entries_after):%H:%M} ET")
        try:
            confidence = int(idea.get("confidence"))
            stop_fraction = Decimal(str(idea.get("stop_pct"))) / 100
            target_fraction = Decimal(str(idea.get("target_pct"))) / 100
        except (TypeError, ValueError, ArithmeticError):
            return Verdict(False, "confidence, stop_pct or target_pct missing or not numeric")
        if not (stop_fraction.is_finite() and target_fraction.is_finite()) or stop_fraction <= 0 or target_fraction <= 0:
            return Verdict(False, "stop_pct and target_pct must be positive distances")
        if confidence < cfg.min_confidence:
            return Verdict(False, f"confidence {confidence} below {cfg.min_confidence}")
        if not cfg.min_stop_fraction <= stop_fraction <= cfg.max_stop_fraction:
            return Verdict(False, f"stop {stop_fraction:.2%} from entry, outside [{cfg.min_stop_fraction:.1%}, "
                                  f"{cfg.max_stop_fraction:.0%}]")
        if target_fraction < cfg.min_reward_risk * stop_fraction:
            return Verdict(False, f"reward:risk {target_fraction / stop_fraction:.2f} below {cfg.min_reward_risk}")
        if bullish(instrument) is False and target_fraction >= 1:
            return Verdict(False, "a put target 100% or more below entry is not a price")
        if live and self._quotes(symbol) is None:
            return Verdict(False, "no live quote for the underlying")
        return Verdict(True)

    def concentration_keys(self, symbol: str, theme: Any) -> set[tuple[str, str]]:
        """What an idea or a position occupies: its theme, and its sector when
        the gate's map knows it (an unmapped name is its own sector, so it can
        only collide by theme or by symbol)."""
        keys: set[tuple[str, str]] = set()
        cfg = self.config.concentration
        # an exempt theme ("other") is no shared bet: only its sector collides
        if isinstance(theme, str) and theme in cfg.themes and theme not in cfg.exempt_themes:
            keys.add(("theme", theme))
        sector = self._sectors.sector_of(symbol)
        if not sector.startswith("unmapped:"):
            keys.add(("sector", sector))
        return keys

    def concentration(self, idea: dict, keys: set[tuple[str, str]], held: dict[tuple[str, str], str],
                      proposed: dict[tuple[str, str], str]) -> Verdict:
        """One bet per sector or theme (human ruling 2026-11-12, item 2, a live
        rule). ``proposed`` holds what earlier ideas in the pass occupy: in
        the live rule only those that placed an entry order (readings
        2026-11-12, later); in the strict shadow every earlier idea."""
        for kind, name in sorted(keys):
            if (kind, name) in held:
                return Verdict(False, f"concentration: {kind} {name} already held ({held[(kind, name)]})")
            if (kind, name) in proposed:
                return Verdict(False, f"concentration: {kind} {name} already proposed in this pass "
                                      f"({proposed[(kind, name)]})")
        limit = self.config.concentration.max_new_options_per_scan
        if idea.get("instrument") in OPTIONS and self._options_this_scan >= limit:
            return Verdict(False, f"concentration: at most {limit} new option(s) per scan")
        return Verdict(True)

    def _shadow_levels(self, idea: dict, anchor: Optional[Decimal]) -> Optional[dict]:
        """The stop and target a refused idea would have had (for the replay shadow)."""
        try:
            stop_fraction = Decimal(str(idea.get("stop_pct"))) / 100
            target_fraction = Decimal(str(idea.get("target_pct"))) / 100
        except (ArithmeticError, ValueError, TypeError):
            return None
        if anchor is None or not (stop_fraction.is_finite() and target_fraction.is_finite()):
            return None
        stop, target = levels(str(idea.get("instrument")), anchor, stop_fraction, target_fraction)
        return {"anchor": str(anchor), "stop": str(stop), "target": str(target)}

    def _log_moves_since_public(self, ideas: list[dict], label: str, now: datetime,
                                anchors: dict[str, Optional[Decimal]]) -> None:
        """Move since public (human ruling 2026-11-12, item 4): one row per
        proposed idea, taken or not. Logged only - the entries are already
        placed when this runs, and a failure is a row, never a stopped scan."""
        if self._catalyst_clock is None or not self.config.move_since_public.enabled:
            return
        for idea in ideas:
            symbol = str(idea.get("symbol", "")).upper().strip()
            if not _SYMBOL.match(symbol):
                continue
            row = dict(self._move_row(idea, label, now, anchors.get(symbol)) or {})
            row["pid"] = self._opened_pids.get(symbol)
            self.account.ledger.append("move_since_public", **row)

    def _move_row(self, idea: dict, label: str, now: datetime, anchor: Optional[Decimal]) -> Optional[dict]:
        """One idea's move since public, measured once per symbol per scan
        (the day-trader rules read it before entry; the log after). None while
        the measurement is off."""
        if self._catalyst_clock is None or not self.config.move_since_public.enabled:
            return None
        symbol = str(idea.get("symbol", "")).upper().strip()
        if symbol in self._moves:
            return self._moves[symbol]
        sign = -1 if idea.get("instrument") == "put" else 1
        row: dict[str, Any] = dict(scan=label, symbol=symbol, instrument=idea.get("instrument"),
                                   horizon=idea.get("horizon"), pid=None,
                                   catalyst=str(idea.get("catalyst") or "")[:300])
        try:
            row.update(self._catalyst_clock.measure(
                symbol, str(idea.get("catalyst") or idea.get("thesis") or ""), sign, now, anchor))
        except Exception as error:  # noqa: BLE001 - a failed read is a gap, never a stopped scan
            row.update(status="error", error=f"{type(error).__name__}: {str(error)[:200]}")
            datahealth.report("AI trader move since public", f"{symbol}: {row['error']}")
        self._moves[symbol] = row
        return row

    def anchor_check(self, idea: dict, live_mid: Decimal) -> dict:
        """Ruling 2026-10-10: log the gap between the live price and any price
        the model cites (``price_seen``, and $-figures in its thesis). Logged,
        never gated: the levels come from the live quote either way."""
        cited: list[Decimal] = []
        seen = idea.get("price_seen")
        try:
            seen_value = Decimal(str(seen)) if seen not in (None, "") else None
        except (ArithmeticError, ValueError):
            seen_value = None
        for match in _CITED.finditer(str(idea.get("thesis", ""))):
            try:
                cited.append(Decimal(match.group(1).replace(",", "")))
            except ArithmeticError:
                continue

        def gap(value: Decimal) -> Optional[str]:
            return f"{(value / live_mid - 1):.4f}" if live_mid > ZERO and value > ZERO else None

        return dict(live_mid=str(live_mid), price_seen=str(seen_value) if seen_value is not None else None,
                    price_seen_gap=gap(seen_value) if seen_value is not None else None,
                    thesis_prices=[str(c) for c in cited[:10]], thesis_gaps=[gap(c) for c in cited[:10]])

    # -- entries -----------------------------------------------------------

    def _enter(self, idea: dict, now: datetime, label: str) -> Optional[str]:
        cfg = self.config
        gate = self.gate
        symbol = str(idea["symbol"]).upper().strip()
        instrument = idea["instrument"]
        stop_fraction = Decimal(str(idea["stop_pct"])) / 100
        target_fraction = Decimal(str(idea["target_pct"])) / 100
        plan, budget = self._base_plan(idea, label)
        if cfg.day_trader.enabled:
            plan.update(entry_mode="at_market")
        # The levels come from the LIVE price at order time (ruling 2026-10-10),
        # never from a price the model cited - read by the quote rule (ruling
        # 2026-11-12, item 1): the mid of a tight quote, else the last trade,
        # never a wide quote's mid (LUNR 11-12: an IEX mid 6.8% under the fill
        # put an "8%" stop 14% away).
        reading = self._reader.quote(symbol)
        if reading.bid is None or reading.ask is None:
            plan.update(stop="0", target="0")
            self._reject(plan, "quote vanished before entry")
            return None
        anchor = usable_price(reading)
        if anchor is None:
            plan.update(stop="0", target="0")
            self._reject(plan, "no usable price: the quote is wider than 2% of its mid and there is no fresh "
                               "last trade (a level is never set from a wide quote's mid)")
            return None
        bid, ask = reading.bid, reading.ask
        stop, target = levels(instrument, anchor, stop_fraction, target_fraction)
        plan.update(anchor_mid=str(anchor), anchor_source="mid" if reading.value is not None else "last_trade",
                    stop=str(stop), target=str(target))
        if instrument in OPTIONS:
            return self._enter_option(plan, idea, now, budget)
        if not self.account.adapter.tradeable_equity(symbol):
            self._reject(plan, "not tradeable at the broker (inactive, untradable or not fractionable)")
            return None
        limit = (ask * (1 + cfg.limit_buffer)).quantize(CENTS, rounding=ROUND_UP)
        per_share = limit - Decimal(plan["stop"])
        if per_share <= ZERO:
            self._reject(plan, f"limit {limit} at or below the stop")
            return None
        quantity = self._shares(budget, per_share, limit)
        if quantity * limit < Decimal("5"):
            self._reject(plan, f"size {quantity} x {limit} below the dust floor")
            return None
        order = EquityBuyOrder(symbol=symbol, quantity=quantity, execution=LimitExecution(limit_price=limit),
                               sleeve="aggressive", confidence=plan["confidence"])
        return self._open(order, plan, limit, anchor)

    def _shares(self, budget: Decimal, per_share: Decimal, limit: Decimal) -> Decimal:
        """Shares for ``budget`` at risk ``per_share``, inside the gate's
        single-position cap and the settled cash."""
        gate = self.gate
        quantity = budget / per_share
        cap = gate.sleeve_nav(Sleeve.AGGRESSIVE) * gate.limits.aggressive_sleeve.max_single_position * Decimal("0.995")
        if self._max_units is not None:
            quantity = min(quantity, self._max_units)
        return min(quantity, cap / limit, gate.state.settled_buying_power / limit).quantize(STEP, rounding=ROUND_DOWN)

    def _base_plan(self, idea: dict, label: str) -> tuple[dict, Decimal]:
        """The plan every entry carries, and its risk budget (2% of NAV)."""
        cfg = self.config
        symbol = str(idea["symbol"]).upper().strip()
        instrument = idea["instrument"]
        nav = self.gate.state.nav
        budget = (nav * cfg.risk_fraction).quantize(CENTS, rounding=ROUND_DOWN)
        pid = uuid.uuid4().hex[:12]
        plan = dict(pid=pid, symbol=symbol, instrument=instrument, horizon=idea["horizon"],
                    stop_pct=str(idea["stop_pct"]), target_pct=str(idea["target_pct"]),
                    thesis=str(idea.get("thesis", ""))[:1000], source=str(idea.get("source", ""))[:1000],
                    catalyst=str(idea.get("catalyst", ""))[:500], invalidation=str(idea.get("invalidation", ""))[:500],
                    confidence=int(idea["confidence"]), scan=label, risk_budget=budget, nav=nav,
                    theme=str(idea.get("theme")))
        if self._arm is not None:  # the A/B arm rides on the position (ruling 2026-10-15)
            plan.update(brief_arm=self._arm)
        if "brief_use" in idea:  # the brief's use rides on the position (ruling 2026-10-15)
            plan.update(brief_use=str(idea.get("brief_use"))[:20], brief_items=str(idea.get("brief_items") or "")[:500])
        # Leverage is allowed and MEASURED (ruling 2026-10-14, second): the
        # factor and its tag ride on the plan, so the gross figures, the
        # scoreboard and the journal can tell a 3x fund from a stock pick.
        factor = self.leverage_factor(symbol, instrument)
        plan.update(leverage=str(factor), tag=leverage.tag(factor))
        # measured on every entry while the day-trader rules are on (ruling
        # 2026-11-16, item 5): the setup the model named and the context the
        # anti-chase rule read
        if "setup" in idea:
            plan.update(setup=str(idea.get("setup"))[:40])
        if symbol in self._scan_context:
            plan.update(context=self._scan_context[symbol])
        return plan, budget

    # -- stop chasing (human ruling 2026-11-16) ----------------------------------------

    def _day_trader_routes(self, ideas: list[dict], now: datetime, label: str, taken: list[str],
                           held: list[Position], waiting: list[dict],
                           anchors: dict[str, Optional[Decimal]]) -> dict[int, tuple]:
        """Items 1-3 before any entry. Each idea that passes validation is
        measured; one whose extension cannot be measured, or that is extended
        with no pullback level beyond the price, is refused (``refuse``); the
        rest go to the confirm step with their context line, and come back as
        ``market`` (not extended, kept), ``pullback`` (extended, kept) or
        ``drop``. A route is (kind, reason-or-level, code)."""
        cfg = self.config
        multiple = cfg.day_trader.extended_atr
        routes: dict[int, tuple] = {}
        asking: list[tuple[int, ConfirmItem]] = []
        free = cfg.max_positions - len(held) - len(waiting)
        for index, idea in enumerate(ideas):
            symbol = str(idea.get("symbol", "")).upper().strip()
            if not _SYMBOL.match(symbol):
                continue
            anchors[symbol] = usable_price(self._reader.quote(symbol))
            if anchors[symbol] is not None:
                self.account.ledger.append("anchor_check", scan=label, symbol=idea.get("symbol"),
                                           **self.anchor_check(idea, anchors[symbol]))
            if not self.validate(idea, now, taken, slots=free, live=True).ok:
                continue    # the main loop refuses it with its own reason
            bull = bullish(str(idea.get("instrument")))
            sign = 1 if bull else -1
            ctx = self._context(idea, now, label, anchors[symbol])
            extended = ctx.extended(sign, multiple)
            measured = self._measured(ctx, sign, multiple, extended)
            self._scan_context[symbol] = measured
            if extended is None:
                routes[index] = ("refuse", "anti-chase: the move since the prior close cannot be measured (no price, "
                                           "prior close or ATR), so an entry is refused rather than guessed",
                                 "anti_chase")
                self._anti_chase(label, symbol, idea, measured, "refused_unmeasured")
                continue
            entry = "entered at the market."
            if extended:
                level, why = pullback_level(ctx, bull)
                if level is None:
                    routes[index] = ("refuse", f"anti-chase: extended past {multiple:g}x ATR(14) and {why}",
                                     "anti_chase")
                    self._anti_chase(label, symbol, idea, measured, "refused_no_level", why=why)
                    continue
                measured["pullback_level"] = str(level)
                until = self._pullback_until(idea, now)
                entry = (f"a pullback entry at {level} ({'the higher of VWAP and the opening-range low' if bull else 'the lower of VWAP and the opening-range high'}), "
                         f"valid until {until.astimezone(NY):%H:%M} ET; no entry if the price does not come back.")
                routes[index] = ("pullback", level, "anti_chase")
            else:
                routes[index] = ("market", None, "")
            asking.append((index, ConfirmItem(idea, ctx.line(sign, multiple), entry)))
        if not asking:
            return routes
        decisions = self._confirm(now, label, asking)
        for index, item in asking:
            symbol = str(item.idea.get("symbol", "")).upper().strip()
            verdict, why = (decisions or {}).get(symbol, ("drop", "no decision returned" if decisions is not None
                                                          else "the confirm step failed"))
            extended = routes[index][0] == "pullback"
            if verdict != "keep":
                routes[index] = ("drop", f"dropped after the intraday context: {why}"[:400], "context_drop")
                if extended:
                    self._anti_chase(label, symbol, item.idea, self._scan_context.get(symbol), "dropped", why=why)
        return routes

    def _context(self, idea: dict, now: datetime, label: str, anchor: Optional[Decimal]) -> Context:
        symbol = str(idea.get("symbol", "")).upper().strip()
        if self._intraday is None:
            datahealth.report("AI trader intraday context", f"{symbol}: no intraday data source wired")
            return Context(symbol, now)
        try:
            ctx = self._intraday.context(symbol, now, anchor)
        except Exception as error:  # noqa: BLE001 - a failed read refuses the entry, never stops the scan
            datahealth.report("AI trader intraday context", f"{symbol}: {type(error).__name__}: {str(error)[:200]}")
            return Context(symbol, now)
        ctx.catalyst = dict(self._move_row(idea, label, now, anchor) or {})
        return ctx

    @staticmethod
    def _measured(ctx: Context, sign: int, multiple: Decimal, extended: Optional[bool]) -> dict:
        def s(value: Any) -> Optional[str]:
            return None if value is None else f"{value:.6g}"

        return dict(price=s(ctx.price), prior_close=s(ctx.prior_close), atr_fraction=s(ctx.atr_fraction),
                    from_close=s(ctx.from_close), from_open=s(ctx.from_open), from_vwap=s(ctx.from_vwap),
                    vwap=s(ctx.vwap), or_low=s(ctx.or_low), or_high=s(ctx.or_high), rel_volume=s(ctx.rel_volume),
                    catalyst_move=s(ctx.catalyst_move()), catalyst_public_at=ctx.catalyst.get("public_at"),
                    direction=sign, extended_atr=str(multiple), extended=extended, gaps=ctx.gaps or None)

    def _anti_chase(self, label: str, symbol: str, idea: dict, measured: Optional[dict], outcome: str,
                    **extra: Any) -> None:
        """One row per extended idea and what became of it (item 5's shadow)."""
        self.account.ledger.append("anti_chase", scan=label, symbol=symbol, outcome=outcome, idea=idea,
                                   context=measured, **extra)

    def _confirm(self, now: datetime, label: str, asking: list[tuple[int, ConfirmItem]]) -> Optional[dict]:
        """The confirm step: one forced-tool call (no search) with every idea's
        context. Returns symbol -> (keep|drop, reason), or None when the call
        failed - and then nothing is entered (Constraint #6)."""
        cfg = self.config
        symbols = sorted({str(item.idea.get("symbol", "")).upper().strip() for _, item in asking})
        try:
            result = self.llm.confirm(system=confirm_system(cfg), user=confirm_user(now, [i for _, i in asking]),
                                      tool=confirm_tool(symbols), max_tokens=cfg.day_trader.confirm_max_tokens)
        except Exception as error:  # noqa: BLE001 - a failed call is a fault, never a trade
            self._fault(f"scan {label}: the confirm step failed ({type(error).__name__}: {str(error)[:200]}); "
                        f"nothing from this pass entered")
            return None
        decisions: dict[str, tuple[str, str]] = {}
        for row in (result.structured or {}).get("decisions") or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "")).upper().strip()
            if symbol in symbols and symbol not in decisions and row.get("decision") in ("keep", "drop"):
                decisions[symbol] = (row["decision"], str(row.get("reason") or "")[:500])
        self.account.ledger.append(
            "context_pass", scan=label, model=result.model, cost_usd=result.cost_usd,
            token_cost_usd=result.token_cost_usd, input_tokens=result.input_tokens,
            output_tokens=result.output_tokens, transcript_hash=result.transcript_hash,
            reported=result.structured is not None,
            items=[{"symbol": str(i.idea.get("symbol", "")).upper(), "context": i.context, "entry": i.entry}
                   for _, i in asking],
            decisions={s: {"decision": d, "reason": r} for s, (d, r) in decisions.items()})
        if result.structured is None:
            self._fault(f"scan {label}: the confirm step returned no report ({result.stop_reason}); nothing from "
                        f"this pass entered")
            return None
        return decisions

    def _pullback_until(self, idea: dict, now: datetime) -> datetime:
        """A pullback entry lives ``pullback_minutes`` at most, and never past
        the day's entry cutoff for its horizon."""
        cfg = self.config
        day = now.astimezone(NY).date()
        cutoff = self.cut(day, cfg.no_intraday_entries_after if idea.get("horizon") == "intraday"
                          else cfg.no_entries_after)
        return min(now + timedelta(minutes=cfg.day_trader.pullback_minutes), cutoff)

    def _place_pullback(self, idea: dict, now: datetime, label: str, level: Decimal,
                        anchor: Optional[Decimal]) -> Optional[str]:
        """Item 2. A stock or ETF: a resting limit at ``level``, sized from the
        level and its stop (2% of NAV at risk; a fill at or under the limit
        risks no more). A call or put: no order yet - a trigger on the
        underlying; the option is bought, by the normal path, when the
        underlying reaches the level. Returns the symbol once placed."""
        cfg = self.config
        symbol = str(idea["symbol"]).upper().strip()
        instrument = idea["instrument"]
        plan, budget = self._base_plan(idea, label)
        stop_fraction = Decimal(str(idea["stop_pct"])) / 100
        target_fraction = Decimal(str(idea["target_pct"])) / 100
        stop_level, target_level = levels(instrument, level, stop_fraction, target_fraction)
        until = self._pullback_until(idea, now)
        plan.update(entry_mode="pullback", pullback_level=str(level), chase_anchor=str(anchor) if anchor else None)
        row = dict(pid=plan["pid"], scan=label, symbol=symbol, instrument=instrument, horizon=idea["horizon"],
                   level=str(level), stop_level=str(stop_level), target_level=str(target_level),
                   expires_at=until.isoformat(), plan={k: v for k, v in plan.items() if k != "nav"}, idea=idea,
                   chase=self._shadow_levels(idea, anchor))
        if instrument in OPTIONS:
            self.account.ledger.append("pullback_placed", kind="trigger", **row)
            self._anti_chase(label, symbol, idea, self._scan_context.get(symbol), "pullback", pid=plan["pid"])
            self._placed.add(symbol)
            self._options_this_scan += 1
            self.notes.append(f"pullback trigger {symbol} {instrument} at {level} until {until.astimezone(NY):%H:%M}")
            return symbol
        if not self.account.adapter.tradeable_equity(symbol):
            self._reject(plan, "not tradeable at the broker (inactive, untradable or not fractionable)")
            return None
        per_share = level - stop_level
        if per_share <= ZERO:
            self._reject(plan, f"pullback level {level} at or below its stop")
            return None
        quantity = self._shares(plan["risk_budget"], per_share, level)
        if quantity * level < Decimal("5"):
            self._reject(plan, f"size {quantity} x {level} below the dust floor")
            return None
        face, adjusted = self.gross()
        cost = quantity * level
        factor = abs(Decimal(str(plan.get("leverage", "1"))))
        nav = self.gate.state.nav
        basis = adjusted + cost * factor if cfg.gross_basis == "leverage_adjusted" else face + cost
        if basis > nav * cfg.max_gross_fraction:
            self._reject(plan, f"gross cap ({cfg.gross_basis}): {basis:.2f} with this pullback entry would pass "
                               f"{cfg.max_gross_fraction:.0%} of NAV")
            return None
        order = EquityBuyOrder(symbol=symbol, quantity=quantity, execution=LimitExecution(limit_price=level),
                               sleeve="aggressive", confidence=plan["confidence"])
        decision = self.gate.submit(order)
        if not decision.is_approved:
            self._reject(plan, f"gate: {decision.code}: {decision.message}")
            return None
        reference = f"lab-{self.account.name}-pb-{plan['pid']}"
        # the plan is on the ledger BEFORE the order: a crash between the two
        # is recovered by the reference (``_pullback_order_id``)
        self.account.ledger.append("pullback_placed", kind="limit", quantity=str(quantity),
                                   client_reference=reference, **row)
        try:
            receipt = self.account.adapter.submit_order(decision, client_reference=reference)
        except BrokerError as error:
            self.gate.cancel(decision)
            self.account.ledger.append("pullback_done", pid=plan["pid"], outcome="refused",
                                       detail=f"broker refused: {str(error)[:200]}", filled_qty="0")
            self._fault(f"broker refused the pullback entry {symbol}: {error}")
            return None
        self.account.ledger.append("pullback_order", pid=plan["pid"], order_id=receipt.broker_order_id)
        self._anti_chase(label, symbol, idea, self._scan_context.get(symbol), "pullback", pid=plan["pid"])
        self._placed.add(symbol)
        self.notes.append(f"pullback limit {symbol} x {quantity} at {level} until {until.astimezone(NY):%H:%M}")
        return symbol

    def _work_pullbacks(self, now: datetime) -> None:
        """Every tick: each waiting pullback entry is filled, kept, or ended."""
        for row in list(self.pending().values()):
            try:
                if row.get("kind") == "limit":
                    self._work_limit(row, now)
                else:
                    self._work_trigger(row, now)
            except Exception as error:  # noqa: BLE001 - one entry's failure must not stop the tick's exits
                self._fault(f"pullback entry {row.get('symbol')}: {type(error).__name__}: {str(error)[:200]}")

    def _stale(self, row: dict, now: datetime, price: Optional[Decimal]) -> Optional[str]:
        """Why a waiting entry ends now, if it does: its time, the day's
        cutoff, the kill switch or halt, or its stop level breached first."""
        if now >= datetime.fromisoformat(row["expires_at"]):
            return f"expired unfilled after {self.config.day_trader.pullback_minutes} minutes or at the entry cutoff"
        if (self.gate is not None and self.gate.kill_switch_tripped) or self.account.halted():
            return "the kill switch or an operator halt stopped new entries"
        if price is not None:
            stop = Decimal(row["stop_level"])
            if (price <= stop) if bullish(row["instrument"]) else (price >= stop):
                return f"the price {price} breached its stop level {stop} before the entry"
        return None

    def _work_limit(self, row: dict, now: datetime) -> None:
        adapter = self.account.adapter
        order_id = row.get("order_id") or self._pullback_order_id(row)
        if order_id is None:
            self._finish_pullback(row, None, "never reached the broker (no order under its reference)")
            return
        status = adapter.get_order(order_id)
        if status.is_terminal:
            self._finish_pullback(row, status, f"the broker ended it ({status.status})", order_id=order_id)
            return
        why = self._stale(row, now, usable_price(self._reader.quote(row["symbol"])))
        if why is not None:
            self._finish_pullback(row, self._terminal(order_id), why, order_id=order_id)

    def _pullback_order_id(self, row: dict) -> Optional[str]:
        lookup = getattr(self.account.adapter, "get_order_by_client_reference", None)
        status = lookup(row.get("client_reference")) if lookup is not None and row.get("client_reference") else None
        order_id = getattr(status, "broker_order_id", None) if status is not None else None
        if order_id:
            self.account.ledger.append("pullback_order", pid=row["pid"], order_id=order_id, recovered=True)
        return order_id

    def _finish_pullback(self, row: dict, status: Optional[OrderStatus], why: str, *,
                         order_id: Optional[str] = None) -> None:
        """A limit's end: filled (all or part) becomes a position with its stop
        and target measured from the ACTUAL fill; nothing filled is a refusal
        with the chase it would have been, for the shadow and the journal."""
        filled = status.filled_quantity if status is not None else ZERO
        if filled > ZERO and status.filled_avg_price is not None:
            fill = status.filled_avg_price
            plan = dict(row["plan"])
            stop, target = levels(row["instrument"], fill, Decimal(plan["stop_pct"]) / 100,
                                  Decimal(plan["target_pct"]) / 100)
            plan.update(anchor_mid=str(fill), anchor_source="pullback_fill", stop=str(stop), target=str(target),
                        pullback_placed_at=row["at"])
            self.account.ledger.append("position_opened", **plan)
            position = Position(pid=plan["pid"], symbol=plan["symbol"], instrument=plan["instrument"],
                                horizon=plan["horizon"], stop=stop, target=target, thesis=plan.get("thesis", ""),
                                opened_at="")
            self._settle(None, order_id or "", position, "buy", row["level"], row["level"], status, "entry", 1)
            self.account.ledger.append("pullback_done", pid=row["pid"], outcome="filled",
                                       detail=why if status.status != "filled" else "filled",
                                       filled_qty=str(filled), avg_price=str(fill))
            self.notes.append(f"pullback entry filled: {plan['symbol']} x {filled} at {fill}")
            return
        self._pullback_unfilled(row, why)

    def _pullback_unfilled(self, row: dict, why: str) -> None:
        self.account.ledger.append("pullback_done", pid=row["pid"], outcome="unfilled", detail=why, filled_qty="0")
        self.account.ledger.append(
            "idea_rejected", scan=row.get("scan"), symbol=row["symbol"], code="pullback_unfilled", pid=row["pid"],
            reason=f"pullback entry at {row['level']} not reached: {why}"[:400], idea=row.get("idea"),
            levels=row.get("chase"), context=(row.get("plan") or {}).get("context"))
        self.notes.append(f"pullback entry {row['symbol']} ended unfilled: {why}")

    def _work_trigger(self, row: dict, now: datetime) -> None:
        """An option's pullback: when the underlying reaches the level, the
        option is bought by the normal path (contract pick, premium budget,
        the gate), its stop and target set from the underlying's price then."""
        reading = self._reader.quote(row["symbol"])
        price = usable_price(reading)
        why = self._stale(row, now, price)
        if why is not None:
            self._pullback_unfilled(row, why)
            return
        if price is None:
            return
        level = Decimal(row["level"])
        if (price > level) if bullish(row["instrument"]) else (price < level):
            return
        plan = dict(row["plan"])
        nav = self.gate.state.nav
        budget = (nav * self.config.risk_fraction).quantize(CENTS, rounding=ROUND_DOWN)
        stop, target = levels(row["instrument"], price, Decimal(plan["stop_pct"]) / 100,
                              Decimal(plan["target_pct"]) / 100)
        plan.update(nav=nav, risk_budget=budget, anchor_mid=str(price), anchor_source="pullback_trigger",
                    stop=str(stop), target=str(target), pullback_placed_at=row["at"])
        opened = self._enter_option(plan, row.get("idea") or {}, now, budget)
        self.account.ledger.append("pullback_done", pid=row["pid"], outcome="filled" if opened else "refused",
                                   detail=f"the underlying reached {level} at {price}",
                                   filled_qty="1" if opened else "0")
        if not opened:
            self.account.ledger.append(
                "idea_rejected", scan=row.get("scan"), symbol=row["symbol"], code="pullback_unfilled", pid=row["pid"],
                reason=f"pullback trigger at {level} reached but the option entry failed (see the ledger)",
                idea=row.get("idea"), levels=row.get("chase"), context=plan.get("context"))

    def _enter_option(self, plan: dict, idea: dict, now: datetime, budget: Decimal) -> Optional[str]:
        cfg = self.config.options
        today = now.astimezone(NY).date()
        low, high = cfg.dte[plan["horizon"]]
        chain = self.chain.chain_for(plan["symbol"], min_expiry=today + timedelta(days=low),
                                     max_expiry=today + timedelta(days=high))
        pick, why = select_contract(chain, "call" if plan["instrument"] == "call" else "put", today, cfg)
        if pick is None:
            self._reject(plan, f"no contract: {why}")
            return None
        limit = (pick.ask * (1 + self.config.limit_buffer)).quantize(CENTS, rounding=ROUND_UP)
        contracts = int((budget / (limit * pick.multiplier)).to_integral_value(rounding=ROUND_DOWN))
        if self._max_units is not None:
            contracts = min(contracts, int(self._max_units))
        if contracts < 1:
            self._reject(plan, f"one contract costs {limit * pick.multiplier}, above the {budget} premium budget")
            return None
        plan.update(occ=pick.occ_symbol, expiry=pick.expiration.isoformat(), right=pick.right,
                    strike=str(pick.strike), delta=str(pick.delta), multiplier=pick.multiplier)
        order = OptionBuyToOpenOrder(symbol=pick.occ_symbol, underlying=pick.underlying, right=pick.right,
                                     expiration=pick.expiration, strike=pick.strike, contracts=contracts,
                                     multiplier=pick.multiplier, execution=LimitExecution(limit_price=limit),
                                     confidence=plan["confidence"])
        counterfactual = self._expiry_counterfactual(plan, today, budget, pick)
        opened = self._open(order, plan, limit, pick.mid)
        if opened is not None and counterfactual is not None:
            self.account.ledger.append("expiry_counterfactual", phase="entry", **counterfactual)
            if counterfactual["status"] == "ok" and not counterfactual["same_contract"]:
                pending = self.account.state.engine.setdefault("expiry_counterfactual", {})
                pending[plan["pid"]] = {k: counterfactual[k] for k in ("occ", "expiry", "opened_day")}
                self.account.state.save(self.account.state_path)
        return opened

    # -- the expiry counterfactual (human ruling 2026-11-12, item 3) ------------------

    def _expiry_counterfactual(self, plan: dict, today: date, budget: Decimal, pick: Any) -> Optional[dict]:
        """The contract the window before the ruling would have bought, priced
        now. Logged only; a failed read is a logged gap, never a stopped entry."""
        window = self.config.options.counterfactual_dte.get(plan["horizon"])
        if window is None:
            return None
        low, high = window
        try:
            chain = self.chain.chain_for(plan["symbol"], min_expiry=today + timedelta(days=low),
                                         max_expiry=today + timedelta(days=high))
            old, why = select_contract(chain, pick.right, today, self.config.options)
        except Exception as error:  # noqa: BLE001 - logged only
            datahealth.report("AI trader expiry counterfactual", f"{plan['symbol']}: {type(error).__name__}: {error}")
            return {"pid": plan["pid"], "chosen": pick.occ_symbol, "status": "chain_failed"}
        if old is None:
            return {"pid": plan["pid"], "chosen": pick.occ_symbol, "status": f"no contract: {why}"}
        limit = (old.ask * (1 + self.config.limit_buffer)).quantize(CENTS, rounding=ROUND_UP)
        contracts = int((budget / (limit * old.multiplier)).to_integral_value(rounding=ROUND_DOWN))
        return {"pid": plan["pid"], "chosen": pick.occ_symbol, "status": "ok",
                "same_contract": old.occ_symbol == pick.occ_symbol, "occ": old.occ_symbol,
                "expiry": old.expiration.isoformat(), "strike": str(old.strike), "delta": str(old.delta),
                "bid": str(old.bid), "ask": str(old.ask), "limit": str(limit), "contracts": contracts,
                "multiplier": old.multiplier, "opened_day": today.isoformat()}

    def _counterfactual_mark(self, pid: str, when: str) -> None:
        """Price a pending counterfactual contract once: at our exit, or at its
        OWN expiry close if that comes first (it would have been closed then)."""
        pending = self.account.state.engine.get("expiry_counterfactual") or {}
        entry = pending.pop(pid, None)
        if entry is None:
            return
        quote = None
        try:
            quote = self.chain.option_bid_ask(entry["occ"])
        except Exception as error:  # noqa: BLE001 - logged only
            datahealth.report("AI trader expiry counterfactual", f"{entry['occ']}: {type(error).__name__}: {error}")
        self.account.ledger.append("expiry_counterfactual", phase="mark", pid=pid, occ=entry["occ"], when=when,
                                   bid=str(quote[0]) if quote else None, ask=str(quote[1]) if quote else None)
        self.account.state.save(self.account.state_path)

    def _counterfactual_expiries(self, now: datetime) -> None:
        local = now.astimezone(NY)
        for pid, entry in list((self.account.state.engine.get("expiry_counterfactual") or {}).items()):
            expiry, opened = date.fromisoformat(entry["expiry"]), date.fromisoformat(entry["opened_day"])
            close_day = self.calendar.previous_session(expiry) if expiry > opened else expiry
            if local.date() > close_day or (local.date() == close_day and
                                            local >= self.cut(close_day, self.config.options.expiry_close)):
                self._counterfactual_mark(pid, "its own expiry close")

    def _open(self, order: Any, plan: dict, limit: Decimal, modelled: Decimal) -> Optional[str]:
        position = Position(pid=plan["pid"], symbol=plan["symbol"], instrument=plan["instrument"],
                            horizon=plan["horizon"], stop=Decimal(plan["stop"]), target=Decimal(plan["target"]),
                            thesis=plan["thesis"], opened_at="", occ=plan.get("occ"))
        # Explicit gross cap (human ruling 2026-10-14, audit item 4): what is
        # open (with working reservations) plus this order, against NAV - on
        # the configured basis (face until the human rules; ruling 2026-10-14,
        # second, item 2). Both figures are measured and ride on the plan.
        if hasattr(order, "contracts"):
            cost = Decimal(order.contracts) * limit * Decimal(order.multiplier)
        else:
            cost = order.quantity * limit
        face, adjusted = self.gross()
        factor = abs(Decimal(str(plan.get("leverage", "1"))))
        after_face, after_adjusted = face + cost, adjusted + cost * factor
        nav = self.gate.state.nav
        plan.update(gross_after_face=str(round(after_face / nav, 4)) if nav > 0 else None,
                    gross_after_leverage_adjusted=str(round(after_adjusted / nav, 4)) if nav > 0 else None)
        ceiling = nav * self.config.max_gross_fraction
        basis = after_adjusted if self.config.gross_basis == "leverage_adjusted" else after_face
        if basis > ceiling:
            self._reject(plan, f"gross cap ({self.config.gross_basis}): {basis:.2f} after this order would pass "
                               f"{self.config.max_gross_fraction:.0%} of NAV ({ceiling:.2f}); face {after_face:.2f}, "
                               f"leverage-adjusted {after_adjusted:.2f}")
            return None
        decision = self.gate.submit(order)
        if not decision.is_approved:
            self._reject(plan, f"gate: {decision.code}: {decision.message}")
            return None
        # the plan is on the ledger BEFORE the order, so a crash mid-fill
        # recovers into a position with its stop and target
        self.account.ledger.append("position_opened", **plan)
        self._opened_pids[plan["symbol"]] = plan["pid"]
        if plan["instrument"] in OPTIONS:   # one new option per scan (ruling 2026-11-12, item 2)
            self._options_this_scan += 1
        filled = self._work_approved(decision, position, "buy", limit, modelled, reason="entry",
                                     wait=self.config.fill_wait_seconds)
        if filled is not None:   # the broker took the order (filled or not): it was placed
            self._placed.add(plan["symbol"])
        if not filled:
            self.account.ledger.append("position_closed", pid=plan["pid"], symbol=position.trade_symbol,
                                       reason="entry_unfilled")
            self.notes.append(f"entry {position.trade_symbol} unfilled; dropped")
            return None
        self.notes.append(f"opened {position.trade_symbol} x {filled} ({plan['instrument']}, {plan['horizon']})")
        return position.trade_symbol

    def _reject(self, plan: dict, reason: str) -> None:
        self.account.ledger.append("idea_rejected", scan=plan.get("scan"), symbol=plan["symbol"], reason=reason,
                                   idea={k: v for k, v in plan.items() if k not in ("nav",)})

    # -- order working -------------------------------------------------------

    def work(self, order: Any, position: Position, side: str, limit: Decimal, modelled: Decimal, *,
             reason: str) -> Optional[Decimal]:
        """Submit one order through the gate and work it to a terminal state
        (the round trip drives the order path through this)."""
        return self._work(order, position, side, limit, modelled, reason=reason,
                          wait=self.config.fill_wait_seconds)

    def _work(self, order: Any, position: Position, side: str, limit: Decimal, modelled: Decimal, *,
              reason: str, wait: int) -> Optional[Decimal]:
        decision = self.gate.submit(order)
        if not decision.is_approved:
            self._fault(f"gate refused {side} {position.trade_symbol}: {decision.code}: {decision.message}")
            return None
        return self._work_approved(decision, position, side, limit, modelled, reason=reason, wait=wait)

    def _work_approved(self, decision: ApprovedOrder, position: Position, side: str, limit: Decimal,
                       modelled: Decimal, *, reason: str, wait: int) -> Optional[Decimal]:
        reference = f"lab-{self.account.name}-{uuid.uuid4().hex[:10]}"
        try:
            receipt = self.account.adapter.submit_order(decision, client_reference=reference)
        except BrokerError as error:
            self.gate.cancel(decision)
            self._fault(f"broker refused {side} {position.trade_symbol}: {error}")
            return None
        multiplier = 100 if position.occ else 1
        self.account.ledger.append("order", order_id=receipt.broker_order_id, pid=position.pid, side=side,
                                   symbol=position.trade_symbol, instrument=position.instrument, limit=limit,
                                   modelled=modelled, reason=reason, client_reference=reference,
                                   multiplier=multiplier)
        deadline = self._monotonic() + wait
        status = self.account.adapter.get_order(receipt.broker_order_id)
        while not status.is_terminal and self._monotonic() < deadline:
            self._sleep(2)
            status = self.account.adapter.get_order(receipt.broker_order_id)
        if not status.is_terminal:
            status = self._terminal(receipt.broker_order_id)
        return self._settle(decision, receipt.broker_order_id, position, side, limit, modelled, status, reason,
                            multiplier)

    def _settle(self, decision: Optional[ApprovedOrder], order_id: str, position: Position, side: str,
                limit: Any, modelled: Any, status: OrderStatus, reason: str, multiplier: int,
                recovered: bool = False) -> Decimal:
        filled = status.filled_quantity
        if decision is not None:
            if filled > ZERO and status.filled_avg_price is not None:
                self.gate.record_fill(decision, status.filled_avg_price, filled_units=filled)
            else:
                self.gate.cancel(decision)
        self.account.ledger.append(
            "settled", order_id=order_id, pid=position.pid, side=side, symbol=position.trade_symbol,
            instrument=position.instrument, filled_qty=filled, avg_price=status.filled_avg_price or ZERO,
            status=status.status, limit=limit, modelled=modelled, reason=reason, multiplier=multiplier,
            recovered=recovered,
        )
        return filled

    def _terminal(self, order_id: str) -> OrderStatus:
        adapter = self.account.adapter
        status = adapter.get_order(order_id)
        if status.is_terminal:
            return status
        try:
            adapter.cancel_order(order_id)
        except BrokerError as error:
            logger.warning("cancel of %s failed: %s", order_id, error)
        for _ in range(30):
            self._sleep(2)
            status = adapter.get_order(order_id)
            if status.is_terminal:
                return status
        raise RuntimeError(f"order {order_id} is not terminal after cancel; a human must look")

    def _recover(self) -> None:
        """Settle any order a previous process left working."""
        ledger = self.account.ledger
        settled = {e["order_id"] for e in ledger.events(["settled"])}
        plans = {e["pid"]: e for e in ledger.events(["position_opened"])}
        for event in ledger.events(["order"]):
            if event["order_id"] in settled:
                continue
            plan = plans.get(event["pid"], {})
            position = Position(pid=event["pid"], symbol=plan.get("symbol", event["symbol"]),
                                instrument=event.get("instrument", ""), horizon=plan.get("horizon", ""),
                                stop=ZERO, target=ZERO, thesis="", opened_at="", occ=plan.get("occ"))
            status = self._terminal(event["order_id"])
            self._settle(None, event["order_id"], position, event["side"], event["limit"], event["modelled"],
                         status, event.get("reason", ""), int(event.get("multiplier", 1)), recovered=True)
            self.notes.append(f"recovered order {event['order_id']}: {status.status}, filled {status.filled_quantity}")

    def _fault(self, message: str) -> None:
        logger.warning("LAB %s: %s", self.account.name, message)
        self.notes.append(f"FAULT: {message}")
        self.account.ledger.append("fault", message=message)
        # a fault is a card when it happens, not only at the end of the day (data-health rule 2026-10-15)
        datahealth.report("AI trader", message)


def select_contract(chain: Optional[list], right: str, today: date, cfg: Any) -> tuple[Optional[Any], str]:
    """The deterministic contract pick for a long call/put: nearest expiry in
    the window that has a qualifying contract; |delta| in [min, max] closest
    to the target; open-interest and spread floors; IV-percentile ceiling."""
    if not chain:
        return None, "no chain data"
    pool = [q for q in chain if q.right == right]
    population = [q.implied_volatility for q in chain if q.implied_volatility is not None]
    for expiry in sorted({q.expiration for q in pool}):
        candidates = [
            q for q in pool
            if q.expiration == expiry and q.delta is not None
            and cfg.delta_min <= abs(q.delta) <= cfg.delta_max
            and q.open_interest >= cfg.min_open_interest
            and q.ask is not None and q.ask > ZERO
            and q.spread_pct is not None and q.spread_pct <= cfg.max_spread_pct_of_mid
        ]
        if not candidates:
            continue
        pick = min(candidates, key=lambda q: (abs(abs(q.delta) - cfg.target_delta), -q.open_interest, q.strike))
        if pick.implied_volatility is None or not population:
            return None, f"{pick.occ_symbol} carries no implied volatility"
        percentile = Decimal(sum(1 for v in population if v < pick.implied_volatility)) / Decimal(len(population))
        if percentile > cfg.max_iv_percentile:
            return None, f"{pick.occ_symbol} IV at the {percentile:.0%} percentile of its chain"
        return pick, ""
    return None, f"no {right} in the window clears delta, open-interest and spread floors"


def levels(instrument: str, anchor: Decimal, stop_fraction: Decimal, target_fraction: Decimal) -> tuple[Decimal, Decimal]:
    """Stop and target as underlying prices, from the live anchor (ruling
    2026-10-10): below/above for calls, stocks and ETFs; above/below for puts.
    Rounded toward the anchor's side that keeps each level at least its stated
    distance away (a stop never tighter, a target never nearer than asked)."""
    if bullish(instrument):
        stop = (anchor * (1 - stop_fraction)).quantize(CENTS, rounding=ROUND_DOWN)
        target = (anchor * (1 + target_fraction)).quantize(CENTS, rounding=ROUND_UP)
    else:
        stop = (anchor * (1 + stop_fraction)).quantize(CENTS, rounding=ROUND_UP)
        target = (anchor * (1 - target_fraction)).quantize(CENTS, rounding=ROUND_DOWN)
    return stop, target
