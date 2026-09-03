"""Exit-authority probation and the directional-bias measurements
(human rulings 2026-09-02, second batch).

The claims: inside the probation window a review CLOSE on a profitable,
validity-intact position is recorded (ShadowCloseRecord) and NOT executed while
an invalidation close still executes and everything outside the window behaves
as before; beta arithmetic is exact and absent-below-overlap; Form 4 sell
clusters emit measurement-only rows that prefilter to code bearish_measurement;
13D stake percents parse for the reduction slice; and the shipped config arms
the probation window from 2026-09-02.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from audit.attribution import beta_from_closes, build_attribution
from audit.records import ShadowCloseRecord, snapshot_stake_percent, SignalSnapshot
from orchestrator.config import OrchestratorConfig, ReviewCloseProbation
from signals import SignalClass
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from test_exits import (
    MutablePrices,  # noqa: F401 - harness re-export
    enter_position,
    routing,
)
from test_orchestrator import FakeClock

PROBATION_NOW = datetime(2026, 9, 3, 14, 30, tzinfo=timezone.utc)


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

PROFIT_TAKING_CLOSE = {
    "assessment": "Thesis intact but the run feels extended; take the profit.",
    "invalidation_triggered": False,
    "action": "close",
    "validity": "intact",
    "progress": "ahead",
    "case_for_holding": "The thesis is intact and ahead of schedule; the trailing stop already protects most of the gain.",
    "case_for_selling": "The run is extended relative to the catalyst, and the remaining upside to target is small against the stop.",
    "verdict_reason": "the selling case wins on a stretched risk:reward.",
}


def test_probation_window_boundaries():
    probation = ReviewCloseProbation(start_date=date(2026, 9, 2), days=90)
    assert not probation.active_on(date(2026, 9, 1))
    assert probation.active_on(date(2026, 9, 2))
    assert probation.active_on(date(2026, 11, 30))
    assert not probation.active_on(date(2026, 12, 1))


def test_shipped_config_arms_probation():
    probation = OrchestratorConfig.load().exits.review_close_probation
    assert probation is not None
    assert probation.start_date == date(2026, 9, 2) and probation.days == 90


def test_a_profitable_intact_close_is_shadowed_not_executed(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(PROFIT_TAKING_CLOSE),
        clock=clock,
    )
    prices.set("NUE", "150.00")  # profitable over the 140 entry
    clock.advance(hours=25)  # cadence review due
    report = started.loop.tick()
    assert report.reviews_run == 1
    # NOT executed: no exit went out, the position is still tracked and armed.
    assert report.exits_started == 0
    assert len(started.exits.tracked) == 1
    assert not started.exits.tracked[0].close_verdict

    shadows = started.audit.shadow_closes()
    assert len(shadows) == 1
    assert shadows[0].symbol == "NUE"
    assert shadows[0].mark == Decimal("150.00")
    assert shadows[0].entry_price == Decimal("140.00")
    assert shadows[0].validity == "intact"


def test_an_invalidation_close_still_executes_under_probation(
    tmp_path, limits, signals_config, research_config
):
    from test_exits import CLOSE_REVIEW

    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(CLOSE_REVIEW),  # invalidation_triggered=True
        clock=clock,
    )
    prices.set("NUE", "150.00")  # profitable — but invalidation overrides
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert report.exits_started == 1
    assert started.audit.shadow_closes() == []


def test_an_unprofitable_intact_close_still_executes_under_probation(
    tmp_path, limits, signals_config, research_config
):
    clock = FakeClock(PROBATION_NOW)
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(PROFIT_TAKING_CLOSE),
        clock=clock,
    )
    prices.set("NUE", "130.00")  # below entry: not the shadowed class
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.exits_started == 1
    assert started.audit.shadow_closes() == []


def test_outside_the_window_every_close_executes(
    tmp_path, limits, signals_config, research_config
):
    # The default FakeClock sits at 2026-08-17, before the probation start.
    started, prices, clock = enter_position(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=routing(PROFIT_TAKING_CLOSE),
    )
    prices.set("NUE", "150.00")
    clock.advance(hours=25)
    report = started.loop.tick()
    assert report.exits_started == 1
    assert started.audit.shadow_closes() == []


# ================================================================================
# Beta measurement
# ================================================================================


def test_beta_arithmetic_is_exact():
    base = date(2026, 1, 1)
    benchmark = []
    asset = []
    level_b, level_a = Decimal("100"), Decimal("100")
    for day in range(60):
        move = Decimal("0.01") if day % 2 else Decimal("-0.01")
        level_b *= 1 + move
        level_a *= 1 + move * 2  # exactly twice the benchmark's daily return
        benchmark.append((base.fromordinal(base.toordinal() + day), level_b))
        asset.append((base.fromordinal(base.toordinal() + day), level_a))
    beta = beta_from_closes(asset, benchmark)
    assert beta is not None and abs(beta - Decimal("2")) <= Decimal("0.02")


def test_beta_is_absent_below_the_overlap_floor():
    base = date(2026, 1, 1)
    series = [
        (base.fromordinal(base.toordinal() + day), Decimal("100") + day)
        for day in range(10)
    ]
    assert beta_from_closes(series, series) is None


def test_book_beta_is_value_weighted():
    report = build_attribution(
        [],
        generated_at=datetime(2026, 9, 3, tzinfo=timezone.utc),
        position_betas=(
            ("AAA", Decimal("2.00"), Decimal("1000")),
            ("BBB", Decimal("1.00"), Decimal("3000")),
            ("CCC", None, Decimal("500")),  # unmeasurable: excluded from the mean
        ),
    )
    assert report.book_beta == Decimal("1.25")
    rendered = report.render()
    assert "book beta 1.25" in rendered
    assert "CCC n/a" in rendered


# ================================================================================
# Bearish groundwork
# ================================================================================


def test_13d_stake_percent_parses():
    snapshot = SignalSnapshot(
        signal_id="s",
        source_id="form_13d",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        observed_at=PROBATION_NOW,
        content="SCHEDULE 13D/A filing\nstake: 7.2% of class, 25000000 shares",
        raw_content="x",
    )
    assert snapshot_stake_percent(snapshot) == Decimal("7.2")


def test_form4_sell_cluster_emits_measurement_only(signals_config):
    from test_form4 import fetcher_with, form4_xml

    sale_a = (
        "0001111111-26-000021",
        "0001111111",
        "2026-08-31",
        form4_xml("0001111111", "Avery Cfo", code="S", shares=4_000, price=30.0,
                  txn_date="2026-08-28"),
    )
    sale_b = (
        "0002222222-26-000021",
        "0002222222",
        "2026-09-01",
        form4_xml("0002222222", "Blair Director", code="S", shares=3_000,
                  price=30.0, txn_date="2026-08-30"),
    )
    source = signals_config.source("class_2", "form4_insiders")
    fetcher, _ = fetcher_with([sale_a, sale_b])
    items = fetcher(source)
    # The single sale emits nothing; the completed cluster emits ONE
    # measurement-only row.
    assert len(items) == 1
    fields = items[0].fields
    assert fields["measurement_only"] == "true"
    assert fields["transaction"] == "Sale"
    assert fields["cluster"] == "true"
    assert "BEARISH MEASUREMENT ONLY" in items[0].content


def test_measurement_rows_prefilter_to_their_own_code(signals_config):
    from orchestrator import ResearchPreFilter
    from test_form4 import form4_item, scanner_signal

    item = form4_item(cluster=True)
    item.fields["measurement_only"] = "true"
    item.fields["transaction"] = "Sale"
    signal = scanner_signal(signals_config, item)
    prefilter = ResearchPreFilter.from_config(signals_config)
    verdict = prefilter.skip_verdict(signal, now=PROBATION_NOW)
    assert verdict is not None
    reason, rule = verdict
    assert rule == "measurement" and "never traded" in reason


def test_report_renders_the_bearish_and_shadow_sections():
    from forward.funnel import FunnelEntry
    from forward.report import render_forward_report

    sell = FunnelEntry(
        decision_id="d1",
        source_id="form4_insiders",
        credibility_key="form4_insiders",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        observed_at=PROBATION_NOW,
        tickers=("CLST",),
        bucket="prefiltered",
        code="bearish_measurement",
        confidence=None,
        lag_days=None,
        transaction="Sale",
    )

    def thirteen_d(decision_id, stake, when):
        return FunnelEntry(
            decision_id=decision_id,
            source_id="form_13d",
            credibility_key="form_13d/Sarissa Capital",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=when,
            tickers=("AMRN",),
            bucket="declined",
            code="",
            confidence=40,
            lag_days=None,
            stake_percent=stake,
        )

    shadow = ShadowCloseRecord(
        decision_id="d9",
        recorded_at=PROBATION_NOW,
        symbol="NUE",
        mark=Decimal("150"),
        entry_price=Decimal("140"),
        days_held=17,
        validity="intact",
        assessment="run feels extended",
    )
    report = render_forward_report(
        [
            sell,
            thirteen_d("d2", Decimal("9.9"), PROBATION_NOW),
            thirteen_d(
                "d3", Decimal("7.2"), PROBATION_NOW.replace(day=4)
            ),
        ],
        rows={},
        shadow_closes=(shadow,),
    )
    assert "Form 4 insider SELL clusters" in report
    assert "13D stake changes" in report
    assert "reductions (1)" in report
    assert "Shadowed review closes" in report
    assert "NUE: shadowed at 150 (+7.1% over entry)" in report
