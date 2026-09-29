"""Feed spend against what it bought (requested 2026-09-28 for the 2026-10-15
review), and the Form 4 verdict package's population line.

Claims: a judged trade is counted once for its source with its pass, its
research estimate and its realised outcome; the mechanical arm, the sweep and
the baseline never appear in a source's row; a paid feed is billed from its
start date; the table renders one line per source with the paid-feed verdict
line; and the Form 4 section names the date its 60d cells can exist.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from audit.spend import feed_spend_rows, render_feed_spend
from execution.environment import LIVE_CONFIRMATION_VARIABLE

from test_exits import enter_position


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def limits():
    from risk_gate import RiskLimits

    return RiskLimits.load()


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    from research.config import ResearchConfig

    return ResearchConfig.load()


def test_a_traded_and_closed_signal_lands_in_its_sources_row(
    tmp_path, limits, signals_config, research_config
):
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config
    )
    prices.set("NUE", "119.00")  # through the stop: the guardrail closes it
    started.loop.tick()
    started.loop.tick()
    trail = started.audit.trail("dec-1")
    assert trail.outcome is not None

    sources = [
        (s.id, key, s.monthly_cost)
        for key, klass in signals_config.classes.items()
        for s in klass.sources
    ]
    now = clock.now
    feed_to_date = {
        row.source_id: row.cost
        for row in signals_config.feed_cost_breakdown(now - timedelta(days=400), now)
    }
    rows = feed_spend_rows(
        started.audit.records(),
        started.audit.trails(),
        sources,
        feed_to_date,
        {"trump_posts": {5: (-1.25, 1)}},
    )
    by_id = {r.source_id: r for r in rows}
    trump = by_id["trump_posts"]
    assert (trump.passes, trump.traded, trump.open_positions, trump.closed, trump.wins) == (1, 1, 0, 1, 0)
    assert trump.realised_pnl == Decimal("-399.00")  # 19 x (119 - 140)
    assert trump.forward_5d == (-1.25, 1) and trump.forward_20d is None
    assert trump.net == trump.realised_pnl - trump.research_cost - trump.feed_to_date
    # The sweep's SGOV buy and any mechanical/baseline row never count as a
    # source's trade: only the judged trail did.
    assert sum(r.traded for r in rows) == 1
    assert "cash_management" not in by_id and "baseline_sleeve" not in by_id
    # Every configured source has a row, paid or free.
    assert len(rows) == len(sources)
    # A paid feed is billed from its start date, not from the beginning of time.
    paid = [r for r in rows if r.monthly_cost > 0]
    assert paid and all(r.feed_to_date >= 0 for r in paid)

    text = render_feed_spend(rows, now)
    assert "Feed spend against what it bought" in text
    assert "trump_posts" in text and "realised" in text
    assert "paid feeds:" in text
    assert "all sources: feed" in text


def test_the_form4_package_names_when_its_60d_cells_populate():
    from datetime import date
    from types import SimpleNamespace

    from forward.report import _populates_line

    def entry(ticker, day):
        return SimpleNamespace(
            primary_ticker=ticker,
            observed_at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc),
        )

    first = date(2026, 9, 3)
    entries = [entry(f"T{i:02d}", first + timedelta(days=i)) for i in range(25)]
    line = _populates_line("clusters", entries, 60)
    assert "first mark due 2026-11-02" in line
    assert "20 distinct tickers marked from 2026-11-21" in line  # the 20th arrival + 60
    thin = _populates_line("clusters", entries[:5], 60)
    assert "only 5 distinct ticker(s) have arrived" in thin
    assert _populates_line("clusters", [], 60) == "clusters, 60d: no arrivals yet"
