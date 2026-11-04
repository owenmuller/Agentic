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
        bid, ask, _ = self.reader.book[order.symbol]
        buy = type(order).__name__ == "EquityBuyOrder"
        self.orders[oid] = OrderStatus(oid, "filled", order.quantity, D(ask if buy else bid))
        held = D(self.reader.held.get(order.symbol, "0")) + (order.quantity if buy else -order.quantity)
        self.reader.held[order.symbol] = str(held)
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
    reader.cash, reader.pending = "1100", "1000"       # in transit: not settled
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
