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
  levels    stop and target are set from the LIVE mid at order time, at the
            stated distances; the gap between that mid and any price the model
            cites (price_seen, $-figures in its thesis) is logged
            (``anchor_check``), never used.
  size      2% of the account's NAV at risk: shares = budget / (limit - stop)
            for a stock or ETF (capped by the gate's single-position cap and
            the cash), contracts = floor(budget / premium) for an option -
            whose whole premium is the risk. Never a function of P&L.
  pick      the contract for a call/put: the nearest expiry inside the
            horizon's DTE window (0DTE allowed intraday), |delta| in the band
            closest to the target delta, open interest and spread floors, IV
            percentile ceiling. Never the model's choice of strike.
  exit      the stop or target touched (underlying mid, every tick); the
            horizon ended (intraday at 15:50 ET; days/weeks after 5/20
            sessions, at 15:50); an option on its expiry day or the session
            before it, at 15:45 ET. Marketable limits, re-priced until filled.
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
from lab.account import LabAccount
from lab.ai_prompts import HORIZONS, INSTRUMENTS, HeldView, build_user, report_tool, system_prompt
from lab.brief import BEGIN as BRIEF_BEGIN, END as BRIEF_END, Brief
from lab import leverage
from lab.config import AiTraderConfig
from risk_gate.gate import ApprovedOrder, RiskGate
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
    sessions: a horizon then ends a session EARLY - the less-risk error)."""
    days, current = 0, start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


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
            )
        elif kind == "settled" and event.get("pid") in positions:
            filled = Decimal(event["filled_qty"])
            sign = Decimal(1) if event["side"] == "buy" else Decimal(-1)
            positions[event["pid"]].quantity += sign * filled
    return {pid: p for pid, p in positions.items() if p.quantity > ZERO}


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
    ) -> None:
        self.account = account
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
        return face, adjusted

    def spent_today(self, day: date) -> Decimal:
        return sum(
            (Decimal(str(e["cost_usd"])) for e in self.account.ledger.events(["llm_pass"])
             if datetime.fromisoformat(e["at"]).astimezone(NY).date() == day),
            ZERO,
        )

    def traded_today(self, day: date) -> list[str]:
        return [e["symbol"] for e in self.account.ledger.events(["position_opened"])
                if datetime.fromisoformat(e["at"]).astimezone(NY).date() == day]

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
        if gate.kill_switch_tripped and not tripped_before and self._alert is not None:
            self._alert(f"LAB {self.account.name} KILL SWITCH", self.account.state.trip_reason or "tripped")
        self.gate = gate
        return gate

    # -- the tick ----------------------------------------------------------

    def tick(self, now: datetime) -> list[str]:
        self.notes = []
        day = now.astimezone(NY).date()
        self._recover()
        self.rebuild_gate(day)
        self._leverage_watch()
        for position in list(self.positions().values()):
            reason = self.exit_reason(position, now)
            if reason:
                self.close(position, reason, now)
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
        passed = [t for t in self.config.scan_times if local.time() >= dtime.fromisoformat(t)]
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

    def exit_reason(self, position: Position, now: datetime) -> Optional[str]:
        local = now.astimezone(NY)
        today = local.date()
        cfg = self.config
        if position.occ and position.expiry is not None:
            close_day = position.expiry
            if position.expiry > position.opened_day:
                # a multi-day contract closes on the session before expiry
                close_day = position.expiry - timedelta(days=1)
                while close_day.weekday() >= 5:
                    close_day -= timedelta(days=1)
            if today > close_day or (today == close_day and local >= at(today, cfg.options.expiry_close)):
                return "option_expiry_close"
        if position.horizon == "intraday" and local >= at(today, cfg.intraday_close):
            return "intraday_close"
        if position.horizon in cfg.horizon_sessions:
            held = sessions_between(position.opened_day, today)
            if held > cfg.horizon_sessions[position.horizon] or (
                held == cfg.horizon_sessions[position.horizon] and local >= at(today, cfg.intraday_close)
            ):
                return "horizon_end"
        quote = self._quotes(position.symbol)
        if quote is None:
            return None
        mid = (quote[0] + quote[1]) / 2
        if bullish(position.instrument):
            if mid <= position.stop:
                return "stop"
            if mid >= position.target:
                return "target"
        else:
            if mid >= position.stop:
                return "stop"
            if mid <= position.target:
                return "target"
        return None

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
        entries_open = (now.astimezone(NY) < at(day, cfg.no_entries_after)
                        and not (gate is not None and gate.kill_switch_tripped))
        if not dry_run:
            if not entries_open:
                return ScanOutcome(False, note="entries closed (time, kill switch or halt); no pass bought")
            if len(held) >= cfg.max_positions:
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
        try:
            result = self.llm.scan(system=system_prompt(cfg, brief=brief is not None), user=user,
                                   tool=report_tool(brief is not None))
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
        for idea in ideas:
            if not dry_run:
                quote = self._quotes(str(idea.get("symbol", "")).upper().strip()) if _SYMBOL.match(
                    str(idea.get("symbol", "")).upper().strip()) else None
                if quote is not None:
                    self.account.ledger.append("anchor_check", scan=label, symbol=idea.get("symbol"),
                                               **self.anchor_check(idea, (quote[0] + quote[1]) / 2))
            verdict = self.validate(idea, now, taken, slots=cfg.max_positions - len(held) - len(outcome.opened),
                                    live=not dry_run)
            if not verdict.ok:
                outcome.rejected.append((str(idea.get("symbol")), verdict.reason))
                if not dry_run:
                    self.account.ledger.append("idea_rejected", scan=label, symbol=idea.get("symbol"),
                                               reason=verdict.reason, idea=idea)
                continue
            if dry_run:
                outcome.opened.append(str(idea["symbol"]).upper())
                taken.append(str(idea["symbol"]).upper())
                continue
            opened = self._enter(idea, now, label)
            if opened is None:
                outcome.rejected.append((str(idea.get("symbol")), "entry not filled or refused (see ledger)"))
                continue
            outcome.opened.append(opened)
            taken.append(str(idea["symbol"]).upper())
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
                          p.opened_day.isoformat(), str(p.stop), str(p.target), p.thesis[:300]) for p in held]
        local = now.astimezone(NY)
        return build_user(
            now, held=views, traded_today=self.traded_today(day), max_positions=cfg.max_positions,
            max_ideas=cfg.max_ideas_per_scan,
            entries_open=local < at(day, cfg.no_entries_after),
            intraday_open=local < at(day, cfg.no_intraday_entries_after),
            brief=brief.text if brief is not None else None,
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
        if slots <= 0:
            return Verdict(False, "no free slot")
        if symbol in {t.upper() for t in taken}:
            return Verdict(False, "already held or traded today")
        if local >= at(day, cfg.no_entries_after):
            return Verdict(False, f"no new entries after {cfg.no_entries_after} ET")
        if horizon == "intraday" and local >= at(day, cfg.no_intraday_entries_after):
            return Verdict(False, f"no intraday entries after {cfg.no_intraday_entries_after} ET")
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
        nav = gate.state.nav
        budget = (nav * cfg.risk_fraction).quantize(CENTS, rounding=ROUND_DOWN)
        pid = uuid.uuid4().hex[:12]
        stop_fraction = Decimal(str(idea["stop_pct"])) / 100
        target_fraction = Decimal(str(idea["target_pct"])) / 100
        plan = dict(pid=pid, symbol=symbol, instrument=instrument, horizon=idea["horizon"],
                    stop_pct=str(idea["stop_pct"]), target_pct=str(idea["target_pct"]),
                    thesis=str(idea.get("thesis", ""))[:1000], source=str(idea.get("source", ""))[:1000],
                    catalyst=str(idea.get("catalyst", ""))[:500], invalidation=str(idea.get("invalidation", ""))[:500],
                    confidence=int(idea["confidence"]), scan=label, risk_budget=budget, nav=nav)
        if self._arm is not None:  # the A/B arm rides on the position (ruling 2026-10-15)
            plan.update(brief_arm=self._arm)
        if "brief_use" in idea:  # the brief's use rides on the position (ruling 2026-10-15)
            plan.update(brief_use=str(idea.get("brief_use"))[:20], brief_items=str(idea.get("brief_items") or "")[:500])
        # Leverage is allowed and MEASURED (ruling 2026-10-14, second): the
        # factor and its tag ride on the plan, so the gross figures, the
        # scoreboard and the journal can tell a 3x fund from a stock pick.
        factor = self.leverage_factor(symbol, instrument)
        plan.update(leverage=str(factor), tag=leverage.tag(factor))
        # The levels come from the LIVE quote at order time (ruling 2026-10-10),
        # never from a price the model cited.
        quote = self._quotes(symbol)
        if quote is None:
            plan.update(stop="0", target="0")
            self._reject(plan, "quote vanished before entry")
            return None
        bid, ask = quote
        anchor = (bid + ask) / 2
        stop, target = levels(instrument, anchor, stop_fraction, target_fraction)
        plan.update(anchor_mid=str(anchor), stop=str(stop), target=str(target))
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
        quantity = budget / per_share
        cap = gate.sleeve_nav(Sleeve.AGGRESSIVE) * gate.limits.aggressive_sleeve.max_single_position * Decimal("0.995")
        quantity = min(quantity, cap / limit, gate.state.settled_buying_power / limit).quantize(STEP, rounding=ROUND_DOWN)
        if quantity * limit < Decimal("5"):
            self._reject(plan, f"size {quantity} x {limit} below the dust floor")
            return None
        order = EquityBuyOrder(symbol=symbol, quantity=quantity, execution=LimitExecution(limit_price=limit),
                               sleeve="aggressive", confidence=plan["confidence"])
        return self._open(order, plan, limit, (bid + ask) / 2)

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
        if contracts < 1:
            self._reject(plan, f"one contract costs {limit * pick.multiplier}, above the {budget} premium budget")
            return None
        plan.update(occ=pick.occ_symbol, expiry=pick.expiration.isoformat(), right=pick.right,
                    strike=str(pick.strike), delta=str(pick.delta), multiplier=pick.multiplier)
        order = OptionBuyToOpenOrder(symbol=pick.occ_symbol, underlying=pick.underlying, right=pick.right,
                                     expiration=pick.expiration, strike=pick.strike, contracts=contracts,
                                     multiplier=pick.multiplier, execution=LimitExecution(limit_price=limit),
                                     confidence=plan["confidence"])
        return self._open(order, plan, limit, pick.mid)

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
        filled = self._work_approved(decision, position, "buy", limit, modelled, reason="entry",
                                     wait=self.config.fill_wait_seconds)
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
