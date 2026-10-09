"""The B execution test (human ruling 2026-10-09 evening).

The SPY noise-area momentum rules (Zarattini, Aziz & Barbon 2024; measured in
SESSION_NOTES 2026-10-09: the paper reproduces in-sample, the edge did not
survive publication) run LIVE in the aggressive paper sleeve for a fixed number
of sessions, then stop themselves. Purpose, by ruling: prove the intraday
plumbing - entries, trailing stops, the 15:50 close-out, the inverse-ETF leg,
the PDT gate - and measure live fills against the backtest's modelled prices.
NOT a strategy: fixed risk per trade, no LLM anywhere in the path, its own
attribution bucket (``strategy="execution_test"``) outside every alpha line.

The rules
---------
  bands       sigma(k) = mean over the previous ``lookback_sessions`` of
              |close(minute k) / open - 1|; UB = max(open, prior close) x
              (1 + sigma), LB = min(open, prior close) x (1 - sigma). History
              comes from SIP minute bars (15-minute-delayed on this
              subscription, so always complete for past sessions); today's
              open from the SIP 9:30 bar (or IEX before it is served).
  decisions   at each mark 10:00, 10:30 ... 15:30 New York, once, within a
              grace window (a later evaluation is a recorded TIMING FAULT):
                flat:     price > UB -> buy SPY; price < LB -> buy SH (the
                          inverse leg, instead of shorting: no margin, 1x)
                long SPY: price < max(UB, VWAP) -> sell (stop); then if
                          price < LB -> buy SH (reversal)
                long SH:  price > min(LB, VWAP) -> sell (stop); then if
                          price > UB -> buy SPY (reversal)
              price = the IEX quote midpoint at the mark; VWAP = IEX session
              VWAP (a partial-volume venue - the next-morning replay uses SIP
              and reports where the two disagree).
  close-out   15:50 New York: anything held is sold. Never held overnight.
  size        fixed risk: shares = risk_fraction x the aggressive sleeve NAV
              / the distance to the stop (floored at ``min_stop_fraction`` of
              price), capped at the sleeve's single-position cap. Confidence
              plays no part; nothing reads P&L (Constraint #6).
  orders      marketable limits through the RiskGate, one working order at a
              time; an entry not filled in ``working_timeout`` is cancelled (a
              fault), an exit is re-priced until it fills.
  stop        after ``max_sessions`` sessions the test opens nothing more.

Every morning the previous session is replayed from SIP minute bars on the
backtest's own fill model (the close of the bar ending at each mark) and the
live record is reported against it: decisions that agree, fill slippage at
entry, stop and close-out in basis points, faults, P&L live vs modelled.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Callable, Optional, Protocol
from zoneinfo import ZoneInfo

from audit.log import AuditLog
from audit.records import ExitReason
from execution.base import BrokerAdapter, BrokerError
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import Sleeve

logger = logging.getLogger("orchestrator.exec_test")

NY = ZoneInfo("America/New_York")
ZERO = Decimal("0")
CENTS = Decimal("0.01")
SESSION_OPEN = time(9, 30)
WORKING_TIMEOUT = timedelta(seconds=120)
REPORT_AFTER = time(9, 35)


class MinuteBarSource(Protocol):
    def bars(self, symbol: str, start: datetime, end: datetime, feed: str) -> list[dict]: ...


# ----------------------------------------------------------------- pure rules


def _minute(bar: dict) -> tuple[date, int]:
    t = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).astimezone(NY)
    return t.date(), (t.hour - 9) * 60 + t.minute - 30


def by_session(bars: list[dict]) -> dict[date, dict[int, dict]]:
    """NY date -> minute index (minutes after 9:30) -> bar, regular session only."""
    out: dict[date, dict[int, dict]] = {}
    for bar in bars:
        day, k = _minute(bar)
        if 0 <= k < 390:
            out.setdefault(day, {})[k] = bar
    return out


def close_at(session: dict[int, dict], k: int) -> Optional[float]:
    """The close of the last bar at or before minute k (a quiet minute carries
    the previous print forward)."""
    keys = [m for m in session if m <= k]
    return float(session[max(keys)]["c"]) if keys else None


def sigma_table(history: list[dict[int, dict]], marks: list[int]) -> dict[int, float]:
    """sigma(k): mean |close(bar ending at k) / open - 1| over past sessions."""
    out: dict[int, float] = {}
    for k in marks:
        moves = []
        for session in history:
            if 0 not in session:
                continue
            o = float(session[0]["o"])
            c = close_at(session, k - 1)
            if c is not None and o > 0:
                moves.append(abs(c / o - 1))
        if moves:
            out[k] = statistics.mean(moves)
    return out


def bands(day_open: float, prior_close: float, sigma: float) -> tuple[float, float]:
    return max(day_open, prior_close) * (1 + sigma), min(day_open, prior_close) * (1 - sigma)


def session_vwap(session: dict[int, dict], k: int) -> Optional[float]:
    """VWAP over the bars that ENDED by minute k (bars starting before k)."""
    pv = vol = 0.0
    for m, bar in session.items():
        if m < k:
            v = float(bar.get("v") or 0)
            pv += float(bar.get("vw") or bar["c"]) * v
            vol += v
    return pv / vol if vol > 0 else None


def decide(held: Optional[str], price: float, ub: float, lb: float, vwap: float,
           symbol: str = "SPY", inverse: str = "SH") -> list[tuple[str, str]]:
    """The rule, as a list of actions: ("exit", "stop"|"reversal") and
    ("enter", symbol|inverse)."""
    if held == symbol:
        if price < max(ub, vwap):
            return [("exit", "reversal" if price < lb else "stop")] + ([("enter", inverse)] if price < lb else [])
        return []
    if held == inverse:
        if price > min(lb, vwap):
            return [("exit", "reversal" if price > ub else "stop")] + ([("enter", symbol)] if price > ub else [])
        return []
    if price > ub:
        return [("enter", symbol)]
    if price < lb:
        return [("enter", inverse)]
    return []


# -------------------------------------------------------------------- engine


@dataclass(slots=True)
class _Working:
    approved: ApprovedOrder
    decision_id: str
    side: str  # buy | sell
    symbol: str
    kind: str  # entry | stop | reversal | close_out
    mark: int
    intended: Decimal  # the live quote the decision acted on
    submitted_at: datetime


class ExecutionTest:
    """Owns the execution test: marks, orders, the close-out, the morning report."""

    def __init__(
        self,
        *,
        config,
        gate: RiskGate,
        adapter: BrokerAdapter,
        audit: AuditLog,
        bars: MinuteBarSource,
        prices: Callable[[str], Optional[Decimal]],
        bids: Optional[Callable[[str], Optional[Decimal]]],
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        state_path: Path,
        daily_path: Path,
        note: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._c = config
        self._gate = gate
        self._adapter = adapter
        self._audit = audit
        self._bars = bars
        self._prices = prices
        self._bids = bids
        self._clock = clock
        self._id_factory = id_factory
        self._state_path = state_path
        self._daily_path = daily_path
        self._note = note or (lambda message: None)
        self._marks = self._mark_list()
        self._close_out = self._minute_of(config.close_out)
        self._working: dict[str, _Working] = {}
        self._day: Optional[date] = None
        self._inputs: Optional[dict] = None
        self._state = self._load_state()

    # -- schedule ---------------------------------------------------------------

    @staticmethod
    def _minute_of(hhmm: str) -> int:
        h, m = (int(x) for x in hhmm.split(":"))
        return (h - 9) * 60 + m - 30

    def _mark_list(self) -> list[int]:
        first, last = self._minute_of(self._c.first_decision), self._minute_of(self._c.last_decision)
        return list(range(first, last + 1, self._c.step_minutes))

    @staticmethod
    def _at(day: date, k: int) -> datetime:
        return datetime.combine(day, SESSION_OPEN, NY) + timedelta(minutes=k)

    # -- state ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("sessions", [])
        state.setdefault("reported", [])
        state.setdefault("days", {})
        state.setdefault("position", None)
        state.setdefault("complete_noted", False)
        return state

    def _save(self) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=1, default=str), encoding="utf-8")
        tmp.replace(self._state_path)

    def _today(self, day: date) -> dict:
        return self._state["days"].setdefault(
            day.isoformat(), {"decisions": [], "fills": [], "faults": [], "done_marks": [], "sleeve_nav": None}
        )

    def _fault(self, day: date, what: str) -> None:
        self._today(day)["faults"].append({"at": self._clock().isoformat(), "fault": what})
        self._note(f"EXEC-TEST FAULT {day}: {what}")
        self._save()

    # -- introspection ----------------------------------------------------------

    @property
    def sessions_run(self) -> int:
        return len(self._state["sessions"])

    @property
    def held(self) -> Optional[dict]:
        return self._state["position"]

    def active_on(self, day: date) -> bool:
        if not self._c.enabled or self._c.first_session is None or day < self._c.first_session:
            return False
        sessions = self._state["sessions"]
        return day.isoformat() in sessions or len(sessions) < self._c.max_sessions

    # -- one pass ---------------------------------------------------------------

    def tick(self, now: datetime) -> int:
        """Settle, report, close out, decide. Returns orders placed."""
        ny = now.astimezone(NY)
        day = ny.date()
        self._reconcile(now)
        if ny.time() >= REPORT_AFTER:
            self._morning_reports(day)
        if not self.active_on(day):
            if self.held is not None and not self._working:
                return self._exit(day, "close_out", self._close_out, now, reason="the test is over")
            if (
                self._c.enabled and not self._state["complete_noted"]
                and len(self._state["sessions"]) >= self._c.max_sessions
                and set(self._state["sessions"]) <= set(self._state["reported"])
            ):
                self._final_report()
            return 0
        if ny.time() < SESSION_OPEN:
            return 0
        if day.isoformat() not in self._state["sessions"]:
            self._state["sessions"].append(day.isoformat())
            self._note(f"EXEC-TEST session {len(self._state['sessions'])} of {self._c.max_sessions}: {day}")
            self._save()
        minute = int((ny - datetime.combine(day, SESSION_OPEN, NY)).total_seconds() // 60)
        today = self._today(day)
        if self._working:
            self._expire_working(day, now)
            return 0
        # 15:50: everything out, nothing in.
        if minute >= self._close_out:
            if self.held is not None:
                return self._exit(day, "close_out", self._close_out, now)
            return 0
        placed = 0
        for k in self._marks:
            if k in today["done_marks"]:
                continue
            if minute < k:
                break
            today["done_marks"].append(k)
            if minute >= k + self._c.decision_grace_minutes:
                self._fault(day, f"decision at {self._at(day, k).strftime('%H:%M')} missed (evaluated {minute - k} min late or not at all); no action")
                continue
            placed += self._decide(day, k, now)
            break  # one mark per tick; a reversal's entry follows its exit fill
        self._save()
        return placed

    # -- the decision -----------------------------------------------------------

    def _prepare(self, day: date) -> Optional[dict]:
        if self._day == day and self._inputs is not None and self._inputs.get("open") is not None:
            return self._inputs
        sym = self._c.symbol
        start = datetime.combine(day - timedelta(days=35), SESSION_OPEN, NY)
        hist = by_session(self._bars.bars(sym, start, datetime.combine(day, time(0, 0), NY), "sip"))
        past = [hist[d] for d in sorted(hist) if d < day][-self._c.lookback_sessions:]
        if len(past) < self._c.lookback_sessions:
            return None
        prior = past[-1]
        prior_close = close_at(prior, 389)
        sigma = sigma_table(past, self._marks)
        today_sip = by_session(self._bars.bars(sym, datetime.combine(day, SESSION_OPEN, NY), datetime.combine(day, SESSION_OPEN, NY) + timedelta(minutes=1), "sip")).get(day, {})
        source = "sip"
        if 0 in today_sip:
            day_open = float(today_sip[0]["o"])
        else:
            today_iex = by_session(self._bars.bars(sym, datetime.combine(day, SESSION_OPEN, NY), datetime.combine(day, SESSION_OPEN, NY) + timedelta(minutes=1), "iex")).get(day, {})
            day_open = float(today_iex[0]["o"]) if 0 in today_iex else None
            source = "iex"
        self._day = day
        self._inputs = {"open": day_open, "open_source": source, "prior_close": prior_close, "sigma": sigma}
        return self._inputs

    def _live_price(self, symbol: str) -> Optional[Decimal]:
        try:
            ask = self._prices(symbol)
            bid = self._bids(symbol) if self._bids is not None else None
        except Exception:  # noqa: BLE001 - a price bug must not kill the loop
            logger.exception("exec-test quote failed for %s", symbol)
            return None
        if ask is None or ask <= 0:
            return None
        if bid is not None and bid > 0:
            return (ask + bid) / 2
        return ask

    def _decide(self, day: date, k: int, now: datetime) -> int:
        inputs = self._prepare(day)
        mark = self._at(day, k).strftime("%H:%M")
        if inputs is None or inputs.get("open") is None or inputs.get("prior_close") is None or k not in inputs["sigma"]:
            self._fault(day, f"decision {mark}: inputs unavailable (history, open or sigma); no action")
            return 0
        price = self._live_price(self._c.symbol)
        today_iex = by_session(self._bars.bars(self._c.symbol, datetime.combine(day, SESSION_OPEN, NY), now, "iex")).get(day, {})
        vwap = session_vwap(today_iex, k)
        if price is None or vwap is None:
            self._fault(day, f"decision {mark}: no live quote or VWAP; no action")
            return 0
        ub, lb = bands(inputs["open"], inputs["prior_close"], inputs["sigma"][k])
        held = self.held["symbol"] if self.held else None
        actions = decide(held, float(price), ub, lb, vwap, self._c.symbol, self._c.inverse_symbol)
        today = self._today(day)
        today["sleeve_nav"] = today["sleeve_nav"] or str(self._gate.sleeve_nav(Sleeve.AGGRESSIVE))
        today["decisions"].append({
            "mark": k, "time": mark, "evaluated_at": now.isoformat(), "late_seconds": int((now - self._at(day, k)).total_seconds()),
            "price": float(price), "ub": ub, "lb": lb, "vwap_iex": vwap, "held": held, "actions": actions,
            "open": inputs["open"], "open_source": inputs["open_source"], "prior_close": inputs["prior_close"], "sigma": inputs["sigma"][k],
        })
        self._save()
        placed = 0
        for action, what in actions:
            if action == "exit":
                placed += self._exit(day, what, k, now)
                pending = [a for a in actions if a[0] == "enter"]
                if pending:
                    today["pending"] = {"symbol": pending[0][1], "mark": k, "ub": ub, "lb": lb, "vwap": vwap,
                                        "deadline": (self._at(day, k) + timedelta(minutes=self._c.decision_grace_minutes)).isoformat()}
                    self._save()
                break
            placed += self._enter(day, what, k, now, ub, lb, vwap)
        return placed

    def _enter(self, day: date, symbol: str, k: int, now: datetime, ub: float, lb: float, vwap: float) -> int:
        caps = self._gate.limits.aggressive_sleeve
        nav = self._gate.sleeve_nav(Sleeve.AGGRESSIVE)
        if caps is None or nav <= 0:
            self._fault(day, "entry skipped: the aggressive sleeve has no cap table or no NAV")
            return 0
        spy = self._live_price(self._c.symbol)
        ask = self._prices(symbol)
        if spy is None or ask is None or ask <= 0:
            self._fault(day, f"entry {symbol} skipped: no quote")
            return 0
        price = float(spy)
        floor = float(self._c.min_stop_fraction)
        if symbol == self._c.symbol:
            stop_level = max(ub, vwap)
            move = max((price - stop_level) / price, floor)
        else:
            stop_level = min(lb, vwap)
            move = max((stop_level - price) / price, floor)
        risk = nav * self._c.risk_fraction
        per_share = ask * Decimal(str(move))
        wanted = risk / per_share if per_share > 0 else ZERO
        limit = (ask * (1 + self._c.limit_buffer)).quantize(CENTS, rounding=ROUND_UP)
        # the gate prices the position at the LIMIT, so the cap is measured there
        cap = nav * caps.max_single_position / limit
        step = self._adapter.equity_quantity_step
        quantity = min(wanted, cap).quantize(step, rounding=ROUND_DOWN)
        if quantity <= 0:
            self._fault(day, f"entry {symbol} sized to zero")
            return 0
        order = EquityBuyOrder(symbol=symbol, quantity=quantity, execution=LimitExecution(limit_price=limit), sleeve="aggressive")
        decision = self._gate.submit(order)
        detail = (
            f"execution test {day} mark {self._at(day, k).strftime('%H:%M')}: buy {quantity} {symbol} at limit {limit}; "
            f"SPY {price:.4f} vs UB {ub:.4f} / LB {lb:.4f}, VWAP {vwap:.4f}; stop move {move:.4%} sizes "
            f"{self._c.risk_fraction:.0%} of sleeve NAV {nav:.2f} at risk (capped at the single-position cap)"
        )
        record = self._audit.record_execution_test(side="buy", detail=detail, gate_decision=decision,
                                                   capital=(quantity * limit).quantize(CENTS), decision_id=self._id_factory())
        if not decision.is_approved:
            self._fault(day, f"gate refused entry {symbol}: {decision.code} - {getattr(decision, 'message', '')}")
            return 0
        try:
            receipt = self._adapter.submit_order(decision, client_reference=record.decision_id)
        except BrokerError as error:
            self._gate.cancel(decision)
            self._fault(day, f"broker refused entry {symbol}: {error}")
            return 0
        self._working[receipt.broker_order_id] = _Working(decision, record.decision_id, "buy", symbol, "entry", k,
                                                          self._live_price(symbol) or ask, now)
        return 1

    def _exit(self, day: date, kind: str, k: int, now: datetime, reason: str = "") -> int:
        held = self.held
        if held is None:
            return 0
        symbol, quantity, decision_id = held["symbol"], Decimal(held["quantity"]), held["decision_id"]
        position = self._gate.state.position((Sleeve.AGGRESSIVE.value, symbol))
        if position is None or position.quantity <= 0:
            self._fault(day, f"exit {symbol} ({kind}): the gate holds no such position; the test's record is dropped (the broker is authoritative)")
            self._state["position"] = None
            self._save()
            return 0
        quantity = min(quantity, position.available_to_close)
        bid = self._bids(symbol) if self._bids is not None else None
        base = bid if bid is not None and bid > 0 else self._prices(symbol)
        if base is None or base <= 0:
            self._fault(day, f"exit {symbol} ({kind}) delayed: no quote")
            return 0
        limit = max((base * (1 - self._c.limit_buffer)).quantize(CENTS, rounding=ROUND_DOWN), CENTS)
        order = EquitySellToCloseOrder(symbol=symbol, quantity=quantity, execution=LimitExecution(limit_price=limit), sleeve="aggressive")
        decision = self._gate.submit(order)
        exit_reason = {"stop": ExitReason.EXEC_TEST_STOP, "reversal": ExitReason.EXEC_TEST_REVERSAL}.get(kind, ExitReason.EXEC_TEST_CLOSE_OUT)
        detail = f"execution test {day}: {kind} - sell {quantity} {symbol} at limit {limit}" + (f" ({reason})" if reason else "")
        submitted, broker_order_id, broker_error = False, None, None
        if decision.is_approved:
            try:
                receipt = self._adapter.submit_order(decision)
                submitted, broker_order_id = True, receipt.broker_order_id
                self._working[receipt.broker_order_id] = _Working(decision, decision_id, "sell", symbol, kind, k,
                                                                  self._live_price(symbol) or base, now)
            except BrokerError as error:
                self._gate.cancel(decision)
                broker_error = str(error)
                self._fault(day, f"broker refused exit {symbol} ({kind}): {error}")
        else:
            self._fault(day, f"gate refused exit {symbol} ({kind}): {decision.code}")
        self._audit.record_exit(decision_id, exit_reason, detail, decision, submitted=submitted,
                                broker_order_id=broker_order_id, broker_error=broker_error)
        return 1 if submitted else 0

    # -- settlement -------------------------------------------------------------

    def _expire_working(self, day: date, now: datetime) -> None:
        for order_id, working in list(self._working.items()):
            if now - working.submitted_at < WORKING_TIMEOUT:
                continue
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.warning("exec-test could not cancel %s: %s", order_id, error)
            self._fault(day, f"{working.kind} order {order_id} ({working.side} {working.symbol}) unfilled after {WORKING_TIMEOUT.seconds}s; cancelled"
                        + ("; the exit is re-priced next tick" if working.side == "sell" else "; entry abandoned"))
            self._reconcile(now, force=order_id)

    def _reconcile(self, now: datetime, force: Optional[str] = None) -> None:
        for order_id, working in list(self._working.items()):
            try:
                status = self._adapter.get_order(order_id)
            except BrokerError as error:
                logger.warning("exec-test could not poll %s: %s", order_id, error)
                continue
            if not status.is_terminal and order_id != force:
                continue
            del self._working[order_id]
            day = working.submitted_at.astimezone(NY).date()
            filled, price = status.filled_quantity, status.filled_avg_price
            if filled <= 0 or price is None:
                self._gate.cancel(working.approved)
                self._audit.record_unfilled_order(working.decision_id, order_id, status.status,
                                                  f"execution-test {working.kind} order {order_id} ended {status.status} unfilled")
                continue
            self._gate.record_fill(working.approved, price, filled_units=filled)
            seconds = Decimal(str(round((now - working.submitted_at).total_seconds(), 1)))
            self._audit.record_fill(working.decision_id, order_id, filled, price, filled_value=filled * price,
                                    side=working.side, intended_price=working.intended, seconds_to_fill=seconds)
            self._today(day)["fills"].append({
                "order_id": order_id, "side": working.side, "symbol": working.symbol, "kind": working.kind,
                "mark": working.mark, "quantity": str(filled), "price": str(price), "intended": str(working.intended),
                "seconds_to_fill": float(seconds),
            })
            if working.side == "buy":
                self._state["position"] = {"symbol": working.symbol, "quantity": str(filled), "decision_id": working.decision_id,
                                           "entry": str(price), "opened": now.isoformat()}
            else:
                held = self._state["position"]
                if held is not None:
                    remaining = Decimal(held["quantity"]) - filled
                    if remaining > 0:
                        held["quantity"] = str(remaining)
                    else:
                        cost = Decimal(held["entry"]) * Decimal(held["quantity"])
                        self._audit.record_outcome(working.decision_id, filled * price - cost, closed_at=now,
                                                   note=f"execution test {working.kind}")
                        self._state["position"] = None
                        pending = self._today(day).pop("pending", None)
                        if pending and now < datetime.fromisoformat(pending["deadline"]):
                            self._save()
                            self._enter(day, pending["symbol"], pending["mark"], now, pending["ub"], pending["lb"], pending["vwap"])
                        elif pending:
                            self._fault(day, f"reversal entry {pending['symbol']} dropped: the exit filled after its window")
            self._save()

    def cancel_working(self) -> list[str]:
        released = []
        for order_id in list(self._working):
            try:
                self._adapter.cancel_order(order_id)
            except BrokerError as error:
                logger.error("exec-test could not cancel %s: %s", order_id, error)
            self._reconcile(self._clock(), force=order_id)
            released.append(order_id)
        return released

    def replay(self) -> None:
        """A restart keeps the open position only when the gate holds it."""
        held = self._state["position"]
        if held is None:
            return
        position = self._gate.state.position((Sleeve.AGGRESSIVE.value, held["symbol"]))
        if position is None or position.quantity <= 0:
            logger.warning("exec-test state says %s is held but the gate does not; dropping", held)
            self._state["position"] = None
            self._save()

    # -- the morning report -----------------------------------------------------

    def _morning_reports(self, today: date) -> None:
        for day_s in list(self._state["sessions"]):
            if day_s in self._state["reported"] or day_s >= today.isoformat():
                continue
            try:
                line = self._report_day(date.fromisoformat(day_s))
            except Exception as error:  # noqa: BLE001 - a report must never stop trading
                logger.exception("exec-test report for %s failed", day_s)
                line = {"day": day_s, "error": str(error)}
            with open(self._daily_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=str) + "\n")
            self._state["reported"].append(day_s)
            self._save()
            self._note("EXEC-TEST daily " + json.dumps(line, default=str))

    def modelled_day(self, day: date) -> dict:
        """The backtest's model on SIP bars: same rules, fills at the close of
        the bar ending at each mark (and at 15:50)."""
        sym, inv = self._c.symbol, self._c.inverse_symbol
        start = datetime.combine(day - timedelta(days=35), SESSION_OPEN, NY)
        # the SIP tape is served 15 minutes delayed: never ask past now - 16 min
        end = min(datetime.combine(day + timedelta(days=1), time(0, 0), NY), self._clock() - timedelta(minutes=16))
        sessions = by_session(self._bars.bars(sym, start, end, "sip"))
        inverse = by_session(self._bars.bars(inv, datetime.combine(day, SESSION_OPEN, NY), end, "sip")).get(day, {})
        past = [sessions[d] for d in sorted(sessions) if d < day][-self._c.lookback_sessions:]
        today = sessions.get(day, {})
        sigma = sigma_table(past, self._marks)
        o = float(today[0]["o"]) if 0 in today else None
        pc = close_at(past[-1], 389) if past else None
        actions, prices = {}, {}
        held = None
        for k in self._marks:
            if o is None or pc is None or k not in sigma:
                continue
            ub, lb = bands(o, pc, sigma[k])
            price = close_at(today, k - 1)
            vwap = session_vwap(today, k)
            if price is None or vwap is None:
                continue
            acts = decide(held, price, ub, lb, vwap, sym, inv)
            actions[k] = acts
            prices[k] = {sym: price, inv: close_at(inverse, k - 1)}
            for a, what in acts:
                if a == "exit":
                    held = None
                else:
                    held = what
        prices[self._close_out] = {sym: close_at(today, self._close_out - 1), inv: close_at(inverse, self._close_out - 1)}
        return {"actions": actions, "prices": prices, "held_at_close_out": held}

    def _report_day(self, day: date) -> dict:
        record = self._today(day)
        model = self.modelled_day(day)
        live_actions = {d["mark"]: [tuple(a) for a in d["actions"]] for d in record["decisions"]}
        marks = sorted(set(live_actions) | set(model["actions"]))
        agree = sum(1 for k in marks if live_actions.get(k, []) == [tuple(a) for a in model["actions"].get(k, [])])
        slip: dict[str, list[float]] = {"entry": [], "stop": [], "reversal": [], "close_out": []}
        for f in record["fills"]:
            modelled = (model["prices"].get(f["mark"]) or {}).get(f["symbol"])
            if not modelled:
                continue
            price = float(f["price"])
            adverse = (price - modelled) / modelled if f["side"] == "buy" else (modelled - price) / modelled
            slip.setdefault(f["kind"], []).append(round(adverse * 1e4, 2))
        buys = sum(float(f["price"]) * float(f["quantity"]) for f in record["fills"] if f["side"] == "buy")
        sells = sum(float(f["price"]) * float(f["quantity"]) for f in record["fills"] if f["side"] == "sell")
        # modelled P&L at the live trades' sizes: each live round trip, re-priced at the model's prices
        model_pnl = 0.0
        open_buy = None
        for f in record["fills"]:
            p = (model["prices"].get(f["mark"]) or {}).get(f["symbol"])
            if p is None:
                continue
            if f["side"] == "buy":
                open_buy = (p, float(f["quantity"]))
            elif open_buy is not None:
                model_pnl += (p - open_buy[0]) * open_buy[1]
                open_buy = None
        return {
            "day": day.isoformat(),
            "session": self._state["sessions"].index(day.isoformat()) + 1,
            "decisions_live": len(record["decisions"]),
            "decisions_agree_with_model": f"{agree}/{len(marks)}",
            "fills": len(record["fills"]),
            "slippage_bp_vs_model": {k: {"n": len(v), "mean": round(statistics.mean(v), 2) if v else None} for k, v in slip.items()},
            "seconds_to_fill_mean": round(statistics.mean([f["seconds_to_fill"] for f in record["fills"]]), 1) if record["fills"] else None,
            "late_decisions": sum(1 for d in record["decisions"] if d["late_seconds"] > 60),
            "faults": record["faults"],
            "pnl_live": round(sells - buys, 2),
            "pnl_modelled_at_live_sizes": round(model_pnl, 2),
            "day_trades_in_window": self._gate.state.day_trades_in_window(day, self._gate.limits.pdt.window_business_days),
        }

    def _final_report(self) -> None:
        lines = []
        try:
            lines = [json.loads(l) for l in open(self._daily_path, encoding="utf-8")]
        except OSError:
            pass
        live = sum(l.get("pnl_live", 0) for l in lines)
        modelled = sum(l.get("pnl_modelled_at_live_sizes", 0) for l in lines)
        kinds: dict[str, list[float]] = {}
        for l in lines:
            for k, v in (l.get("slippage_bp_vs_model") or {}).items():
                if v.get("mean") is not None:
                    kinds.setdefault(k, []).extend([v["mean"]] * v["n"])
        faults = sum(len(l.get("faults") or []) for l in lines)
        summary = {k: round(statistics.mean(v), 2) for k, v in kinds.items() if v}
        self._note(
            f"EXECUTION TEST COMPLETE after {len(self._state['sessions'])} sessions: P&L live {live:.2f} vs modelled "
            f"{modelled:.2f} (gap {live - modelled:+.2f}); mean slippage vs model, bp: {summary}; faults {faults}. "
            f"The test opens nothing more (ruling 2026-10-09 evening)."
        )
        self._state["complete_noted"] = True
        self._save()
