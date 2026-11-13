"""The AI trader review's rulings (human ruling 2026-11-12, items 1-4): the
quote rule for stops, targets and levels with the "would exit" shadow; one
bet per sector or theme and one new option per scan; at least 7 days to
expiry for days and weeks, with the old window's contract priced beside it;
move since public, logged only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from execution.quotes import QuoteReader, Reading
from lab.ai_prompts import TOOL, report_tool, system_prompt
from lab.ai_trader import Position, price_exit
from lab.config import LabConfig
from test_lab_ai import AAPL_LONG, CFG, Q, FakeLLM, World, _chain, et, lab_env, trader_for  # noqa: F401

D = Decimal


def _reader(world, last):
    """The quote rule over the world's quotes, with a last-trade table."""
    return QuoteReader(lambda s: None, sides=world.quote, last_trade=lambda s: last.get(s), report=lambda *a: None)


def _ruled(world, tmp_path, ideas, last=None):
    trader = trader_for(world, tmp_path, FakeLLM(ideas))
    trader._reader = _reader(world, {} if last is None else last)
    return trader


# -- item 1: the quote rule for stops, targets and levels ------------------------

def test_a_wide_quote_mid_under_the_stop_never_stops_and_the_shadow_logs_it(lab_env):
    world, last = World(), {}
    trader = _ruled(world, lab_env, [AAPL_LONG], last)
    trader.tick(et(12, "09:46"))                        # in at mid 200.05: stop 194.04, target 215.06
    world.quotes["AAPL"] = (D("180.00"), D("206.00"))   # 13% wide: its mid 193.00 is under the stop
    last["AAPL"] = D("199.90")                          # what really traded
    notes = trader.tick(et(12, "10:00"))
    assert not any("closed AAPL" in n for n in notes) and world.held["AAPL"]
    [row] = trader.account.ledger.events(["exit_shadow"])
    assert (row["old_rule"], row["new_rule"], row["wide"]) == ("stop", None, True)
    assert D(row["last"]) == D("199.90") and D(row["bid"]) == D("180.00")
    trader.tick(et(12, "10:01"))                        # one row per position, pair and day
    assert len(trader.account.ledger.events(["exit_shadow"])) == 1
    last["AAPL"] = D("194.00")                          # the market trades through the stop: out
    assert any("closed AAPL (stop)" in n for n in trader.tick(et(12, "10:02")))


def test_a_wide_ask_never_fires_a_target_the_market_never_traded(lab_env):
    """TRMK 2026-11-12: an IEX ask of 54.78 against a 47.88 bid put the mid
    over the target and sold the position below its entry."""
    world, last = World(), {"AAPL": D("199.20")}
    trader = _ruled(world, lab_env, [AAPL_LONG], last)
    trader.tick(et(12, "09:46"))
    world.quotes["AAPL"] = (D("199.00"), D("240.00"))   # mid 219.50, over the 215.06 target
    assert not any("closed AAPL" in n for n in trader.tick(et(12, "10:00")))
    [row] = trader.account.ledger.events(["exit_shadow"])
    assert (row["old_rule"], row["new_rule"]) == ("target", None)


def test_on_a_tight_quote_a_long_stops_on_the_bid(lab_env):
    world = World()
    trader = _ruled(world, lab_env, [AAPL_LONG])
    trader.tick(et(12, "09:46"))
    world.quotes["AAPL"] = (D("194.00"), D("194.20"))   # bid at the stop, mid 194.10 above it
    assert any("closed AAPL (stop)" in n for n in trader.tick(et(12, "10:00")))
    [row] = trader.account.ledger.events(["exit_shadow"])
    assert (row["old_rule"], row["new_rule"]) == (None, "stop")


def test_a_puts_stop_reads_a_tight_mid_or_else_the_last_trade():
    put = Position(pid="p", symbol="AAPL", instrument="put", horizon="days", stop=D("210"), target=D("190"),
                   thesis="", opened_at=et(12, "10:00").isoformat())
    assert price_exit(put, Reading(D("209.95"), D("210.15"), D("210.05"), D("209.95"))) == "stop"
    assert price_exit(put, Reading(D("200"), D("230"), None, D("205"), D("205"), wide=True)) is None
    assert price_exit(put, Reading(D("200"), D("230"), None, D("211"), D("211"), wide=True)) == "stop"
    assert price_exit(put, Reading(None, None, None, None)) is None       # no usable price: nothing fires


def test_the_exit_shadow_ends_on_its_date(lab_env):
    world, last = World(), {"AAPL": D("199.90")}
    trader = _ruled(world, lab_env, [AAPL_LONG], last)
    trader.config = CFG.model_copy(update={"exit_shadow_until": date(2026, 10, 11)})
    trader.tick(et(12, "09:46"))
    world.quotes["AAPL"] = (D("180.00"), D("206.00"))
    trader.tick(et(12, "10:00"))
    assert trader.account.ledger.events(["exit_shadow"]) == []
    assert CFG.exit_shadow_until == date(2026, 11, 26)      # 10 sessions from 2026-11-13


def test_levels_come_from_the_last_trade_when_the_quote_is_wide(lab_env):
    """LUNR 2026-11-12: an IEX mid 6.8% under the fill put an "8%" stop 14% away."""
    world, last = World(), {"AAPL": D("200.00")}
    world.quotes["AAPL"] = (D("180.00"), D("220.00"))
    trader = _ruled(world, lab_env, [AAPL_LONG], last)
    trader.tick(et(12, "09:46"))
    [plan] = trader.account.ledger.events(["position_opened"])
    assert plan["anchor_source"] == "last_trade" and D(plan["anchor_mid"]) == D("200.00")
    assert D(plan["stop"]) == D("194.00") and D(plan["target"]) == D("215.00")


def test_no_usable_price_means_no_entry(lab_env):
    world = World()
    world.quotes["AAPL"] = (D("180.00"), D("220.00"))      # wide, and no last trade
    trader = _ruled(world, lab_env, [AAPL_LONG])
    trader.tick(et(12, "09:46"))
    assert not trader.positions()
    assert "no usable price" in trader.account.ledger.events(["idea_rejected"])[-1]["reason"]


# -- item 2: one bet per sector or theme; one new option per scan --------------------

def test_a_second_idea_in_the_same_theme_is_refused_and_logged_with_its_levels(lab_env):
    world = World()
    world.quotes["ABCD"] = (D("50.00"), D("50.02"))        # unmapped: it can collide only by theme
    trader = _ruled(world, lab_env, [AAPL_LONG, {**AAPL_LONG, "symbol": "ABCD"}])
    trader.tick(et(12, "09:46"))
    assert {p.symbol for p in trader.positions().values()} == {"AAPL"}
    [row] = trader.account.ledger.events(["idea_rejected"])
    assert row["reason"] == "concentration: theme software_internet already proposed in this pass (AAPL)"
    assert row["code"] == "concentration"
    assert row["levels"] == {"anchor": "50.01", "stop": "48.50", "target": "53.77"}   # never nearer than asked


def test_an_earlier_refused_idea_never_blocks_and_the_strict_shadow_logs_it(lab_env):
    """Readings 2026-11-12, later: only placed ideas count; the stricter
    reading (every earlier idea) is a logged shadow."""
    world = World()
    world.quotes["ABCD"] = (D("50.00"), D("50.02"))
    trader = _ruled(world, lab_env, [{**AAPL_LONG, "confidence": 40}, {**AAPL_LONG, "symbol": "ABCD"}])
    trader.tick(et(12, "09:46"))
    assert {p.symbol for p in trader.positions().values()} == {"ABCD"}
    [refused] = trader.account.ledger.events(["idea_rejected"])
    assert refused["reason"].startswith("confidence 40")
    [shadow] = trader.account.ledger.events(["concentration_strict_shadow"])
    [plan] = trader.account.ledger.events(["position_opened"])
    assert shadow["symbol"] == "ABCD" and shadow["pid"] == plan["pid"] and shadow["placed"] is True
    assert shadow["reason"] == "concentration: theme software_internet already proposed in this pass (AAPL)"


def test_a_placed_entry_that_did_not_fill_still_counts(lab_env):
    world = World()
    world.fill = False
    world.quotes["ABCD"] = (D("50.00"), D("50.02"))
    trader = _ruled(world, lab_env, [AAPL_LONG, {**AAPL_LONG, "symbol": "ABCD"}])
    trader.tick(et(12, "09:46"))
    assert [p["symbol"] for p in trader.account.ledger.events(["position_opened"])] == ["AAPL"]
    assert trader.account.ledger.events(["idea_rejected"])[-1]["reason"] == (
        "concentration: theme software_internet already proposed in this pass (AAPL)")
    assert trader.account.ledger.events(["concentration_strict_shadow"]) == []


def test_other_never_collides_by_theme_but_its_sector_still_does(lab_env):
    world = World()
    world.quotes["ABCD"] = (D("50.00"), D("50.02"))      # unmapped: no sector
    world.quotes["MSFT"] = (D("500.00"), D("500.10"))
    other = {**AAPL_LONG, "theme": "other"}
    trader = _ruled(world, lab_env, [{**other, "symbol": "ABCD"}, other, {**other, "symbol": "MSFT"}])
    trader.tick(et(12, "09:46"))
    assert [p["symbol"] for p in trader.account.ledger.events(["position_opened"])] == ["ABCD", "AAPL"]
    assert trader.account.ledger.events(["idea_rejected"])[-1]["reason"] == (
        "concentration: sector big_tech already proposed in this pass (AAPL)")
    assert CFG.concentration.exempt_themes == ("other",)


def test_a_held_sector_refuses_an_idea_under_another_theme(lab_env):
    world = World()
    world.quotes["MSFT"] = (D("500.00"), D("500.10"))
    trader = _ruled(world, lab_env, [AAPL_LONG])
    trader.tick(et(12, "09:46"))
    trader.llm.ideas = [{**AAPL_LONG, "symbol": "MSFT", "theme": "consumer"}]
    trader.tick(et(12, "10:46"))
    assert trader.account.ledger.events(["idea_rejected"])[-1]["reason"] == (
        "concentration: sector big_tech already held (AAPL)")
    assert "theme software_internet" in trader.llm.calls[-1]      # the held position's theme reaches the prompt


def test_at_most_one_new_option_per_scan(lab_env):
    world = World()
    world.chain = _chain(date(2026, 10, 12))
    world.option_quotes["SPY261012C00660000"] = (D("2.00"), D("2.10"))
    spy = {**AAPL_LONG, "symbol": "SPY", "instrument": "call", "horizon": "intraday", "theme": "broad_market",
           "stop_pct": 1.5, "target_pct": 3}
    world.quotes["ABCD"] = (D("50.00"), D("50.02"))
    trader = _ruled(world, lab_env, [spy, {**spy, "symbol": "ABCD", "theme": "crypto"}])
    trader.tick(et(12, "09:46"))
    assert trader.account.ledger.events(["idea_rejected"])[-1]["reason"] == (
        "concentration: at most 1 new option(s) per scan")
    assert len(trader.account.ledger.events(["position_opened"])) == 1


def test_an_idea_without_a_listed_theme_is_refused(lab_env):
    trader = _ruled(World(), lab_env, [{**AAPL_LONG, "theme": "memes"}])
    trader.tick(et(12, "09:46"))
    assert "not one of the configured themes" in trader.account.ledger.events(["idea_rejected"])[-1]["reason"]


def test_the_prompt_and_the_tool_carry_the_theme_list_and_the_rule():
    text = system_prompt(CFG)
    assert "One bet per theme" in text and "at most 1 new option(s) per scan" in text and "best first" in text
    for name in CFG.concentration.themes:
        assert f"  {name}: " in text
    tool = report_tool(False, list(CFG.concentration.themes))
    item = tool["input_schema"]["properties"]["ideas"]["items"]
    assert "theme" in item["required"] and item["properties"]["theme"]["enum"] == list(CFG.concentration.themes)
    assert "theme" not in TOOL["input_schema"]["properties"]["ideas"]["items"]["properties"]   # no mutation


# -- item 3: at least 7 days to expiry for days and weeks ----------------------------

def test_days_and_weeks_options_carry_at_least_seven_days():
    assert CFG.options.dte["days"][0] >= 7 and CFG.options.dte["weeks"][0] >= 7
    assert CFG.options.dte["intraday"] == (0, 2)
    assert "at least 7 days to expiry" in system_prompt(CFG)
    raw = LabConfig.load().model_dump(mode="json")
    raw["ai_trader"]["options"]["dte"]["days"] = [2, 14]
    with pytest.raises(Exception, match="7 or more"):
        LabConfig.model_validate(raw)


class WindowWorld(World):
    """A chain whose expiries the requested window filters."""

    def chain_for(self, underlying, *, min_expiry, max_expiry=None):
        return [q for q in self.chain if min_expiry <= q.expiration <= (max_expiry or q.expiration)]


def _two_expiries(world):
    def q(occ, exp):
        return Q(occ, "SPY", "call", exp, D("660"), D("2.00"), D("2.10"), D("0.52"), D("0.20"), 500)
    world.chain = [q("SPY261014C00660000", date(2026, 10, 14)), q("SPY261019C00660000", date(2026, 10, 19))]
    world.option_quotes = {"SPY261014C00660000": (D("1.10"), D("1.20")), "SPY261019C00660000": (D("2.00"), D("2.10"))}


SPY_DAYS_CALL = {**AAPL_LONG, "symbol": "SPY", "instrument": "call", "theme": "broad_market"}


def test_the_old_windows_contract_is_priced_at_entry_and_at_its_own_expiry_close(lab_env):
    world = WindowWorld()
    _two_expiries(world)
    trader = _ruled(world, lab_env, [SPY_DAYS_CALL])
    trader.tick(et(12, "09:46"))
    [plan] = trader.account.ledger.events(["position_opened"])
    assert plan["occ"] == "SPY261019C00660000"                  # 7 DTE, not the 2-DTE contract
    [entry] = trader.account.ledger.events(["expiry_counterfactual"])
    assert entry["phase"] == "entry" and entry["occ"] == "SPY261014C00660000" and entry["same_contract"] is False
    # the 2-DTE contract would have been closed the session before its expiry, at 15:45
    trader.tick(et(13, "15:44"))
    assert len(trader.account.ledger.events(["expiry_counterfactual"])) == 1
    trader.tick(et(13, "15:45"))
    mark = trader.account.ledger.events(["expiry_counterfactual"])[-1]
    assert mark["phase"] == "mark" and mark["when"] == "its own expiry close" and D(mark["bid"]) == D("1.10")
    assert not trader.account.state.engine["expiry_counterfactual"]


def test_the_counterfactual_is_priced_at_our_exit_when_that_comes_first(lab_env):
    world = WindowWorld()
    _two_expiries(world)
    trader = _ruled(world, lab_env, [SPY_DAYS_CALL])
    trader.tick(et(12, "09:46"))
    world.quotes["SPY"] = (D("600.00"), D("600.10"))            # through the stop
    trader.tick(et(12, "10:00"))
    mark = trader.account.ledger.events(["expiry_counterfactual"])[-1]
    assert mark["phase"] == "mark" and mark["when"] == "exit: stop"


# -- item 4: move since public, logged only --------------------------------------------

class FakeClock:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def measure(self, symbol, catalyst, sign, now, price):
        self.calls.append((symbol, catalyst, sign, price))
        if self.fail:
            raise RuntimeError("feed down")
        return {"status": "ok", "move": "0.05", "would_skip": True}


def test_every_proposed_idea_gets_a_move_since_public_row_after_the_entries(lab_env):
    world = World()
    world.quotes["ABCD"] = (D("50.00"), D("50.02"))
    trader = _ruled(world, lab_env, [{**AAPL_LONG, "catalyst": "Q3 beat"}, {**AAPL_LONG, "symbol": "ABCD"}])
    trader._catalyst_clock = FakeClock()
    trader.tick(et(12, "09:46"))
    rows = trader.account.ledger.events(["move_since_public"])
    assert [r["symbol"] for r in rows] == ["AAPL", "ABCD"]                # taken and refused alike
    [plan] = trader.account.ledger.events(["position_opened"])
    assert rows[0]["pid"] == plan["pid"] and rows[1]["pid"] is None and rows[0]["would_skip"] is True
    assert trader._catalyst_clock.calls[0][:3] == ("AAPL", "Q3 beat", 1)
    assert trader._catalyst_clock.calls[0][3] == D("200.05")             # the anchored price


def test_a_failing_clock_is_a_row_never_a_lost_entry(lab_env):
    trader = _ruled(World(), lab_env, [AAPL_LONG])
    trader._catalyst_clock = FakeClock(fail=True)
    trader.tick(et(12, "09:46"))
    assert trader.positions()
    [row] = trader.account.ledger.events(["move_since_public"])
    assert row["status"] == "error" and "feed down" in row["error"]


def test_the_shared_quote_rule_is_one_class():
    from orchestrator.quotes import QuoteReader as Main

    assert QuoteReader is Main
