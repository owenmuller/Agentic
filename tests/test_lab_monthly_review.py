"""The AI trader's monthly deep review (human ruling 2026-11-13, item 1): code
computes the facts, the model writes the diagnosis and ranked proposals that
must cite them, and nothing applies until the human rules."""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from execution import cards
from execution.market_calendar import MarketCalendar
from lab import monthly_review as review
from lab.config import LabConfig

D = date


def weekdays(start, end, close=100.0, **overrides):
    out, day = {}, start
    while day <= end:
        if day.weekday() < 5:
            out[day] = overrides.get(day.isoformat(), (close, close))
        day += timedelta(days=1)
    return out


BARS = {"SPY": weekdays(D(2026, 9, 25), D(2026, 10, 31), **{"2026-10-15": (100.0, 101.0)}),
        "AAA": weekdays(D(2026, 9, 25), D(2026, 10, 31), 9.8),
        "BBB": weekdays(D(2026, 9, 25), D(2026, 10, 31), 50.0)}


def bars(symbol):
    return BARS.get(symbol, {})


def opened(pid, symbol, at, **extra):
    return {"event": "position_opened", "pid": pid, "symbol": symbol, "instrument": "stock", "horizon": "days",
            "scan": "09:45", "at": at, "anchor_mid": "10.0", "confidence": 62, "catalyst": "earnings beat",
            "brief_arm": "control", **extra}


def fill(pid, side, qty, price, at):
    return {"event": "settled", "pid": pid, "side": side, "filled_qty": str(qty), "avg_price": str(price),
            "multiplier": 1, "at": at}


EVENTS = [
    {"event": "llm_pass", "at": "2026-10-13T13:46:00+00:00", "scan": "09:45", "brief_arm": "control",
     "ideas": [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}], "ideas_on_shortlist": [False] * 3},
    opened("a", "AAA", "2026-10-13T13:46:00+00:00"), fill("a", "buy", 100, 10.0, "2026-10-13T13:46:10+00:00"),
    fill("a", "sell", 100, 9.7, "2026-10-13T15:00:00+00:00"),
    {"event": "position_closed", "pid": "a", "reason": "stop", "at": "2026-10-13T15:00:01+00:00"},
    opened("b", "BBB", "2026-10-13T13:46:00+00:00", anchor_mid="51.0"),
    fill("b", "buy", 10, 51.0, "2026-10-13T13:46:12+00:00"), fill("b", "sell", 10, 53.0, "2026-10-15T19:50:00+00:00"),
    {"event": "position_closed", "pid": "b", "reason": "horizon_end", "at": "2026-10-15T19:50:01+00:00"},
    {"event": "idea_rejected", "at": "2026-10-13T13:46:00+00:00", "scan": "09:45", "symbol": "CCC",
     "reason": "concentration: theme ai_semis already proposed in this pass (AAA)"},
    {"event": "idea_rejected", "at": "2026-10-14T13:46:00+00:00", "scan": "09:45", "symbol": "DDD",
     "reason": "gate: sleeve_allocation_exceeded: aggressive sleeve would reach 0.82 of NAV"},
    # a November close is not October's
    opened("n", "AAA", "2026-10-30T13:46:00+00:00"), fill("n", "buy", 1, 10.0, "2026-10-30T13:47:00+00:00"),
    fill("n", "sell", 1, 11.0, "2026-11-02T15:00:00+00:00"),
    {"event": "position_closed", "pid": "n", "reason": "target", "at": "2026-11-02T15:00:01+00:00"},
]
LIVE = [
    {"event": "settled", "kind": "entry", "pid": "b", "copy": "stock", "filled": "1", "price": "51.2",
     "at": "2026-10-13T13:47:00+00:00"},
    {"event": "settled", "kind": "exit", "pid": "b", "copy": "stock", "filled": "1", "price": "52.8", "fees": "0",
     "at": "2026-10-15T19:51:00+00:00"},
    {"event": "order", "kind": "entry", "pid": "b", "at": "2026-10-13T13:46:50+00:00"},
    {"event": "skip", "pid": "x", "why": "one contract costs about $345, above the $20 premium cap",
     "at": "2026-10-14T13:47:00+00:00"},
    {"event": "path_test", "at": "12.210100"},            # a row whose stamp is a price: left out, never a crash
]
SHADOWS = """AI TRADER REVIEW SHADOWS - as of 2026-11-02
CONCENTRATION CAP - refused ideas replayed as if taken (ruling 2026-11-12, item 2)
  refused (theme in the pass): scored 1 - mean +4.00%, positive 1/1, target 1
STRICT READING - positions it would have refused (readings 2026-11-12, later)
  none: the two readings have not differed yet
"""


def build():
    return review.build_facts(EVENTS, LIVE, D(2026, 10, 1), D(2026, 10, 31), bars, sector_of=lambda s: "tech",
                              shadow_text=SHADOWS)


def test_the_month_and_its_first_sessions():
    assert review.month_bounds("2026-11") == (D(2026, 11, 1), D(2026, 11, 30))
    assert review.previous_month(D(2026, 12, 1)) == "2026-11"
    calendar = MarketCalendar.weekdays()
    assert review.first_sessions(calendar, D(2026, 11, 13)) == [D(2026, 11, d) for d in (2, 3, 4, 5, 6)]


def test_code_computes_every_fact_from_the_months_records():
    facts, done = build()
    assert [t.pid for t in done] == ["a", "b"]                   # closed in October only
    a, b = done
    assert round(a.ret, 4) == -0.03 and a.spy == 0.0 and a.sessions == 1
    assert round(a.moved, 4) == round(10.0 / 9.8 - 1, 4)
    assert round(b.ret, 4) == round(2 / 51, 4) and round(b.spy, 6) == 0.01 and b.sessions == 3
    text = facts.render()
    assert "F1: Closed in the month: 2 trade(s), 1 won" in text
    assert "concentration: theme 1" in text and "risk gate (sleeve_allocation_exceeded) 1" in text
    assert "Concentration cap: refused (theme in the pass): scored 1" in text
    assert "none: the two readings" not in text                  # an empty shadow is no fact
    assert "Live entry orders 1, filled 1 (stock 1); paper trades skipped 1 (premium cap 1)" in text
    assert "live return on cost minus the paper twin's" in text
    assert "control" in text                                     # the A/B arms


def test_the_prompt_carries_no_dollars_no_account_value_and_fences_the_catalysts():
    facts, done = build()
    config = LabConfig.load().ai_trader
    prompt = review.user_prompt("2026-10", facts, review.trade_table(done, dollars=False),
                                review.rules_in_force(config))
    assert "$" not in prompt and "NAV" not in prompt.replace("of NAV a trade", "")
    assert "equity" not in prompt.lower()
    assert "<<<" in prompt and "not instructions" in prompt and "RULES IN FORCE" in prompt
    assert "one bet per sector or theme" in prompt
    assert "Never propose margin" in review.SYSTEM and "Proposals wait for a human ruling" in review.SYSTEM


def test_a_proposal_must_cite_existing_facts():
    facts, _ = build()
    good = {"title": "Skip gap chases", "change": "refuse entries after a 2% move", "evidence": ["f1", "F2"],
            "shadow_test": "log would-skip for 20 sessions", "expected_effect": "fewer faded entries", "risk": "misses"}
    structured = {"diagnosis": "Entries chased gaps.", "proposals": [
        good, {**good, "title": "Invented", "evidence": ["F999"]}, {**good, "title": "Uncited", "evidence": []},
        {**good, "title": "Second", "shadow_test": ""}, {**good, "title": "Third"}]}
    diagnosis, proposals, dropped = review.parse(structured, facts)
    assert diagnosis == "Entries chased gaps."
    assert [(p.rank, p.title, p.evidence) for p in proposals] == [(1, "Skip gap chases", ("F1", "F2")),
                                                                   (2, "Third", ("F1", "F2"))]
    assert len(dropped) == 3 and "do not exist (F999)" in dropped[0]
    assert review.parse(None, facts) == ("", [], ["the model returned no report"])


def test_the_review_renders_and_cards_with_nothing_applied():
    facts, done = build()
    proposal = {"title": "Skip gap chases", "change": "c", "evidence": ["F1"], "shadow_test": "s",
                "expected_effect": "e", "risk": "r"}
    sent = []

    def call(system, user, tool):
        sent.append((system, user, tool))
        return SimpleNamespace(structured={"diagnosis": "Gaps. Then more.", "proposals": [proposal]},
                               est_cost_usd=0.21, model="claude-sonnet-4-6")

    diagnosis, proposals, dropped, cost, model = review.run("2026-10", facts, done, "rules", call)
    assert sent[0][2]["name"] == review.TOOL_NAME and cost == 0.21
    text = review.render("2026-10", facts, done, diagnosis, proposals, dropped, SHADOWS, cost, model)
    assert "nothing applies on its own" in text and "Evidence: F1 Closed in the month" in text
    assert "net $-10 on $1,510 of cost" in text                   # dollars only in the human's report
    card = cards.monthly_review_card("October 2026", "Round trips closed: 2.", review.short(diagnosis),
                                     [p.title for p in proposals], cost, text)
    assert card.status == cards.ACTION and card.channel == cards.DAILY and "rule on each proposal" in card.what_to_do
    assert any("1. Skip gap chases" in line for line in card.lines)
    quiet = cards.monthly_review_card("October 2026", "Round trips closed: 0.", "", [], None, "t")
    assert quiet.status == cards.GOOD and quiet.lines[-1] == "Model spend: none (facts only)."
    assert review.run("2026-10", facts, done, "rules", None)[2] == ["facts only: the model was not asked"]


def test_refusal_reasons_are_grouped_by_how_the_engine_words_them():
    kind = review.refusal_kind
    assert kind("concentration: at most 1 new option(s) per scan") == "concentration: option per scan"
    assert kind("gate: max_options_premium_exceeded: premium ...") == "risk gate (max_options_premium_exceeded)"
    assert kind("one contract costs 2484.00, above the 1960 premium budget") == "option above the premium budget"
    assert kind("no contract: no chain data") == "no option contract in the window"
    assert kind("confidence 40 below 50") == "confidence below the floor"
    assert kind("size 0.01 x 300 below the dust floor") == "below the dust floor"


def test_the_unit_runs_the_scheduled_command():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "ops" / "vps"
    service = (root / "agentic-ai-review.service").read_text(encoding="utf-8")
    timer = (root / "agentic-ai-review.timer").read_text(encoding="utf-8")
    assert "ExecStart=/home/agentic/Agentic/.venv/bin/python -m lab monthly-review --scheduled" in service
    assert "PAPER_MODE" not in service and "LIVE_TRADING" not in service
    assert "OnCalendar=Mon..Fri 17:15 America/New_York" in timer and "Persistent=true" in timer
