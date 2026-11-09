"""The AI trader's LIVE MIRROR on the Robinhood Agentic account (GO LIVE,
human ruling 2026-11-04; CLAUDE.md § Live Mirror).

The paper AI trader decides; this engine copies. It reads the paper
account's ledger (a file - nothing of the paper engine is imported) and, for
every paper entry that FILLS, places the same idea in the live account,
scaled to live equity: the same 2% of equity at risk, the same stop and
target distances, the same horizon. A paper close closes the live copy.
The paper AI trader is unchanged and remains the measured version.

The rules (the ruling's items 2-3; every Inviolable Constraint applies):
  * every order passes this account's own RiskGate (cash-secured, settled
    funds only: the gate's cash is the broker's settled cash and its
    aggressive path refuses today's unsettled sale proceeds);
  * stocks and plain ETFs only: a leveraged or inverse ETF is skipped and
    logged; an option is skipped unless one contract fits the premium cap,
    and even then skipped until a live option round trip proves that path;
  * whole shares as day limits when at least one share fits, else a
    fractional market buy (the venue takes fractions only as market orders
    in regular hours; the gate reserves at the ask + 5% and the order
    carries its justification);
  * a 12% kill switch on STARTING equity and a 4% daily loss limit halt new
    entries (exits always run); the gate's own 12%-from-peak switch applies
    as well; a HALT marker halts entries by hand;
  * the live copy also watches its own stop, target and horizon on Robinhood
    quotes, so a paper outage never leaves it unwatched.

Sequence (item 5): wait for the funding to settle; one tiny live round trip;
mirroring from the NEXT session. Every order, fill and skip is carded
(#urgent for the first week of mirroring, #reports after). Without a live
broker (the process is not live under Constraint #4) it runs DRY: it reads,
sizes and logs what it would do, and sends nothing.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from execution import BrokerError, OrderOutcomeUnknown, cards
from risk_gate.gate import RiskGate
from risk_gate.schema import (
    EquityBuyOrder, EquitySellToCloseOrder, LimitExecution, MarketBuyExecution, MarketSellExecution,
)
from risk_gate.state import AccountState, AccountType, Sleeve

NY = ZoneInfo("America/New_York")
ZERO = Decimal("0")
CENTS = Decimal("0.01")
SHARE_STEP = Decimal("0.000001")
SLEEVE = Sleeve.AGGRESSIVE
MARKET_BUY_WHY = ("fractional quantity: Robinhood accepts fractional shares only as market orders in regular hours "
                  "(GO LIVE ruling 2026-11-04, whole shares first)")
MARKET_SELL_WHY = ("closing a fractional holding: Robinhood sells fractional shares only as market orders in "
                   "regular hours (GO LIVE ruling 2026-11-04)")

logger = logging.getLogger("lab.live_mirror")


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else ZERO
    except Exception:  # noqa: BLE001
        return ZERO


@dataclass
class Snapshot:
    equity: Decimal
    settled_cash: Decimal
    unsettled: Decimal
    pending_deposits: Decimal
    held: dict[str, Decimal]
    #: The broker's raw cash (it includes early access to deposits in transit):
    #: what the cash-flow reconciliation compares.
    cash: Decimal = ZERO

    @property
    def tradeable_equity(self) -> Decimal:
        """Equity less deposits still in transit: what sizing scales with."""
        return max(ZERO, self.equity - self.pending_deposits)


class LiveMirror:
    def __init__(self, *, config, ai_config, reader, broker, paper_ledger: Path, ledger, state, state_path: Path,
                 limits, notify: Callable[[cards.Card], None], clock: Callable[[], datetime],
                 halt_path: Optional[Path] = None) -> None:
        self.config = config
        self.ai = ai_config
        self.reader = reader                 # the read-only client: account, quotes, orders
        self.broker = broker                 # RobinhoodAgenticAdapter, or None: DRY
        self.paper_ledger = paper_ledger
        self.ledger = ledger
        self.state = state
        self.state_path = state_path
        self.limits = limits
        self.notify = notify
        self.clock = clock
        self.halt_path = halt_path
        self.gate: Optional[RiskGate] = None
        self._working: Optional[dict] = None

    # -- state ------------------------------------------------------------------------

    @property
    def e(self) -> dict:
        return self.state.engine

    @property
    def _queue(self) -> list[dict]:
        """Paper entries read but not yet copied or skipped. Saved with the
        state (2026-11-09): the paper offset is saved as soon as an entry is
        read, so a queue kept only in memory lost it on a restart, uncarded."""
        return self.e.setdefault("queue", [])

    def _save(self) -> None:
        if self.gate is not None:
            self.e["day_trades"] = [d.isoformat() for d in self.gate.state.day_trades]
            self.state.high_water_mark = str(self.gate.state.high_water_mark)
        self.state.save(self.state_path)

    @property
    def dry(self) -> bool:
        return self.broker is None

    def _today(self) -> date:
        return self.clock().astimezone(NY).date()

    def _urgent(self) -> bool:
        until = self.e.get("urgent_until")
        return until is None or self._today() <= date.fromisoformat(until)

    def _card(self, happened: str, status: str, lines: list[str], detail: str = "", look_at: Optional[str] = None,
              what_to_do: Optional[str] = None) -> None:
        if self.dry:
            return   # a dry run logs, it never cards
        card = cards.Card("Live mirror", happened, status, tuple(lines), what_to_do=what_to_do, detail=detail or None,
                          channel=cards.URGENT if self._urgent() else cards.DAILY, look_at=look_at)
        try:
            self.notify(card)
        except Exception:  # noqa: BLE001 - a card must never stop the mirror
            logger.exception("live mirror card failed")

    # -- the account --------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        portfolio = self.reader.portfolio()
        row = self.reader.account_row()
        if row.get("type") != "cash":
            raise RuntimeError(f"the Agentic account is type {row.get('type')!r}, not cash: the mirror will not trade")
        held = {}
        for position in self.reader.equity_positions():
            quantity = _d(position.get("quantity"))
            if quantity > 0:
                held[str(position.get("symbol", "")).upper()] = quantity
        unsettled, pending = _d(row.get("unsettled_funds")), _d(portfolio.get("pending_deposits"))
        cash = _d(portfolio.get("cash"))
        return Snapshot(equity=_d(portfolio.get("total_value")), settled_cash=max(ZERO, cash - unsettled - pending),
                        unsettled=unsettled, pending_deposits=pending, held=held, cash=cash)

    # -- cash flows (ruling 2026-11-04, second: deposits are flows, never P&L) -----------

    def _flows(self, snap: Snapshot) -> None:
        """A change in the broker's cash that the mirror's own fills and fees
        do not explain is a DEPOSIT or a WITHDRAWAL (the Agentic MCP has no
        transfer records; a dividend or interest credit cannot be told apart
        and is treated the same way - the stricter reading). Each one moves
        the kill-switch baseline, today's loss-limit baseline and the gate's
        high-water mark by its amount, enters the day's time-weighting, and
        is carded."""
        if "cash_expected" not in self.e:
            self.e["cash_expected"] = str(snap.cash)
            return
        last_fill = self.e.get("last_fill_at")
        if self._working is not None or (
                last_fill and (self.clock() - datetime.fromisoformat(last_fill)).total_seconds() < 60):
            return   # the broker's cash can trail a fill: reconcile once it has caught up
        amount = snap.cash - _d(self.e["cash_expected"])
        self.e["cash_expected"] = str(snap.cash)
        if abs(amount) < self.config.flow_threshold_usd:
            return
        kind = "deposit" if amount > 0 else "withdrawal"
        if self.e.get("starting_equity"):
            self.e["starting_equity"] = str(_d(self.e["starting_equity"]) + amount)
        self.e.setdefault("flows_today", []).append({"amount": str(amount), "at": self.clock().isoformat()})
        self.e["net_flows"] = str(_d(self.e.get("net_flows")) + amount)
        if self.state.high_water_mark:
            self.state.high_water_mark = str(_d(self.state.high_water_mark) + amount)
        if self.gate is not None:
            self.gate.state.high_water_mark += amount
            if amount < 0:   # a withdrawal leaves the gate's cash, at once (a deposit counts once settled)
                self.gate.state.cash = max(ZERO, self.gate.state.cash + amount)
        self.ledger.append("cash_flow", kind=kind, amount=amount, cash=snap.cash,
                           pending_deposits=snap.pending_deposits, starting_equity=self.e.get("starting_equity"))
        self._card(f"a {kind} of {cards.usd(float(abs(amount)))}", cards.GOOD, [
            f"Cash moved by {cards.usd(float(amount), signed=True)} with no trade behind it: counted as a "
            f"{kind}, not as gain or loss.",
            "The kill switch's baseline, today's loss-limit baseline and the gate's high-water mark moved by the "
            "same amount." + (f" Kill-switch baseline now {cards.usd(float(_d(self.e['starting_equity'])))}."
                              if self.e.get("starting_equity") else ""),
            "A dividend or interest credit looks the same and is treated the same way: it moves the baselines "
            "and is never counted as gain (accepted at this account's size, ruling 2026-11-04, third)."])
        self._save()

    def _expect(self, side: str, value: Decimal, fees: Decimal) -> None:
        """The mirror's own fill moves the cash it expects to see."""
        if "cash_expected" in self.e:
            delta = (value if side == "sell" else -value) - fees
            self.e["cash_expected"] = str(_d(self.e["cash_expected"]) + delta)
        self.e["last_fill_at"] = self.clock().isoformat()

    def _build_gate(self, snap: Snapshot) -> RiskGate:
        account = AccountState(cash=snap.settled_cash, high_water_mark=ZERO, account_type=AccountType.CASH,
                               kill_switch_tripped=self.state.kill_switch_tripped)
        for symbol, quantity in snap.held.items():
            position = account.ensure_position((SLEEVE.value, symbol), SLEEVE, 1, False)
            mirrored = next((p for p in self.e.get("positions", {}).values() if p["symbol"] == symbol), None)
            cost = _d(mirrored["entry_price"]) if mirrored else ZERO
            bid, ask, last = self._quote(symbol)
            mark = (bid + ask) / 2 if bid > 0 and ask >= bid else (last or cost)
            position.quantity = quantity
            position.cost_basis = quantity * (cost or mark)
            position.market_value = quantity * mark
        account.day_trades = [date.fromisoformat(d) for d in self.e.get("day_trades", [])]
        recorded = _d(self.state.high_water_mark)
        account.high_water_mark = max(recorded, account.nav)
        return RiskGate(self.limits, account, self.clock)

    # -- the session --------------------------------------------------------------------

    def start(self) -> None:
        """Session start: settle a working order an earlier process left,
        read the account, roll the day, build the gate."""
        self._stray()
        snap = self.snapshot()
        today = self._today()
        self._flows(snap)   # an overnight deposit moves the baselines BEFORE the day rolls
        if self.e.get("day") != today.isoformat():
            self.e["day"] = today.isoformat()
            self.e["day_start_equity"] = str(snap.equity)
            self.e["flows_today"] = []
            self.e.pop("daily_halt", None)
        self.e.setdefault("phase", "waiting_funds")
        self.e.setdefault("positions", {})
        self.gate = self._build_gate(snap)
        if not self.dry and self.reader.approvals_required():
            self.e["approvals_on"] = True
            self._card("trade approvals are on in the Robinhood app", cards.ACTION,
                       ["Every live order would wait for a tap in the app, so the mirror places none.",
                        "Exits included: a stop could not fire. Nothing is traded until approvals are off."],
                       what_to_do=cards._ask("Robinhood trade approvals are off again; resume the live mirror."))
        else:
            self.e.pop("approvals_on", None)
        self.ledger.append("session_start", dry=self.dry, equity=snap.equity, settled_cash=snap.settled_cash,
                           unsettled=snap.unsettled, pending_deposits=snap.pending_deposits, phase=self.e["phase"],
                           held={k: str(v) for k, v in snap.held.items()})
        self._save()

    def _stray(self) -> None:
        """A working order an earlier process left in the state file. Its gate
        reservation died with that process, so it is never left to run
        unwatched: cancelled if still open, recorded if finished or never
        placed. A fill it left shows up as a held position and is caught by
        ``_check_unwatched``. A failed lookup raises: the start is retried at
        the next tick and nothing trades meanwhile."""
        left = self.e.get("working")
        if not left or self.dry:
            return
        status = (self.broker.get_order(left["order_id"]) if left.get("order_id")
                  else self.broker.get_order_by_client_reference(left["ref"]))
        if status is None:
            outcome = "never placed"
        elif status.is_terminal:
            outcome = f"{status.status}, {status.filled_quantity} filled"
        else:
            self.broker.cancel_order(status.broker_order_id)
            outcome = f"still {status.status} with {status.filled_quantity} filled: cancel requested"
        self.ledger.append("stray_order", **{k: v for k, v in left.items() if k != "ref"}, client_ref=left.get("ref"),
                           outcome=outcome)
        self.e.pop("working", None)
        self._card(f"a live {left['symbol']} order from an earlier run", cards.LOOK if status is None else cards.ACTION,
                   [f"The mirror restarted with a {left['kind']} order working ({outcome}).",
                    "Any shares it left are checked against what the mirror tracks; new entries wait while they "
                    "do not match."],
                   look_at="a live order an earlier run left behind" if status is None else None,
                   what_to_do=None if status is None else
                   cards._ask(f"Show me the live {left['symbol']} order the mirror found after its restart."))
        self.state.save(self.state_path)

    def _check_unwatched(self, snap: Snapshot) -> None:
        """Every holding must be a tracked position (or the working order's
        symbol): anything else has no stop watching it. It blocks new
        entries and is carded once a symbol a day; it is never sold blind."""
        known = {p["symbol"] for p in self.e.get("positions", {}).values()}
        if self._working is not None:
            known.add(self._working["symbol"])
        unwatched = sorted(s for s in snap.held if s not in known)
        if unwatched:
            self.e["unwatched"] = unwatched
        else:
            self.e.pop("unwatched", None)
        noted, today = self.e.setdefault("unwatched_noted", {}), self._today().isoformat()
        for symbol in unwatched:
            if noted.get(symbol) == today:
                continue
            noted[symbol] = today
            self.ledger.append("unwatched_position", symbol=symbol, quantity=snap.held[symbol])
            self._card(f"the live account holds {symbol} that the mirror does not track", cards.ACTION, [
                f"{snap.held[symbol]} {symbol} is held with no mirrored position behind it, so no stop watches it.",
                "New live entries are blocked until it is closed or explained; exits of tracked positions keep "
                "running."], what_to_do=cards._ask(f"Show me the untracked live {symbol} holding and how it got there."))

    def tick(self) -> None:
        if self.gate is None:
            self.start()
        if self.e.get("approvals_on"):
            return
        snap = self.snapshot()
        self._flows(snap)
        today = self._today()
        phase = self.e["phase"]
        if self._working is not None and phase != "mirroring":
            done = self._settle(self._working)   # e.g. a round-trip order whose outcome was unknown
            if done is None:
                return
            self._after_fill(self._working, done)
            self._release()
        if phase == "waiting_funds":
            # settled cash excludes deposits in transit and unsettled funds
            if snap.settled_cash >= self.config.min_funding_usd:
                if self.dry:
                    if not self.e.get("dry_noted") == today.isoformat():
                        self.e["dry_noted"] = today.isoformat()
                        self.ledger.append("dry", note="funded; the round trip waits for a live process")
                    return
                self.round_trip()
            else:
                self._waiting_note(snap)
            return
        if phase == "round_trip_failed":
            return
        if phase == "armed":
            if self.e.get("armed_on") == today.isoformat():
                return  # mirroring starts at the NEXT session (item 5c)
            self.e["phase"] = "mirroring"
            self.e["mirroring_since"] = today.isoformat()
            self.e["urgent_until"] = (today + timedelta(days=self.config.urgent_days)).isoformat()
            self.e["paper_offset"] = self._paper_length()   # only paper entries from now on
            self.ledger.append("mirroring_started", paper_offset=self.e["paper_offset"])
            self._card("mirroring started", cards.GOOD,
                       [f"From today the live account copies the paper AI trader, scaled to {cards.usd(snap.equity)}.",
                        f"Every order, fill and skip comes here until {self.e['urgent_until']}, then to #reports."])
            self._save()
        if self.e["phase"] == "mirroring":
            self._mirror(snap)

    # -- waiting, and the round trip (item 5b) --------------------------------------------

    def _waiting_note(self, snap: Snapshot) -> None:
        today = self._today().isoformat()
        if self.e.get("waiting_noted") == today:
            return
        self.e["waiting_noted"] = today
        self.ledger.append("waiting_funds", settled_cash=snap.settled_cash, unsettled=snap.unsettled,
                           pending_deposits=snap.pending_deposits)
        self._save()

    def round_trip(self) -> bool:
        """One tiny live round trip: buy then sell a fractional share."""
        symbol, quantity = self.config.round_trip_symbol, self.config.round_trip_shares
        quote = self.reader.quotes([symbol]).get(symbol, {})
        ask, bid = _d(quote.get("ask_price")), _d(quote.get("bid_price"))
        if ask <= 0 or bid <= 0:
            return False   # no quote yet (before the open): next tick
        self.e["phase"] = "round_trip"
        self._save()
        buy = self._send(EquityBuyOrder(symbol=symbol, quantity=quantity, sleeve=SLEEVE,
                                        execution=MarketBuyExecution(justification=MARKET_BUY_WHY,
                                                                     max_price=(ask * Decimal("1.05")).quantize(CENTS, ROUND_UP))),
                         "round_trip_buy", wait=True)
        sell = None
        if buy and buy["filled"] > 0:
            sell = self._send(EquitySellToCloseOrder(symbol=symbol, quantity=buy["filled"], sleeve=SLEEVE,
                                                     execution=MarketSellExecution(justification=MARKET_SELL_WHY,
                                                                                   min_price=(bid * Decimal("0.95")).quantize(CENTS, ROUND_DOWN))),
                              "round_trip_sell", wait=True)
        ok = bool(buy and sell and sell["filled"] == buy["filled"])
        mid = (ask + bid) / 2
        record = {"symbol": symbol, "quantity": quantity, "quote_bid": bid, "quote_ask": ask,
                  "buy": buy, "sell": sell, "passed": ok}
        if ok:
            record["buy_vs_mid_bp"] = ((buy["price"] / mid - 1) * 10000).quantize(Decimal("0.1"))
            record["sell_vs_mid_bp"] = ((1 - sell["price"] / mid) * 10000).quantize(Decimal("0.1"))
            record["fees"] = buy["fees"] + sell["fees"]
            record["net"] = sell["filled"] * sell["price"] - buy["filled"] * buy["price"] - record["fees"]
        self.ledger.append("round_trip", **record)
        if ok:
            self.e["phase"] = "armed"
            self.e["armed_on"] = self._today().isoformat()
            self.e["starting_equity"] = str(self.snapshot().equity)
            self._card("live round trip passed", cards.GOOD, [
                f"Bought and sold {quantity} {symbol} through the gate: in at {buy['price']}, out at {sell['price']} a share.",
                f"Against the quote's mid: {record['buy_vs_mid_bp']} bp paid in, {record['sell_vs_mid_bp']} bp given up out; {_fees(record['fees'])}.",
                f"Starting equity for the kill switch: {cards.usd(float(self.e['starting_equity']))}. Mirroring starts at the next session."])
        else:
            self.e["phase"] = "round_trip_failed"
            self._card("live round trip failed", cards.ACTION,
                       [f"The {symbol} round trip did not complete (buy {bool(buy)}, sell {bool(sell)}).",
                        "Nothing will be mirrored until a human has looked."], detail=json.dumps(record, default=str),
                       what_to_do=cards._ask("Show me the live mirror's failed round trip and what the account holds."))
        self._save()
        return ok

    # -- orders ------------------------------------------------------------------------

    def _send(self, order, kind: str, *, wait: bool = False, extra: Optional[dict] = None) -> Optional[dict]:
        """Gate, then broker; with ``wait`` poll to a terminal state and settle."""
        assert self.gate is not None
        decision = self.gate.submit(order)
        if not decision.is_approved:
            self.ledger.append("gate_refused", kind=kind, symbol=order.symbol, code=decision.code,
                               message=decision.message)
            self._card(f"the gate refused a live {order.symbol} order", cards.LOOK,
                       [f"{kind.replace('_', ' ')}: {cards.plain(decision.message)}"],
                       look_at="why the live account's own gate said no")
            return None
        try:
            receipt = self.broker.submit_order(decision, client_reference=(extra or {}).get("ref"))
        except OrderOutcomeUnknown as error:
            # the broker may have acted: keep the reservation and watch it by its reference (never re-sent)
            self.ledger.append("order_unknown", kind=kind, symbol=order.symbol, client_ref=error.client_reference,
                               error=str(error))
            self._card(f"a live {order.symbol} order's outcome is unknown", cards.LOOK, [
                f"{kind.replace('_', ' ')}: Robinhood failed mid-request ({cards.plain(str(error))[:160]}).",
                "It is watched by its reference: adopted if it appears, called never placed if Robinhood's own "
                f"order list has not shown it for {self.config.unknown_order_seconds} seconds. Nothing new is "
                "placed meanwhile."], look_at="a live order Robinhood may or may not have taken")
            self._hold({"decision": decision, "order_id": None, "ref": error.client_reference, "kind": kind,
                        "symbol": order.symbol, "placed": self.clock(), "extra": extra or {}})
            return None
        except BrokerError as error:
            self.gate.cancel(decision)
            self.ledger.append("broker_refused", kind=kind, symbol=order.symbol, error=str(error),
                               body=getattr(error, "body", ""))
            self._card(f"Robinhood refused a live {order.symbol} order", cards.LOOK,
                       [f"{kind.replace('_', ' ')}: {cards.plain(str(error))}"], look_at="what the broker refused")
            return None
        self.ledger.append("order", kind=kind, symbol=order.symbol, quantity=order.quantity,
                           style=order.execution.style, bound=order.execution.price_bound,
                           order_id=receipt.broker_order_id, ref=receipt.client_order_id,
                           review=getattr(self.broker, "last_review", None) and
                           (self.broker.last_review or {}).get("order_checks"),
                           **{k: v for k, v in (extra or {}).items() if k != "ref"})
        working = {"decision": decision, "order_id": receipt.broker_order_id, "kind": kind,
                   "symbol": order.symbol, "placed": self.clock(), "extra": extra or {}}
        if not wait:
            self._hold(working)
            return {"order_id": receipt.broker_order_id}
        wait_for = timedelta(seconds=self.config.fill_wait_seconds)
        deadline, cancelled = self.clock() + wait_for, False
        while True:
            done = self._settle(working)
            if done is not None:
                return done
            if self.clock() >= deadline:
                if cancelled:
                    # a cancel that has not landed: settle it later, never forget it
                    self._hold(working)
                    self.ledger.append("still_working", order_id=working["order_id"], kind=kind)
                    return None
                self._cancel(working)   # accepted means "requested", not "done" (spike 2b)
                cancelled, deadline = True, self.clock() + wait_for
            self.config_sleep()

    def config_sleep(self) -> None:   # overridden in tests
        import time

        time.sleep(2)

    def _hold(self, working: dict) -> None:
        """The one working order, in memory and in the state file, so a
        restarted process finds it (``_stray``) instead of forgetting it."""
        self._working = working
        self.e["working"] = {"order_id": working["order_id"], "ref": working.get("ref"), "kind": working["kind"],
                             "symbol": working["symbol"], "placed": working["placed"].isoformat(),
                             "pid": (working.get("extra") or {}).get("pid")}
        self._save()

    def _release(self) -> None:
        self._working = None
        self.e.pop("working", None)
        self._save()

    def _cancel(self, working: dict) -> None:
        if working.get("order_id") is None:
            return   # not found yet: there is nothing to cancel until it is
        try:
            self.broker.cancel_order(working["order_id"])
        except BrokerError as error:
            self.ledger.append("cancel_failed", order_id=working["order_id"], error=str(error))

    def _settle(self, working: dict) -> Optional[dict]:
        if working.get("order_id") is None:
            # the place call's outcome was unknown: find it by its reference
            try:
                found = self.broker.get_order_by_client_reference(working["ref"])
            except BrokerError as error:
                self.ledger.append("poll_failed", client_ref=working["ref"], error=str(error))
                return None
            if found is None:
                if (self.clock() - working["placed"]).total_seconds() < self.config.unknown_order_seconds:
                    return None
                # absent from Robinhood's own order list for the whole window: never placed
                self.gate.cancel(working["decision"])
                result = {"order_id": None, "status": "not_placed", "filled": ZERO, "price": ZERO, "fees": ZERO}
                self.ledger.append("not_placed", kind=working["kind"], symbol=working["symbol"],
                                   client_ref=working["ref"],
                                   **{k: v for k, v in working.get("extra", {}).items() if k != "ref"})
                self._save()
                return result
            working["order_id"] = found.broker_order_id
            self.ledger.append("order_found", kind=working["kind"], symbol=working["symbol"], client_ref=working["ref"],
                               order_id=found.broker_order_id)
            self._hold(working)
        try:
            status = self.broker.get_order(working["order_id"])
        except BrokerError as error:
            self.ledger.append("poll_failed", order_id=working["order_id"], error=str(error))
            return None
        if not status.is_terminal:
            return None
        filled, price = status.filled_quantity, status.filled_avg_price
        fees = ZERO
        try:
            orders = self.reader.equity_orders(order_id=working["order_id"])
            fees = _d(orders[0].get("fees")) if orders else ZERO
        except Exception:  # noqa: BLE001 - fees are reported, never guessed
            pass
        if filled > 0 and price is not None:
            self.gate.record_fill(working["decision"], price, filled_units=filled)
            side = "buy" if type(working["decision"].order).__name__ == "EquityBuyOrder" else "sell"
            self._expect(side, filled * price, fees)
        else:
            self.gate.cancel(working["decision"])
        result = {"order_id": working["order_id"], "status": status.status, "filled": filled,
                  "price": price or ZERO, "fees": fees}
        self.ledger.append("settled", kind=working["kind"], symbol=working["symbol"], **result,
                           **working.get("extra", {}))
        self._save()
        return result

    # -- mirroring (item 5c) -------------------------------------------------------------

    def _paper_events(self) -> list[dict]:
        if not self.paper_ledger.exists():
            return []
        with open(self.paper_ledger, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        start = int(self.e.get("paper_offset", 0))
        self.e["paper_offset"] = len(lines)
        return [json.loads(line) for line in lines[start:] if line.strip()]

    def _paper_length(self) -> int:
        if not self.paper_ledger.exists():
            return 0
        with open(self.paper_ledger, "r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)

    def _mirror(self, snap: Snapshot) -> None:
        self._check_unwatched(snap)
        plans = self.e.setdefault("plans", {})
        for event in self._paper_events():
            kind, pid = event.get("event"), event.get("pid")
            if kind == "position_opened" and pid:
                plans[pid] = {k: event.get(k) for k in ("symbol", "instrument", "stop_pct", "target_pct", "horizon",
                                                        "tag", "leverage", "occ", "multiplier", "confidence")}
            elif kind == "settled" and event.get("side") == "buy" and event.get("reason") == "entry" \
                    and pid in plans and _d(event.get("filled_qty")) > 0:
                self._queue.append({"pid": pid, "at": self.clock().isoformat(), "paper_price": event.get("avg_price"),
                                    "paper_qty": event.get("filled_qty"), "multiplier": event.get("multiplier") or 1})
            elif kind == "position_closed" and pid:
                if pid in self.e["positions"]:
                    self.e["positions"][pid]["paper_closed"] = event.get("reason") or "closed"
                elif not any(q["pid"] == pid for q in self._queue):
                    plans.pop(pid, None)
        self._save()
        if self._working is not None:
            done = self._settle(self._working)
            if done is None:
                if (self.clock() - self._working["placed"]).total_seconds() >= self.config.fill_wait_seconds \
                        and not self._working.get("cancel_requested"):
                    self._working["cancel_requested"] = True
                    self._cancel(self._working)
                return
            self._after_fill(self._working, done)
            self._release()
        if self._exit_one(snap):
            return
        self._enter_one(snap)

    def _after_fill(self, working: dict, done: dict) -> None:
        extra, pid = working["extra"], working["extra"].get("pid")
        if working["kind"] == "entry" and done["filled"] > 0:
            plan = self.e["plans"].get(pid, {})
            price = done["price"]
            stop = (price * (1 - _d(plan.get("stop_pct")) / 100)).quantize(CENTS)
            target = (price * (1 + _d(plan.get("target_pct")) / 100)).quantize(CENTS)
            self.e["positions"][pid] = {"symbol": working["symbol"], "quantity": str(done["filled"]),
                                        "entry_price": str(price), "stop": str(stop), "target": str(target),
                                        "horizon": plan.get("horizon"), "opened": self._today().isoformat()}
            self._card(f"bought {working['symbol']}", cards.GOOD, [
                f"Mirrored the paper AI trader: {done['filled']} {working['symbol']} at {price} a share "
                f"({cards.usd(float(done['filled'] * price))}).",
                f"Stop {stop}, target {target}, horizon {plan.get('horizon')}; {_fees(done['fees'])}."])
        elif working["kind"] == "exit" and done["filled"] > 0:
            held = self.e["positions"].pop(pid, None) or {}
            pnl = done["filled"] * (done["price"] - _d(held.get("entry_price"))) - done["fees"]
            self.e.setdefault("closed_today", []).append({"pid": pid, "pnl": str(pnl), "fees": str(done["fees"])})
            self._card(f"sold {working['symbol']}", cards.GOOD, [
                f"Closed {done['filled']} {working['symbol']} at {done['price']} a share ({extra.get('why')}).",
                f"Result {cards.usd(float(pnl), signed=True)} after {_fees(done['fees'])}."])
        elif done["status"] == "not_placed":
            self._card(f"a live {working['symbol']} order was never placed", cards.LOOK,
                       [f"{working['kind']}: Robinhood's order list never showed it in "
                        f"{self.config.unknown_order_seconds} seconds; its reservation is released."],
                       look_at="a live order that Robinhood never took")
        elif done["filled"] == 0:
            self._card(f"a live {working['symbol']} order did not fill", cards.LOOK,
                       [f"{working['kind']}: {done['status']} after {self.config.fill_wait_seconds} seconds."],
                       look_at="an unfilled live order")
        self._save()

    def _quote(self, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        quote = self.reader.quotes([symbol]).get(symbol.upper(), {})
        return _d(quote.get("bid_price")), _d(quote.get("ask_price")), _d(quote.get("last_trade_price"))

    def _exit_one(self, snap: Snapshot) -> bool:
        now_local = self.clock().astimezone(NY)
        marks = {}
        for pid, pos in list(self.e["positions"].items()):
            symbol, quantity = pos["symbol"], _d(pos["quantity"])
            held = snap.held.get(symbol, ZERO)
            if held <= 0:
                self.e["positions"].pop(pid)
                self.ledger.append("position_gone", pid=pid, symbol=symbol)
                continue
            quantity = min(quantity, held)
            bid, ask, last = self._quote(symbol)
            if bid > 0 and ask >= bid and (ask - bid) / ((ask + bid) / 2) <= Decimal("0.02"):
                marks[(SLEEVE.value, symbol)] = (bid + ask) / 2   # valued at the mid (ruling 2026-11-04)
            trigger = bid if bid > 0 else last          # never the ask (ruling 2026-11-04)
            why = None
            if pos.get("paper_closed"):
                why = f"the paper AI trader closed it ({pos['paper_closed']})"
            elif trigger > 0 and trigger <= _d(pos["stop"]):
                why = f"the live stop {pos['stop']} (bid or last {trigger})"
            elif trigger > 0 and trigger >= _d(pos["target"]):
                why = f"the live target {pos['target']} (bid or last {trigger})"
            elif pos.get("horizon") == "intraday" and now_local.time() >= _clock_time(self.ai.intraday_close):
                why = "the intraday close"
            elif self._past_horizon(pos):
                why = "past its horizon"
            if why is None:
                continue
            self._mark(marks)
            whole = quantity == quantity.to_integral_value()
            if bid <= 0 and not whole:
                return False
            execution = (LimitExecution(limit_price=bid.quantize(CENTS, ROUND_DOWN)) if whole and bid > 0 else
                         MarketSellExecution(justification=MARKET_SELL_WHY,
                                             min_price=((bid if bid > 0 else trigger) * Decimal("0.95")).quantize(CENTS, ROUND_DOWN)))
            order = EquitySellToCloseOrder(symbol=symbol, quantity=quantity, execution=execution, sleeve=SLEEVE)
            if self.dry:
                self.ledger.append("would_exit", pid=pid, symbol=symbol, quantity=quantity, why=why)
                self.e["positions"].pop(pid)
                return True
            self._send(order, "exit", extra={"pid": pid, "why": why})
            return True
        self._mark(marks)
        return False

    def _mark(self, marks: dict) -> None:
        if marks and self.gate is not None:
            self.gate.mark_to_market(marks)
            self._save()

    def _past_horizon(self, pos: dict) -> bool:
        sessions = self.ai.horizon_sessions.get(pos.get("horizon") or "", None)
        if not sessions:
            return False
        opened = date.fromisoformat(pos["opened"])
        weekdays = sum(1 for i in range(1, (self._today() - opened).days + 1)
                       if (opened + timedelta(days=i)).weekday() < 5)
        return weekdays > sessions + 1      # the paper close should come first; this is the backstop

    def _entries_blocked(self, snap: Snapshot) -> Optional[str]:
        if self.halt_path is not None and self.halt_path.exists():
            return "halted by an operator (HALT marker)"
        if self.e.get("unwatched"):
            return f"the account holds {', '.join(self.e['unwatched'])}, which the mirror does not track"
        start = _d(self.e.get("starting_equity"))
        if start > 0 and snap.equity <= start * (1 - self.config.kill_fraction):
            if not self.state.kill_switch_tripped:
                self.state.kill_switch_tripped = True
                self.state.tripped_at = self.clock().isoformat()
                self.state.trip_reason = (f"equity {snap.equity} at or below {1 - self.config.kill_fraction:.0%} of "
                                          f"starting equity {start}")
                self.ledger.append("kill_switch_tripped", reason=self.state.trip_reason)
                self._card("the live kill switch tripped", cards.ACTION,
                           [f"Equity {cards.usd(float(snap.equity))} is 12% or more below the starting "
                            f"{cards.usd(float(start))}.", "No new live entries; exits keep running."],
                           what_to_do=cards._ask("Review the live mirror's drawdown before any reset."))
                self._save()
            return "the kill switch (12% below starting equity)"
        if self.state.kill_switch_tripped or (self.gate is not None and self.gate.state.kill_switch_tripped):
            return "the kill switch"
        # today's baseline moves with today's deposits and withdrawals
        day_start = _d(self.e.get("day_start_equity")) + self._flows_today()
        if day_start > 0 and snap.equity <= day_start * (1 - self.config.daily_loss_fraction):
            if self.e.get("daily_halt") != self._today().isoformat():
                self.e["daily_halt"] = self._today().isoformat()
                self.ledger.append("daily_loss_limit", equity=snap.equity, day_start=day_start)
                self._card("the live daily loss limit was hit", cards.LOOK,
                           [f"Equity {cards.usd(float(snap.equity))} is 4% or more below today's start of "
                            f"{cards.usd(float(day_start))}.", "No new live entries today; exits keep running."],
                           look_at="the live account's losses today")
                self._save()
            return "the daily loss limit (4%)"
        return None

    def _skip(self, item: dict, plan: dict, why: str) -> None:
        self.e.setdefault("skipped_today", []).append(item["pid"])
        self.ledger.append("skip", pid=item["pid"], symbol=plan.get("symbol"), instrument=plan.get("instrument"),
                           why=why)
        self._card(f"skipped {plan.get('symbol')}", cards.LOOK,
                   [f"The paper AI trader bought {plan.get('symbol')} ({plan.get('instrument')}); the live account "
                    f"did not: {why}."], look_at="a paper trade the live account did not copy")
        self._save()

    def _enter_one(self, snap: Snapshot) -> None:
        while self._queue:
            item = self._queue.pop(0)
            plan = self.e["plans"].get(item["pid"], {})
            age = (self.clock() - datetime.fromisoformat(item["at"])).total_seconds()
            if age > self.config.max_entry_delay_seconds:
                self._skip(item, plan, f"the copy was {int(age)} seconds late")
                continue
            blocked = self._entries_blocked(snap)
            if blocked:
                self._skip(item, plan, blocked)
                continue
            order, why = self._size(item, plan, snap)
            if order is None:
                self._skip(item, plan, why)
                continue
            if self.dry:
                self.ledger.append("would_enter", pid=item["pid"], symbol=order.symbol, quantity=order.quantity,
                                   style=order.execution.style, bound=order.execution.price_bound)
                return
            self._send(order, "entry", extra={"pid": item["pid"], "ref": f"mirror-{item['pid']}"})
            return

    def _size(self, item: dict, plan: dict, snap: Snapshot):
        instrument, symbol = plan.get("instrument"), str(plan.get("symbol") or "").upper()
        equity = snap.tradeable_equity   # live equity, less deposits still in transit
        budget = (equity * self.config.risk_fraction).quantize(CENTS, ROUND_DOWN)
        if instrument in ("call", "put"):
            contract = _d(item.get("paper_price")) * _d(item.get("multiplier") or 100)
            if contract > budget:
                return None, (f"one contract costs about {cards.usd(float(contract))}, above the "
                              f"{cards.usd(float(budget))} premium cap")
            return None, "one contract fits the premium cap, but the live option path has no round trip yet"
        factor = _d(plan.get("leverage") or "1")
        if plan.get("tag") in ("leveraged_etf", "inverse_etf") or factor != 1 \
                or symbol in {s.upper() for s in self.ai.leverage_factors}:
            return None, "leveraged and inverse ETFs are not allowed in the live account"
        bid, ask, _ = self._quote(symbol)
        stop_fraction = _d(plan.get("stop_pct")) / 100
        if ask <= 0 or stop_fraction <= 0:
            return None, "no live quote or no stop distance"
        shares = budget / (ask * stop_fraction)
        # the cap is checked against what the GATE reserves (the limit, or a
        # market buy's ask + 5%), 1% inside it for the gap between the
        # broker's equity and the gate's marked NAV
        cap = equity * self.config.max_position_fraction * Decimal("0.99")
        limit = (ask * (1 + self.config.limit_buffer)).quantize(CENTS, ROUND_UP)
        whole = min(shares, cap / limit).to_integral_value(rounding=ROUND_DOWN)
        if whole >= 1:
            return EquityBuyOrder(symbol=symbol, quantity=whole, sleeve=SLEEVE,
                                  execution=LimitExecution(limit_price=limit)), ""
        bound = (ask * Decimal("1.05")).quantize(CENTS, ROUND_UP)
        fraction = min(shares, cap / bound).quantize(SHARE_STEP, ROUND_DOWN)
        if fraction * ask < self.config.min_notional_usd:
            return None, f"the size is under the {cards.usd(float(self.config.min_notional_usd))} minimum"
        return EquityBuyOrder(symbol=symbol, quantity=fraction, sleeve=SLEEVE,
                              execution=MarketBuyExecution(justification=MARKET_BUY_WHY, max_price=bound)), ""

    # -- the day's card (item 6) -----------------------------------------------------------

    def _flows_today(self) -> Decimal:
        return sum((_d(f["amount"]) for f in self.e.get("flows_today", [])), ZERO)

    def day_return(self, equity: Decimal) -> tuple[Decimal, Decimal, Optional[Decimal]]:
        """(P&L, the Modified Dietz base, the day's return): deposits and
        withdrawals are taken out of the P&L, and weighted into the base by
        the share of the session left after each arrived."""
        day_start = _d(self.e.get("day_start_equity"))
        today = self._today()
        start = datetime.combine(today, _clock_time(self.config.session_start), NY)
        end = datetime.combine(today, _clock_time(self.config.session_end), NY)
        span = (end - start).total_seconds()
        weighted = ZERO
        for flow in self.e.get("flows_today", []):
            at = datetime.fromisoformat(flow["at"])
            share = Decimal(str(min(1.0, max(0.0, (end - at).total_seconds() / span))))
            weighted += _d(flow["amount"]) * share
        pnl = equity - day_start - self._flows_today()
        base = day_start + weighted
        return pnl, base, (pnl / base if base > 0 else None)

    def daily_card(self) -> Optional[cards.Card]:
        snap = self.snapshot()
        self._flows(snap)
        live, base, rate = self.day_return(snap.equity)
        today = self._today().isoformat()
        if rate is not None and self.e.get("twr_day") != today:
            self.e["twr_day"] = today   # chained once a day: time-weighted since mirroring began
            self.e["twr_index"] = str(_d(self.e.get("twr_index") or "1") * (1 + rate))
        since = _d(self.e.get("twr_index") or "1") - 1
        paper_start, paper_end = _paper_day(self.paper_ledger, self._today())
        scaled = None
        if paper_start and paper_end and base > 0:
            scaled = (paper_end / paper_start - 1) * base
        skipped = set(self.e.get("skipped_today", []))
        skip_effect = -_paper_pnl(self.paper_ledger, self._today(), skipped) * (base / paper_start) \
            if paper_start and skipped else ZERO
        fees = sum((_d(c.get("fees")) for c in self.e.get("closed_today", [])), ZERO)
        gap = None if scaled is None else live - scaled
        fills = None if gap is None else gap - skip_effect + fees
        flows = self._flows_today()
        lines = [f"Live today: {cards.usd(float(live), signed=True)} ({cards.pct(float(rate)) if rate is not None else 'n/a'}"
                 f" time-weighted) on {cards.usd(float(base))}; since mirroring began {cards.pct(float(since))}."
                 + (f" Deposits and withdrawals today: {cards.usd(float(flows), signed=True)}, not counted as gain."
                    if flows else ""),
                 ("Paper AI trader scaled to the same size: " + cards.usd(float(scaled), signed=True) + "."
                  if scaled is not None else "Paper AI trader: no day to compare."),
                 (f"Gap {cards.usd(float(gap), signed=True)}: skips {cards.usd(float(skip_effect), signed=True)}, "
                  f"fees {cards.usd(float(-fees), signed=True)}, fills and timing {cards.usd(float(fills), signed=True)}."
                  if gap is not None else "Gap: n/a."),
                 f"Open live positions: {len(self.e.get('positions', {}))}; skipped today: {len(skipped)}."]
        self.e["closed_today"], self.e["skipped_today"] = [], []
        self._save()
        return cards.Card("Live mirror", "the day against the paper AI trader", cards.GOOD, tuple(lines),
                          channel=cards.URGENT if self._urgent() else cards.DAILY)


def _fees(value: Decimal) -> str:
    """Fees in plain words: cents, not dollars and cents (the card rules)."""
    value = _d(value)
    if value <= 0:
        return "no fees"
    if value < 1:
        return f"{int((value * 100).to_integral_value(rounding=ROUND_UP))} cents of fees"
    return f"{cards.usd(float(value))} of fees"


def _clock_time(text: str) -> dtime:
    hour, minute = text.split(":")
    return dtime(int(hour), int(minute))


def _paper_day(path: Path, day: date) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """The paper account's equity at the last snapshot before ``day`` and the latest on it."""
    if not path.exists():
        return None, None
    before = latest = None
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if '"snapshot"' not in line:
                continue
            event = json.loads(line)
            stamp = datetime.fromisoformat(event["at"]).astimezone(NY).date()
            if stamp < day:
                before = _d(event.get("equity"))
            elif stamp == day:
                latest = _d(event.get("equity"))
    return before, latest


def _paper_pnl(path: Path, day: date, pids: set) -> Decimal:
    """Realized paper P&L of the ``pids`` that CLOSED on ``day`` (every fill
    of those round trips; a skipped trade still open counts nothing yet)."""
    total = ZERO
    if not path.exists():
        return total
    events = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if '"settled"' in line or '"position_closed"' in line:
                events.append(json.loads(line))
    closed = {e.get("pid") for e in events if e.get("event") == "position_closed" and e.get("pid") in pids
              and datetime.fromisoformat(e["at"]).astimezone(NY).date() == day}
    for event in events:
        if event.get("event") != "settled" or event.get("pid") not in closed:
            continue
        value = _d(event.get("filled_qty")) * _d(event.get("avg_price")) * _d(event.get("multiplier") or 1)
        total += value if event.get("side") == "sell" else -value
    return total
