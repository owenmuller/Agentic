"""Valuations at the mid, stops on the bid or last trade, never the ask
(human rulings 2026-11-04): the shared quote reader, the exit engine and the
mechanical arm. Origin: the momentum sleeve marked EPD and THC at a thin IEX
ask (36.85 x 42.28, 265.06 x 299.01) and trimmed two holdings that were not
over target; a stale high ask can likewise hold a stop off."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import httpx
import pytest

import datahealth
from orchestrator.quotes import QuoteReader

from test_exits import (  # noqa: F401 - fixtures
    MutablePrices, enter_position, limits, paper_mode, research_config, signals_config,
)

D = Decimal


class SidedPrices(MutablePrices):
    """A price source with both sides of a quote and a last trade. Called
    plain, it answers the ask, as the production source does."""

    def __init__(self, **quotes: tuple[str, str]) -> None:
        super().__init__(**{s: ask for s, (_bid, ask) in quotes.items()})
        self.book = {s: (D(bid), D(ask)) for s, (bid, ask) in quotes.items()}
        self.trades: dict[str, Optional[Decimal]] = {}

    def quote(self, symbol: str, bid: str, ask: str, trade: Optional[str] = None) -> None:
        self.book[symbol] = (D(bid), D(ask))
        self.set(symbol, ask)
        self.trades[symbol] = D(trade) if trade else None

    def sides(self, symbol: str):
        return self.book.get(symbol)

    def bid(self, symbol: str):
        return self.book[symbol][0] if symbol in self.book else None

    def last_trade(self, symbol: str):
        return self.trades.get(symbol)


@pytest.fixture
def cards():
    seen: list[tuple[str, str]] = []
    datahealth.reset()
    datahealth.set_sink(lambda source, message, urgent=False: seen.append((source, message)))
    yield seen
    datahealth.set_sink(None)
    datahealth.reset()


# ================================================================================
# The reader
# ================================================================================


def test_a_tight_quote_values_at_the_mid_and_triggers_on_the_bid(cards):
    prices = SidedPrices(EPD=("36.80", "36.88"))
    reader = QuoteReader(prices, sides=prices.sides, last_trade=prices.last_trade)
    assert reader.read("EPD") == (D("36.84"), D("36.80"))
    assert cards == []


def test_a_wide_quote_is_no_value_falls_back_to_the_last_trade_and_cards_once_a_day(cards):
    prices = SidedPrices(EPD=("36.85", "42.28"))
    clock = [datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)]
    reader = QuoteReader(prices, sides=prices.sides, last_trade=prices.last_trade, engine="momentum",
                         clock=lambda: clock[0])
    assert reader.read("EPD") == (None, None)                     # no last trade: no trigger either
    prices.trades["EPD"] = D("36.81")
    assert reader.read("EPD") == (None, D("36.81"))               # never the 42.28 ask
    assert len(cards) == 1 and cards[0][0] == "Quote quality" and "EPD quoted 36.85 x 42.28" in cards[0][1]
    clock[0] += timedelta(days=1)
    reader.read("EPD")
    assert len(cards) == 2                                        # the next day, again


def test_no_quote_triggers_on_the_last_trade_and_a_single_price_source_is_unchanged(cards):
    prices = SidedPrices()
    prices.trades["XYZ"] = D("10.00")
    reader = QuoteReader(prices, sides=prices.sides, last_trade=prices.last_trade)
    assert reader.read("XYZ") == (None, D("10.00"))
    plain = QuoteReader(MutablePrices(XYZ="11.00"))
    assert plain.read("XYZ") == (D("11.00"), D("11.00"))


def test_the_price_source_reads_a_fresh_last_trade_and_refuses_a_stale_one():
    from execution import DATA_BASE_URL, AlpacaPriceSource

    now = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)

    def source(stamp: datetime) -> AlpacaPriceSource:
        body = {"trade": {"p": 36.81, "t": stamp.isoformat().replace("+00:00", "Z")}}
        client = httpx.Client(base_url=DATA_BASE_URL,
                              transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
        return AlpacaPriceSource(client, clock=lambda: now)

    assert source(now - timedelta(seconds=20)).last_trade("EPD") == D("36.81")
    assert source(now - timedelta(minutes=30)).last_trade("EPD") is None


# ================================================================================
# The exit engine
# ================================================================================


def test_the_stop_fires_on_the_bid_when_the_ask_sits_above_it(tmp_path, limits, signals_config, research_config,
                                                              cards):
    prices = SidedPrices(NUE=("139.90", "140.00"))
    started, prices, _ = enter_position(tmp_path, limits, signals_config, research_config, prices=prices)
    # the stop is 119.00: the ask (119.40) would have held it off; the bid fires it
    prices.quote("NUE", "118.95", "119.40")
    assert started.loop.tick().exits_started == 1
    exit_ = started.audit.trail("dec-1").exits[0]
    assert "118.95 (bid or last trade)" in exit_.detail


def test_valuations_are_the_mid_and_a_wide_quote_keeps_the_last_mark(tmp_path, limits, signals_config,
                                                                     research_config, cards):
    prices = SidedPrices(NUE=("139.90", "140.00"))
    started, prices, _ = enter_position(tmp_path, limits, signals_config, research_config, prices=prices)
    held = started.gate.state.position(("equity", "NUE"))
    prices.quote("NUE", "130.00", "130.40")
    started.loop.tick()
    assert held.market_value == held.quantity * D("130.20")
    # a thin quote: no valuation, no stop on its 100 bid, a card
    prices.quote("NUE", "100.00", "140.00")
    assert started.loop.tick().exits_started == 0
    assert held.market_value == held.quantity * D("130.20")
    assert any(source == "Quote quality" and "NUE quoted 100.00 x 140.00" in text for source, text in cards)
    # the last trade under the stop fires it
    prices.quote("NUE", "100.00", "140.00", trade="118.50")
    assert started.loop.tick().exits_started == 1


# ================================================================================
# The mechanical arm
# ================================================================================


def test_the_mechanical_arm_values_at_the_mid_and_skips_a_wide_quote(tmp_path, cards):
    from test_mechanical import enter_one, signals_config as mech_signals_config  # noqa: F401

    from signals.config import SignalsConfig

    prices = SidedPrices(NUE=("139.90", "140.00"))
    started, clock = enter_one(tmp_path, SignalsConfig.load(), prices=prices)
    held = started.gate.state.position(("mechanical", "NUE"))
    prices.quote("NUE", "69.90", "70.10")
    clock.advance(days=1)
    started.loop.tick()
    assert held.market_value == held.quantity * D("70.00")
    prices.quote("NUE", "50.00", "90.00")
    clock.advance(days=1)
    started.loop.tick()
    assert held.market_value == held.quantity * D("70.00")
    assert any("NUE quoted 50.00 x 90.00" in text and "mechanical" in text for _, text in cards)
