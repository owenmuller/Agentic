"""The momentum book's monthly prep (human ruling 2026-10-13), on a synthetic
store: when a month turns; a plan the session's validator accepts; the veto
loop (a vetoed slot goes to the next rank, the replacement is reviewed, a held
name is never vetoed, a failed review never vetoes); the shadows; the files.
"""
from __future__ import annotations

import json

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from momentum import engine
from orchestrator import momentum_prep as prep
from orchestrator.momentum_sleeve import load_plan, plan_path
from orchestrator.momentum_veto import Review, Reviewer, parse
from risk_gate import RiskLimits

from test_momentum_engine import _truncated, make_store


@pytest.fixture(scope="module")
def world():
    store = make_store(n_stocks=60, sessions=620)
    starts = engine.month_starts(store.calendar)
    t = starts[-2]
    return store, _truncated(store, t), store.calendar[t]


@pytest.fixture(scope="module")
def caps():
    return RiskLimits.load().momentum_sleeve


def clean(context):
    return Review(context["symbol"], context["new_entrant"], False, "none", "nothing found", 10, "fine")


# ================================================================================
# When
# ================================================================================


SESSIONS = ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]


@pytest.mark.parametrize("utc, expected", [
    (datetime(2026, 10, 30, 22, 30, tzinfo=timezone.utc), prep.Due("2026-11-02", "2026-10-30")),  # 18:30 ET
    (datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc), None),                                   # 17:00 ET
    (datetime(2026, 11, 2, 13, 0, tzinfo=timezone.utc), prep.Due("2026-11-02", "2026-10-30")),    # 08:00 ET
    (datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc), None),                                    # 10:00 ET
    (datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc), None),                                   # mid-month evening
    (datetime(2026, 11, 1, 23, 0, tzinfo=timezone.utc), None),                                    # a Sunday
])
def test_a_plan_is_due_only_when_the_month_turns(utc, expected):
    assert prep.due(utc, SESSIONS, evening_after="18:15", morning_before="09:15") == expected


# ================================================================================
# The plan
# ================================================================================


def test_the_plan_is_one_the_session_accepts(world, caps, tmp_path):
    full, past, day = world
    result = prep.compute(past, day, [])
    path = prep.write(tmp_path, result)
    plan = load_plan(path, caps)
    assert plan.month == day[:7] and plan.rebalance_date.isoformat() == day
    assert 0 < len(plan.targets) <= 15
    assert all(t.weight <= Decimal("0.10") for t in plan.targets)
    assert plan.data_through == past.calendar[-1]
    # the same decision the backtest makes on that day
    starts = engine.month_starts(full.calendar)
    t = full.index[day]
    book = engine.run(full, engine.rules.LIVE, max(1, t - 1 - 756 - 30), t - 1).book_returns
    expected = engine.decide(full, t, engine.rules.LIVE, [], book, starts)
    assert {x.symbol for x in plan.targets} == {full.by_key[k].symbol for k in expected.holdings}
    assert any("the AI leg is off" in p for p in result.problems)


def test_a_veto_hands_the_slot_to_the_next_rank_and_the_replacement_is_reviewed(world):
    _, past, day = world
    first = prep.compute(past, day, [], review=clean)
    top = first.detail["selection"]["bought"][0]
    seen = []

    def vetoing(context):
        seen.append((context["symbol"], context["new_entrant"]))
        if context["symbol"] == top:
            return Review(top, True, True, "pending_acquisition", "deal signed 2026-10-01 (source)", -5, "pinned")
        return clean(context)

    result = prep.compute(past, day, [], review=vetoing)
    symbols = [t["symbol"] for t in result.plan["targets"]]
    assert top not in symbols and top in result.plan["vetoed"]
    assert len(symbols) == len(first.plan["targets"])  # the slot was refilled
    replacement = (set(symbols) - {t["symbol"] for t in first.plan["targets"]}).pop()
    assert (replacement, True) in seen                   # and the newcomer was reviewed
    assert result.plan["vetoed"][top].startswith("pending_acquisition")


def test_a_held_name_is_reviewed_for_sentiment_only_and_never_vetoed(world):
    _, past, day = world
    first = prep.compute(past, day, [], review=clean)
    keep = first.detail["selection"]["bought"][0]

    def hostile(context):
        if context["symbol"] != keep:
            return clean(context)
        return Review(context["symbol"], context["new_entrant"], True, "squeeze_or_meme_spike", "spike", -80, "x")

    result = prep.compute(past, day, [keep], review=hostile)
    assert keep in [t["symbol"] for t in result.plan["targets"]]
    assert keep not in result.plan["vetoed"]
    held_review = next(r for r in result.reviews if r.symbol == keep)
    assert held_review.new_entrant is False and held_review.applies is False


def test_a_failed_review_never_vetoes(world):
    _, past, day = world

    def broken(context):
        return Review(context["symbol"], context["new_entrant"], True, "accounting_issue", "", None, "",
                      fault="call failed: TimeoutError")

    baseline = prep.compute(past, day, [])
    result = prep.compute(past, day, [], review=broken)
    assert result.plan["targets"] == baseline.plan["targets"] and result.plan["vetoed"] == {}
    assert any("no veto, v1 stands" in p for p in result.problems)


def test_a_held_name_missing_from_the_data_is_sold_and_flagged(world):
    _, past, day = world
    result = prep.compute(past, day, ["GONE"])
    assert result.plan["sold"]["GONE"].startswith("not in the data")
    assert any("GONE" in p for p in result.problems)


def test_the_review_cap_leaves_names_unreviewed_and_says_so(world):
    _, past, day = world
    result = prep.compute(past, day, [], review=clean, max_reviews=2)
    assert len(result.reviews) == 2
    assert any("2-review cap" in p for p in result.problems)


# ================================================================================
# Shadows and files
# ================================================================================


def test_every_shadow_is_computed_and_its_state_rolls_forward(world, tmp_path):
    _, past, day = world
    result = prep.compute(past, day, [])
    books = result.shadows["books"]
    assert set(books) == set(engine.rules.SHADOWS) | {"x_thread", "spmo"}
    assert books["spmo"]["weights"] == {"SPMO": 1.0}
    assert all(sum(b["weights"].values()) <= 1 + 1e-9 for b in books.values())
    prep.write(tmp_path, result)
    held, pick = prep.read_shadow_state(tmp_path)
    assert held["v1_top_1000"] == sorted(books["v1_top_1000"]["weights"])
    assert (tmp_path / f"detail-{day[:7]}.json").exists() and plan_path(tmp_path, day[:7]).exists()


def test_the_x_thread_picks_last_years_top_gainer_in_january():
    store = make_store(n_stocks=40, sessions=620)
    january = next(i for i in engine.month_starts(store.calendar) if store.calendar[i][:7] == "2018-01")
    past = _truncated(store, january)
    result = prep.compute(past, store.calendar[january], [])
    pick = next(iter(result.shadows["books"]["x_thread"]["weights"]))
    year_end = january - 1
    base = max(i for i, d in enumerate(store.calendar) if d[:4] == "2016")
    gains = {s.symbol: store.adj[s.key][year_end] / store.adj[s.key][base] for s in store.segments}
    assert pick == max(gains, key=gains.get)


# ================================================================================
# The reviewer
# ================================================================================


def test_parse_reads_a_malformed_report_as_no_veto():
    assert parse("X", True, None).fault == "no report"
    bad = parse("X", True, {"veto": "yes", "category": "none"})
    assert bad.fault and not bad.applies
    nameless = parse("X", True, {"veto": True, "category": "none", "evidence": "", "sentiment_6m": 0,
                                 "sentiment_reason": ""})
    assert nameless.veto is False and not nameless.applies
    good = parse("X", True, {"veto": True, "category": "dilutive_offering", "evidence": "notice 2026-10-01",
                             "sentiment_6m": 500, "sentiment_reason": "r"})
    assert good.applies and good.sentiment_6m is None  # out of range: not recorded


class FakeClient:
    def __init__(self, cost="0.10", fail=False):
        self.cost, self.fail, self.calls = Decimal(cost), fail, []

    def research(self, *, system, user, tool, tier):
        self.calls.append(dict(system=system, user=user, tool=tool, tier=tier))
        if self.fail:
            raise TimeoutError("slow")
        return SimpleNamespace(structured={"veto": False, "category": "none", "evidence": "ok", "sentiment_6m": 20,
                                           "sentiment_reason": "r"},
                               est_cost_usd=self.cost, model="claude-sonnet-4-6", transcript_hash="h")


def _context(new=True):
    return dict(symbol="ABC", name="Abc Corp", sector="energy", rank=3, new_entrant=new, data_through="2026-10-30",
                last_close=12.5, return_12_1=0.8, max_daily_move_3m=0.07, residual_z=1.9, earnings_z=1.2)


def test_the_reviewer_runs_the_production_class_3_tier_under_a_hard_cap():
    client = FakeClient(cost="0.30")
    reviewer = Reviewer(client, cap_usd=Decimal("1.00"), reserve_usd=Decimal("0.40"))
    assert reviewer.review(_context()).fault == ""
    assert client.calls[0]["tier"] == "class_3" and client.calls[0]["tool"]["name"] == "momentum_review"
    assert "NEW ENTRANT" in client.calls[0]["user"] and "ABC" in client.calls[0]["user"]
    assert reviewer.review(_context(new=False)).fault == ""
    assert "ALREADY HELD" in client.calls[1]["user"]
    capped = reviewer.review(_context())   # 0.62 spent + 0.40 reserve > 1.00
    assert capped.fault.startswith("spend cap") and len(client.calls) == 2
    failed = Reviewer(FakeClient(fail=True), cap_usd=Decimal("5"), reserve_usd=Decimal("0.4")).review(_context())
    assert failed.fault.startswith("call failed") and not failed.applies


def test_the_prompt_never_carries_performance():
    from orchestrator.momentum_veto import SYSTEM, user_prompt

    text = (SYSTEM + user_prompt(_context())).lower()
    for word in ("p&l", "nav", "drawdown", "target return", "performance", "portfolio value"):
        assert word not in text


def test_the_plan_card_is_green_when_clean_and_yellow_naming_a_problem():
    from execution import cards

    clean_card = cards.momentum_plan_card("November", "2026-11-02", 15, ["AAPL", "NVDA"], ["XOM"],
                                          [("ABC", "pending acquisition")], 0.87, True, [], 1.85, 8.0, "{}")
    assert clean_card.status == cards.GOOD
    assert clean_card.title == "Momentum: November plan ready, nothing to do"
    text = " ".join(clean_card.all_lines)
    assert "15 names at 87% invested" in text and "$1.85 of $8 cap" in text and "ABC" in text
    problem = cards.momentum_plan_card("November", "2026-11-02", 15, [], [], [], 1.0, False,
                                       ["bought without a veto review (the AI leg is off): AAPL"], None, 8.0, "{}")
    assert problem.status == cards.LOOK and problem.look_at
    assert any("200-day" in line for line in problem.all_lines)
    failed = cards.momentum_prep_failed_card("2026-10-30 18:40 ET", "HTTPError: 503")
    assert failed.status == cards.ACTION and "momentum-prep" in failed.what_to_do


def test_the_golden_grades_the_veto_alone():
    from orchestrator import momentum_golden as golden

    cases = golden.load_cases()
    assert {c["name"] for c in cases} >= {"pending_acquisition_ptc", "resolved_squeeze_avis", "clean_leader_micron",
                                           "meme_spike_amc_2021", "accounting_smci_2024",
                                           "held_name_is_never_vetoed"}
    veto_case = next(c for c in cases if c["name"] == "pending_acquisition_ptc")
    hit = Review("PTC", True, True, "pending_acquisition", "deal 2026-10-05", 5, "pinned")
    wrong = Review("PTC", True, True, "squeeze_or_meme_spike", "x", 5, "x")
    assert golden.grade(veto_case, hit)[0] and not golden.grade(veto_case, wrong)[0]
    held_case = next(c for c in cases if c["name"] == "held_name_is_never_vetoed")
    assert not golden.grade(held_case, Review("PTC", False, True, "pending_acquisition", "x", 0, "x"))[0]
    closes = [100.0 * (1.001 ** i) for i in range(300)]
    closes[-5] *= 1.2
    got = golden.facts(closes)
    assert got["return_12_1"] == pytest.approx(closes[-22] / closes[-253] - 1)
    assert got["max_daily_move_3m"] == pytest.approx(0.2 * 1.001 / 1.0 + 0.001, rel=0.05)
    outcomes = golden.run([veto_case], lambda ctx: hit, lambda s, as_of=None: (closes, "2026-10-12"))
    assert outcomes[0].passed and "1/1 passed" in golden.summary(outcomes, Decimal("0.11"))


def test_the_review_client_is_production_with_the_labs_search_settings(monkeypatch):
    from orchestrator.momentum_veto import review_client
    from research.config import ResearchConfig

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    base = ResearchConfig.load()
    client = review_client(base, max_searches=3, search_tool="web_search_20250305", timeout_seconds=300)
    config = client._config
    tier = config.tier_for("class_3")
    assert tier.model == base.tier_for("class_3").model and tier.max_searches == 3
    assert config.web_search.tool_type == "web_search_20250305"
    assert base.tier_for("class_3").max_searches == 1  # production's own config untouched
    assert client._client.timeout == 300 and client._client.max_retries == 1


def test_the_off_cycle_plan_card_shows_scores_vetoes_and_the_sleeve_size():
    from execution import cards

    card = cards.momentum_plan_card(
        "October", "2026-10-15", 15, ["MATX", "IEX"], [], [("ABC", "pending acquisition: deal signed 2026-10-05")],
        1.0, True, [], 1.91, 8.0, "table", scores={"MATX": 2.13, "IEX": 1.874}, sleeve_value=14893.0,
        sleeve_weight=0.15, off_cycle=True)
    assert card.title == "Momentum: first plan ready (off-cycle start), nothing to do"
    text = " ".join(card.all_lines)
    assert "sleeve $14,893 (15% of the book)" in text and "MATX +2.13" in text and "IEX +1.87" in text
    assert "ABC (pending acquisition: deal signed 2026-10-05)" in text
    clean = cards.momentum_plan_card("October", "2026-10-15", 15, ["MATX"], [], [], 1.0, True, [], None, 8.0, "t")
    assert any("AI vetoed: nothing" in line for line in clean.all_lines)


def test_the_fills_report_compares_each_name_with_its_target(tmp_path):
    import json as _json
    from datetime import date as _date, datetime as _dt, timezone as _tz

    from audit.log import AuditLog
    from orchestrator import momentum_fills
    from risk_gate import RiskGate
    from risk_gate.schema import EquityBuyOrder, LimitExecution
    from risk_gate.state import AccountState

    limits = RiskLimits.load()
    limits = limits.model_copy(update={"momentum_sleeve": limits.momentum_sleeve.model_copy(
        update={"entries_enabled": True})})
    clock = lambda: _dt(2026, 10, 15, 15, 0, tzinfo=_tz.utc)  # noqa: E731
    gate = RiskGate(limits, AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")), clock)
    audit = AuditLog(tmp_path / "audit.jsonl", clock=clock)
    (tmp_path / "momentum").mkdir()
    (tmp_path / "momentum" / "plan-2026-10.json").write_text(_json.dumps({
        "rebalance_date": "2026-10-15", "spec": "v1.1", "data_through": "2026-10-14",
        "targets": [{"symbol": "AAPL", "weight": "0.1"}, {"symbol": "MSFT", "weight": "0.1"}]}))
    order = EquityBuyOrder(symbol="AAPL", quantity=Decimal(7), execution=LimitExecution(limit_price=Decimal("200")),
                           sleeve="momentum")
    approved = gate.submit(order)
    record = audit.record_momentum(detail="AAPL", gate_decision=approved, capital=Decimal("1400"),
                                   sleeve_nav=Decimal("15000"), plan_month="2026-10", decision_id="d1")
    audit.record_fill(record.decision_id, "o1", Decimal(7), Decimal("200"), filled_value=Decimal("1400"), side="buy")
    complete, text = momentum_fills.report(audit, tmp_path / "momentum", _date(2026, 10, 15))
    assert not complete                              # MSFT never traded
    assert "AAPL" in text and "93%" in text and "INCOMPLETE" in text
    assert "1400.00 filled of 3000.00 planned (47%)" in text


def test_the_aggressive_reserve_is_one_position_while_only_the_execution_test_trades():
    from orchestrator.sweep import liquidity_buffer, momentum_spendable
    from risk_gate import RiskGate
    from risk_gate.state import AccountState

    limits = RiskLimits.load()
    assert limits.cash_management.aggressive_reserve_positions == 1
    gate = RiskGate(limits, AccountState(cash=Decimal("100000"), high_water_mark=Decimal("100000")))
    capped = liquidity_buffer(gate, limits.cash_management, include_momentum=False)
    uncapped_config = limits.cash_management.model_copy(update={"aggressive_reserve_positions": None})
    full = liquidity_buffer(gate, uncapped_config, include_momentum=False)
    # one slot is 25% of the aggressive sleeve instead of its 100% daily cap
    sleeve = Decimal("100000") * limits.portfolio.sleeves.aggressive
    assert full - capped == sleeve - sleeve * Decimal("0.25")
    # the whole momentum sleeve is fundable from settled cash (step 2: 30% of NAV)
    assert momentum_spendable(gate, limits.cash_management) >= Decimal("100000") * limits.portfolio.sleeves.momentum


def test_a_shadow_declared_after_the_plan_is_backfilled_from_its_start(world, tmp_path):
    from datetime import datetime as _dt, timezone as _tz

    _, past, day = world
    prep.write(tmp_path, prep.compute(past, day, []))
    # simulate a shadow declared after the plan: drop it from the month file
    shadow_file = tmp_path / f"shadows-{day[:7]}.json"
    payload = json.loads(shadow_file.read_text())
    del payload["books"]["v1_1_gap_earnings_exempt"]
    shadow_file.write_text(json.dumps(payload))
    assert prep.missing_shadows(tmp_path, day[:7]) == ["v1_1_gap_earnings_exempt"]
    plan = prep.latest_plan(tmp_path)
    before_open = _dt.fromisoformat(day + "T08:00:00-04:00").astimezone(_tz.utc)
    begin = prep.shadow_start(plan, before_open, [past.calendar[-1], day])
    assert begin == prep.Due(day, past.calendar[-1])          # the plan's own date and data
    added = prep.backfill_shadows(past, begin.rebalance_day, ["v1_1_gap_earnings_exempt"], tmp_path, day[:7])
    assert added == ["v1_1_gap_earnings_exempt"]
    book = json.loads(shadow_file.read_text())["books"]["v1_1_gap_earnings_exempt"]
    assert book["started"] == day and sum(book["weights"].values()) <= 1 + 1e-9
    assert prep.missing_shadows(tmp_path, day[:7]) == []
    assert "v1_1_gap_earnings_exempt" in json.loads((tmp_path / "shadows.json").read_text())["held"]


def test_a_shadow_declared_mid_month_starts_at_the_next_session():
    from datetime import datetime as _dt, timezone as _tz

    plan = {"rebalance_date": "2026-10-15", "data_through": "2026-10-14"}
    sessions = ["2026-10-19", "2026-10-20", "2026-10-21"]
    evening = _dt(2026, 10, 20, 22, 0, tzinfo=_tz.utc)    # 18:00 ET, after the close
    assert prep.shadow_start(plan, evening, sessions) == prep.Due("2026-10-21", "2026-10-20")
    morning = _dt(2026, 10, 20, 12, 0, tzinfo=_tz.utc)    # 08:00 ET
    assert prep.shadow_start(plan, morning, sessions) == prep.Due("2026-10-20", "2026-10-19")
