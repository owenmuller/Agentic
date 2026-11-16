"""STOP CHASING, TRADE LIKE A DAY TRADER (human ruling 2026-11-16): the
intraday context, the anti-chase rule, pullback entries (a resting limit for
stocks, an underlying trigger for options), the confirm step, the prompt
redirect and its setup list - and that, switched off, nothing changed."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.base import OrderReceipt
from lab.account import open_account
from lab.ai_prompts import build_user, confirm_system, confirm_user, report_tool, system_prompt, ConfirmItem
from lab.ai_trader import NY, AiTrader
from lab.config import SETUPS, LabConfig
from lab.intraday import Context, compute, pullback_level
from lab.llm import PassResult
from test_lab_ai import AAPL_LONG, CFG, World, _chain, et, lab_env  # noqa: F401 - lab_env is a fixture

D = Decimal
ON = CFG.model_copy(update={"day_trader": CFG.day_trader.model_copy(update={"enabled": True})})


# ------------------------------------------------------------- the measures (pure)

def bar(t: datetime, o, h, l, c, v, vw=None):
    return {"t": t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "o": o, "h": h, "l": l, "c": c,
            "v": v, "vw": vw if vw is not None else c}


def daily(closes, end: date):
    out, day = [], end - timedelta(days=len(closes) * 2)
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(bar(datetime(day.year, day.month, day.day, 5, tzinfo=timezone.utc), close, close * 1.01,
                       close * 0.99, close, 1000))
        day += timedelta(days=1)
    return out


OPEN = datetime(2026, 11, 17, 9, 30, tzinfo=NY)


def session(minutes, start=100.0, step=0.1, volume=100):
    return [bar(OPEN + timedelta(minutes=i), start + i * step, start + i * step + 0.05, start + i * step - 0.05,
                start + i * step, volume) for i in range(minutes)]


def test_the_context_measures_vwap_opening_range_open_and_relative_volume():
    today = session(30)                                    # 09:30-09:59, rising 0.1 a minute
    history = []
    for back in range(1, 4):                               # three earlier sessions, 50 shares a minute
        start = OPEN - timedelta(days=back)
        history += [bar(start + timedelta(minutes=5 * i), 1, 1, 1, 1, 250) for i in range(12)]
    opens = {(OPEN - timedelta(days=back)).date(): OPEN - timedelta(days=back) for back in range(1, 4)}
    ctx = compute("ABC", OPEN + timedelta(minutes=30), D("103"), daily=daily([98.0] * 20, OPEN.date()),
                  today=today, history=history, session_open=OPEN, opens=opens, opening_range_minutes=15,
                  relvol_sessions=20)
    assert ctx.open == D("100.0") and ctx.prior_close == D("98.0")
    assert ctx.or_low == D("99.95") and ctx.or_high == pytest.approx(D("101.45"), abs=D("0.001"))
    assert ctx.or_complete and ctx.vwap == pytest.approx(D("101.45"), abs=D("0.001"))
    assert ctx.rel_volume == D("2")                        # 100 a minute today against 50
    assert ctx.from_close == pytest.approx(D("103") / D("98") - 1)
    assert ctx.atr_fraction == pytest.approx(D("0.02"), abs=D("0.0001"))
    # +5.1% from the close against a 2% ATR: extended for a long, not for a put
    assert ctx.extended(1, D("1.0")) is True and ctx.extended(-1, D("1.0")) is False
    line = ctx.line(1, D("1.0"))
    assert "from the prior close 98.00" in line and "above VWAP" in line and "relative volume 2.0x" in line
    assert "EXTENDED" in line and "$" not in line


def test_either_measure_extends_and_an_unmeasurable_one_is_unknown():
    ctx = Context("ABC", OPEN, price=D("100"), prior_close=D("99.5"), atr_fraction=D("0.02"))
    assert ctx.extended(1, D("1")) is False
    ctx.catalyst = {"status": "ok", "move": "0.031", "public_at": "2026-11-17T13:05:00Z"}
    assert ctx.extended(1, D("1")) is True                 # the catalyst measure alone passes (the stricter reading)
    assert Context("ABC", OPEN, price=D("100")).extended(1, D("1")) is None


@pytest.mark.parametrize("vwap, or_low, or_high, price, bull, level, why", [
    ("101", "100", "104", "103", True, "101.00", ""),                         # the higher of VWAP and the range low
    ("99", "100", "104", "103", True, "100.00", ""),
    ("103.5", "100", "104", "103", True, None, "at or above"),               # no pullback below the price
    ("99", "96", "100.5", "97", False, "99.00", ""),                         # a put: the lower of VWAP and range high
    ("96.5", "95", "100.5", "97", False, None, "at or below"),
])
def test_the_pullback_level(vwap, or_low, or_high, price, bull, level, why):
    ctx = Context("ABC", OPEN, price=D(price), vwap=D(vwap), or_low=D(or_low), or_high=D(or_high), or_complete=True)
    got, reason = pullback_level(ctx, bull)
    assert (str(got) if got is not None else None) == level and why in reason
    ctx.or_complete = False
    assert pullback_level(ctx, bull)[0] is None


# ------------------------------------------------------------- off: nothing changed

def test_switched_off_the_prompt_the_tool_and_the_user_message_are_as_before():
    assert not CFG.day_trader.enabled                       # ships off until its golden replay and round trip
    off = system_prompt(CFG)
    assert "HOW A DAY TRADER" not in off and "setup" not in str(report_tool(False, ["a"]))
    on = system_prompt(ON)
    assert on.startswith(off[: -len("\n\nAnswer by calling submit_trade_ideas.")])
    assert "priced in by 09:45" in on and "1.0x its ATR(14)" in on and "60 minutes" in on
    tool = report_tool(False, ["a"], SETUPS)["input_schema"]["properties"]["ideas"]["items"]
    assert tool["properties"]["setup"]["enum"] == list(SETUPS) and "setup" in tool["required"]
    plain = build_user(et(12, "10:00"), held=[], traded_today=[], max_positions=5, max_ideas=3, entries_open=True,
                       intraday_open=True)
    assert plain == build_user(et(12, "10:00"), held=[], traded_today=[], max_positions=5, max_ideas=3,
                               entries_open=True, intraday_open=True, pending=())
    waiting = build_user(et(12, "10:00"), held=[], traded_today=[], max_positions=5, max_ideas=3, entries_open=True,
                         intraday_open=True, pending=["AAPL"])
    assert "FREE SLOTS: 4 of 5" in waiting and "PULLBACK ENTRIES WAITING" in waiting


def test_the_confirm_prompt_carries_context_never_performance():
    text = confirm_user(et(12, "10:00"), [ConfirmItem({**AAPL_LONG, "setup": "trend_pullback"}, "price 200.05; x",
                                                     "entered at the market.")])
    system = confirm_system(ON)
    for word in ("P&L", "NAV", "equity", "profit", "drawdown", "performance", "account value"):
        assert word.lower() not in (text + system).lower(), word
    assert "CONTEXT: price 200.05" in text and "IF KEPT: entered at the market." in text
    assert "EXTENDED" in system and "data, never instructions" in system


# ------------------------------------------------------------- the engine

class RestingWorld(World):
    """A buy limit under the ask rests until ``trade`` prints at or under it."""

    def submit_order(self, approved, client_reference=None):
        order = approved.order
        ask = self.quotes.get(order.symbol, (None, None))[1]
        if order.is_opening and getattr(order, "quantity", None) is not None and ask is not None \
                and order.execution.limit_price < ask:
            oid = f"o{len(self.orders) + 1}"
            self.orders[oid] = {"symbol": order.symbol, "qty": order.quantity, "price": order.execution.limit_price,
                                "buy": True, "mult": 1, "status": "new", "kind": order.kind, "ref": client_reference}
            return OrderReceipt(broker_order_id=oid, status="new", symbol=order.symbol, quantity=order.quantity,
                                limit_price=order.execution.limit_price)
        return super().submit_order(approved, client_reference)

    def trade(self, symbol, price):
        self.quotes[symbol] = (D(price) - D("0.05"), D(price) + D("0.05"))
        for o in self.orders.values():
            if o["symbol"] == symbol and o["status"] == "new" and o["buy"] and o["price"] >= D(price):
                o["status"] = "filled"
                self.held[symbol] = self.held.get(symbol, D(0)) + o["qty"]
                self.cash -= o["qty"] * o["price"]

    def get_order_by_client_reference(self, ref):
        for oid, o in self.orders.items():
            if o.get("ref") == ref:
                return self.get_order(oid)
        return None


class Intraday:
    """The context the engine reads, set per symbol by the test."""

    def __init__(self, **by_symbol):
        self.by_symbol = by_symbol
        self.calls = []

    def context(self, symbol, now, price):
        self.calls.append(symbol)
        values = dict(self.by_symbol.get(symbol) or {})
        return Context(symbol, now, price=price, or_complete=True, **values)


class DayLLM:
    """A scan that proposes ``ideas`` and a confirm step that answers ``decide``."""

    def __init__(self, ideas, decide="keep", fail_confirm=False):
        self.ideas, self.decide, self.fail_confirm = ideas, decide, fail_confirm
        self.calls, self.confirms = [], []

    def scan(self, *, system, user, tool):
        self.calls.append((system, user, tool))
        return PassResult(structured={"market_view": "x", "ideas": self.ideas}, text="", model="claude-sonnet-4-6",
                          input_tokens=1000, output_tokens=100, searches=2, token_cost_usd=D("0.18"),
                          cost_usd=D("0.20"), transcript_hash="abc", stop_reason="tool_use")

    def confirm(self, *, system, user, tool, max_tokens):
        self.confirms.append((system, user, tool))
        if self.fail_confirm:
            raise TimeoutError("read timed out")
        symbols = tool["input_schema"]["properties"]["decisions"]["items"]["properties"]["symbol"]["enum"]
        verdict = self.decide if isinstance(self.decide, dict) else {s: self.decide for s in symbols}
        return PassResult(structured={"decisions": [{"symbol": s, "decision": verdict.get(s, "keep"), "reason": "r"}
                                                    for s in symbols]},
                          text="", model="claude-sonnet-4-6", input_tokens=900, output_tokens=80, searches=0,
                          token_cost_usd=D("0.01"), cost_usd=D("0.01"), transcript_hash="def", stop_reason="tool_use")


def day_trader(world, tmp_path, llm, intraday, config=ON):
    account = open_account("ai-trader", LabConfig.load(), adapter=world, main_account_number=lambda: "PA-MAIN",
                           data_dir=tmp_path / "ai", clock=lambda: et(12, "10:00"), allow_disabled=True)
    ticker = iter(range(0, 10**9, 5))
    return AiTrader(account, config, llm=llm, quotes=world.quote, chain=world, sleep=lambda s: None,
                    monotonic=lambda: next(ticker), intraday=intraday)


IDEA = {**AAPL_LONG, "setup": "trend_pullback"}
CALM = {"prior_close": D("199.50"), "atr_fraction": D("0.02"), "vwap": D("199.80"), "or_low": D("199.00"),
        "or_high": D("200.50")}
# AAPL quotes 200.00 x 200.10 (mid 200.05): +5.3% from 190 against a 2% ATR
RUN_UP = {"prior_close": D("190.00"), "atr_fraction": D("0.02"), "vwap": D("197.00"), "or_low": D("196.50"),
          "or_high": D("201.00")}


def test_a_name_not_extended_is_entered_at_the_market_after_the_confirm_step(lab_env):
    world, llm = World(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL=CALM))
    notes = trader.tick(et(12, "09:46"))
    assert any("opened AAPL" in n for n in notes), notes
    system, user, tool = llm.calls[0]
    assert "HOW A DAY TRADER" in system and "setup" in str(tool)
    confirm = llm.confirms[0][1]
    assert "AAPL stock" in confirm and "from the prior close 199.50" in confirm and "entered at the market" in confirm
    [plan] = trader.account.ledger.events(["position_opened"])
    assert plan["setup"] == "trend_pullback" and plan["entry_mode"] == "at_market"
    assert plan["context"]["extended"] is False and plan["context"]["prior_close"] == "199.50"
    assert trader.spent_today(date(2026, 10, 12)) == D("0.21")          # the pass and the confirm step


def test_an_extended_stock_becomes_a_resting_limit_at_its_pullback_level_and_fills_with_levels_from_the_fill(lab_env):
    world, llm = RestingWorld(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL=RUN_UP))
    trader.tick(et(12, "09:46"))
    [placed] = trader.account.ledger.events(["pullback_placed"])
    assert placed["kind"] == "limit" and placed["level"] == "197.00" and "AAPL" not in world.held
    assert placed["chase"]["anchor"] == "200.05"                        # the at-market entry it replaced
    assert "a pullback entry at 197.00" in llm.confirms[0][1]
    # 2% of NAV at risk from the level to its stop (197 x 0.97 = 191.09)
    quantity = D(placed["quantity"])
    assert quantity * (D("197.00") - D("191.09")) <= D("2000.01")
    # it holds its cash, its slot and its name while it rests
    trader.tick(et(12, "09:50"))
    assert trader.gate.state.reserved_cash == quantity * D("197.00")
    assert "AAPL" in trader.traded_today(date(2026, 10, 12)) and len(trader.pending()) == 1
    assert "PULLBACK ENTRIES WAITING" in trader.user_prompt(et(12, "10:00"), [])
    # the price comes back: filled at 196.95 or better, stop and target from the FILL
    world.trade("AAPL", "196.95")
    trader.tick(et(12, "10:05"))
    [opened] = trader.account.ledger.events(["position_opened"])
    assert opened["entry_mode"] == "pullback" and opened["anchor_mid"] == "197.00"
    assert opened["stop"] == "191.09" and opened["target"] == "211.78"   # 197 x 0.97 down; x 1.075 up
    [settled] = trader.account.ledger.events(["settled"])
    assert settled["reason"] == "entry" and settled["side"] == "buy"     # what the live mirror copies
    assert trader.account.ledger.events(["pullback_done"])[0]["outcome"] == "filled" and not trader.pending()


def test_a_pullback_that_never_comes_expires_and_leaves_the_chase_for_the_shadow(lab_env):
    world, llm = RestingWorld(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL=RUN_UP))
    trader.tick(et(12, "09:46"))
    trader.tick(et(12, "10:40"))
    assert trader.pending()                                             # 54 minutes: still waiting
    trader.tick(et(12, "10:47"))
    assert not trader.pending() and world.orders["o1"]["status"] == "canceled"
    [refused] = [e for e in trader.account.ledger.events(["idea_rejected"]) if e.get("code") == "pullback_unfilled"]
    assert "expired" in refused["reason"] and refused["levels"]["anchor"] == "200.05"
    assert not trader.account.ledger.events(["position_opened"])


def test_a_stop_breached_before_the_fill_ends_the_entry(lab_env):
    world, llm = RestingWorld(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL=RUN_UP))
    trader.tick(et(12, "09:46"))
    world.orders["o1"]["price"] = D("1")                               # the paper book never filled it
    world.quotes["AAPL"] = (D("190.00"), D("190.10"))                  # under the 191.09 stop level
    trader.tick(et(12, "09:50"))
    done = trader.account.ledger.events(["pullback_done"])
    assert done and done[0]["outcome"] == "unfilled" and "breached its stop level" in done[0]["detail"]


def test_the_kill_switch_or_a_halt_ends_a_waiting_entry(lab_env):
    world, llm = RestingWorld(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL=RUN_UP))
    trader.tick(et(12, "09:46"))
    trader.account.halted = lambda: "operator halt"
    trader.tick(et(12, "09:50"))
    assert not trader.pending() and world.orders["o1"]["status"] == "canceled"


def test_extended_with_no_pullback_below_the_price_is_refused(lab_env):
    world, llm = World(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL={**RUN_UP, "vwap": D("200.50")}))
    trader.tick(et(12, "09:46"))
    assert not llm.confirms and not world.orders                        # never asked, never ordered
    [row] = trader.account.ledger.events(["anti_chase"])
    assert row["outcome"] == "refused_no_level"
    [refused] = trader.account.ledger.events(["idea_rejected"])
    assert refused["code"] == "anti_chase" and refused["levels"]["anchor"] == "200.05"


def test_an_extension_that_cannot_be_measured_is_refused_not_guessed(lab_env):
    world, llm = World(), DayLLM([IDEA])
    trader = day_trader(world, lab_env, llm, Intraday(AAPL={"vwap": D("199")}))       # no prior close, no ATR
    trader.tick(et(12, "09:46"))
    assert not world.orders
    assert trader.account.ledger.events(["anti_chase"])[0]["outcome"] == "refused_unmeasured"
    no_source = day_trader(World(), lab_env / "b", DayLLM([IDEA]), None)
    no_source.tick(et(12, "09:46"))
    assert not no_source.account.adapter.orders


def test_a_dropped_idea_or_a_failed_confirm_step_enters_nothing(lab_env):
    world, llm = World(), DayLLM([IDEA], decide="drop")
    trader = day_trader(world, lab_env, llm, Intraday(AAPL=CALM))
    trader.tick(et(12, "09:46"))
    assert not world.orders
    [refused] = trader.account.ledger.events(["idea_rejected"])
    assert refused["code"] == "context_drop" and refused["reason"].startswith("dropped after the intraday context")
    world2 = World()
    failing = day_trader(world2, lab_env / "b", DayLLM([IDEA], fail_confirm=True), Intraday(AAPL=CALM))
    failing.tick(et(12, "09:46"))
    assert not world2.orders and failing.account.ledger.events(["fault"])


def test_with_the_rules_on_an_idea_needs_a_setup(lab_env):
    trader = day_trader(World(), lab_env, DayLLM([]), Intraday())
    assert "setup" in trader.validate(AAPL_LONG, et(12, "10:00"), [], slots=5).reason
    assert trader.validate(IDEA, et(12, "10:00"), [], slots=5).ok


def test_an_extended_call_waits_for_its_underlying_then_buys_the_option_by_the_normal_path(lab_env):
    world = RestingWorld()
    today = date(2026, 10, 12)
    world.chain = _chain(today)
    world.option_quotes["SPY261012C00660000"] = (D("2.00"), D("2.10"))
    call = {**IDEA, "symbol": "SPY", "instrument": "call", "horizon": "intraday", "stop_pct": 1.5, "target_pct": 3,
            "theme": "broad_market"}
    # SPY 660.025: +2.3% from 645 against a 1% ATR; pullback at the higher of 655 VWAP and 654 range low
    context = {"prior_close": D("645"), "atr_fraction": D("0.01"), "vwap": D("655"), "or_low": D("654"),
               "or_high": D("661")}
    trader = day_trader(world, lab_env, DayLLM([call]), Intraday(SPY=context))
    trader.tick(et(12, "09:46"))
    [placed] = trader.account.ledger.events(["pullback_placed"])
    assert placed["kind"] == "trigger" and placed["level"] == "655.00" and not world.orders
    trader.tick(et(12, "09:50"))
    assert not world.orders                                             # still above the level
    world.quotes["SPY"] = (D("654.90"), D("655.00"))
    trader.tick(et(12, "09:55"))
    assert world.held.get("SPY261012C00660000")
    [opened] = trader.account.ledger.events(["position_opened"])
    assert opened["anchor_source"] == "pullback_trigger" and opened["anchor_mid"] == "654.95"
    assert trader.account.ledger.events(["pullback_done"])[0]["outcome"] == "filled"


def test_a_put_waits_for_a_bounce_to_the_lower_of_vwap_and_the_range_high(lab_env):
    world = RestingWorld()
    put = {**IDEA, "instrument": "put", "setup": "extended_negative_put"}
    # AAPL 200.05: -5% from 210.6 against a 2% ATR; the bounce level is min(203 VWAP, 204 range high)
    context = {"prior_close": D("210.60"), "atr_fraction": D("0.02"), "vwap": D("203"), "or_low": D("199"),
               "or_high": D("204")}
    trader = day_trader(world, lab_env, DayLLM([put]), Intraday(AAPL=context))
    trader.tick(et(12, "09:46"))
    [placed] = trader.account.ledger.events(["pullback_placed"])
    assert placed["kind"] == "trigger" and placed["level"] == "203.00"


# ------------------------------------------------------------- the kill-switch card (2026-11-16)

def test_the_kill_switch_card_reads_the_new_gate_and_a_failing_card_never_costs_the_tick(lab_env):
    """09:30:01 on 2026-11-16: the trip's card read ``trader.gate`` while it
    was still None, raised, and the kill-switch card was never sent."""
    world = World()
    seen = []
    holder = {}
    account = open_account("ai-trader", LabConfig.load(), adapter=world, main_account_number=lambda: "PA-MAIN",
                           data_dir=lab_env / "ai", clock=lambda: et(12, "10:00"), allow_disabled=True)
    account.state.high_water_mark = "150000"            # NAV 100,000: a 33% drawdown trips on the first tick

    def card(subject, body):
        seen.append(holder["trader"].gate.state.nav)

    trader = holder["trader"] = AiTrader(account, CFG, llm=DayLLM([]), quotes=world.quote, chain=world,
                                         sleep=lambda s: None, monotonic=iter(range(0, 10**9, 5)).__next__,
                                         alert=card)
    trader.tick(et(12, "09:31"))
    assert seen == [D("100000")] and trader.gate.kill_switch_tripped

    broken = World()
    account2 = open_account("ai-trader", LabConfig.load(), adapter=broken, main_account_number=lambda: "PA-MAIN",
                            data_dir=lab_env / "ai2", clock=lambda: et(12, "10:00"), allow_disabled=True)
    account2.state.high_water_mark = "150000"
    trader2 = AiTrader(account2, CFG, llm=DayLLM([]), quotes=broken.quote, chain=broken, sleep=lambda s: None,
                       monotonic=iter(range(0, 10**9, 5)).__next__,
                       alert=lambda subject, body: (_ for _ in ()).throw(RuntimeError("webhook down")))
    trader2.tick(et(12, "09:31"))                       # no exception: the card's failure is a fault
    assert any("kill-switch card failed" in e["message"] for e in account2.ledger.events(["fault"]))


# ------------------------------------------------------------- measurement (item 5) and the dry run

def test_the_scoreboard_groups_closed_trades_by_setup_and_by_entry_mode():
    from lab.scoreboard import day_trader_lines

    def trade(pid, setup, mode, buy, sell):
        return [{"event": "position_opened", "pid": pid, "symbol": "X", "setup": setup, "entry_mode": mode},
                {"event": "settled", "pid": pid, "side": "buy", "filled_qty": "10", "avg_price": str(buy)},
                {"event": "settled", "pid": pid, "side": "sell", "filled_qty": "10", "avg_price": str(sell)},
                {"event": "position_closed", "pid": pid}]

    events = (trade("a", "trend_pullback", "pullback", 10, 11) + trade("b", "trend_pullback", "at_market", 10, 9)
              + trade("c", "scheduled_catalyst", "at_market", 10, 10.5)
              + [{"event": "position_opened", "pid": "old", "symbol": "Y"}])          # before the rules: not counted
    lines = day_trader_lines(events)
    assert "(3 closed, 0 open)" in lines[0]
    assert "  trend_pullback: 2 closed, mean +0.0% on cost, won 1/2" in lines
    assert "  pullback: 1 closed, mean +10.0% on cost, won 1/1" in lines
    assert "  at_market: 2 closed, mean -2.5% on cost, won 1/2" in lines
    assert day_trader_lines([{"event": "position_opened", "pid": "x", "symbol": "Y"}]) == []


class Sim:
    """The review-shadows simulator, faked: a fixed return per anchor."""

    def __init__(self, by_anchor):
        self.by_anchor = by_anchor

    def run(self, symbol, instrument, horizon, opened, anchor, stop, target):
        from lab.review_shadows import Sim as Result

        ret = self.by_anchor.get(anchor)
        return Result("done", "stop", "t", 1.0, ret) if ret is not None else Result("no_levels")


def test_the_chase_shadow_replays_what_was_kept_out_and_sets_filled_pullbacks_beside_their_chase():
    from lab.review_shadows import chase

    events = [
        {"event": "idea_rejected", "at": "2026-11-17T15:00:00+00:00", "symbol": "MOD", "code": "anti_chase",
         "idea": {"instrument": "stock", "horizon": "days"}, "levels": {"anchor": "139", "stop": "1", "target": "2"}},
        {"event": "idea_rejected", "at": "2026-11-17T15:00:00+00:00", "symbol": "SOXS", "code": "context_drop",
         "idea": {"instrument": "etf", "horizon": "intraday"}, "levels": {"anchor": "46.82", "stop": "1", "target": "2"}},
        {"event": "pullback_placed", "at": "2026-11-17T16:00:00+00:00", "pid": "p1", "symbol": "TRMK",
         "chase": {"anchor": "48.12", "stop": "1", "target": "2"}},
        {"event": "position_opened", "at": "2026-11-17T16:20:00+00:00", "pid": "p1", "symbol": "TRMK",
         "instrument": "stock", "horizon": "days", "entry_mode": "pullback", "anchor_mid": "47.81", "stop": "1",
         "target": "2"},
        {"event": "idea_rejected", "at": "2026-11-17T17:00:00+00:00", "symbol": "FIGR", "code": "pullback_unfilled",
         "pid": "p2", "idea": {"instrument": "stock", "horizon": "days"},
         "levels": {"anchor": "26.92", "stop": "1", "target": "2"}},
    ]
    lines = chase(events, Sim({139.0: -0.06, 46.82: -0.035, 48.12: -0.025, 47.81: 0.01, 26.92: -0.05}))
    text = "\n".join(lines)
    assert "refused by the anti-chase rule, if chased: scored 1 - mean -6.00%" in text
    assert "dropped by the model after the context, if chased: scored 1 - mean -3.50%" in text
    assert "pullback entry never filled, if chased: scored 1 - mean -5.00%" in text
    assert "filled pullback minus the chase it replaced: n 1, mean +3.50%" in text


def test_the_monthly_review_groups_the_new_refusals():
    from lab.monthly_review import refusal_kind

    assert refusal_kind("anti-chase: extended past 1x ATR(14) and no pullback level") == "anti-chase: extended, no entry"
    assert refusal_kind("dropped after the intraday context: weak volume") == "dropped after the context"
    assert refusal_kind("pullback entry at 47.81 not reached: expired") == "pullback entry never filled"


def test_the_dry_run_fills_a_resting_limit_at_its_level_and_walks_from_the_fill():
    from lab import day_trader_dryrun as dry

    t0 = datetime(2026, 11, 12, 12, 1, tzinfo=NY)

    def b(minute, o, h, l, c):
        return {"t": (t0 + timedelta(minutes=minute)).astimezone(timezone.utc).isoformat(), "o": o, "h": h, "l": l,
                "c": c}

    rows = [b(1, 48.1, 48.2, 48.0, 48.1), b(2, 48.0, 48.0, 47.7, 47.9), b(3, 47.9, 48.5, 47.9, 48.4)]
    got, why = dry.fill(rows, True, D("47.81"), D("46.37"), "limit")
    assert got["price"] == D("47.81") and why == ""
    gapped, _ = dry.fill([b(1, 47.5, 47.6, 47.4, 47.5)], True, D("47.81"), D("46.37"), "limit")
    assert gapped["price"] == D("47.5")                                  # a bar opening through it fills at the open
    assert dry.fill([b(1, 46.0, 46.1, 45.9, 46.0)], True, D("47.81"), D("46.37"), "trigger")[0] is None
    walk = dry.walk(rows[2:], True, D("47.81"), D("46.37"), D("48.40"), ended=True)
    assert walk.reason == "target" and walk.ret == D("48.40") / D("47.81") - 1     # a target fills at its level
    from execution.market_calendar import MarketCalendar

    pre = {"t": datetime(2026, 11, 13, 5, 11, tzinfo=NY).astimezone(timezone.utc).isoformat(), "o": 1, "h": 1,
           "l": 1, "c": 1}
    assert dry.regular([pre] + rows, MarketCalendar.weekdays()) == rows    # a pre-market print never counts
