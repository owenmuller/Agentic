"""The learning loop's resolved-case feed (human ruling 2026-11-04): the AI
trader's closed trades from its ledger, and the main book's graded declines,
as post-mortem facts with every computable tag computed."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from orchestrator import learning_feed as feed

D = date
SPY = {D(2026, 10, 12): (100.0, 100.0), D(2026, 10, 13): (100.0, 100.0), D(2026, 10, 14): (100.0, 99.0),
       D(2026, 10, 15): (99.0, 98.0)}
BARS = {
    "SPY": SPY,
    "UNH": {D(2026, 10, 12): (378.0, 378.0), D(2026, 10, 13): (397.5, 377.6)},     # +5.2% gap, faded
    "SOXS": {D(2026, 10, 12): (10.0, 10.0), D(2026, 10, 13): (10.7, 10.9)},        # +7% = semis -2.3% at 3x
    "INTC": {D(2026, 10, 14): (60.0, 60.0), D(2026, 10, 15): (45.0, 44.0)},
    "BE": {D(2026, 8, 27): (210.0, 210.0), D(2026, 8, 28): (212.0, 217.0)},
}


def look():
    return feed.Lookups(bars=lambda s: BARS.get(s, {}), sector=lambda s: {"UNH": "money", "INTC": "business equipment",
                                                                             "BE": "manufacturing"}.get(s, "unknown"),
                        regime=lambda day: "calm_uptrend", fund=lambda s: s in ("SOXS", "SPY"),
                        leverage=lambda s: -3.0 if s == "SOXS" else 1.0)


def opened(pid, symbol, instrument, day, **extra):
    return {"event": "position_opened", "pid": pid, "symbol": symbol, "instrument": instrument,
            "at": f"{day}T14:00:00+00:00", "thesis": f"{symbol} thesis", "confidence": 72, "horizon": "days",
            "stop_pct": "2.5", "target_pct": "6.5", "catalyst": "Q3 beat", "invalidation": "gives it back", **extra}


def fill(pid, side, qty, price, day, mult=1):
    return {"event": "settled", "pid": pid, "side": side, "filled_qty": str(qty), "avg_price": str(price),
            "multiplier": mult, "at": f"{day}T15:00:00+00:00"}


def closed(pid, reason, day):
    return {"event": "position_closed", "pid": pid, "reason": reason, "at": f"{day}T15:00:01+00:00"}


EVENTS = [
    opened("u", "UNH", "stock", "2026-10-13", anchor_mid="397.0"), fill("u", "buy", 10, 397.0, "2026-10-13"),
    fill("u", "sell", 10, 390.45, "2026-10-13"), closed("u", "stop", "2026-10-13"),
    opened("s", "SOXS", "etf", "2026-10-13"), fill("s", "buy", 100, 10.6, "2026-10-13"),
    fill("s", "sell", 100, 11.66, "2026-10-14"), closed("s", "target", "2026-10-14"),
    # closed after the last SPY close: resolved once its exit session has one
    opened("i", "INTC", "put", "2026-10-15", anchor_mid="45.0"), fill("i", "buy", 1, 2.0, "2026-10-15", 100),
    fill("i", "sell", 1, 5.0, "2026-10-16", 100), closed("i", "target", "2026-10-16"),
    # still open: never a case
    opened("o", "UNH", "stock", "2026-10-14"), fill("o", "buy", 1, 390.0, "2026-10-14"),
]


def test_the_ai_traders_closed_trades_become_cases_with_their_tags_computed():
    cases = {c.decision_id: c for c in feed.ai_trader_cases(EVENTS, look())}
    assert set(cases) == {"u", "s"}
    unh = cases["u"]
    assert unh.kind == "trade" and unh.direction == "long" and unh.symbol == "UNH"
    assert unh.return_pct == pytest.approx(-1.65, abs=0.01) and unh.spy_pct == pytest.approx(0.0)
    assert unh.tags == {"sector": "money", "regime": "calm_uptrend", "confidence_band": "70-85",
                        "source": "ai_trader", "instrument": "stock", "entry": "long_after_gap_up"}
    # shares: the stock's own move is the position's return, signed in its favour
    assert unh.own_move == pytest.approx(-1.65, abs=0.01) and unh.market_move == pytest.approx(0.0)
    soxs = cases["s"]
    # an inverse fund is a bearish bet; its +7% gap is the market's -2.3% at 3x
    assert soxs.direction == "short" and soxs.tags["sector"] == "leveraged etf"
    assert soxs.tags["entry"] == "short_after_gap_down" and soxs.tags["instrument"] == "etf"
    assert soxs.own_move is None                       # not comparable with SPY's move
    assert soxs.excess == pytest.approx(10.0 - (-1.0), abs=0.01)
    assert any("leverage factor of the fund: -3" in n for n in soxs.notes)


class Mark(SimpleNamespace):
    pass


def rejection(decision_id, ticker, code, direction="no_position", horizon="weeks", observed="2026-08-28"):
    research = SimpleNamespace(tickers=[ticker], direction=direction, time_horizon=horizon, confidence=82,
                               thesis=f"{ticker}: priced in", priced_in_analysis="+30% since the trade")
    signal = SimpleNamespace(observed_at=datetime.fromisoformat(f"{observed}T14:00:00+00:00"), tickers=(ticker,),
                             source_id="congressional_disclosures")
    return SimpleNamespace(decision_id=decision_id, code=code, research=research, signal=signal,
                           message="research returned direction no_position")


def test_declines_are_graded_at_the_verdicts_horizon_as_the_trade_not_made():
    rows = {("BE", D(2026, 8, 28)): SimpleNamespace(marks={
        5: Mark(return_pct=Decimal("10"), excess_pct=Decimal("11"), marked_on=D(2026, 9, 4)),
        20: Mark(return_pct=Decimal("33.13"), excess_pct=Decimal("33.99"), marked_on=D(2026, 9, 17))})}
    records = [rejection("b1", "BE", "no_position"),
               rejection("b2", "BE", "no_position", horizon="months"),        # 60 sessions: not marked yet
               rejection("b3", "BE", "pre_filter"),                           # not an LLM decline
               rejection("b4", "ZZZ", "no_position")]                         # no forward row
    cases = feed.decline_cases(records, rows, look())
    assert [c.decision_id for c in cases] == ["b1"]
    be = cases[0]
    assert be.kind == "decline" and be.direction == "long" and be.resolved == "2026-09-17"
    assert be.excess == pytest.approx(33.99) and be.own_move == pytest.approx(33.99)
    assert be.market_move == pytest.approx(-0.86) and be.tags["source"] == "congressional_disclosures"
    assert be.tags["entry"] == "long_no_gap" and be.tags["sector"] == "manufacturing"
    # a refused puts verdict: the trade not made was bearish, so the signs flip
    short = feed.decline_cases([rejection("p1", "BE", "insufficient_reward_risk", direction="short_via_puts")],
                               rows, look())[0]
    assert short.direction == "short" and short.excess == pytest.approx(-33.99)


def test_a_decline_that_named_no_instrument_is_never_read_as_a_symbol():
    # 26 declines carry tickers ["NONE"]: the model's words, kept in the audit
    # log as written, never a symbol to fetch bars for or grade
    assert not feed.is_symbol("NONE") and not feed.is_symbol(" none ") and not feed.is_symbol("")
    assert not feed.is_symbol(None) and feed.is_symbol("BE")
    rows = {("NONE", D(2026, 8, 28)): SimpleNamespace(marks={
        20: Mark(return_pct=Decimal("5"), excess_pct=Decimal("4"), marked_on=D(2026, 9, 17))})}
    assert feed.decline_cases([rejection("n1", "NONE", "no_position")], rows, look()) == []
