"""Cross-source dispatch scoring (AGGRESSION RULING 2026-09-18, step 2 as revised).

Claims under test: slices are built from structured fields with content
fallbacks and agree between the live signal and the audit snapshot; priors
resolve exact facet -> single facet -> source fallback and say whether they are
grounded; the score is prior - age/7 + bonus; grounding replaces only slices
with n >= min_n; the loop sorts on the score, releases pooled filing sources in
windows with a spread allowance, dispatches posts at once, and leaves the
pre-ruling behaviour untouched when scoring is off; the what-if ranks a day's
candidates under the same ceilings.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from orchestrator.config import DispatchConfig, OrchestratorConfig, ResearchClassCaps
from orchestrator.scoring import (
    DispatchPriors,
    DispatchScorer,
    SliceStat,
    compute_slice_stats,
    facet_of_content,
    facet_of_signal,
    merged_priors_mapping,
    whatif_dispatch,
)
from signals.records import Priority, Signal, SignalClass
from test_orchestrator import (
    PURE_FORWARD_CALL,
    FakeBroker,
    FakeClock,
    FakeLLM,
    build,
    feed,
    orchestrator_config,
    prices_of,
)

NOW = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)  # 10:00 ET, session open


@pytest.fixture(scope="session")
def signals_config():
    from signals import SignalsConfig

    return SignalsConfig.load()


def signal(source_id, content, metadata=None, klass=SignalClass.CLASS_2_MOMENTUM, observed=NOW):
    return Signal(
        signal_id=f"s-{source_id}-{abs(hash(content)) % 10_000}",
        source_id=source_id,
        signal_class=klass,
        observed_at=observed,
        content=content,
        raw_content=content,
        priority=Priority.for_class(klass),
        external_id=f"e-{abs(hash(content)) % 10_000}",
        metadata=dict(metadata or {}),
    )


EIGHT_K = "Form 8-K current report (SEC EDGAR)\nissuer: QUANTA SERVICES, INC. (PWR)\nitems: 5.02, 7.01, 9.01\n"
FORM4 = (
    "Form 4 insider filing (SEC EDGAR, structured XML)\nissuer: Aura Minerals Inc. (AUGO)\n"
    "single qualifying purchase — no cluster in the 15-day window (1 insider(s), aggregate $21,792,751)\n"
)
CONGRESS = (
    "Congressional trading disclosure (STOCK Act filing)\nrepresentative: Nancy Pelosi (Representatives)\n"
    "ticker: UBER\ntransaction: Purchase\namount range: $500,001 - $1,000,000\ndisclosure lag: 25 days\n"
)


def test_facets_come_from_structured_fields_and_agree_with_the_content_parsers():
    assert facet_of_content("form_8k", EIGHT_K) == "item=5.02"
    assert facet_of_signal(signal("form_8k", EIGHT_K, {"whitelisted_items": "1.01,5.02"})) == "item=5.02"
    assert facet_of_signal(signal("form_8k", "x", {"whitelisted_items": "1.01"})) == "item=1.01"
    assert facet_of_signal(signal("form_8k", EIGHT_K)) == "item=5.02"  # content fallback
    assert facet_of_content("form4_insiders", FORM4) == "qual=single|size=>5M"
    assert facet_of_signal(
        signal("form4_insiders", "x", {"qualifies": "cluster", "amount_range": "$640,000"})
    ) == "qual=cluster|size=<=1M"
    assert facet_of_content("congressional_disclosures", CONGRESS) == "amount=<=1M|lag=<=35d"
    assert facet_of_signal(
        signal("congressional_disclosures", CONGRESS, {"amount_range": "$1,001 - $15,000", "disclosure_lag_days": "40"})
    ) == "amount=<=15K|lag=>35d"
    assert facet_of_signal(signal("form_13d", "x", {"percent_of_class": "12.4"})) == "stake=>=10%"
    assert facet_of_content("form_13d", "stake: 6.1% of class\n") == "stake=5-10%"
    assert facet_of_signal(signal("trump_posts", "Tariffs!", klass=SignalClass.CLASS_1_REALTIME)) == ""


def test_priors_resolve_exact_then_single_facet_then_fallback_and_say_if_grounded():
    priors = DispatchPriors.from_mapping(
        {
            "horizon_days": 5,
            "min_n": 20,
            "sources": {
                "form4_insiders": {
                    "fallback": 0.1,
                    "slices": {
                        "qual=cluster": {"prior": 0.5, "grounded": False},
                        "qual=cluster|size=<=1M": {"prior": 1.2, "grounded": True, "n": 40},
                    },
                },
                "trump_posts": {"fallback": 0.25},
            },
        }
    )
    assert priors.prior_for("form4_insiders", "qual=cluster|size=<=1M") == (1.2, True, "qual=cluster|size=<=1M")
    assert priors.prior_for("form4_insiders", "qual=cluster|size=>5M") == (0.5, False, "qual=cluster")
    assert priors.prior_for("form4_insiders", "qual=single|size=>5M") == (0.1, False, "fallback")
    assert priors.prior_for("trump_posts", "") == (0.25, False, "fallback")
    assert priors.prior_for("unknown_source", "") == (0.0, False, "")


def test_the_committed_priors_file_loads_and_is_all_ruled_until_grounded():
    priors = DispatchPriors.load()
    assert priors.horizon_days == 5 and priors.min_n == 20
    assert abs(priors.freshness_points_per_day - 1 / 7) < 1e-4
    for source in priors.sources.values():
        for spec in source.slices.values():
            assert spec.grounded is False or spec.n >= priors.min_n
    assert priors.prior_for("form_8k", "item=5.02")[0] > priors.prior_for("form_8k", "item=1.01")[0]


def test_the_score_is_prior_minus_age_plus_bonus():
    priors = DispatchPriors.from_mapping(
        {"sources": {"congressional_disclosures": {"fallback": 0.0, "slices": {"amount=<=1M": {"prior": 0.4}}}}}
    )
    scorer = DispatchScorer(priors, bonus=lambda s: Decimal("0.3"))
    fresh = signal("congressional_disclosures", CONGRESS, {"amount_range": "$500,001 - $1,000,000", "report_date": "2026-09-18"})
    week_old = signal("congressional_disclosures", CONGRESS, {"amount_range": "$500,001 - $1,000,000", "report_date": "2026-09-11"})
    a, b = scorer.score(fresh, NOW), scorer.score(week_old, NOW)
    assert a.prior == 0.4 and a.freshness == 0.0 and a.bonus == 0.3 and abs(a.total - 0.7) < 1e-9
    assert b.age_days == 7 and abs(b.total - (0.4 - 1.0 + 0.3)) < 1e-9
    assert scorer.sort_key(fresh, NOW) < scorer.sort_key(week_old, NOW)
    assert "prior +0.40 (amount=<=1M, ruled)" in a.line()


def _row(excess_by_horizon):
    marks = {h: SimpleNamespace(excess_pct=Decimal(str(v))) for h, v in excess_by_horizon.items()}
    return SimpleNamespace(marks=marks)


def _rejection(decision_id, source_id, content, symbol, observed, code="source_cap", stage="pre_filter"):
    from audit.records import RejectedStage, SignalSnapshot, StageRejectionRecord

    snap = SignalSnapshot(
        signal_id=decision_id, source_id=source_id,
        signal_class=SignalClass.CLASS_1_REALTIME if source_id == "form_8k" else SignalClass.CLASS_2_MOMENTUM,
        observed_at=observed, content=content, raw_content=content, tickers=(symbol,),
    )
    return StageRejectionRecord(
        decision_id=decision_id, recorded_at=observed, stage=RejectedStage(stage), code=code,
        message="x", signal=snap,
    )


def test_grounding_replaces_only_slices_with_enough_sample():
    day = date(2026, 9, 10)
    observed = datetime(2026, 9, 10, 15, tzinfo=timezone.utc)
    records = [
        _rejection(f"c{i}", "congressional_disclosures", CONGRESS, f"T{i}", observed) for i in range(25)
    ] + [_rejection("k1", "form_8k", EIGHT_K, "PWR", observed)]
    rows = {(f"T{i}", day): _row({5: 1.0 + (i % 3)}) for i in range(25)}
    rows[("PWR", day)] = _row({5: 9.0})
    stats = compute_slice_stats(records, rows, 5)
    by = {(s.source_id, s.facet): s for s in stats}
    full = by[("congressional_disclosures", "amount=<=1M|lag=<=35d")]
    assert full.n == 25 and full.grounded(20) and abs(full.mean - 1.96) < 0.01
    assert by[("congressional_disclosures", "amount=<=1M")].n == 25  # the marginal
    assert by[("form_8k", "item=5.02")].n == 1 and not by[("form_8k", "item=5.02")].grounded(20)
    current = {
        "horizon_days": 5, "min_n": 20,
        "sources": {"form_8k": {"fallback": 0.0, "slices": {"item=5.02": {"prior": 0.5, "grounded": False}}}},
    }
    merged = merged_priors_mapping(current, stats, "2026-09-18T00:00:00+00:00")
    cong = merged["sources"]["congressional_disclosures"]["slices"]
    assert cong["amount=<=1M|lag=<=35d"]["grounded"] is True and cong["amount=<=1M|lag=<=35d"]["prior"] == 1.96
    k8 = merged["sources"]["form_8k"]["slices"]["item=5.02"]
    assert k8["grounded"] is False and k8["prior"] == 0.5 and k8["n"] == 1  # ruled default kept, n recorded
    # An ungrounded compound slice the file never carried is NOT written: it
    # would shadow the single-facet resolution.
    assert "amount=<=1M|lag=<=20d" not in cong or cong["amount=<=1M|lag=<=20d"].get("grounded")
    sparse = merged_priors_mapping(
        {"min_n": 20, "sources": {"form4_insiders": {"fallback": 0.0, "slices": {"qual=cluster": {"prior": 0.5}}}}},
        [SliceStat("form4_insiders", "qual=cluster|size=<=5M", 8, -1.07, -1.5, 0.38)],
        "2026-09-18T00:00:00+00:00",
    )
    assert "qual=cluster|size=<=5M" not in sparse["sources"]["form4_insiders"]["slices"]
    assert DispatchPriors.from_mapping(sparse).prior_for("form4_insiders", "qual=cluster|size=<=5M") == (0.5, False, "qual=cluster")
    assert merged["generated_at"].startswith("2026-09-18")


def test_the_whatif_ranks_the_days_candidates_under_the_same_ceilings():
    from audit.records import RejectedStage

    day = date(2026, 9, 18)
    observed = datetime(2026, 9, 18, 14, tzinfo=timezone.utc)
    priors = DispatchPriors.load()
    # Two 8-Ks were researched (a 1.01 and a 5.02); two lost the cap (a 5.02 and a 1.01).
    five = EIGHT_K
    one = EIGHT_K.replace("items: 5.02, 7.01, 9.01", "items: 1.01, 9.01")
    records = [
        _rejection("a", "form_8k", one, "AAA", observed, code="no_position", stage="sizing"),
        _rejection("b", "form_8k", five, "BBB", observed, code="no_position", stage="sizing"),
        _rejection("c", "form_8k", five, "CCC", observed, code="source_cap"),
        _rejection("d", "form_8k", one, "DDD", observed, code="source_cap"),
        _rejection("e", "form_8k", one, "EEE", observed, code="pre_filter"),  # content rule: not a candidate
        _rejection("f", "form_8k", five, "FFF", observed - timedelta(days=1), code="source_cap"),  # other day
    ]
    rows = {("AAA", day): _row({1: -1.0}), ("BBB", day): _row({1: 2.0}), ("CCC", day): _row({1: 3.0})}
    text = whatif_dispatch(records, day, priors, {"form_8k": 6}, ResearchClassCaps(class_1=15, class_2_3=20), rows)
    assert "4 candidates reached dispatch, 2 were researched" in text
    assert "scored slots by source:  {'form_8k': 2}" in text
    assert "overlap: 1 of 2" in text  # BBB (5.02) kept; AAA (1.01) dropped for CCC (5.02)
    assert "NEW  +0.50 r form_8k" in text and "CCC" in text
    assert "drop +0.10 r form_8k" in text and "AAA" in text
    assert "realised excess at 1d: actual n=2 mean=+0.50; scored n=2 mean=+2.50" in text
    assert RejectedStage("pre_filter")  # the stage enum accepts the value used above


def _src(result):
    record = result.decision if result.decision is not None else result.rejection
    return record.signal.source_id


# ================================================================================
# The loop: scored sort, pooled windows, immediate posts, off = unchanged
# ================================================================================


def _config(**dispatch):
    return orchestrator_config(
        review_budget_reserve_fraction="0.125",
        research_class_caps={"class_1": 15, "class_2_3": 20},
        dispatch={"scored": True, "pool_release_interval_minutes": 30,
                  "pooled_sources": ["form_8k", "form4_insiders", "congressional_disclosures", "form_13d", "form_13f"],
                  **dispatch},
    )


def test_the_live_config_turns_scoring_on_with_the_filing_sources_pooled():
    live = OrchestratorConfig.load().dispatch
    assert live.scored and live.pool_release_interval_minutes == 30
    assert set(live.pooled_sources) == {"form_8k", "form4_insiders", "congressional_disclosures", "form_13d", "form_13f"}
    assert DispatchConfig().scored is False  # the harness default: pre-ruling behaviour


def test_pooled_filings_wait_for_a_window_while_posts_dispatch_at_once(tmp_path, signals_config):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    clock = FakeClock(datetime(2026, 8, 17, 14, 0, tzinfo=timezone.utc))  # 10:00 ET
    disclosures = [
        f"Congressional trading disclosure (STOCK Act filing)\nrepresentative: Nancy Pelosi (Representatives)\n"
        f"ticker: NUE\ntransaction: Purchase\namount range: $500,001 - $1,000,000\n"
        f"transaction date: 2026-08-0{i}\nreport date: 2026-08-16\ndisclosure lag: 1{i} days\n"
        for i in range(1, 4)
    ]
    started = build(
        tmp_path, RiskLimits.load(), signals_config, ResearchConfig.load(),
        llm=FakeLLM(), broker=FakeBroker(), clock=clock,
        fetcher=feed(nolimitgains=[PURE_FORWARD_CALL], congressional_disclosures=disclosures),
        prices=prices_of(NUE="140.00"),
        config=_config(),
    )
    first = started.loop.tick()
    # The post went at once. The first window opens on the first tick: with the
    # congressional cap of 5 and 12 half-hour windows left, the allowance is 1.
    processed = [_src(r) for r in first.processed]
    assert processed.count("nolimitgains") == 1
    assert processed.count("congressional_disclosures") == 1
    assert len(started.loop.deferred) == 2
    # Ten minutes later: still inside the window, nothing pooled moves.
    clock.advance(minutes=10)
    second = started.loop.tick()
    assert not [r for r in second.processed if _src(r) == "congressional_disclosures"]
    assert len(started.loop.deferred) == 2
    # Half an hour on: the next window releases one more.
    clock.advance(minutes=25)
    third = started.loop.tick()
    assert [_src(r) for r in third.processed] == ["congressional_disclosures"]
    assert len(started.loop.deferred) == 1


def test_with_scoring_off_the_batch_dispatches_as_before(tmp_path, signals_config):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    disclosures = [
        f"Congressional trading disclosure (STOCK Act filing)\nrepresentative: Nancy Pelosi (Representatives)\n"
        f"ticker: NUE\ntransaction: Purchase\namount range: $500,001 - $1,000,000\n"
        f"transaction date: 2026-08-0{i}\nreport date: 2026-08-16\ndisclosure lag: 1{i} days\n"
        for i in range(1, 4)
    ]
    started = build(
        tmp_path, RiskLimits.load(), signals_config, ResearchConfig.load(),
        llm=FakeLLM(), broker=FakeBroker(),
        fetcher=feed(congressional_disclosures=disclosures),
        prices=prices_of(NUE="140.00"),
    )
    report = started.loop.tick()
    assert len([r for r in report.processed if _src(r) == "congressional_disclosures"]) == 3
    assert started.loop.deferred == ()


def test_within_a_window_the_higher_prior_wins_the_slot():
    """Two 8-Ks in one window, allowance one: the 5.02 goes, the 1.01 waits."""
    from orchestrator.loop import TradingLoop

    priors = DispatchPriors.load()
    scorer = DispatchScorer(priors)
    loop = TradingLoop.__new__(TradingLoop)
    loop._scorer = scorer
    loop._dispatch = DispatchConfig(scored=True, pool_release_interval_minutes=30, pooled_sources=("form_8k",))
    loop._pooled_sources = frozenset({"form_8k"})
    loop._next_release = None
    loop._last_window = None
    loop._source_caps = {"form_8k": 6}
    loop._source_passes = {"form_8k": 5}
    loop._source_pass_day = NOW.date()
    low = signal("form_8k", "x", {"whitelisted_items": "1.01", "report_date": "2026-09-18"}, klass=SignalClass.CLASS_1_REALTIME)
    high = signal("form_8k", "y", {"whitelisted_items": "5.02", "report_date": "2026-09-18"}, klass=SignalClass.CLASS_1_REALTIME)
    post = signal("trump_posts", "Tariffs on steel $NUE", klass=SignalClass.CLASS_1_REALTIME)
    released, held = loop._release_pooled([low, post, high], NOW)
    assert released == [post, high] and held == [low]
    # Inside the same window nothing pooled is released.
    released, held = loop._release_pooled([low], NOW + timedelta(minutes=5))
    assert released == [] and held == [low]
