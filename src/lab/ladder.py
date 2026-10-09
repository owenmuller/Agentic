"""2a. THE LEVERAGE LADDER (PAPER PUSH, human ruling 2026-10-09 late).

"Buy and hold 1.5x SPY (SPY + SSO mix), 2x SPY (SSO), 2x QQQ (QLD). Rebalance
monthly. No other logic. This is the benchmark every sleeve must beat."

Three rungs, each an independent sub-book funded with one third of the
account's cash at its first run, so each rung's return is its own and the
scoreboard can grade every sleeve against each. A rung is bought at once
(the initial build) and then, at the first session of every month, traded
back to ``(1 - reserve) x weights`` of its own NAV: for the 1.5x rung that
restores the SPY/SSO half-and-half; for a single-ETF rung it invests the
dividends and residual cash. Nothing else - no signal, no LLM, no stop.

Mechanics
---------
* Every order passes the account's own RiskGate (BASELINE sleeve: cash-
  secured, dust-floored, bound by the allocation ceiling, refused under the
  kill switch). Marketable limits; sells first, then buys from the rung's own
  cash; an unfilled order is re-priced every ``reprice_seconds`` and abandoned
  after ``fill_wait_seconds`` - the month stays due and the next run retries.
* Books are a replay of the ledger (funding, settled fills, cash
  adjustments). Each run reconciles them to the broker: unexplained cash
  (dividends, fees) is allocated across rungs by NAV and logged; a share
  count that does not reconcile FREEZES trading (a human's problem).
* NO drawdown kill switch (ruling 2026-10-10, item 3a): the benchmark keeps
  rebalancing through drawdowns. Its trip is set at 100% of the high-water
  mark, which a cash account cannot reach. Instead, a drawdown of
  ``drawdown_alert`` (25%) from peak raises an urgent alert, once per peak.
  The operator halt marker still freezes it by hand: while halted (or if a
  switch were ever tripped) the ladder NEITHER buys NOR sells.
* Settlement recovery: an order the previous run left working is cancelled
  and its fills settled into the ledger before anything else happens.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from execution import AlpacaPriceSource, BrokerError
from execution.base import OrderStatus
from lab.account import LabAccount
from lab.config import LadderConfig, Rung
from risk_gate.gate import ApprovedOrder, RiskGate
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import Sleeve

ZERO = Decimal("0")
CENTS = Decimal("0.01")
STEP = Decimal("0.000001")
NY = ZoneInfo("America/New_York")
SLEEVE = Sleeve.BASELINE

logger = logging.getLogger("lab.ladder")


@dataclass
class Book:
    """One rung's sub-book."""

    cash: Decimal = ZERO
    qty: dict[str, Decimal] = field(default_factory=dict)

    def value(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + sum((q * marks[s] for s, q in self.qty.items() if q), ZERO)


@dataclass(frozen=True)
class Trade:
    rung: str
    symbol: str
    side: str  # "buy" | "sell"
    quantity: Decimal
    limit: Decimal
    modelled: Decimal  # the quote midpoint the plan was made at

    @property
    def notional(self) -> Decimal:
        return (self.quantity * self.limit).quantize(CENTS)


def replay_books(events: list[dict], rungs: tuple[Rung, ...]) -> dict[str, Book]:
    """Books from the ledger: funding, settled fills, cash adjustments."""
    books = {rung.name: Book() for rung in rungs}
    for event in events:
        kind = event.get("event")
        book = books.get(event.get("rung", ""))
        if book is None:
            continue
        if kind == "rung_funded" or kind == "cash_adjustment":
            book.cash += Decimal(event["amount"])
        elif kind == "settled":
            filled = Decimal(event["filled_qty"])
            if filled <= ZERO:
                continue
            value = filled * Decimal(event["avg_price"])
            sign = Decimal(1) if event["side"] == "buy" else Decimal(-1)
            book.qty[event["symbol"]] = book.qty.get(event["symbol"], ZERO) + sign * filled
            book.cash -= sign * value
    return books


def plan(
    books: dict[str, Book],
    rungs: tuple[Rung, ...],
    quotes: dict[str, tuple[Decimal, Decimal]],
    config: LadderConfig,
    side: str,
) -> list[Trade]:
    """The trades that bring each rung back to ``(1 - reserve) x weights``.

    ``side`` = "sell" plans the sells only; "buy" the buys only, each limited
    to the rung's own cash - the executor sells first, settles, then plans the
    buys from the books as they stand."""
    trades: list[Trade] = []
    buffer = config.limit_buffer
    for rung in rungs:
        book = books[rung.name]
        mids = {s: (b + a) / 2 for s, (b, a) in quotes.items()}
        nav = book.value(mids)
        investable = nav * (1 - config.cash_reserve_fraction)
        symbols = sorted(set(rung.weights) | {s for s, q in book.qty.items() if q > ZERO})
        cash = book.cash
        for symbol in symbols:
            bid, ask = quotes[symbol]
            mid = mids[symbol]
            held = book.qty.get(symbol, ZERO)
            gap = investable * rung.weights.get(symbol, ZERO) - held * mid
            if side == "sell" and gap < -config.min_trade_usd:
                limit = (bid * (1 - buffer)).quantize(CENTS, rounding=ROUND_DOWN)
                quantity = min(held, ((-gap) / mid).quantize(STEP, rounding=ROUND_DOWN))
                if quantity > ZERO and quantity * limit >= config.min_trade_usd:
                    trades.append(Trade(rung.name, symbol, "sell", quantity, limit, mid))
            elif side == "buy" and gap > config.min_trade_usd:
                limit = (ask * (1 + buffer)).quantize(CENTS, rounding=ROUND_UP)
                spend = min(gap, cash)
                quantity = (spend / limit).quantize(STEP, rounding=ROUND_DOWN)
                if quantity > ZERO and quantity * limit >= config.min_trade_usd:
                    trades.append(Trade(rung.name, symbol, "buy", quantity, limit, mid))
                    cash -= quantity * limit
    return trades


@dataclass
class _Working:
    trade: Trade
    approved: ApprovedOrder
    order_id: str
    submitted_at: float


class LadderEngine:
    """One run of the ladder (``python -m lab tick ladder``)."""

    def __init__(
        self,
        account: LabAccount,
        config: LadderConfig,
        *,
        quotes: Optional[Callable[[str], Optional[tuple[Decimal, Decimal]]]] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        dry_run: bool = False,
        alert: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.account = account
        self.config = config
        #: ``alert(subject, body)``: the urgent tier (the drawdown alert).
        self._alert = alert
        self._quotes = quotes or self._alpaca_quotes()
        self._sleep = sleep
        self._monotonic = monotonic
        self.dry_run = dry_run
        self.notes: list[str] = []
        self.gate: Optional[RiskGate] = None

    # -- quotes ------------------------------------------------------------------

    def _alpaca_quotes(self) -> Callable[[str], Optional[tuple[Decimal, Decimal]]]:
        source = AlpacaPriceSource(api_key=self.account.api_key, api_secret=self.account.api_secret)

        def read(symbol: str) -> Optional[tuple[Decimal, Decimal]]:
            ask, bid = source(symbol), source.bid(symbol)
            if ask is None or bid is None or bid <= ZERO or ask < bid:
                return None
            return bid, ask

        return read

    def _all_quotes(self) -> Optional[dict[str, tuple[Decimal, Decimal]]]:
        quotes = {}
        for symbol in self.config.symbols:
            quote = self._quotes(symbol)
            if quote is None:
                self._fault(f"no usable quote for {symbol}; no trading this run")
                return None
            quotes[symbol] = quote
        return quotes

    # -- the run -----------------------------------------------------------------

    def tick(self, now: datetime) -> list[str]:
        account = self.account
        if not self.dry_run:
            self._recover()
        positions = account.adapter.get_positions()
        cash = account.adapter.get_buying_power()
        self.gate = account.build_gate(cash, positions, SLEEVE)
        held = {p.symbol: p.quantity for p in positions}
        marks = {p.symbol: (p.market_value / p.quantity) for p in positions if p.quantity}
        stray = sorted(set(held) - set(self.config.symbols))
        if stray:
            self._fault(f"the account holds symbols the ladder does not trade: {stray}; frozen")
            return self._finish(marks, frozen=True)

        events = account.ledger.events()
        if not any(e["event"] == "rung_funded" for e in events):
            if positions:
                self._fault("first run but the account is not flat; the ladder starts from cash only; frozen")
                return self._finish(marks, frozen=True)
            funding = self._fund(cash)
            events = account.ledger.events() if not self.dry_run else events + funding

        books = replay_books(events, self.config.rungs)
        if not self._reconcile(books, cash, held, marks):
            return self._finish(marks, frozen=True)
        books = replay_books(account.ledger.events(), self.config.rungs) if not self.dry_run else books

        self.gate.mark_to_market({(SLEEVE.value, s): p for s, p in marks.items()})
        halt = account.halted()
        if halt and not self.gate.kill_switch_tripped:
            self.gate.trip_kill_switch(f"operator halt: {halt}")
            if not self.dry_run:
                account.persist_gate(self.gate, reason=f"operator halt marker: {halt}")
        if not self.dry_run:
            account.persist_gate(self.gate)
            self._drawdown_alert()
        if self.gate.kill_switch_tripped:
            self.notes.append(
                f"FROZEN: kill switch tripped ({account.state.trip_reason or 'halt'}); the ladder neither "
                f"buys nor sells until a human resets it"
            )
            return self._finish(marks, books=books, frozen=True)

        month = now.astimezone(NY).strftime("%Y-%m")
        if account.state.engine.get("rebalanced_month") == month:
            self.notes.append(f"{month} rebalance already done; snapshot only")
            return self._finish(marks, books=books)
        if not self._in_window(now):
            self.notes.append(f"{month} rebalance due; outside the trading window, waiting for the next run")
            return self._finish(marks, books=books)

        quotes = self._all_quotes()
        if quotes is None:
            return self._finish(marks, books=books)
        for side in ("sell", "buy"):
            trades = plan(books, self.config.rungs, quotes, self.config, side)
            if self.dry_run:
                self.notes.extend(f"DRY RUN would {t.side} {t.quantity} {t.symbol} for {t.rung} at limit {t.limit} "
                                  f"(mid {t.modelled:.4f}, {t.notional})" for t in trades)
                if side == "sell":
                    continue
                return self._finish(marks, books=books)
            self._execute(trades)
            books = replay_books(account.ledger.events(), self.config.rungs)
            quotes = self._all_quotes() or quotes
        remaining = plan(books, self.config.rungs, quotes, self.config, "sell") + plan(
            books, self.config.rungs, quotes, self.config, "buy"
        )
        if remaining:
            self.notes.append(f"{month} rebalance incomplete ({len(remaining)} trades left); retried next run")
        else:
            account.state.engine["rebalanced_month"] = month
            account.state.save(account.state_path)
            account.ledger.append("rebalanced", month=month)
            self.notes.append(f"{month} rebalance complete")
        positions = account.adapter.get_positions()
        marks = {p.symbol: (p.market_value / p.quantity) for p in positions if p.quantity}
        return self._finish(marks, books=books)

    # -- steps -------------------------------------------------------------------

    def _drawdown_alert(self) -> None:
        """Ruling 2026-10-10, item 3a: an urgent alert at ``drawdown_alert``
        from peak, once per peak (a new high-water mark re-arms it). The
        alert changes nothing the ladder does."""
        threshold = self.account.config.drawdown_alert
        state = self.gate.state
        if threshold is None:
            return
        drawdown = state.drawdown()
        peak = str(state.high_water_mark)
        if drawdown < threshold or self.account.state.engine.get("drawdown_alerted_peak") == peak:
            return
        message = (
            f"lab account {self.account.name}: NAV {state.nav:.2f} is {drawdown:.1%} below its peak "
            f"{state.high_water_mark:.2f} (alert at {threshold:.0%}). The ladder has no kill switch by "
            f"ruling and keeps rebalancing; this is information, not a halt."
        )
        self.account.ledger.append("drawdown_alert", drawdown=drawdown, nav=state.nav,
                                   high_water_mark=state.high_water_mark, threshold=threshold)
        self.account.state.engine["drawdown_alerted_peak"] = peak
        self.account.state.save(self.account.state_path)
        self.notes.append(f"DRAWDOWN ALERT: {message}")
        if self._alert is not None:
            self._alert(f"LAB {self.account.name} drawdown {drawdown:.1%} from peak", message)

    def _in_window(self, now: datetime) -> bool:
        clock = self.account.adapter.market_clock()
        if not clock.get("is_open"):
            return False
        local = now.astimezone(NY).time()
        after = dtime.fromisoformat(self.config.trade_after)
        before = dtime.fromisoformat(self.config.trade_before)
        return after <= local < before

    def _fund(self, cash: Decimal) -> list[dict]:
        """Fund each rung with an equal share of the starting cash (a dry run
        returns the funding without writing it)."""
        share = (cash / len(self.config.rungs)).quantize(CENTS, rounding=ROUND_DOWN)
        funding = [dict(event="rung_funded", rung=rung.name, amount=str(share), label=rung.label)
                   for rung in self.config.rungs]
        if not self.dry_run:
            for event in funding:
                self.account.ledger.append(**event)
        self.notes.append(f"funded {len(self.config.rungs)} rungs with {share} each from {cash} cash")
        return funding

    def _reconcile(self, books: dict[str, Book], cash: Decimal, held: dict[str, Decimal],
                   marks: dict[str, Decimal]) -> bool:
        booked: dict[str, Decimal] = {}
        for book in books.values():
            for symbol, qty in book.qty.items():
                booked[symbol] = booked.get(symbol, ZERO) + qty
        for symbol in set(booked) | set(held):
            gap = held.get(symbol, ZERO) - booked.get(symbol, ZERO)
            if abs(gap) > Decimal("0.0001"):
                self._fault(
                    f"{symbol}: broker holds {held.get(symbol, ZERO)}, the rung books {booked.get(symbol, ZERO)}; "
                    f"trading frozen until a human reconciles"
                )
                return False
        residual = cash - sum((b.cash for b in books.values()), ZERO)
        if abs(residual) >= CENTS and any(e["event"] == "rung_funded" for e in self.account.ledger.events()):
            values = {name: max(book.value({s: marks.get(s, ZERO) for s in book.qty}), ZERO)
                      for name, book in books.items()}
            total = sum(values.values(), ZERO)
            names = list(books)
            allotted = ZERO
            for i, name in enumerate(names):
                if i == len(names) - 1:
                    amount = residual - allotted
                else:
                    share = values[name] / total if total > ZERO else Decimal(1) / len(names)
                    amount = (residual * share).quantize(CENTS, rounding=ROUND_DOWN)
                allotted += amount
                if not self.dry_run and amount:
                    self.account.ledger.append(
                        "cash_adjustment", rung=name, amount=amount,
                        reason="broker cash not explained by fills (dividends, fees), allocated by rung NAV",
                    )
            self.notes.append(f"cash adjustment {residual} allocated across rungs by NAV")
        return True

    def _recover(self) -> None:
        """Settle what a previous run left working: cancel, wait, record."""
        ledger = self.account.ledger
        settled = {e["order_id"] for e in ledger.events(["settled"])}
        for event in ledger.events(["order"]):
            order_id = event["order_id"]
            if order_id in settled:
                continue
            status = self._terminal(order_id)
            ledger.append(
                "settled", order_id=order_id, rung=event["rung"], symbol=event["symbol"], side=event["side"],
                filled_qty=status.filled_quantity, avg_price=status.filled_avg_price or ZERO,
                status=status.status, limit=event["limit"], modelled=event["modelled"], recovered=True,
            )
            self.notes.append(f"recovered order {order_id}: {status.status}, filled {status.filled_quantity}")

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

    def _submit(self, trade: Trade) -> Optional[_Working]:
        if trade.side == "buy":
            order = EquityBuyOrder(symbol=trade.symbol, quantity=trade.quantity,
                                   execution=LimitExecution(limit_price=trade.limit), sleeve=SLEEVE.value)
        else:
            order = EquitySellToCloseOrder(symbol=trade.symbol, quantity=trade.quantity,
                                           execution=LimitExecution(limit_price=trade.limit), sleeve=SLEEVE.value)
        decision = self.gate.submit(order)
        if not decision.is_approved:
            self.account.ledger.append("gate_rejected", rung=trade.rung, symbol=trade.symbol, side=trade.side,
                                       quantity=trade.quantity, limit=trade.limit, code=str(decision.code),
                                       message=decision.message)
            self._fault(f"gate rejected {trade.side} {trade.symbol} for {trade.rung}: {decision.code}")
            return None
        reference = f"lab-{self.account.name}-{uuid.uuid4().hex[:10]}"
        try:
            receipt = self.account.adapter.submit_order(decision, client_reference=reference)
        except BrokerError as error:
            self.gate.cancel(decision)
            self._fault(f"broker refused {trade.side} {trade.symbol} for {trade.rung}: {error}")
            return None
        self.account.ledger.append("order", order_id=receipt.broker_order_id, rung=trade.rung, symbol=trade.symbol,
                                   side=trade.side, quantity=trade.quantity, limit=trade.limit,
                                   modelled=trade.modelled, client_reference=reference)
        return _Working(trade, decision, receipt.broker_order_id, self._monotonic())

    def _settle(self, working: _Working, status: OrderStatus) -> Decimal:
        filled = status.filled_quantity
        if filled > ZERO and status.filled_avg_price is not None:
            self.gate.record_fill(working.approved, status.filled_avg_price, filled_units=filled)
        else:
            self.gate.cancel(working.approved)
        trade = working.trade
        self.account.ledger.append(
            "settled", order_id=working.order_id, rung=trade.rung, symbol=trade.symbol, side=trade.side,
            filled_qty=filled, avg_price=status.filled_avg_price or ZERO, status=status.status,
            limit=trade.limit, modelled=trade.modelled,
        )
        return filled

    def _execute(self, trades: list[Trade]) -> None:
        deadline = self._monotonic() + self.config.fill_wait_seconds
        working = [w for w in (self._submit(t) for t in trades) if w is not None]
        while working:
            self._sleep(5)
            still: list[_Working] = []
            for item in working:
                status = self.account.adapter.get_order(item.order_id)
                if status.is_terminal:
                    self._settle(item, status)
                    continue
                expired = self._monotonic() >= deadline
                stale = self._monotonic() - item.submitted_at >= self.config.reprice_seconds
                if not (expired or stale):
                    still.append(item)
                    continue
                filled = self._settle(item, self._terminal(item.order_id))
                left = (item.trade.quantity - filled).quantize(STEP, rounding=ROUND_DOWN)
                if expired:
                    self._fault(f"{item.trade.side} {item.trade.symbol} for {item.trade.rung} unfilled after "
                                f"{self.config.fill_wait_seconds}s; {left} left for the next run")
                    continue
                quote = self._quotes(item.trade.symbol)
                if quote is None or left <= ZERO:
                    continue
                bid, ask = quote
                buffer = self.config.limit_buffer
                limit = ((ask * (1 + buffer)).quantize(CENTS, rounding=ROUND_UP) if item.trade.side == "buy"
                         else (bid * (1 - buffer)).quantize(CENTS, rounding=ROUND_DOWN))
                if item.trade.side == "buy":
                    left = min(left, (self.gate.state.buying_power / limit).quantize(STEP, rounding=ROUND_DOWN))
                if left * limit < self.config.min_trade_usd:
                    continue
                again = self._submit(Trade(item.trade.rung, item.trade.symbol, item.trade.side, left, limit,
                                           (bid + ask) / 2))
                if again is not None:
                    still.append(again)
            working = still

    def _finish(self, marks: dict[str, Decimal], books: Optional[dict[str, Book]] = None,
                frozen: bool = False) -> list[str]:
        account = self.account
        snap = account.adapter.account_snapshot()
        rungs = {}
        if books is not None:
            rungs = {name: book.value({s: marks.get(s, ZERO) for s in book.qty}).quantize(CENTS)
                     for name, book in books.items()}
        gate = self.gate
        record = dict(
            equity=snap.get("equity"), cash=snap.get("cash"), marks=marks, rungs=rungs, frozen=frozen,
            high_water_mark=gate.state.high_water_mark if gate else None,
            drawdown=gate.state.drawdown() if gate else None,
            kill_switch=gate.kill_switch_tripped if gate else None,
        )
        if not self.dry_run:
            account.ledger.append("snapshot", **record)
        self.notes.append(
            f"equity {snap.get('equity')}, rungs " + ", ".join(f"{k} {v}" for k, v in rungs.items())
            + (" [FROZEN]" if frozen else "")
        )
        return self.notes

    def _fault(self, message: str) -> None:
        logger.warning("LAB %s: %s", self.account.name, message)
        self.notes.append(f"FAULT: {message}")
        if not self.dry_run:
            self.account.ledger.append("fault", message=message)
