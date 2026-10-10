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
    assert "0.3%" in text and "25%" in text
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


AAPL_LONG = {"symbol": "AAPL", "instrument": "stock", "horizon": "days", "entry_reference": 200.05,
             "stop": 194, "target": 215, "confidence": 70, "thesis": "t", "source": "s", "invalidation": "i"}


# -------------------------------------------------------------- validation

@pytest.mark.parametrize(
    "change, now, reason",
    [
        ({}, "10:00", None),
        ({"stop": 201}, "10:00", "stop < price < target"),
        ({"instrument": "put"}, "10:00", "put idea needs"),
        ({"stop": 199.8}, "10:00", "outside"),                    # 0.1% stop
        ({"stop": 140}, "10:00", "outside"),                      # 30% stop
        ({"target": 203}, "10:00", "reward:risk"),
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
    put = {**AAPL_LONG, "instrument": "put", "stop": 206, "target": 185}
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
    # 2% of 100,000 at risk to the 194 stop, capped at 18.75% of NAV notional
    assert qty * (limit - 194) <= D("2000") + D("0.01")
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
    idea = {**AAPL_LONG, "stop": 170, "target": 260}  # 15% stop: risk-sized, under the cap
    trader = trader_for(world, lab_env, FakeLLM([idea]))
    trader.tick(et(12, "09:46"))
    limit = world.orders["o1"]["price"]
    assert abs(world.held["AAPL"] * (limit - 170) - D("2000")) < D("0.01")


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
    idea = {**AAPL_LONG, "symbol": "SPY", "instrument": "call", "horizon": "intraday", "entry_reference": 660.02,
            "stop": 655, "target": 668}
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
    llm = FakeLLM([{**AAPL_LONG, "symbol": "SPY", "entry_reference": 660, "stop": 650, "target": 690}])
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
        ({"name": "x", "time": "10:00", "held": [], "expect": {}}, [{**AAPL_LONG, "stop": 210}], False),
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
