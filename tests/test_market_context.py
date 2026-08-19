"""Deterministic market context + benchmark returns (bolt-ons, 2026-08-19).

The claims: the context block is pure arithmetic over daily bars and renders the
exact numbers; every missing input degrades to a sentence saying so — a pass is
never blocked and a number is never invented; the block reaches the research
prompt inside a data fence with the IV-crush guidance alongside; and the window
return used for benchmarking is close-to-close arithmetic that returns None on
any gap.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from execution.environment import LIVE_CONFIRMATION_VARIABLE
from execution.market_data import AlpacaDailyBars, MarketContextBuilder
from research.prompts import build_user_prompt
from research.reports import ResearchReport
from research.research_pass import ResearchPass
from signals.records import UNTRUSTED_CONTENT_PREAMBLE
from test_audit import make_signal
from test_orchestrator import REPORT, FakeLLM, structured

NOW = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


def bars_client(bars_by_symbol: dict[str, list[dict]], status: int = 200):
    """A real httpx.Client against a mock transport — the seam the code owns."""

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.path.split("/")[3]
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"bars": bars_by_symbol.get(symbol, [])})

    return httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://test"
    )


def daily(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    volumes = volumes or [1_000_000.0] * len(closes)
    day = NOW - timedelta(days=len(closes))
    out = []
    for index, (close, volume) in enumerate(zip(closes, volumes)):
        out.append(
            {
                "t": (day + timedelta(days=index)).isoformat(),
                "c": close,
                "v": volume,
            }
        )
    return out


# ================================================================================
# Window return (the benchmark's arithmetic)
# ================================================================================


def test_window_return_is_close_to_close():
    source = AlpacaDailyBars(client=bars_client({"SPY": daily([100.0, 105.0, 110.0])}))
    assert source.window_return_pct(
        "SPY", NOW - timedelta(days=90), NOW
    ) == Decimal("10.00")


def test_window_return_is_none_on_any_gap():
    empty = AlpacaDailyBars(client=bars_client({"SPY": []}))
    single = AlpacaDailyBars(client=bars_client({"SPY": daily([100.0])}))
    broken = AlpacaDailyBars(client=bars_client({}, status=500))
    window = (NOW - timedelta(days=90), NOW)
    assert empty.window_return_pct("SPY", *window) is None
    assert single.window_return_pct("SPY", *window) is None
    assert broken.window_return_pct("SPY", *window) is None


# ================================================================================
# The context block: exact numbers, exact degradations
# ================================================================================


def context_signal(tickers: str = "NUE"):
    signal = make_signal(content=f"Buying ${tickers} here.")
    object.__setattr__(signal, "metadata", {"tickers": tickers})
    return signal


def rich_history() -> list[float]:
    """260 trading days: flat at 100, a 52-week high of 200 in the middle, then a
    tail engineered so the 5d and 20d changes are exact round numbers."""
    closes = [100.0] * 239
    closes[120] = 200.0  # the 52-week high
    closes += [110.0] * 15  # days -21..-6: base for the 20d change
    closes += [120.0] * 5  # days -6..-1: base for the 5d change
    closes += [132.0]  # last close
    return closes


def test_the_block_renders_the_exact_arithmetic():
    closes = rich_history()
    volumes = [1_000_000.0] * (len(closes) - 1) + [3_000_000.0]
    builder = MarketContextBuilder(
        AlpacaDailyBars(client=bars_client({"NUE": daily(closes, volumes)})),
        clock=lambda: NOW,
    )
    block = builder.context_for(context_signal())

    assert "NUE: last close 132" in block
    assert "5-day change: +10.00%" in block  # 132 vs 120
    assert "20-day change: +20.00%" in block  # 132 vs 110
    assert "vs 52-week high (200.0): -34.00%" in block
    assert "latest volume vs 20-day average: 3.00x" in block
    assert "next earnings date: unavailable (no earnings data source" in block


def test_earnings_within_reach_render_as_days_away():
    builder = MarketContextBuilder(
        AlpacaDailyBars(client=bars_client({"NUE": daily(rich_history())})),
        clock=lambda: NOW,
        earnings_provider=lambda ticker: date(2026, 8, 28),
    )
    block = builder.context_for(context_signal())
    assert "next earnings: 2026-08-28 (9 days away)" in block


def test_no_tickers_means_a_block_that_says_so():
    builder = MarketContextBuilder(
        AlpacaDailyBars(client=bars_client({})), clock=lambda: NOW
    )
    block = builder.context_for(context_signal(tickers=""))
    assert "No instrument was extracted" in block


def test_missing_history_degrades_per_ticker_without_invented_numbers():
    builder = MarketContextBuilder(
        AlpacaDailyBars(client=bars_client({"NUE": daily(rich_history())})),
        clock=lambda: NOW,
    )
    block = builder.context_for(context_signal(tickers="NUE,GHOST"))
    assert "NUE: last close 132" in block
    assert "GHOST: market context unavailable (no usable price history)" in block
    assert "do not infer or invent" in block


def test_a_fetch_outage_degrades_instead_of_raising():
    builder = MarketContextBuilder(
        AlpacaDailyBars(client=bars_client({}, status=503)), clock=lambda: NOW
    )
    block = builder.context_for(context_signal())
    assert "market context unavailable" in block


def test_at_most_three_tickers_get_context():
    builder = MarketContextBuilder(
        AlpacaDailyBars(client=bars_client({})), clock=lambda: NOW
    )
    block = builder.context_for(context_signal(tickers="A,B,C,D,E"))
    assert block.count("market context unavailable") == 3
    assert "D:" not in block and "E:" not in block


# ================================================================================
# Into the prompt: fenced as data, guidance alongside, never a blocked pass
# ================================================================================


def test_the_context_rides_inside_a_data_fence_with_iv_crush_guidance():
    prompt = build_user_prompt(
        context_signal(), market_context="NUE: last close 132"
    )
    assert "MARKET CONTEXT" in prompt
    assert "IV crush" in prompt
    # The block sits inside a fence, marked as data like all non-prompt content.
    context_at = prompt.index("NUE: last close 132")
    fence_at = prompt.rindex(UNTRUSTED_CONTENT_PREAMBLE, 0, context_at)
    assert fence_at < context_at


def test_without_context_the_prompt_is_unchanged_in_shape():
    prompt = build_user_prompt(context_signal())
    assert "MARKET CONTEXT" not in prompt
    assert "IV crush" not in prompt


def test_a_context_builder_crash_never_blocks_the_pass():
    def exploding_builder(signal):
        raise ConnectionError("data host down")

    llm = FakeLLM(structured(REPORT))
    research = ResearchPass(llm, market_context=exploding_builder)
    outcome = research.run(context_signal())

    assert isinstance(outcome, ResearchReport)  # the pass proceeded
    assert "context builder failed" in llm.calls[0]["user"]
    assert "never infer" in llm.calls[0]["user"]


def test_the_pass_threads_the_context_into_the_prompt():
    llm = FakeLLM(structured(REPORT))
    research = ResearchPass(
        llm, market_context=lambda signal: "CTX-SENTINEL last close 99"
    )
    research.run(context_signal())
    assert "CTX-SENTINEL" in llm.calls[0]["user"]
