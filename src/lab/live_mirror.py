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
  * stocks and plain ETFs: a leveraged or inverse ETF is skipped and logged;
  * options (TEMPORARY ruling 2026-11-09, while the account is too small for
    typical contracts): a paper option whose contract fits the premium cap
    (2% of live equity) is copied as the option itself once the live option
    round trip has passed; a CALL that does not fit is copied as its
    underlying stock (the proxy: the same 2% at risk to the paper option's
    own underlying stop, its underlying target, its horizon; out on the paper
    close or that stop or target), OFF until the human approves the method;
    a PUT that does not fit is skipped;
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
from execution.robinhood import occ_parts, option_position_id, option_tick
from risk_gate.gate import RiskGate
from risk_gate.schema import (
    EquityBuyOrder, EquitySellToCloseOrder, LimitExecution, MarketBuyExecution, MarketSellExecution,
    OptionBuyToOpenOrder, OptionSellToCloseOrder,
)
from risk_gate.state import AccountState, AccountType, Sleeve

NY = ZoneInfo("America/New_York")
ZERO = Decimal("0")
CENTS = Decimal("0.01")
SHARE_STEP = Decimal("0.000001")
SLEEVE = Sleeve.AGGRESSIVE
#: The live option round trip's position (temporary option ruling 2026-11-09).
OPTION_RT_PID = "option-round-trip"
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
    #: Long option contracts held, by Robinhood instrument id, and a readable
    #: name for each (the temporary option ruling, 2026-11-09).
    options: Optional[dict] = None
    option_names: Optional[dict] = None

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
        options, names = {}, {}
        for position in self.reader.option_positions():
            quantity = _d(position.get("quantity"))
            if quantity > 0:   # a short is never tracked, so it is flagged as unwatched
                option_id = option_position_id(position)
                options[option_id] = quantity
                names[option_id] = " ".join(str(position.get(k)) for k in
                                            ("type", "chain_symbol", "expiration_date", "strike_price", "option_type")
                                            if position.get(k)) or option_id
        unsettled, pending = _d(row.get("unsettled_funds")), _d(portfolio.get("pending_deposits"))
        cash = _d(portfolio.get("cash"))
        return Snapshot(equity=_d(portfolio.get("total_value")), settled_cash=max(ZERO, cash - unsettled - pending),
                        unsettled=unsettled, pending_deposits=pending, held=held, cash=cash, options=options,
                        option_names=names)

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
        natives = {p["option_id"]: p for p in self.e.get("positions", {}).values()
                   if p.get("copy") == "native" and p.get("option_id") in (snap.options or {})}
        quotes = self.reader.option_quotes(list(natives)) if natives else {}
        for option_id, mirrored in natives.items():
            # a held contract the gate must know, so its sell to close is covered
            expiry = date.fromisoformat(mirrored["expiry"]) if mirrored.get("expiry") else None
            position = account.ensure_position(("option", mirrored["symbol"]), Sleeve.EQUITY,
                                               int(mirrored.get("multiplier") or 100), True, expiration=expiry)
            quote = quotes.get(option_id, {})
            bid, ask = _d(quote.get("bid_price")), _d(quote.get("ask_price"))
            mark = (bid + ask) / 2 if bid > 0 and ask >= bid else _d(mirrored["entry_price"])
            contracts = snap.options[option_id]
            multiplier = int(mirrored.get("multiplier") or 100)
            position.quantity = contracts
            position.cost_basis = contracts * _d(mirrored["entry_price"]) * multiplier
            position.market_value = contracts * mark * multiplier
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
        positions = self.e.get("positions", {}).values()
        known = {p["symbol"] for p in positions if p.get("copy") != "native"}
        known_options = {p.get("option_id") for p in positions if p.get("copy") == "native"}
        if self._working is not None:
            known.add(self._working["symbol"])
            known_options.add((self._working.get("extra") or {}).get("option_id"))
        held = dict(snap.held)
        for option_id, contracts in (snap.options or {}).items():
            if option_id not in known_options:
                held[f"option {(snap.option_names or {}).get(option_id, option_id)}"] = contracts
        unwatched = sorted(s for s in held if s not in known and not s.startswith("option ")) \
            + sorted(s for s in held if s.startswith("option "))
        if unwatched:
            self.e["unwatched"] = unwatched
        else:
            self.e.pop("unwatched", None)
        noted, today = self.e.setdefault("unwatched_noted", {}), self._today().isoformat()
        for symbol in unwatched:
            if noted.get(symbol) == today:
                continue
            noted[symbol] = today
            self.ledger.append("unwatched_position", symbol=symbol, quantity=held[symbol])
            self._card(f"the live account holds {symbol} that the mirror does not track", cards.ACTION, [
                f"{held[symbol]} {symbol} is held with no mirrored position behind it, so no stop watches it.",
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
        self.ledger.append("order", kind=kind, symbol=order.symbol,
                           quantity=getattr(order, "quantity", None) or getattr(order, "contracts", None),
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
        order = working["decision"].order
        option = isinstance(order, (OptionBuyToOpenOrder, OptionSellToCloseOrder))
        fees, raw = ZERO, None
        try:
            rows = (self.reader.option_orders if option else self.reader.equity_orders)(order_id=working["order_id"])
            raw = rows[0] if rows else None
            fees = _order_fees(raw) if raw else ZERO
        except Exception:  # noqa: BLE001 - fees are reported, never guessed
            pass
        if option and raw is not None:
            # the first live option orders: the venue's own record, kept whole
            self.ledger.append("option_order_raw", order_id=working["order_id"], kind=working["kind"], raw=raw)
        if option and filled > 0 and price is not None:
            price = self._option_price(order, price)
        if filled > 0 and price is not None:
            self.gate.record_fill(working["decision"], price, filled_units=filled)
            side = "buy" if isinstance(order, (EquityBuyOrder, OptionBuyToOpenOrder)) else "sell"
            self._expect(side, filled * price * (order.multiplier if option else 1), fees)
        else:
            self.gate.cancel(working["decision"])
        result = {"order_id": working["order_id"], "status": status.status, "filled": filled,
                  "price": price or ZERO, "fees": fees}
        self.ledger.append("settled", kind=working["kind"], symbol=working["symbol"], **result,
                           **working.get("extra", {}))
        self._save()
        return result

    def _option_price(self, order, price: Decimal) -> Decimal:
        """An option fill's price PER SHARE (the quote's unit). A buy cannot
        fill above its limit, so a price above it that is the limit's unit
        times the multiplier was reported per contract: it is converted and
        the conversion is recorded (the first live option orders, 2026-11-09)."""
        limit, multiplier = order.execution.price_bound, Decimal(order.multiplier)
        buy = isinstance(order, OptionBuyToOpenOrder)
        if (buy and price > limit * Decimal("1.01") and price / multiplier <= limit * Decimal("1.01")) or (
                not buy and price > limit * 50 and price / multiplier <= limit * 5):
            self.ledger.append("option_price_unit", symbol=order.symbol, reported=price,
                               per_share=price / multiplier, limit=limit)
            return price / multiplier
        return price

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
                # an option's stop and target are UNDERLYING prices in the paper
                # engine (anchor mid at the stated distances): kept as levels
                plans[pid] = {k: event.get(k) for k in ("symbol", "instrument", "stop_pct", "target_pct", "horizon",
                                                        "tag", "leverage", "occ", "multiplier", "confidence",
                                                        "stop", "target", "anchor_mid", "expiry")}
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
        if self._working is None and self._option_round_trip_due(snap):
            self._option_round_trip(snap)
        self._option_round_trip_overdue()

    # -- the live option round trip (temporary option ruling 2026-11-09, item 3) ---------

    def _option_round_trip_due(self, snap: Snapshot) -> bool:
        if not self.config.option_round_trip_enabled or \
                self.e.get("option_round_trip") in ("passed", "failed", "dry"):
            return False
        if OPTION_RT_PID in self.e.get("positions", {}) or self._entries_blocked(snap):
            return False
        local = self.clock().astimezone(NY)
        start, end = (_clock_time(t) for t in self.config.option_round_trip_window)
        return local.weekday() < 5 and start <= local.time() <= end

    def _option_round_trip(self, snap: Snapshot) -> None:
        """Buy one cheap, liquid call that fits the premium cap through the
        gate; the next tick sells it through the native exit path. Native
        option copies wait for it to pass."""
        attempts, today = int(self.e.get("option_rt_attempts", 0)), self._today().isoformat()
        if attempts >= self.config.option_round_trip_attempts:
            self.e["option_round_trip"] = "failed"
            self.ledger.append("option_round_trip", passed=False, attempts=attempts)
            self._card("the live option round trip failed", cards.ACTION,
                       [f"{attempts} attempts to buy one cheap {self.config.option_round_trip_symbol} call did not fill.",
                        "Native option copies stay off; calls that do not fit follow the proxy rule."],
                       what_to_do=cards._ask("Show me the live option round trip's attempts and the account."))
            self._save()
            return
        budget = (snap.tradeable_equity * self.config.risk_fraction).quantize(CENTS, ROUND_DOWN)
        pick = self._cheap_contract(budget)
        if pick is None:
            if self.e.get("option_rt_none_noted") != today:
                self.e["option_rt_none_noted"] = today
                self.ledger.append("option_round_trip_no_contract", budget=budget)
                self._save()
            return
        row, quote, limit = pick
        underlying, right = str(row["chain_symbol"]).upper(), str(row["type"])
        expiration, strike = date.fromisoformat(row["expiration_date"]), _d(row["strike_price"])
        occ = _occ(underlying, expiration, right, strike)
        order = OptionBuyToOpenOrder(symbol=occ, underlying=underlying, right=right, expiration=expiration,
                                     strike=strike, contracts=1, execution=LimitExecution(limit_price=limit))
        meta = {"copy": "native", "option_id": str(row["id"]), "multiplier": 100,
                "min_ticks": row.get("min_ticks") or {}, "underlying": underlying, "expiry": expiration.isoformat(),
                "stop": "0", "target": "0", "paper_closed": "the option round trip's sell",
                "quote_bid": str(_d(quote.get("bid_price"))), "quote_ask": str(_d(quote.get("ask_price")))}
        self.e.setdefault("plans", {})[OPTION_RT_PID] = {"symbol": underlying, "instrument": right, "occ": occ,
                                                          "horizon": "intraday", "multiplier": 100}
        if self.dry:
            self.e["option_round_trip"] = "dry"
            self.ledger.append("would_option_round_trip", occ=occ, limit=limit, budget=budget,
                               bid=meta["quote_bid"], ask=meta["quote_ask"],
                               open_interest=quote.get("open_interest"), delta=quote.get("delta"))
            self._save()
            return
        self.e["option_rt_attempts"] = attempts + 1
        self.ledger.append("option_round_trip_start", occ=occ, limit=limit, attempt=attempts + 1,
                           bid=meta["quote_bid"], ask=meta["quote_ask"], open_interest=quote.get("open_interest"))
        self._save()
        self._send(order, "entry", extra={"pid": OPTION_RT_PID, "ref": f"mirror-option-rt-{attempts + 1}", **meta})

    def _option_round_trip_overdue(self) -> None:
        """No silent gap: a session whose round-trip window closes without the
        round trip done is carded once (no contract qualified, or the buy did
        not fill); it tries again at the next session."""
        if not self.config.option_round_trip_enabled or \
                self.e.get("option_round_trip") in ("passed", "failed", "dry"):
            return
        if OPTION_RT_PID in self.e.get("positions", {}) or self._working is not None:
            return   # bought and selling: it finishes on its own
        local, today = self.clock().astimezone(NY), self._today().isoformat()
        if local.weekday() >= 5 or local.time() <= _clock_time(self.config.option_round_trip_window[1]):
            return
        if self.e.get("option_rt_overdue_noted") == today:
            return
        self.e["option_rt_overdue_noted"] = today
        no_contract = self.e.get("option_rt_none_noted") == today
        attempts = int(self.e.get("option_rt_attempts", 0))
        self.ledger.append("option_round_trip_pending", attempts=attempts, no_contract=no_contract)
        why = (f"no {self.config.option_round_trip_symbol} call qualified (a spread of "
               f"{self.config.option_round_trip_max_spread} or less, open interest 500 or more, one contract "
               f"inside the premium cap)" if no_contract and attempts == 0
               else f"{attempts} of {self.config.option_round_trip_attempts} attempts so far did not complete")
        self._card("the live option round trip did not run today", cards.LOOK,
                   [f"By {self.config.option_round_trip_window[1]} ET: {why}.",
                    "It tries again at the next session from "
                    f"{self.config.option_round_trip_window[0]} ET; native option copies wait for it."],
                   look_at="the live option round trip")
        self._save()

    def _cheap_contract(self, budget: Decimal):
        """The round trip's contract: a call on the round-trip symbol at the
        nearest expiry after today, struck above the market, with a bid, a
        spread at most ``option_round_trip_max_spread``, open interest of at
        least 500, a premium of at least 5 cents and one contract inside the
        premium cap; the dearest such (the nearest the money, the most liquid)."""
        symbol = self.config.option_round_trip_symbol
        _, spot, _ = self._quote(symbol)
        if spot <= 0:
            return None
        day = self._today()
        for _ in range(7):
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            rows = [r for r in self.reader.option_instruments(symbol, day, "call")
                    if spot < _d(r.get("strike_price")) <= spot * Decimal("1.05")]
            if not rows:
                continue
            quotes = self.reader.option_quotes([str(r["id"]) for r in rows])
            best = None
            for row in rows:
                quote = quotes.get(str(row["id"]), {})
                bid, ask = _d(quote.get("bid_price")), _d(quote.get("ask_price"))
                if bid <= 0 or ask < bid or ask - bid > self.config.option_round_trip_max_spread:
                    continue
                if quote.get("open_interest") is not None and int(quote["open_interest"]) < 500:
                    continue
                tick = option_tick(row, ask)
                limit = (ask / tick).to_integral_value(rounding=ROUND_UP) * tick
                if limit < Decimal("0.05") or limit * 100 > budget:
                    continue
                if best is None or limit > best[2]:
                    best = (row, quote, limit)
            if best is not None:
                return best
        return None

    def _option_round_trip_done(self, held: dict, done: dict) -> None:
        buy, sell = _d(held.get("entry_price")), done["price"]
        contracts = done["filled"]
        fees = _d(held.get("entry_fees")) + done["fees"]
        net = (sell - buy) * contracts * 100 - fees
        bid, ask = _d(held.get("quote_bid")), _d(held.get("quote_ask"))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else ZERO
        record = {"occ": held.get("symbol"), "contracts": contracts, "buy": buy, "sell": sell, "fees": fees,
                  "net": net, "quote_bid": bid, "quote_ask": ask, "passed": True,
                  "buy_vs_mid": (buy - mid) if mid else None}
        self.e["option_round_trip"] = "passed"
        self.ledger.append("option_round_trip", **record)
        self._card("live option round trip passed", cards.GOOD, [
            f"Bought and sold {contracts} {held.get('symbol')} through the gate: in at {buy}, out at {sell} a share "
            f"of premium; {cards.usd(float(net), signed=True)} after {_fees(fees)}.",
            f"Quote at entry {bid} x {ask}.",
            "Native option copies are on: a paper option whose contract fits the 2% premium cap is now copied as "
            "the option itself."])
        self._save()

    def _after_fill(self, working: dict, done: dict) -> None:
        extra, pid = working["extra"], working["extra"].get("pid")
        if working["kind"] == "entry" and done["filled"] > 0:
            plan = self.e["plans"].get(pid, {})
            price, copy = done["price"], extra.get("copy", "stock")
            multiplier = int(extra.get("multiplier") or 1)
            if copy in ("native", "proxy"):
                # the paper option's own UNDERLYING levels (0 = none: the paper close and the backstops exit it)
                stop, target = _d(extra.get("stop")), _d(extra.get("target"))
            else:
                stop = (price * (1 - _d(plan.get("stop_pct")) / 100)).quantize(CENTS)
                target = (price * (1 + _d(plan.get("target_pct")) / 100)).quantize(CENTS)
            position = {"symbol": working["symbol"], "quantity": str(done["filled"]), "entry_price": str(price),
                        "stop": str(stop), "target": str(target), "horizon": plan.get("horizon"),
                        "opened": self._today().isoformat(), "copy": copy, "entry_fees": str(done["fees"]),
                        "cost": str(done["filled"] * price * multiplier)}
            if copy in ("native", "proxy"):
                position.update(instrument=plan.get("instrument"), underlying=extra.get("underlying"),
                                paper_occ=plan.get("occ"), expiry=extra.get("expiry"))
            if copy == "native":
                position.update(option_id=extra.get("option_id"), multiplier=multiplier,
                                min_ticks=extra.get("min_ticks") or {}, quote_bid=extra.get("quote_bid"),
                                quote_ask=extra.get("quote_ask"))
            if extra.get("paper_closed"):
                position["paper_closed"] = extra["paper_closed"]
            self.e["positions"][pid] = position
            self.e.setdefault("copies_today", []).append({"pid": pid, "copy": copy, "symbol": working["symbol"]})
            unit = "a contract" if copy == "native" else "a share"
            what = {"native": "the paper option itself",
                    "proxy": f"a proxy for the paper's {plan.get('occ')} call (the option does not fit the premium cap)",
                    "stock": "the paper AI trader"}[copy]
            levels = (f"underlying stop {stop}, target {target}" if copy in ("native", "proxy") and stop > 0
                      else "exits with the paper option" if copy == "native" else f"stop {stop}, target {target}")
            self._card(f"bought {working['symbol']}", cards.GOOD, [
                f"Mirrored {what}: {done['filled']} {working['symbol']} at {price} {unit}"
                f"{' of premium a share' if copy == 'native' else ''} "
                f"({cards.usd(float(done['filled'] * price * multiplier))}).",
                f"{levels[0].upper() + levels[1:]}, horizon {plan.get('horizon')}; {_fees(done['fees'])}."])
        elif working["kind"] == "exit" and done["filled"] > 0:
            held = self.e["positions"].pop(pid, None) or {}
            multiplier = int(held.get("multiplier") or 1)
            pnl = done["filled"] * (done["price"] - _d(held.get("entry_price"))) * multiplier - done["fees"]
            closed = {"pid": pid, "pnl": str(pnl), "fees": str(done["fees"]), "copy": held.get("copy", "stock"),
                      "symbol": working["symbol"], "cost": held.get("cost"), "paper_occ": held.get("paper_occ")}
            self.e.setdefault("closed_today", []).append(closed)
            self._card(f"sold {working['symbol']}", cards.GOOD, [
                f"Closed {done['filled']} {working['symbol']} at {done['price']} a share ({extra.get('why')}).",
                f"Result {cards.usd(float(pnl), signed=True)} after {_fees(done['fees'])}."])
            if pid == OPTION_RT_PID:
                self._option_round_trip_done(held, done)
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
            if pos.get("copy") == "native":
                if self._native_exit(pid, pos, snap, now_local):
                    self._mark(marks)
                    return True
                continue
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

    def _native_exit(self, pid: str, pos: dict, snap: Snapshot, now_local: datetime) -> bool:
        """A natively copied option: out on the paper close, on the paper's own
        UNDERLYING stop or target (a call's on the bid or last trade, never the
        ask; a put's on the mid of a tight quote, else the last trade), at the
        option's expiry close or past its horizon (backstops: the paper close
        should come first). Sold as a limit at the contract's bid."""
        option_id = pos.get("option_id")
        held = (snap.options or {}).get(option_id, ZERO)
        if held <= 0:
            self.e["positions"].pop(pid)
            self.ledger.append("position_gone", pid=pid, symbol=pos["symbol"])
            return False
        contracts = min(_d(pos["quantity"]), held)
        why = None
        if pos.get("paper_closed"):
            why = f"the paper AI trader closed it ({pos['paper_closed']})"
        stop, target = _d(pos.get("stop")), _d(pos.get("target"))
        if why is None and pos.get("underlying") and (stop > 0 or target > 0):
            bid, ask, last = self._quote(pos["underlying"])
            if pos.get("instrument") == "put":
                tight = bid > 0 and ask >= bid and (ask - bid) / ((ask + bid) / 2) <= Decimal("0.02")
                price = (bid + ask) / 2 if tight else last
                if price > 0 and stop > 0 and price >= stop:
                    why = f"the underlying stop {stop} ({pos['underlying']} at {price})"
                elif price > 0 and target > 0 and price <= target:
                    why = f"the underlying target {target} ({pos['underlying']} at {price})"
            else:
                price = bid if bid > 0 else last
                if price > 0 and stop > 0 and price <= stop:
                    why = f"the underlying stop {stop} ({pos['underlying']} bid or last {price})"
                elif price > 0 and target > 0 and price >= target:
                    why = f"the underlying target {target} ({pos['underlying']} bid or last {price})"
        if why is None and self._option_close_due(pos, now_local):
            why = "the option's expiry close"
        if why is None and pos.get("horizon") == "intraday" and now_local.time() >= _clock_time(self.ai.intraday_close):
            why = "the intraday close"
        if why is None and self._past_horizon(pos):
            why = "past its horizon"
        if why is None:
            return False
        quote = self.reader.option_quotes([option_id]).get(option_id, {})
        bid = _d(quote.get("bid_price"))
        if bid <= 0:
            # nothing to sell into; the most it can lose is the premium paid
            noted, today = self.e.setdefault("no_bid_noted", {}), self._today().isoformat()
            if noted.get(pid) != today:
                noted[pid] = today
                self.ledger.append("no_option_bid", pid=pid, symbol=pos["symbol"], why=why)
                self._card(f"no bid to close {pos['symbol']}", cards.LOOK,
                           [f"The live contract is due out ({why}) but has no bid; it is retried every tick.",
                            "The most it can lose is the premium paid."], look_at="a live option with no bid")
                self._save()
            return False
        tick = option_tick({"min_ticks": pos.get("min_ticks") or {}}, bid)
        limit = max(tick, (bid / tick).to_integral_value(rounding=ROUND_DOWN) * tick)
        underlying, expiration, right, strike = occ_parts(pos["symbol"])
        order = OptionSellToCloseOrder(symbol=pos["symbol"], underlying=underlying, right=right,
                                       expiration=expiration, strike=strike, contracts=contracts,
                                       execution=LimitExecution(limit_price=limit))
        if self.dry:
            self.ledger.append("would_exit", pid=pid, symbol=pos["symbol"], quantity=contracts, why=why, limit=limit)
            self.e["positions"].pop(pid)
            return True
        self._send(order, "exit", extra={"pid": pid, "why": why, "option_id": option_id, "quote_bid": str(bid),
                                         "quote_ask": quote.get("ask_price")})
        return True

    def _option_close_due(self, pos: dict, now_local: datetime) -> bool:
        """The paper engine's rule: a same-day contract at the expiry-close
        time on expiry day, any other on the session before expiry."""
        if not pos.get("expiry"):
            return False
        expiry, opened, today = date.fromisoformat(pos["expiry"]), date.fromisoformat(pos["opened"]), self._today()
        close_day = expiry
        if expiry > opened:
            close_day = expiry - timedelta(days=1)
            while close_day.weekday() >= 5:
                close_day -= timedelta(days=1)
        return today > close_day or (today == close_day and now_local.time() >= _clock_time(self.ai.options.expiry_close))

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
        self.e.setdefault("skips_today", []).append({"pid": item["pid"], "symbol": plan.get("symbol"),
                                                     "instrument": plan.get("instrument"), "why": why})
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
            order, why, meta = self._size(item, plan, snap)
            if order is None:
                self._skip(item, plan, why)
                continue
            extra = {"pid": item["pid"], "ref": f"mirror-{item['pid']}", **meta}
            if self.dry:
                self.ledger.append("would_enter", pid=item["pid"], symbol=order.symbol,
                                   quantity=getattr(order, "quantity", None) or getattr(order, "contracts", None),
                                   style=order.execution.style, bound=order.execution.price_bound,
                                   **{k: v for k, v in meta.items() if k != "min_ticks"})
                return
            self._send(order, "entry", extra=extra)
            return

    def _size(self, item: dict, plan: dict, snap: Snapshot):
        """(order, why it is skipped, what the entry carries). An option goes
        native when one contract fits, as its stock when it is a call that does
        not (the proxy), and is skipped otherwise (the temporary option ruling,
        2026-11-09)."""
        instrument, symbol = plan.get("instrument"), str(plan.get("symbol") or "").upper()
        equity = snap.tradeable_equity   # live equity, less deposits still in transit
        budget = (equity * self.config.risk_fraction).quantize(CENTS, ROUND_DOWN)
        if instrument in ("call", "put"):
            return self._size_option(item, plan, equity, budget)
        if self._leveraged(plan, symbol):
            return None, "leveraged and inverse ETFs are not allowed in the live account", {}
        bid, ask, _ = self._quote(symbol)
        stop_fraction = _d(plan.get("stop_pct")) / 100
        if ask <= 0 or stop_fraction <= 0:
            return None, "no live quote or no stop distance", {}
        order, why = self._equity_buy(symbol, ask, budget / (ask * stop_fraction), equity)
        return order, why, {"copy": "stock"}

    def _leveraged(self, plan: dict, symbol: str) -> bool:
        factor = _d(plan.get("leverage") or "1")
        return plan.get("tag") in ("leveraged_etf", "inverse_etf") or factor != 1 \
            or symbol in {s.upper() for s in self.ai.leverage_factors}

    def _equity_buy(self, symbol: str, ask: Decimal, shares: Decimal, equity: Decimal):
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

    def _size_option(self, item: dict, plan: dict, equity: Decimal, budget: Decimal):
        occ = str(plan.get("occ") or "")
        if not occ:
            return None, "the paper option names no contract", {}
        underlying, expiration, right, strike = occ_parts(occ)
        multiplier = int(_d(item.get("multiplier") or plan.get("multiplier") or 100))
        contract = live = None
        rows = self.reader.option_instruments(underlying, expiration, right, strike)
        if len(rows) == 1:
            quote = self.reader.option_quotes([str(rows[0]["id"])]).get(str(rows[0]["id"]), {})
            ask = _d(quote.get("ask_price"))
            if ask > 0:
                tick = option_tick(rows[0], ask)
                limit = (ask / tick).to_integral_value(rounding=ROUND_UP) * tick
                live = {"row": rows[0], "limit": limit, "ask": ask, "bid": _d(quote.get("bid_price"))}
                contract = limit * multiplier
        if contract is None:   # no live quote: the paper's own price says whether it could fit
            contract = _d(item.get("paper_price")) * multiplier
        cost = f"one contract costs about {cards.usd(float(contract))}"
        if contract <= budget:
            if live is None:
                return None, f"{cost}, within the {cards.usd(float(budget))} premium cap, but it has no live quote", {}
            if self.e.get("option_round_trip") != "passed":
                return None, (f"{cost}, within the {cards.usd(float(budget))} premium cap, but the live option "
                              f"round trip has not passed yet"), {}
            contracts = int((budget / contract).to_integral_value(rounding=ROUND_DOWN))
            order = OptionBuyToOpenOrder(symbol=occ, underlying=underlying, right=right, expiration=expiration,
                                         strike=strike, contracts=contracts, multiplier=multiplier,
                                         execution=LimitExecution(limit_price=live["limit"]))
            return order, "", {"copy": "native", "option_id": str(live["row"]["id"]), "multiplier": multiplier,
                               "min_ticks": live["row"].get("min_ticks") or {}, "underlying": underlying,
                               "expiry": expiration.isoformat(), "stop": str(_d(plan.get("stop"))),
                               "target": str(_d(plan.get("target"))), "quote_bid": str(live["bid"]),
                               "quote_ask": str(live["ask"])}
        over = f"{cost}, above the {cards.usd(float(budget))} premium cap"
        if right == "put":
            return None, f"{over}; a put that does not fit is skipped (no shorting, no inverse ETFs live)", {}
        if not self.config.option_proxy_enabled:
            return None, f"{over}; the call proxy is off until its method is approved", {}
        return self._size_proxy(plan, underlying, expiration, over, equity, budget)

    def _size_proxy(self, plan: dict, underlying: str, expiration: date, over: str, equity: Decimal,
                    budget: Decimal):
        """A call too dear for the account, bought as its underlying stock: the
        same 2% of equity at risk to the paper option's own UNDERLYING stop,
        the paper's underlying target, the same horizon. Out when the paper
        option closes or on that stop or target, whichever first."""
        if self._leveraged(plan, underlying):
            return None, f"{over}; its underlying is a leveraged or inverse ETF, not allowed live", {}
        bid, ask, _ = self._quote(underlying)
        if ask <= 0:
            return None, f"{over}; no live quote for {underlying} to proxy it", {}
        stop = _d(plan.get("stop")) or (ask * (1 - _d(plan.get("stop_pct")) / 100)).quantize(CENTS, ROUND_DOWN)
        target = _d(plan.get("target")) or (ask * (1 + _d(plan.get("target_pct")) / 100)).quantize(CENTS, ROUND_UP)
        if stop <= 0 or ask <= stop:
            return None, f"{over}; {underlying} at {ask} is already at or below the paper option's stop {stop}", {}
        if ask >= target:
            return None, f"{over}; {underlying} at {ask} is already at or above the paper option's target {target}", {}
        order, why = self._equity_buy(underlying, ask, budget / (ask - stop), equity)
        if order is None:
            return None, f"{over}; as a proxy, {why}", {}
        return order, "", {"copy": "proxy", "underlying": underlying, "expiry": expiration.isoformat(),
                           "stop": str(stop), "target": str(target)}

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

    def _copy_rows(self) -> list[str]:
        """The day by kind of copy (temporary option ruling 2026-11-09, item 4):
        stock and ETF copies, native options and call proxies on their own
        rows, each closed proxy against the paper option it stood in for, and
        the skips with their reasons."""
        copies = self.e.get("copies_today", [])
        closed = self.e.get("closed_today", [])
        labels = {"stock": "Stock and ETF copies", "native": "Native option copies", "proxy": "Call proxies"}
        rows = []
        for kind, label in labels.items():
            opened = [c["symbol"] for c in copies if c.get("copy") == kind]
            shut = [c for c in closed if c.get("copy", "stock") == kind and c.get("pid") != OPTION_RT_PID]
            pnl = sum((_d(c["pnl"]) for c in shut), ZERO)
            rows.append(f"{label}: {len(opened)} opened" + (f" ({', '.join(opened)})" if opened else "")
                        + f", {len(shut)} closed" + (f" for {cards.usd(float(pnl), signed=True)}" if shut else "") + ".")
        for c in closed:
            if c.get("copy") != "proxy":
                continue
            cost, proceeds, done = _paper_trade(self.paper_ledger, c["pid"])
            live_cost = _d(c.get("cost"))
            live = (f"Proxy {c['symbol']} for {c.get('paper_occ')}: {cards.usd(float(_d(c['pnl'])), signed=True)}"
                    + (f" ({cards.pct(float(_d(c['pnl']) / live_cost))} on {cards.usd(float(live_cost))})"
                       if live_cost > 0 else ""))
            if done and cost > 0:
                paper = (f"the paper option {cards.usd(float(proceeds - cost), signed=True)} "
                         f"({cards.pct(float((proceeds - cost) / cost))} on {cards.usd(float(cost))})")
            else:
                paper = "the paper option is still open"
            rows.append(f"{live}, against {paper}.")
        skips = self.e.get("skips_today", [])
        rows.append(f"Skips: {len(skips)}" + (": " + "; ".join(f"{s['symbol']} {s.get('instrument')}: {s['why']}"
                                                             for s in skips) if skips else "") + ".")
        return rows

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
        lines += self._copy_rows()
        self.e["closed_today"], self.e["skipped_today"] = [], []
        self.e["copies_today"], self.e["skips_today"] = [], []
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


def _occ(underlying: str, expiration: date, right: str, strike: Decimal) -> str:
    return f"{underlying.upper()}{expiration:%y%m%d}{'C' if right == 'call' else 'P'}{int(_d(strike) * 1000):08d}"


def _order_fees(order: dict) -> Decimal:
    """The fees on one order: an equity order's ``fees``; an option order's
    ``fees`` or, failing that, its ``regulatory_fees``."""
    return _d(order.get("fees")) or _d(order.get("regulatory_fees"))


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


def _paper_trade(path: Path, pid: str) -> tuple[Decimal, Decimal, bool]:
    """One paper position's (cost, proceeds, closed): every fill of it."""
    cost = proceeds = ZERO
    closed = False
    if not path.exists():
        return cost, proceeds, closed
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if pid not in line:
                continue
            event = json.loads(line)
            if event.get("pid") != pid:
                continue
            if event.get("event") == "position_closed":
                closed = True
            elif event.get("event") == "settled":
                value = _d(event.get("filled_qty")) * _d(event.get("avg_price")) * _d(event.get("multiplier") or 1)
                if event.get("side") == "sell":
                    proceeds += value
                else:
                    cost += value
    return cost, proceeds, closed


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
