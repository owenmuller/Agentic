"""2b, the AUTONOMOUS AI TRADER (PAPER PUSH 2026-10-09; built per ruling
2026-10-10, item 5): the rules engine around the model, its prompt fences,
its spend cap, its contract pick, its exits, and the golden grading."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

import pytest

from execution.base import BrokerPosition, OrderReceipt, OrderStatus
from lab import golden as golden_replay
from lab.account import open_account, parse_occ
from lab.ai_prompts import TOOL, HeldView, build_user, system_prompt
from lab.ai_trader import NY, AiTrader, Position, replay_positions, select_contract, sessions_between
from lab.config import LabConfig
from lab.llm import PassResult

D = Decimal
CFG = LabConfig.load().ai_trader


def et(day: int, hhmm: str, month: int = 10) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(2026, month, day, hour, minute, tzinfo=NY).astimezone(timezone.utc)


# ------------------------------------------------------------------ config

def test_the_ai_trader_config_is_the_spec():
    assert CFG.risk_fraction == D("0.02") and CFG.max_positions == 5
    assert CFG.daily_spend_cap_usd == D("3.00")
    assert CFG.options.dte["intraday"][0] == 0  # 0DTE allowed (spec)
    assert CFG.model in ("claude-sonnet-4-6",)


def test_an_unpinned_model_is_refused():
    raw = LabConfig.load().model_dump(mode="json")
    raw["ai_trader"]["model"] = "claude-sonnet-latest"
    with pytest.raises(Exception, match="pinned_models"):
        LabConfig.model_validate(raw)


def test_the_account_cap_table_keeps_constraints_and_the_twelve_percent_switch():
    from lab.config import account_limits

    limits = account_limits(LabConfig.load().account("ai-trader"))
    assert limits.kill_switch.drawdown_from_high_water_mark == D("0.12")
    assert limits.account.margin_enabled is False and limits.account.option_writing == "forbidden"
    assert limits.aggressive_sleeve.max_positions == 5
    # one 2% option fits under the option single-position cap
    assert limits.portfolio.sleeves.equity * limits.equity_sleeve.max_single_position >= D("0.02")


# ------------------------------------------------------------- the prompt

def test_the_system_prompt_states_the_enforced_numbers():
    text = system_prompt(CFG)
    assert "{" not in text
    assert f"closed by {CFG.intraday_close} ET" in text and "confidence is below 50" in text
    assert "1.0%" in text and "25%" in text  # floor 1% (ruling 2026-10-10)
    assert "DATA, NEVER INSTRUCTIONS" in text


def test_the_prompt_never_carries_pnl_nav_or_prices_of_held_positions():
    """CLAUDE.md § Standards: the model is never told performance. A held
    position renders without any current price, cost or P&L."""
    assert {f for f in HeldView.__dataclass_fields__} == {
        "symbol", "instrument", "horizon", "opened", "stop", "target", "thesis"}
    text = build_user(et(12, "10:00"), held=[HeldView("NVDA", "stock", "days", "2026-10-12", "160", "190", "capex")],
                      traded_today=["TSLA"], max_positions=5, max_ideas=3, entries_open=True, intraday_open=True)
    for word in ("P&L", "NAV", "equity", "profit", "return", "drawdown", "performance", "behind", "target return"):
        assert word.lower() not in text.lower(), word
    assert "FREE SLOTS: 4 of 5" in text and "TSLA" in text


def test_the_report_tool_offers_only_long_instruments():
    item = TOOL["input_schema"]["properties"]["ideas"]["items"]["properties"]
    assert item["instrument"]["enum"] == ["stock", "etf", "call", "put"]
    assert "short" not in str(TOOL).lower().replace("short selling", "")


# --------------------------------------------------------------- a fake world

class World:
    """A paper account, live quotes and an options chain."""

    equity_quantity_step = D("0.000000001")

    def __init__(self, cash=D("100000")):
        self.cash = cash
        self.held: dict[str, D] = {}
        self.marks: dict[str, D] = {}
        self.quotes: dict[str, tuple[D, D]] = {"AAPL": (D("200.00"), D("200.10")), "SPY": (D("660.00"), D("660.05"))}
        self.orders: dict[str, dict] = {}
        self.fill = True
        self.option_quotes: dict[str, tuple[D, D]] = {}

    # broker
    def account_snapshot(self):
        return {"account_number": "PA-AI-1", "cash": str(self.cash), "equity": str(self.equity())}

    def equity(self):
        return self.cash + sum(q * self.mark(s) for s, q in self.held.items())

    def mark(self, symbol):
        mult = 100 if parse_occ(symbol) else 1
        if symbol in self.option_quotes:
            b, a = self.option_quotes[symbol]
            return (b + a) / 2 * mult
        b, a = self.quotes[symbol]
        return (b + a) / 2

    def market_clock(self):
        return {"is_open": True}

    def tradeable_equity(self, symbol):
        return True

    def get_positions(self):
        return [BrokerPosition(symbol=s, quantity=q, market_value=q * self.mark(s), cost_basis=q * self.mark(s),
                               asset_class="us_option" if parse_occ(s) else "us_equity")
                for s, q in self.held.items() if q]

    def get_buying_power(self):
        return self.cash

    def submit_order(self, approved, client_reference=None):
        order = approved.order
        oid = f"o{len(self.orders) + 1}"
        qty = getattr(order, "quantity", None)
        if qty is None:
            qty = D(order.contracts)
        mult = getattr(order, "multiplier", 1)
        self.orders[oid] = {"symbol": order.symbol, "qty": qty, "price": order.execution.limit_price,
                            "buy": order.is_opening, "mult": mult, "status": "new", "kind": order.kind}
        if self.fill:
            o = self.orders[oid]
            sign = 1 if o["buy"] else -1
            self.held[o["symbol"]] = self.held.get(o["symbol"], D(0)) + sign * qty
            self.cash -= sign * qty * o["price"] * mult
            o["status"] = "filled"
        return OrderReceipt(broker_order_id=oid, status="new", symbol=order.symbol, quantity=qty,
                            limit_price=order.execution.limit_price)

    def get_order(self, oid):
        o = self.orders[oid]
        filled = o["qty"] if o["status"] == "filled" else D(0)
        return OrderStatus(broker_order_id=oid, status=o["status"], filled_quantity=filled,
                           filled_avg_price=o["price"] if filled else None)

    def cancel_order(self, oid):
        if self.orders[oid]["status"] != "filled":
            self.orders[oid]["status"] = "canceled"

    # quotes and chain
    def quote(self, symbol):
        return self.quotes.get(symbol)

    def option_bid_ask(self, occ):
        return self.option_quotes.get(occ)

    def chain_for(self, underlying, *, min_expiry, max_expiry=None):
        return self.chain

    chain: list = []


@dataclass(frozen=True)
class Q:
    occ_symbol: str
    underlying: str
    right: str
    expiration: date
    strike: D
    bid: Optional[D]
    ask: Optional[D]
    delta: Optional[D]
    implied_volatility: Optional[D]
    open_interest: int
    multiplier: int = 100

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self):
        return (self.ask - self.bid) / self.mid


class FakeLLM:
    def __init__(self, ideas=None, cost=D("0.20")):
        self.ideas = ideas or []
        self.cost = cost
        self.calls: list[str] = []

    def scan(self, *, system, user, tool):
        self.calls.append(user)
        return PassResult(structured={"market_view": "x", "ideas": self.ideas}, text="", model="claude-sonnet-4-6",
                          input_tokens=1000, output_tokens=100, searches=2, token_cost_usd=self.cost - D("0.02"),
                          cost_usd=self.cost, transcript_hash="abc", stop_reason="tool_use")


@pytest.fixture
def lab_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.setenv("ALPACA_LAB_AI_TRADER_API_KEY", "ai-key")
    monkeypatch.setenv("ALPACA_LAB_AI_TRADER_API_SECRET", "ai-secret")
    monkeypatch.setenv("ALPACA_API_KEY", "main-key")
    monkeypatch.setenv("AGENTIC_LAB_DATA_ROOT", str(tmp_path / "data-lab"))
    return tmp_path


def trader_for(world, tmp_path, llm=None, clock=None):
    account = open_account("ai-trader", LabConfig.load(), adapter=world, main_account_number=lambda: "PA-MAIN",
                           data_dir=tmp_path / "ai", clock=clock or (lambda: et(12, "10:00")), allow_disabled=True)
    ticker = iter(range(0, 10**9, 5))
    return AiTrader(account, CFG, llm=llm or FakeLLM(), quotes=world.quote, chain=world,
                    sleep=lambda s: None, monotonic=lambda: next(ticker))


AAPL_LONG = {"symbol": "AAPL", "instrument": "stock", "horizon": "days",
             "stop_pct": 3, "target_pct": 7.5, "confidence": 70, "thesis": "t", "source": "s", "invalidation": "i"}
# live AAPL mid 200.05: stop 200.05 x 0.97 = 194.04 (down), target x 1.075 = 215.06 (up)


# -------------------------------------------------------------- validation

@pytest.mark.parametrize(
    "change, now, reason",
    [
        ({}, "10:00", None),
        ({"stop_pct": -1}, "10:00", "positive distances"),
        ({"instrument": "put", "target_pct": 120}, "10:00", "not a price"),
        ({"stop_pct": 0.1}, "10:00", "outside"),
        ({"stop_pct": 0.78}, "10:00", "outside"),                # under the 1% floor (2026-10-10)
        ({"stop_pct": 30}, "10:00", "outside"),
        ({"target_pct": 1.5}, "10:00", "reward:risk"),
        ({"confidence": 45}, "10:00", "below 50"),
        ({"horizon": "intraday"}, "15:20", "no intraday entries"),
        ({}, "15:35", "no new entries"),
        ({"symbol": "aapl;rm"}, "10:00", "malformed"),
        ({"instrument": "short"}, "10:00", "unknown instrument"),
    ],
)
def test_the_rules_engine_verdict(lab_env, change, now, reason):
    trader = trader_for(World(), lab_env)
    verdict = trader.validate({**AAPL_LONG, **change}, et(12, now), [], slots=5)
    assert verdict.ok is (reason is None), verdict.reason
    if reason:
        assert reason in verdict.reason


def test_held_traded_and_full_are_refused(lab_env):
    trader = trader_for(World(), lab_env)
    assert "already held" in trader.validate(AAPL_LONG, et(12, "10:00"), ["AAPL"], slots=5).reason
    assert "no free slot" in trader.validate(AAPL_LONG, et(12, "10:00"), [], slots=0).reason


def test_a_put_needs_its_levels_inverted(lab_env):
    trader = trader_for(World(), lab_env)
    put = {**AAPL_LONG, "instrument": "put"}
    assert trader.validate(put, et(12, "10:00"), [], slots=5).ok


# ------------------------------------------------------------- the scan + entry

def test_a_scan_opens_a_stock_at_two_percent_risk(lab_env):
    world = World()
    llm = FakeLLM([AAPL_LONG])
    trader = trader_for(world, lab_env, llm)
    notes = trader.tick(et(12, "09:46"))
    assert any("opened AAPL" in n for n in notes), notes
    qty = world.held["AAPL"]
    limit = world.orders["o1"]["price"]
    # 2% of 100,000 at risk to the 194.04 stop (set from the live mid), capped at 18.75% of NAV
    assert qty * (limit - D("194.04")) <= D("2000") + D("0.01")
    assert qty * limit <= D("100000") * D("0.75") * D("0.25")
    passes = trader.account.ledger.events(["llm_pass"])
    assert len(passes) == 1 and D(passes[0]["cost_usd"]) == D("0.20")
    # the same scan time does not run twice
    trader.tick(et(12, "09:50"))
    assert len(llm.calls) == 1
    # held symbols are rendered for the next scan, with no price
    trader.tick(et(12, "10:46"))
    assert "AAPL stock" in llm.calls[1] and "200.0" not in llm.calls[1]


def test_risk_sized_quantity_is_exact_when_uncapped(lab_env):
    world = World()
    idea = {**AAPL_LONG, "stop_pct": 15, "target_pct": 30}  # 15% stop: risk-sized, under the cap
    trader = trader_for(world, lab_env, FakeLLM([idea]))
    trader.tick(et(12, "09:46"))
    limit = world.orders["o1"]["price"]
    assert abs(world.held["AAPL"] * (limit - D("170.04")) - D("2000")) < D("0.01")


def test_the_target_and_the_stop_close_it(lab_env):
    world = World()
    trader = trader_for(world, lab_env, FakeLLM([AAPL_LONG]))
    trader.tick(et(12, "09:46"))
    world.quotes["AAPL"] = (D("215.50"), D("215.60"))
    notes = trader.tick(et(12, "10:00"))
    assert any("closed AAPL (target)" in n for n in notes) and not world.held["AAPL"]
    assert not replay_positions(trader.account.ledger.events())


def test_intraday_positions_close_at_1550(lab_env):
    world = World()
    trader = trader_for(world, lab_env, FakeLLM([{**AAPL_LONG, "horizon": "intraday"}]))
    trader.tick(et(12, "09:46"))
    assert trader.exit_reason(next(iter(trader.positions().values())), et(12, "15:49")) is None
    assert trader.exit_reason(next(iter(trader.positions().values())), et(12, "15:50")) == "intraday_close"


def test_horizons_end_after_their_sessions():
    position = Position(pid="p", symbol="AAPL", instrument="stock", horizon="days", stop=D(1), target=D(1000),
                        thesis="", opened_at=et(12, "10:00").isoformat())
    assert sessions_between(date(2026, 10, 12), date(2026, 10, 16)) == 5
    trader = SimpleNamespace(config=CFG, _quotes=lambda s: (D(100), D(100)))
    assert AiTrader.exit_reason(trader, position, et(16, "15:49")) is None
    assert AiTrader.exit_reason(trader, position, et(16, "15:50")) == "horizon_end"
    assert AiTrader.exit_reason(trader, position, et(19, "09:31")) == "horizon_end"


def test_options_close_before_expiry():
    trader = SimpleNamespace(config=CFG, _quotes=lambda s: (D(100), D(100)))
    zero = Position(pid="p", symbol="SPY", instrument="call", horizon="intraday", stop=D(1), target=D(1000),
                    thesis="", opened_at=et(12, "10:00").isoformat(), occ="SPY261012C00660000",
                    expiry=date(2026, 10, 12))
    assert AiTrader.exit_reason(trader, zero, et(12, "15:44")) is None
    assert AiTrader.exit_reason(trader, zero, et(12, "15:45")) == "option_expiry_close"
    weekly = Position(pid="q", symbol="SPY", instrument="call", horizon="days", stop=D(1), target=D(1000),
                      thesis="", opened_at=et(12, "10:00").isoformat(), occ="SPY261019C00660000",
                      expiry=date(2026, 10, 19))
    # expiry Monday -> closed the Friday before at 15:45
    assert AiTrader.exit_reason(trader, weekly, et(16, "15:44")) is None
    assert AiTrader.exit_reason(trader, weekly, et(16, "15:45")) == "option_expiry_close"


def _chain(today):
    def q(occ, delta, oi=500, bid="2.00", ask="2.10", iv="0.20", exp=today):
        return Q(occ, "SPY", "call", exp, D("660"), D(bid), D(ask), D(delta), D(iv), oi)
    return [
        q("SPY261012C00650000", "0.80"),
        q("SPY261012C00660000", "0.52"),
        q("SPY261012C00665000", "0.43", oi=50),
        q("SPY261012C00670000", "0.30"),
    ]


def test_the_contract_pick_is_the_rules_not_the_model():
    today = date(2026, 10, 12)
    pick, why = select_contract(_chain(today), "call", today, CFG.options)
    assert pick.occ_symbol == "SPY261012C00660000", why
    assert select_contract(_chain(today), "put", today, CFG.options)[0] is None
    wide = [Q(c.occ_symbol, c.underlying, c.right, c.expiration, c.strike, D("1.00"), D("2.00"), c.delta,
              c.implied_volatility, c.open_interest) for c in _chain(today)]
    assert select_contract(wide, "call", today, CFG.options)[0] is None


def test_an_option_idea_buys_whole_contracts_inside_the_premium_budget(lab_env):
    world = World()
    today = date(2026, 10, 12)
    world.chain = _chain(today)
    world.option_quotes["SPY261012C00660000"] = (D("2.00"), D("2.10"))
    idea = {**AAPL_LONG, "symbol": "SPY", "instrument": "call", "horizon": "intraday",
            "stop_pct": 1.5, "target_pct": 3}  # 1.5% stop: above the 1% floor (2026-10-10)
    trader = trader_for(world, lab_env, FakeLLM([idea]))
    trader.tick(et(12, "09:46"))
    contracts = world.held["SPY261012C00660000"]
    price = world.orders["o1"]["price"]
    assert contracts == int(D("2000") / (price * 100))
    assert contracts * price * 100 <= D("2000")
    # closed at 15:45 on its expiry day, sold at the bid
    notes = trader.tick(et(12, "15:45"))
    assert any("option_expiry_close" in n for n in notes) and not world.held["SPY261012C00660000"]


# -------------------------------------------------------------- spend, switch

def test_the_spend_cap_is_hard(lab_env):
    world = World()
    llm = FakeLLM([], cost=D("0.90"))
    trader = trader_for(world, lab_env, llm)
    for hhmm in ("09:46", "10:46", "12:01", "13:31", "14:46"):
        trader.tick(et(12, hhmm))
    # 0.90 x 3 = 2.70; a fourth pass would need 0.60 of headroom under 3.00
    assert len(llm.calls) == 3
    assert trader.spent_today(date(2026, 10, 12)) == D("2.70")
    assert trader.account.ledger.events(["spend_cap"])


def test_a_late_start_runs_one_scan_not_a_burst(lab_env):
    llm = FakeLLM([])
    trader = trader_for(World(), lab_env, llm)
    trader.tick(et(12, "13:40"))
    trader.tick(et(12, "13:41"))
    assert len(llm.calls) == 1


def test_under_the_kill_switch_no_pass_is_bought_and_exits_still_run(lab_env):
    world = World()
    trader = trader_for(world, lab_env, FakeLLM([AAPL_LONG]))
    trader.tick(et(12, "09:46"))
    trader.account.state.high_water_mark = "130000"
    world.quotes["AAPL"] = (D("193.00"), D("193.10"))  # through the stop
    llm = FakeLLM([{**AAPL_LONG, "symbol": "SPY", "stop_pct": 1.5, "target_pct": 4.5}])
    trader.llm = llm
    notes = trader.tick(et(12, "10:46"))
    assert trader.account.state.kill_switch_tripped
    assert any("closed AAPL (stop)" in n for n in notes) and llm.calls == []


def test_a_failed_entry_leaves_no_position(lab_env):
    world = World()
    world.fill = False
    trader = trader_for(world, lab_env, FakeLLM([AAPL_LONG]))
    trader.tick(et(12, "09:46"))
    assert not trader.positions() and all(o["status"] == "canceled" for o in world.orders.values())


# -------------------------------------------------------------- golden grading

@pytest.mark.parametrize(
    "case, ideas, passed",
    [
        ({"name": "x", "time": "10:00", "held": [], "expect": {"max_ideas": 3}}, [AAPL_LONG], True),
        ({"name": "x", "time": "11:00", "held": [], "expect": {"max_ideas": 0}}, [AAPL_LONG], False),
        ({"name": "x", "time": "12:00", "held": [], "expect": {"forbidden_symbols": ["AAPL"]}}, [AAPL_LONG], False),
        ({"name": "x", "time": "15:20", "held": [], "expect": {"forbidden_horizons": ["intraday"]}},
         [{**AAPL_LONG, "horizon": "intraday"}], False),
        ({"name": "x", "time": "10:00", "held": [], "expect": {}}, [{**AAPL_LONG, "stop_pct": -2}], False),
    ],
)
def test_golden_grading(case, ideas, passed):
    result = golden_replay.run_case(case, CFG, FakeLLM(ideas), date(2026, 10, 12))
    assert result.passed is passed, result.failures


def test_golden_cases_load_and_held_positions_reach_the_prompt():
    cases = golden_replay.load_cases()
    assert {c["name"] for c in cases} >= {"full_book_proposes_nothing", "entries_closed_late"}
    llm = FakeLLM([])
    case = next(c for c in cases if c["name"] == "held_and_traded_symbols_excluded")
    golden_replay.run_case(case, CFG, llm, date(2026, 10, 12))
    assert "NVDA stock" in llm.calls[0] and "TSLA" in llm.calls[0]


# ------------------------------------------------------------- the LLM wrapper

def test_the_wrapper_meters_tokens_and_searches():
    from lab.llm import LabLLM

    class Usage:
        def __init__(self, inp, out, searches):
            self.input_tokens, self.output_tokens = inp, out
            self.cache_creation_input_tokens = self.cache_read_input_tokens = 0
            self.server_tool_use = SimpleNamespace(web_search_requests=searches)

    class Messages:
        def __init__(self):
            self.requests = []

        def create(self, **request):
            self.requests.append(request)
            if request.get("tool_choice"):
                block = SimpleNamespace(type="tool_use", name="submit_trade_ideas", input={"market_view": "m", "ideas": []})
                return SimpleNamespace(content=[block], stop_reason="tool_use", model=request["model"],
                                       usage=Usage(2000, 300, 0))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="searched")], stop_reason="end_turn",
                                   model=request["model"], usage=Usage(10000, 500, 3))

    fake = SimpleNamespace(messages=Messages())
    llm = LabLLM(CFG, client=fake)
    result = llm.scan(system="s", user="u", tool=TOOL)
    search, report = fake.messages.requests
    assert search["model"] == report["model"] == "claude-sonnet-4-6"
    assert search["tools"][0]["max_uses"] == CFG.max_searches
    assert search["tools"][0]["type"] == "web_search_20250305"  # the plain tool (2026-10-10)
    assert report["tool_choice"] == {"type": "tool", "name": "submit_trade_ideas"}
    tokens = (D(12000) * D("3.00") + D(800) * D("15.00")) / D(1_000_000)
    assert result.searches == 3 and result.token_cost_usd == tokens
    assert result.cost_usd == tokens + D("0.03")


def test_a_failed_pass_is_a_fault_not_a_trade(lab_env):
    class Broken(FakeLLM):
        def scan(self, **kwargs):
            raise TimeoutError("request timed out")

    world = World()
    trader = trader_for(world, lab_env, Broken())
    notes = trader.tick(et(12, "09:46"))
    assert any("pass failed" in n for n in notes) and not world.orders


def test_the_main_books_search_tool_is_unchanged():
    from research.client import WEB_SEARCH_TOOL_TYPE
    from research.config import ResearchConfig

    assert ResearchConfig.load().web_search.tool_type is None
    assert WEB_SEARCH_TOOL_TYPE == "web_search_20260209"


# ---------------------------------------- price anchoring (ruling 2026-10-10)

def test_levels_are_set_from_the_live_price_at_the_stated_distances():
    from lab.ai_trader import levels

    assert levels("stock", D("200.05"), D("0.03"), D("0.075")) == (D("194.04"), D("215.06"))
    # a put: stop above, target below; each rounded to keep at least the stated distance
    assert levels("put", D("200.05"), D("0.03"), D("0.075")) == (D("206.06"), D("185.04"))


def test_the_model_cannot_anchor_a_level_to_a_price_it_cites(lab_env):
    """The model 'saw' AAPL at 300 (and cites $310 in its thesis); the live
    mid is 200.05. The levels come from 200.05; the gap is logged, not used."""
    world = World()
    idea = {**AAPL_LONG, "price_seen": 300, "thesis": "breakout above $310 resistance"}
    trader = trader_for(world, lab_env, FakeLLM([idea]))
    trader.tick(et(12, "09:46"))
    opened = trader.account.ledger.events(["position_opened"])[0]
    assert (opened["stop"], opened["target"], opened["anchor_mid"]) == ("194.04", "215.06", "200.05")
    check = trader.account.ledger.events(["anchor_check"])[0]
    assert check["live_mid"] == "200.05" and check["price_seen"] == "300"
    assert D(check["price_seen_gap"]) == D("0.4996")
    assert check["thesis_prices"] == ["310"] and D(check["thesis_gaps"][0]) == D("0.5496")


def test_the_report_tool_carries_no_absolute_level():
    item = TOOL["input_schema"]["properties"]["ideas"]["items"]
    assert not {"stop", "target", "entry_reference"} & set(item["properties"])
    assert {"stop_pct", "target_pct"} <= set(item["required"])
    assert "price_seen" not in item["required"]
    text = system_prompt(CFG)
    assert "PERCENTAGE DISTANCES" in text and "entry_reference" not in text


def test_the_explicit_gross_cap_refuses_an_entry_past_it(lab_env):
    # Human ruling 2026-10-14 (audit item 4): open exposure plus the new order
    # may not pass max_gross_fraction of NAV (shipped 1.00).
    assert CFG.max_gross_fraction == D("1.00")
    world = World()
    trader = trader_for(world, lab_env, FakeLLM([AAPL_LONG]))
    trader.config = CFG.model_copy(update={"max_gross_fraction": D("0.05")})  # 5% of 100,000
    trader.tick(et(12, "09:46"))
    assert "AAPL" not in world.held or not world.held["AAPL"]
    rejected = trader.account.ledger.events(["idea_rejected"])
    assert rejected and "gross cap" in rejected[-1]["reason"]


# ------------------------------------------------- leverage, measured (2026-10-14)

def test_leverage_factors_come_from_the_table_then_the_fund_name():
    from lab import leverage

    assert leverage.from_name("Direxion Daily Semiconductor Bear 3X Shares") == D("-3")
    assert leverage.from_name("ProShares UltraPro QQQ") == D("3")
    assert leverage.from_name("ProShares UltraShort S&P500") == D("-2")
    assert leverage.from_name("ProShares Short S&P500") == D("-1")
    assert leverage.from_name("SPDR S&P 500 ETF Trust") is None
    assert leverage.from_name("iShares 7-10 Year Treasury Bond ETF") is None
    table = {"SOXS": D("-3")}
    assert leverage.factor("SOXS", "etf", table) == D("-3")
    assert leverage.factor("SOXS", "call", table) == D("1")  # an option counts its premium
    assert leverage.factor("XYZ", "etf", table, "Some Bull 2X Shares") == D("2")
    assert leverage.tag(D("-3")) == leverage.LEVERAGED and leverage.tag(D("-1")) == leverage.INVERSE
    assert leverage.tag(D("1")) is None
    assert CFG.leverage_factors["SOXS"] == D("-3") and CFG.gross_basis == "face"


SOXS_LONG = {**AAPL_LONG, "symbol": "SOXS", "instrument": "etf"}


def _soxs_world():
    world = World()
    world.quotes["SOXS"] = (D("40.00"), D("40.02"))
    return world


def test_a_leveraged_etf_entry_is_tagged_and_counted_at_its_leverage(lab_env):
    world = _soxs_world()
    trader = trader_for(world, lab_env, FakeLLM([SOXS_LONG]))
    trader.tick(et(12, "09:46"))
    plan = trader.account.ledger.events(["position_opened"])[-1]
    assert plan["leverage"] == "-3" and plan["tag"] == "leveraged_etf"
    face, adjusted = trader.gross()
    assert face > 0 and adjusted == face * 3
    assert abs(D(plan["gross_after_leverage_adjusted"]) - D(plan["gross_after_face"]) * 3) < D("0.0005")


def test_the_gross_cap_enforces_on_the_configured_basis(lab_env):
    # face: an 18.75%-of-NAV SOXS entry passes a 30% cap; leverage-adjusted
    # (x3 = 56%) does not. Face is shipped until the human rules.
    face_world = _soxs_world()
    trader = trader_for(face_world, lab_env, FakeLLM([SOXS_LONG]))
    trader.config = CFG.model_copy(update={"max_gross_fraction": D("0.30")})
    trader.tick(et(12, "09:46"))
    assert face_world.held.get("SOXS")
    adjusted_world = _soxs_world()
    trader = trader_for(adjusted_world, lab_env / "b", FakeLLM([SOXS_LONG]))
    trader.config = CFG.model_copy(update={"max_gross_fraction": D("0.30"), "gross_basis": "leverage_adjusted"})
    trader.tick(et(12, "09:46"))
    assert not adjusted_world.held.get("SOXS")
    reason = trader.account.ledger.events(["idea_rejected"])[-1]["reason"]
    assert "gross cap (leverage_adjusted)" in reason and "face" in reason


def test_the_scoreboard_shows_leveraged_trades_apart():
    from lab import scoreboard

    events = [
        {"event": "position_opened", "pid": "a", "symbol": "SOXS", "instrument": "etf", "at": "2026-10-13T14:00:00+00:00"},
        {"event": "position_opened", "pid": "b", "symbol": "GS", "instrument": "stock", "at": "2026-10-13T14:00:00+00:00"},
    ]
    for pid, buy, sell in (("a", "40", "42"), ("b", "900", "890")):
        events += [{"event": "settled", "pid": pid, "side": "buy", "filled_qty": "10", "avg_price": buy},
                   {"event": "settled", "pid": pid, "side": "sell", "filled_qty": "10", "avg_price": sell, "reason": "stop"},
                   {"event": "position_closed", "pid": pid}]
    trades = scoreboard.lab_trades(events, {}, {"SOXS": D("-3")})  # opened before tagging: the table tags it
    assert {t.symbol: t.tag for t in trades} == {"SOXS": "leveraged_etf", "GS": None}


def test_the_leverage_watch_alerts_once_per_crossing_and_never_blocks(lab_env):
    # Human ruling 2026-10-14 (third): urgent alert past 150% leverage-adjusted
    # gross, once per crossing, re-armed below 130%; the cap stays on face.
    assert (CFG.leverage_alert_above, CFG.leverage_alert_rearm_below, CFG.gross_basis) == (D("1.50"), D("1.30"), "face")
    world = _soxs_world()
    fired = []
    trader = trader_for(world, lab_env, FakeLLM([SOXS_LONG]))
    trader._leverage_alert = lambda adjusted, face, top: fired.append((adjusted, face, top))
    trader.config = CFG.model_copy(update={"leverage_alert_above": D("0.50"), "leverage_alert_rearm_below": D("0.30")})
    trader.tick(et(12, "09:46"))                 # buys SOXS: ~19% face, ~56% leverage-adjusted
    assert world.held.get("SOXS")                # not blocked
    trader.tick(et(12, "09:47"))                 # the watch sees it: one alert
    trader.tick(et(12, "09:48"))                 # still above: no second alert
    assert len(fired) == 1 and fired[0][2][0][0] == "SOXS" and fired[0][2][0][1] == D("-3")
    assert abs(fired[0][0] - fired[0][1] * 3) < D("0.001")
    assert trader.account.state.engine["leverage_alert_armed"] is False
    # re-arming and the second crossing, driven by the gross figure itself (a
    # real price fall would also hit the position's stop and sell it)
    nav = trader.gate.state.nav
    for adjusted, armed, alerts in ((D("0.40"), False, 1),   # between the lines: still disarmed
                                    (D("0.29"), True, 1),    # below 30%: re-armed, no alert
                                    (D("0.45"), True, 1),    # between: armed, no alert
                                    (D("0.51"), False, 2)):  # crosses 50% again: the second alert
        trader.gross = lambda a=adjusted: (nav * a / 3, nav * a)
        trader._leverage_watch()
        assert trader.account.state.engine["leverage_alert_armed"] is armed and len(fired) == alerts
    assert [e["event"] for e in trader.account.ledger.events(["leverage_alert", "leverage_alert_rearmed"])] == [
        "leverage_alert", "leverage_alert_rearmed", "leverage_alert"]


def test_the_leverage_card_is_yellow_on_urgent_and_says_nothing_is_blocked():
    from execution import cards

    card = cards.leverage_card("AI trader", 1.62, 0.6, [("SOXS", -3.0, 0.61), ("SQQQ", -3.0, 0.58), ("XLE", 1.0, 0.19)],
                               1.5, 1.3)
    assert card.status == cards.LOOK and card.channel == cards.URGENT
    text = " ".join(card.all_lines)
    assert "162%" in text and "60%" in text and "SOXS 61% (-3x)" in text and "Nothing is blocked" in text
    assert "below 130%" in text and "150%" in text


# ----------------------------------------------------- the research brief (2026-10-15)


class ToolLLM(FakeLLM):
    def scan(self, *, system, user, tool):
        self.systems = getattr(self, "systems", []) + [system]
        self.tools = getattr(self, "tools", []) + [tool]
        return super().scan(system=system, user=user, tool=tool)


def test_with_the_brief_on_a_scan_sends_it_logs_it_and_the_position_carries_its_use(lab_env):
    from lab.brief import BEGIN, END, Brief

    world = World()
    idea = dict(AAPL_LONG, brief_use="draws_on", brief_items="E11: published edges decay; this is not one")
    llm = ToolLLM([idea])
    trader = trader_for(world, lab_env, llm)
    trader.config = CFG.model_copy(update={"brief_enabled": True})
    text = f"{BEGIN}\n(b) EVIDENCE LIBRARY\nE11 ...\n{END}"
    trader._brief = lambda: Brief(text, "sha0123456789abc", len(text), {"evidence": "ok"})
    trader.tick(et(12, "09:46"))
    assert llm.calls[0].rstrip().endswith(END) and "RESEARCH BRIEF" in llm.systems[0]
    assert "brief_use" in llm.tools[0]["input_schema"]["properties"]["ideas"]["items"]["required"]
    passes = trader.account.ledger.events(["llm_pass"])
    assert passes[0]["brief"]["sha"] == "sha0123456789abc" and passes[0]["ideas"][0]["brief_use"] == "draws_on"
    opened = trader.account.ledger.events(["position_opened"])
    assert opened[0]["brief_use"] == "draws_on" and opened[0]["brief_items"].startswith("E11")


def test_with_the_brief_off_nothing_changes(lab_env):
    world = World()
    llm = ToolLLM([AAPL_LONG])
    trader = trader_for(world, lab_env, llm)
    trader._brief = lambda: (_ for _ in ()).throw(AssertionError("rendered while off"))
    trader.tick(et(12, "09:46"))
    assert llm.tools[0] is TOOL and "RESEARCH BRIEF" not in llm.systems[0] and "RESEARCH BRIEF" not in llm.calls[0]
    assert trader.account.ledger.events(["llm_pass"])[0]["brief"] is None


def test_a_failed_brief_never_stops_a_scan(lab_env):
    world = World()
    llm = ToolLLM([AAPL_LONG])
    trader = trader_for(world, lab_env, llm)
    trader.config = CFG.model_copy(update={"brief_enabled": True})
    trader._brief = lambda: (_ for _ in ()).throw(OSError("disk"))
    notes = trader.tick(et(12, "09:46"))
    assert "(unavailable this scan)" in llm.calls[0] and any("opened AAPL" in n for n in notes)


@pytest.mark.parametrize("idea, passed", [
    (dict(AAPL_LONG, brief_use="none"), True),
    (dict(AAPL_LONG, brief_use="contradicts", brief_items="E8 says breakouts fail; this is a catalyst"), True),
    (dict(AAPL_LONG, brief_use="draws_on", brief_items="the shortlist"), False),
    (dict(AAPL_LONG), False),
])
def test_golden_grades_the_brief_fields_when_it_is_on(idea, passed):
    class Once(FakeLLM):
        pass

    on = CFG.model_copy(update={"brief_enabled": True})
    result = golden_replay.run_case({"name": "x", "time": "10:00", "held": [], "expect": {}}, on, Once([idea]),
                                    date(2026, 10, 16), brief=None)
    assert result.passed is passed, result.failures
