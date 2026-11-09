"""The AI trader's live mirror on Robinhood Agentic (GO LIVE, human ruling
2026-11-04): the sequence (funds -> round trip -> mirroring from the next
session), the sizing and skip rules, exits on the bid or last trade, the
kill switch on starting equity and the daily loss limit, dry mode, and the
adapter's and the environment's refusals."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from execution import cards
from execution.base import OrderReceipt, OrderStatus
from lab.account import LabState, Ledger
from lab.config import AccountConfig, LabConfig, account_limits
from lab.live_mirror import LiveMirror

D = Decimal
#: Wednesday 2026-11-04, 10:00 New York (EST).
T0 = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Reader:
    def __init__(self, cash="1100", total=None, kind="cash", unsettled="0", pending="0"):
        self.cash, self.total, self.kind, self.unsettled, self.pending = cash, total or cash, kind, unsettled, pending
        self.held: dict[str, str] = {}
        self.book = {"SPY": ("769.90", "770.10", "770.00"), "UNH": ("399.80", "400.00", "399.90"),
                     "F": ("39.95", "40.00", "39.97")}
        self.approvals = False
        self.fees: dict[str, str] = {}
        self.contracts: dict[str, dict] = {}       # option instrument id -> its row
        self.option_book: dict[str, tuple] = {}    # option instrument id -> (bid, ask)
        self.option_held: dict[str, str] = {}      # option instrument id -> contracts

    def add_contract(self, option_id, occ, bid, ask, open_interest=1000):
        from execution.robinhood import occ_parts

        underlying, expiration, right, strike = occ_parts(occ)
        self.contracts[option_id] = {"id": option_id, "chain_symbol": underlying, "type": right,
                                     "expiration_date": expiration.isoformat(), "strike_price": f"{strike:.4f}",
                                     "tradability": "tradable",
                                     "min_ticks": {"above_tick": "0.05", "below_tick": "0.01", "cutoff_price": "3.00"}}
        self.option_book[option_id] = (bid, ask, open_interest)

    def option_instruments(self, underlying, expiration, right, strike=None):
        return [r for r in self.contracts.values()
                if r["chain_symbol"] == underlying and r["expiration_date"] == expiration.isoformat()
                and r["type"] == right and (strike is None or D(r["strike_price"]) == D(strike))]

    def option_quotes(self, ids):
        return {i: {"instrument_id": i, "bid_price": self.option_book[i][0], "ask_price": self.option_book[i][1],
                    "open_interest": self.option_book[i][2]} for i in ids if i in self.option_book}

    def option_positions(self):
        return [{"option_id": i, "quantity": q, "type": "long", "chain_symbol": self.contracts[i]["chain_symbol"]}
                for i, q in self.option_held.items() if D(q) > 0]

    def option_orders(self, **filters):
        return [{"id": filters.get("order_id"), "fees": self.fees.get(filters.get("order_id"), "0")}]

    def portfolio(self):
        return {"cash": self.cash, "total_value": self.total, "pending_deposits": self.pending}

    def account_row(self):
        return {"account_number": "742288012", "type": self.kind, "unsettled_funds": self.unsettled,
                "agentic_allowed": True}

    def equity_positions(self):
        return [{"symbol": s, "quantity": q} for s, q in self.held.items()]

    def quotes(self, symbols):
        return {s: {"symbol": s, "bid_price": self.book[s][0], "ask_price": self.book[s][1],
                    "last_trade_price": self.book[s][2]} for s in symbols if s in self.book}

    def approvals_required(self):
        return self.approvals

    def equity_orders(self, **filters):
        return [{"id": filters.get("order_id"), "fees": self.fees.get(filters.get("order_id"), "0")}]


class Broker:
    """Fills every order at the quote's ask (buys) or bid (sells)."""

    def __init__(self, reader):
        self.reader, self.orders, self.sent = reader, {}, []
        self.last_review = {"order_checks": {}}

    def submit_order(self, approved, client_reference=None):
        order = approved.order
        self.sent.append(order)
        oid = f"o{len(self.sent)}"
        if hasattr(order, "contracts"):                  # a long option: fills at the option's ask or bid
            [row] = self.reader.option_instruments(order.underlying, order.expiration, order.right, order.strike)
            bid, ask, _ = self.reader.option_book[row["id"]]
            buy = type(order).__name__ == "OptionBuyToOpenOrder"
            price = D(ask if buy else bid)
            self.orders[oid] = OrderStatus(oid, "filled", order.contracts, price)
            held = D(self.reader.option_held.get(row["id"], "0")) + (order.contracts if buy else -order.contracts)
            self.reader.option_held[row["id"]] = str(held)
            value = order.contracts * price * 100
            self.reader.cash = str(D(self.reader.cash) + (-value if buy else value))
            return OrderReceipt(oid, "queued", order.symbol, order.contracts, order.execution.price_bound,
                                client_reference)
        bid, ask, _ = self.reader.book[order.symbol]
        buy = type(order).__name__ == "EquityBuyOrder"
        self.orders[oid] = OrderStatus(oid, "filled", order.quantity, D(ask if buy else bid))
        held = D(self.reader.held.get(order.symbol, "0")) + (order.quantity if buy else -order.quantity)
        self.reader.held[order.symbol] = str(held)
        value = order.quantity * D(ask if buy else bid)     # a fill moves the broker's cash
        self.reader.cash = str(D(self.reader.cash) + (-value if buy else value))
        return OrderReceipt(oid, "queued", order.symbol, order.quantity, order.execution.price_bound, client_reference)

    def get_order(self, oid):
        return self.orders[oid]

    def cancel_order(self, oid):
        pass


@pytest.fixture
def config():
    return LabConfig.load()


def build(tmp_path, config, reader, broker="live", clock=None):
    clock = clock or Clock()
    live = config.live_mirror
    limits = account_limits(AccountConfig(enabled=True, label="LIVE", sleeve="live_mirror",
                                          limits_overrides=live.limits_overrides))
    seen: list = []
    mirror = LiveMirror(config=live, ai_config=config.ai_trader, reader=reader,
                        broker=Broker(reader) if broker == "live" else None,
                        paper_ledger=tmp_path / "paper.jsonl", ledger=Ledger(tmp_path / "ledger.jsonl", clock),
                        state=LabState(), state_path=tmp_path / "state.json", limits=limits, notify=seen.append,
                        clock=clock, halt_path=tmp_path / "HALT")
    mirror.config_sleep = lambda: None
    return mirror, seen, clock


def paper(tmp_path, *events):
    with open(tmp_path / "paper.jsonl", "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def opened(pid, symbol, instrument="stock", stop="2.5", target="6.5", horizon="days", **extra):
    return {"event": "position_opened", "pid": pid, "symbol": symbol, "instrument": instrument, "stop_pct": stop,
            "target_pct": target, "horizon": horizon, "at": T0.isoformat(), **extra}


def filled(pid, price="400", qty="10", mult=1):
    return {"event": "settled", "pid": pid, "side": "buy", "reason": "entry", "filled_qty": qty, "avg_price": price,
            "multiplier": mult, "at": T0.isoformat()}


def mirroring(tmp_path, config, reader=None):
    """Funded, round trip passed yesterday, mirroring today."""
    reader = reader or Reader()
    mirror, seen, clock = build(tmp_path, config, reader)
    mirror.start()
    mirror.tick()                                    # round trip on the funded account
    assert mirror.e["phase"] == "armed"
    clock.now = T0 + timedelta(days=1)
    mirror.gate = None                               # a new session
    mirror.tick()
    assert mirror.e["phase"] == "mirroring"
    seen.clear()
    return mirror, seen, clock, reader


def test_it_waits_for_the_funding_then_round_trips_then_mirrors_from_the_next_session(tmp_path, config):
    reader = Reader(cash="100")
    mirror, seen, clock = build(tmp_path, config, reader)
    mirror.start()
    mirror.tick()
    assert mirror.e["phase"] == "waiting_funds" and mirror.broker.sent == []
    reader.cash, reader.pending = "1100", "1000"       # in transit: $100 settled, under the $500 minimum
    mirror.tick()
    assert mirror.e["phase"] == "waiting_funds"
    reader.pending = "0"
    mirror.tick()
    sent = mirror.broker.sent
    assert [type(o).__name__ for o in sent] == ["EquityBuyOrder", "EquitySellToCloseOrder"]
    assert sent[0].symbol == "SPY" and sent[0].quantity == D("0.01") and sent[0].execution.style == "market_buy"
    assert sent[1].quantity == D("0.01") and sent[1].execution.style == "market_sell"
    assert mirror.e["phase"] == "armed" and seen[-1].happened == "live round trip passed"
    assert seen[-1].channel == cards.URGENT
    mirror.tick()                                     # the same day: still not mirroring
    assert mirror.e["phase"] == "armed"
    paper(tmp_path, opened("old", "UNH"), filled("old"))   # history before mirroring is never copied
    clock.now = T0 + timedelta(days=1)
    mirror.tick()
    assert mirror.e["phase"] == "mirroring" and mirror.e["urgent_until"] == "2026-11-12"
    mirror.tick()
    assert len(mirror.broker.sent) == 2


def test_entries_are_sized_whole_shares_first_then_fractional(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    order = mirror.broker.sent[-1]
    # 2% of 1100 = 22 at risk; 22 / (40 x 2.5%) = 22 shares, capped at 18.75% of 1100 / 40 = 5.15 -> 5 whole
    assert order.symbol == "F" and order.quantity == D("5") and order.execution.style == "limit"
    assert order.execution.limit_price == D("40.08")
    mirror.tick()                                     # settles
    assert "a" in mirror.e["positions"] and mirror.e["positions"]["a"]["stop"] == "39.00"
    paper(tmp_path, opened("b", "UNH"), filled("b"))
    mirror.tick()
    order = mirror.broker.sent[-1]
    # the cap (1100 x 18.75% x 0.99 = 204.19) over the gate's bound (ask 400 + 5% = 420): 0.486160
    # shares, under one -> a fractional market buy the gate can approve
    assert order.symbol == "UNH" and order.quantity == D("0.486160") and order.execution.style == "market_buy"
    assert "fractional" in order.execution.justification
    assert any(c.happened == "bought F" for c in seen)


@pytest.mark.parametrize("event, why", [
    (opened("s", "SOXS", instrument="etf", tag="inverse_etf", leverage="-3"), "leveraged and inverse"),
    (opened("o", "INTC", instrument="put", occ="INTC261106P00065000"), "premium cap"),
])
def test_leveraged_funds_and_options_over_the_cap_are_skipped_and_carded(tmp_path, config, event, why):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    paper(tmp_path, event, filled(event["pid"], price="2.77", mult=100 if event["instrument"] == "put" else 1))
    mirror.tick()
    assert len(mirror.broker.sent) == 2              # only the round trip
    skips = [e for e in mirror.ledger.events(["skip"])]
    assert skips and why in skips[-1]["why"]
    assert seen[-1].status == cards.LOOK and seen[-1].channel == cards.URGENT


def test_a_paper_close_closes_the_live_copy_and_stops_watch_the_bid_never_the_ask(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    mirror.tick()
    stop = D(mirror.e["positions"]["a"]["stop"])      # 39.00 from the 40.00 fill
    reader.book["F"] = ("39.20", "39.30", "38.90")    # bid above the stop: no exit (last is not used then)
    mirror.tick()
    assert type(mirror.broker.sent[-1]).__name__ == "EquityBuyOrder"
    reader.book["F"] = ("38.99", "39.30", "39.10")    # the bid at the stop: exit
    mirror.tick()
    sell = mirror.broker.sent[-1]
    assert type(sell).__name__ == "EquitySellToCloseOrder" and sell.execution.limit_price == D("38.99")
    assert sell.quantity == D("5") and stop == D("39.00")
    # and a paper close closes a copy whatever the price
    reader.book["F"] = ("40.00", "40.05", "40.00")
    mirror.tick()
    paper(tmp_path, opened("c", "F"), filled("c", price="40"))
    mirror.tick()
    mirror.tick()
    paper(tmp_path, {"event": "position_closed", "pid": "c", "reason": "target", "at": T0.isoformat()})
    mirror.tick()
    assert type(mirror.broker.sent[-1]).__name__ == "EquitySellToCloseOrder"


def test_the_kill_switch_on_starting_equity_and_the_daily_loss_limit_block_entries_only(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    start = D(mirror.e["starting_equity"])
    reader.total = str(start * D("0.95"))           # 5% down today: the daily limit (4%)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    assert len(mirror.broker.sent) == 2 and "daily loss limit" in mirror.ledger.events(["skip"])[-1]["why"]
    reader.total = str(start * D("0.87"))           # 13% below starting equity: the kill switch
    paper(tmp_path, opened("b", "F"), filled("b", price="40"))
    mirror.tick()
    assert mirror.state.kill_switch_tripped and "kill switch" in mirror.ledger.events(["skip"])[-1]["why"]
    assert any(c.status == cards.ACTION and "kill switch" in c.happened for c in seen)


def test_a_late_copy_is_skipped_not_chased(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    mirror._queue.append({"pid": "z", "at": (clock() - timedelta(minutes=10)).isoformat(), "paper_price": "40"})
    mirror.e["plans"]["z"] = {"symbol": "F", "instrument": "stock", "stop_pct": "2.5", "target_pct": "6.5"}
    mirror.tick()
    assert "late" in mirror.ledger.events(["skip"])[-1]["why"]


def test_dry_mode_sizes_and_logs_and_sends_nothing(tmp_path, config):
    reader = Reader()
    mirror, seen, clock = build(tmp_path, config, reader, broker=None)
    mirror.start()
    mirror.tick()
    assert mirror.e["phase"] == "waiting_funds" and mirror.ledger.events(["dry"])
    mirror.e.update(phase="mirroring", starting_equity="1100", paper_offset=0)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    would = mirror.ledger.events(["would_enter"])
    assert would and would[-1]["symbol"] == "F" and seen == []


def test_trade_approvals_or_a_non_cash_account_stop_everything(tmp_path, config):
    reader = Reader()
    reader.approvals = True
    mirror, seen, clock = build(tmp_path, config, reader)
    mirror.start()
    mirror.tick()
    assert mirror.broker.sent == [] and seen[-1].status == cards.ACTION
    with pytest.raises(RuntimeError, match="not cash"):
        build(tmp_path / "m", config, Reader(kind="limited_margin"))[0].snapshot()


# ================================================================================
# The adapter and the environment
# ================================================================================


def test_the_adapter_cannot_be_built_in_paper(monkeypatch, tmp_path):
    from execution import robinhood

    monkeypatch.setenv("PAPER_MODE", "true")
    mcp = robinhood.RobinhoodMcp(robinhood.TokenStore(tmp_path / "x.json"), "742288012", allow_orders=True)
    with pytest.raises(robinhood.RobinhoodError, match="PAPER"):
        robinhood.RobinhoodAgenticAdapter(mcp)


def test_settled_cash_holds_back_unsettled_funds_and_pending_deposits():
    from execution.robinhood import settled_cash

    assert settled_cash({"cash": "1100", "pending_deposits": "1000"}, {"unsettled_funds": "0"}) == D("100")
    assert settled_cash({"cash": "1100", "pending_deposits": "0"}, {"unsettled_funds": "50"}) == D("1050")


def test_a_shared_dotenv_may_never_carry_a_live_flag(tmp_path, monkeypatch):
    from execution.environment import LiveModeMisconfigured, load_environment

    monkeypatch.delenv("PAPER_MODE", raising=False)
    safe = tmp_path / "safe.env"
    safe.write_text("PAPER_MODE=true\nSOME_KEY=x\n")
    load_environment(safe)
    for text in ("PAPER_MODE=false\n", 'LIVE_TRADING_CONFIRMED="anything"\n'):
        live = tmp_path / "live.env"
        live.write_text(text)
        with pytest.raises(LiveModeMisconfigured, match="only the live mirror"):
            load_environment(live)


def test_the_cash_account_is_exempt_from_the_day_trade_limit_and_margin_never_is(config):
    # human ruling 2026-11-04: settled funds are the cash account's limit
    from datetime import date

    from risk_gate.gate import RiskGate
    from risk_gate.schema import EquityBuyOrder, LimitExecution
    from risk_gate.state import AccountState, AccountType, Sleeve

    live = config.live_mirror
    limits = account_limits(AccountConfig(enabled=True, label="LIVE", sleeve="live_mirror",
                                          limits_overrides=live.limits_overrides))
    assert limits.pdt.aggressive_limit_on_cash_accounts is False
    shipped = account_limits(config.account("ai-trader"))
    assert shipped.pdt.aggressive_limit_on_cash_accounts is True       # every other account keeps the rule
    clock = Clock(datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc))
    order = EquityBuyOrder(symbol="F", quantity=D("5"), sleeve=Sleeve.AGGRESSIVE,
                           execution=LimitExecution(limit_price=D("12.00")))

    def gate(kind, table):
        state = AccountState(cash=D("1000"), high_water_mark=D("1000"), account_type=kind)
        state.day_trades = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]
        return RiskGate(table, state, clock)

    assert gate(AccountType.CASH, limits).submit(order).is_approved
    assert gate(AccountType.MARGIN, limits).submit(order).code == "pdt_limit_reached"
    assert gate(AccountType.CASH, shipped).submit(order).code == "pdt_limit_reached"


# ================================================================================
# Deposits and withdrawals are cash flows (human ruling 2026-11-04, second)
# ================================================================================


def test_a_deposit_moves_the_baselines_and_is_never_gain(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    start, hwm = D(mirror.e["starting_equity"]), D(mirror.state.high_water_mark)
    clock.now += timedelta(minutes=5)
    reader.cash = str(D(reader.cash) + 300)
    reader.total = str(D(reader.total) + 300)
    mirror.tick()
    flow = mirror.ledger.events(["cash_flow"])[-1]
    assert flow["kind"] == "deposit" and D(flow["amount"]) == D("300")
    assert D(mirror.e["starting_equity"]) == start + 300 and D(mirror.state.high_water_mark) == hwm + 300
    assert any(c.happened == "a deposit of $300" for c in seen)
    pnl, base, rate = mirror.day_return(D(reader.total))
    assert pnl == D(reader.total) - D(mirror.e["day_start_equity"]) - 300 and base > D(mirror.e["day_start_equity"])
    # the kill switch reads the raised baseline: a fall that is 12% of it trips, not 12% of the old one
    reader.total = str((start + 300) * D("0.87"))
    paper(tmp_path, opened("k", "F"), filled("k", price="40"))
    mirror.tick()
    assert "kill switch" in mirror.ledger.events(["skip"])[-1]["why"]


def test_a_withdrawal_lowers_the_baselines_and_the_gates_cash_and_fills_are_not_flows(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    mirror.tick()                                     # the buy fills; cash moves by the fill
    clock.now += timedelta(minutes=2)
    mirror.tick()
    assert mirror.ledger.events(["cash_flow"]) == []  # the mirror's own fill is not a flow
    gate_cash = mirror.gate.state.cash
    reader.cash = str(D(reader.cash) - 200)
    reader.total = str(D(reader.total) - 200)
    mirror.tick()
    flow = mirror.ledger.events(["cash_flow"])[-1]
    assert flow["kind"] == "withdrawal" and D(flow["amount"]) == D("-200")
    assert mirror.gate.state.cash == gate_cash - 200


def test_sizing_scales_with_live_equity_less_deposits_in_transit(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    from lab.live_mirror import Snapshot

    plan = {"symbol": "F", "instrument": "stock", "stop_pct": "2.5"}
    small = Snapshot(equity=D("600"), settled_cash=D("600"), unsettled=D(0), pending_deposits=D(0), held={})
    big = Snapshot(equity=D("2000"), settled_cash=D("1000"), unsettled=D(0), pending_deposits=D("1000"), held={})
    assert mirror._size({"pid": "a"}, plan, small)[0].quantity == D("2")     # 600 x 18.75% x 0.99 / 40.08
    assert mirror._size({"pid": "b"}, plan, big)[0].quantity == D("4")       # sized on 1000, not 2000


# ================================================================================
# Transient broker failures (2026-11-05: an HTTP 502 on the MCP failed a live tick)
# ================================================================================


class FlakyBroker(Broker):
    """The next place call fails mid-request; ``acted`` says whether Robinhood
    took the order anyway (then it is in its order list under its reference)."""

    def __init__(self, reader, acted):
        super().__init__(reader)
        self.acted, self.flaky, self.by_ref = acted, True, {}

    def submit_order(self, approved, client_reference=None):
        from execution.base import OrderOutcomeUnknown

        if not self.flaky:
            return super().submit_order(approved, client_reference)
        self.flaky = False
        ref = client_reference or f"ref{len(self.sent) + 1}"
        if self.acted:
            receipt = super().submit_order(approved, ref)
            self.by_ref[ref] = receipt.broker_order_id
        else:
            self.sent.append(approved.order)
        raise OrderOutcomeUnknown(ref, "place_equity_order: MCP HTTP 502 on tools/call")

    def get_order_by_client_reference(self, ref):
        oid = self.by_ref.get(ref)
        return self.orders[oid] if oid else None


def flaky_mirroring(tmp_path, config, acted):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    mirror.broker = FlakyBroker(reader, acted)
    mirror.broker.sent = [None, None]                  # the round trip's two orders
    return mirror, seen, clock, reader


def test_an_order_robinhood_took_despite_a_502_is_adopted_never_resent(tmp_path, config):
    mirror, seen, clock, reader = flaky_mirroring(tmp_path, config, acted=True)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    assert len(mirror.broker.sent) == 3 and mirror.e["working"]["order_id"] is None
    assert any("outcome is unknown" in c.happened for c in seen)
    clock.now += timedelta(seconds=30)
    mirror.tick()                                      # found by its reference, settled, tracked
    assert len(mirror.broker.sent) == 3
    assert "a" in mirror.e["positions"] and "working" not in mirror.e
    assert mirror.ledger.events(["order_found"]) and not mirror.ledger.events(["cash_flow"])
    assert not mirror.e.get("unwatched")


def test_an_order_robinhood_never_took_is_released_after_the_window(tmp_path, config):
    mirror, seen, clock, reader = flaky_mirroring(tmp_path, config, acted=False)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    assert mirror.gate.state.reserved_cash > 0            # held while the outcome is unknown
    clock.now += timedelta(seconds=60)
    mirror.tick()                                      # inside the window: still watched, nothing new sent
    assert mirror.e["working"]["ref"] and len(mirror.broker.sent) == 3
    clock.now += timedelta(seconds=config.live_mirror.unknown_order_seconds)
    mirror.tick()
    assert "working" not in mirror.e and mirror.ledger.events(["not_placed"])
    assert any("never placed" in c.happened for c in seen)
    assert len(mirror.broker.sent) == 3 and "a" not in mirror.e["positions"]
    assert not mirror.ledger.events(["cash_flow"]) and mirror.gate.state.reserved_cash == 0


def test_a_restart_cancels_a_working_order_an_earlier_run_left(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    mirror.broker.orders["rh-9"] = OrderStatus("rh-9", "queued", D("0"))
    cancelled = []
    mirror.broker.cancel_order = cancelled.append
    mirror.e["working"] = {"order_id": "rh-9", "ref": "r9", "kind": "entry", "symbol": "F",
                           "placed": clock().isoformat(), "pid": "a"}
    mirror.gate = None                                 # the process restarted
    mirror.tick()
    assert cancelled == ["rh-9"] and "working" not in mirror.e
    assert mirror.ledger.events(["stray_order"])[-1]["outcome"].startswith("still queued")
    assert any(c.status == cards.ACTION and "earlier run" in c.happened for c in seen)


def test_a_holding_the_mirror_does_not_track_blocks_entries_and_is_carded(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    reader.held["XYZ"] = "3"
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    assert len(mirror.broker.sent) == 2                # nothing new placed, nothing sold blind
    assert "does not track" in mirror.ledger.events(["skip"])[-1]["why"]
    assert any(c.status == cards.ACTION and "XYZ" in c.happened for c in seen)
    mirror.tick()
    assert sum("XYZ" in c.happened for c in seen) == 1  # once a symbol a day
    del reader.held["XYZ"]
    mirror.tick()
    assert not mirror.e.get("unwatched")


def restarted(tmp_path, config, reader, clock, seen):
    """A new process on the saved state, the same broker (its orders and holdings)."""
    live = config.live_mirror
    broker = Broker(reader)
    mirror = LiveMirror(config=live, ai_config=config.ai_trader, reader=reader, broker=broker,
                        paper_ledger=tmp_path / "paper.jsonl", ledger=Ledger(tmp_path / "ledger.jsonl", clock),
                        state=LabState.load(tmp_path / "state.json"), state_path=tmp_path / "state.json",
                        limits=account_limits(AccountConfig(enabled=True, label="LIVE", sleeve="live_mirror",
                                                            limits_overrides=live.limits_overrides)),
                        notify=seen.append, clock=clock, halt_path=tmp_path / "HALT")
    return mirror


@pytest.mark.parametrize("gap_minutes, copied", [(1, True), (10, False)])
def test_a_paper_entry_read_before_a_restart_is_copied_or_carded_never_lost(tmp_path, config, gap_minutes, copied):
    """2026-11-09: the paper offset is saved the moment an entry is read; a
    queue kept only in memory lost the entry on a restart with no card."""
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror._enter_one = lambda snap: None              # the process dies after reading, before copying
    mirror.tick()
    assert [q["pid"] for q in json.loads((tmp_path / "state.json").read_text())["engine"]["queue"]] == ["a"]

    clock.now = clock.now + timedelta(minutes=gap_minutes)
    again = restarted(tmp_path, config, reader, clock, seen)
    again.tick()                                       # the session start
    again.tick()
    if copied:
        assert [o.symbol for o in again.broker.sent] == ["F"] and "a" in again.e["positions"]
    else:
        assert again.broker.sent == []
        assert "seconds late" in again.ledger.events(["skip"])[-1]["why"]
        assert any("skipped F" in c.happened for c in seen)
    assert again.e["queue"] == []


# ================================================================================
# Options: native, the call proxy, the round trip (temporary ruling 2026-11-09)
# ================================================================================

CHEAP = "F261113C00041000"        # a contract that fits under the 2% premium cap ($22 on $1,100)
DEAR = "UNH261120C00400000"       # one that does not: $1,200 a contract


def option_opened(pid, symbol, occ, right="call", stop="38.00", target="44.00"):
    """A paper option entry: its stop and target are UNDERLYING levels."""
    return {**opened(pid, symbol, instrument=right, stop="5.0", target="10.0", occ=occ, multiplier=100),
            "stop": stop, "target": target}


def with_proxy(config):
    return config.model_copy(update={"live_mirror": config.live_mirror.model_copy(
        update={"option_proxy_enabled": True})})


def test_an_option_that_fits_is_copied_natively_once_the_option_round_trip_has_passed(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    reader.add_contract("f41", CHEAP, "0.14", "0.15")
    paper(tmp_path, option_opened("a", "F", CHEAP), filled("a", price="0.15", qty="1", mult=100))
    mirror.tick()
    assert "round trip has not passed" in mirror.ledger.events(["skip"])[-1]["why"]
    mirror.e["option_round_trip"] = "passed"
    paper(tmp_path, option_opened("b", "F", CHEAP), filled("b", price="0.15", qty="1", mult=100))
    mirror.tick()
    order = mirror.broker.sent[-1]
    assert type(order).__name__ == "OptionBuyToOpenOrder" and order.symbol == CHEAP
    assert order.contracts == 1 and order.execution.limit_price == D("0.15")   # 22 / 15 -> one contract
    mirror.tick()                                       # settles
    position = mirror.e["positions"]["b"]
    assert (position["copy"], position["option_id"], position["stop"], position["target"]) == \
        ("native", "f41", "38.00", "44.00")
    assert any(c.happened == f"bought {CHEAP}" for c in seen)
    reader.book["F"] = ("39.50", "39.55", "39.50")    # the underlying above the stop: held
    mirror.tick()
    assert type(mirror.broker.sent[-1]).__name__ == "OptionBuyToOpenOrder"
    paper(tmp_path, {"event": "position_closed", "pid": "b", "reason": "target", "at": T0.isoformat()})
    mirror.tick()
    sell = mirror.broker.sent[-1]
    assert type(sell).__name__ == "OptionSellToCloseOrder" and sell.execution.limit_price == D("0.14")
    mirror.tick()
    assert "b" not in mirror.e["positions"] and reader.option_held["f41"] == "0"
    rows = mirror.daily_card().lines
    assert any(r.startswith(f"Native option copies: 1 opened ({CHEAP}), 1 closed for -$1") for r in rows)
    assert any(r.startswith("Skips: 1: F call:") for r in rows)


def test_a_native_option_exits_on_the_underlying_stop_on_the_bid(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    reader.add_contract("f41", CHEAP, "0.14", "0.15")
    mirror.e["option_round_trip"] = "passed"
    paper(tmp_path, option_opened("a", "F", CHEAP), filled("a", price="0.15", qty="1", mult=100))
    mirror.tick()
    mirror.tick()
    reader.book["F"] = ("37.99", "38.30", "38.10")    # the underlying's bid at the paper's stop 38.00
    mirror.tick()
    sell = mirror.broker.sent[-1]
    assert type(sell).__name__ == "OptionSellToCloseOrder"
    assert "underlying stop 38.00" in mirror.ledger.events(["order"])[-1]["why"]


def test_a_dear_call_is_skipped_while_the_proxy_is_off_and_a_dear_put_always(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    reader.add_contract("unh400", DEAR, "11.90", "12.00")
    paper(tmp_path, option_opened("a", "UNH", DEAR, stop="380.00", target="440.00"),
          filled("a", price="12.00", qty="1", mult=100))
    mirror.tick()
    assert "proxy is off" in mirror.ledger.events(["skip"])[-1]["why"]
    (tmp_path / "p").mkdir()
    mirror, seen, clock, reader = mirroring(tmp_path / "p", with_proxy(config))
    paper(tmp_path / "p", option_opened("b", "UNH", "UNH261120P00400000", right="put", stop="420.00",
                                        target="360.00"), filled("b", price="12.00", qty="1", mult=100))
    mirror.tick()
    assert "a put that does not fit is skipped" in mirror.ledger.events(["skip"])[-1]["why"]
    assert len(mirror.broker.sent) == 2


def test_a_dear_call_is_proxied_by_its_stock_at_the_paper_options_underlying_levels(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, with_proxy(config))
    reader.add_contract("unh400", DEAR, "11.90", "12.00")
    paper(tmp_path, option_opened("a", "UNH", DEAR, stop="380.00", target="440.00"),
          filled("a", price="12.00", qty="1", mult=100))
    mirror.tick()
    order = mirror.broker.sent[-1]
    # 2% of 1100 = 22 at risk to the paper's UNDERLYING stop: 22 / (400.00 - 380.00) = 1.1 shares, capped
    # at 18.75% of 1100 x 0.99 over the gate's bound (420) -> 0.486160, a fractional market buy
    assert type(order).__name__ == "EquityBuyOrder" and order.symbol == "UNH" and order.quantity == D("0.486160")
    mirror.tick()
    position = mirror.e["positions"]["a"]
    assert (position["copy"], position["stop"], position["target"], position["paper_occ"]) == \
        ("proxy", "380.00", "440.00", DEAR)
    reader.book["UNH"] = ("379.90", "380.10", "380.00")   # the underlying's bid at the paper's stop
    mirror.tick()
    sell = mirror.broker.sent[-1]
    assert type(sell).__name__ == "EquitySellToCloseOrder" and sell.symbol == "UNH"
    mirror.tick()
    rows = mirror.daily_card().lines
    assert any(r.startswith("Call proxies: 1 opened (UNH), 1 closed") for r in rows)
    assert any(r.startswith(f"Proxy UNH for {DEAR}:") and "the paper option is still open" in r for r in rows)


def test_a_proxy_closes_with_the_paper_option_and_is_reported_against_it(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, with_proxy(config))
    reader.add_contract("unh400", DEAR, "11.90", "12.00")
    paper(tmp_path, option_opened("a", "UNH", DEAR, stop="380.00", target="440.00"),
          filled("a", price="12.00", qty="1", mult=100))
    mirror.tick()
    mirror.tick()
    paper(tmp_path, {"event": "settled", "pid": "a", "side": "sell", "reason": "target", "filled_qty": "1",
                     "avg_price": "15.00", "multiplier": 100, "at": T0.isoformat()},
          {"event": "position_closed", "pid": "a", "reason": "target", "at": T0.isoformat()})
    mirror.tick()
    assert type(mirror.broker.sent[-1]).__name__ == "EquitySellToCloseOrder"
    mirror.tick()
    rows = mirror.daily_card().lines
    assert any("against the paper option +$300" in r and "25" in r for r in rows), rows


def test_the_option_round_trip_buys_and_sells_one_cheap_call_then_native_copies_are_on(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    assert "option_round_trip" not in mirror.e          # no contract qualified yet: noted, nothing sent
    reader.add_contract("wide", "SPY261106C00775000", "0.20", "0.30")          # too wide
    reader.add_contract("dear", "SPY261106C00772000", "0.40", "0.41")          # $41: over the cap
    reader.add_contract("thin", "SPY261106C00785000", "0.10", "0.11", open_interest=10)
    reader.add_contract("good", "SPY261106C00780000", "0.14", "0.15")
    mirror.tick()
    buy = mirror.broker.sent[-1]
    assert type(buy).__name__ == "OptionBuyToOpenOrder" and buy.symbol == "SPY261106C00780000"
    assert buy.contracts == 1 and buy.execution.limit_price == D("0.15")
    mirror.tick()                                       # the buy settles; the sell goes out
    sell = mirror.broker.sent[-1]
    assert type(sell).__name__ == "OptionSellToCloseOrder" and sell.execution.limit_price == D("0.14")
    mirror.tick()
    assert mirror.e["option_round_trip"] == "passed" and reader.option_held["good"] == "0"
    record = mirror.ledger.events(["option_round_trip"])[-1]
    assert record["passed"] is True and D(record["net"]) == D("-1")
    assert any(c.happened == "live option round trip passed" for c in seen)
    mirror.tick()
    assert len(mirror.broker.sent) == 4                 # once only


def test_a_dry_option_round_trip_picks_its_contract_and_sends_nothing(tmp_path, config):
    reader = Reader()
    mirror, seen, clock = build(tmp_path, config, reader, broker=None)
    clock.now = T0 + timedelta(days=1)
    reader.add_contract("good", "SPY261106C00780000", "0.14", "0.15")
    mirror.start()
    mirror.e.update(phase="mirroring", starting_equity="1100", paper_offset=0)
    mirror.tick()
    would = mirror.ledger.events(["would_option_round_trip"])
    assert would and would[-1]["occ"] == "SPY261106C00780000" and seen == []


def test_an_option_the_mirror_does_not_track_blocks_entries(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    reader.add_contract("zz", "F261113C00045000", "0.05", "0.06")
    reader.option_held["zz"] = "1"
    paper(tmp_path, opened("a", "F"), filled("a", price="40"))
    mirror.tick()
    assert len(mirror.broker.sent) == 2 and "does not track" in mirror.ledger.events(["skip"])[-1]["why"]
    assert any(c.status == cards.ACTION and "option" in c.happened for c in seen)


def test_a_restart_holding_a_native_option_can_still_sell_it(tmp_path, config):
    mirror, seen, clock, reader = mirroring(tmp_path, config)
    reader.add_contract("f41", CHEAP, "0.14", "0.15")
    mirror.e["option_round_trip"] = "passed"
    paper(tmp_path, option_opened("a", "F", CHEAP), filled("a", price="0.15", qty="1", mult=100))
    mirror.tick()
    mirror.tick()
    again = restarted(tmp_path, config, reader, clock, seen)
    again.tick()
    assert again.gate.state.positions[("option", CHEAP)].quantity == 1
    paper(tmp_path, {"event": "position_closed", "pid": "a", "reason": "stop", "at": T0.isoformat()})
    again.tick()
    assert type(again.broker.sent[-1]).__name__ == "OptionSellToCloseOrder"
