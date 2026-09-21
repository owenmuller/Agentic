"""Add decisions (human ruling 2026-09-16): one judged position per symbol.

The claims under test: a tradeable signal on a held name is researched as an
ADD DECISION with the position in view, never as a second position; an add
sizes under a combined cap by the new verdict's band, one band wider for an
independent family, never past the hard cap; a hold records convergence and
triggers a review that sees the new signal; the position stays one — lots
kept for cost basis, one stop, one leash on the later resolution date, one
review stream; a close resolves every lot to its own outcome; a restart
rebuilds the merged position from the log (the CELH merge). Mechanical arm
untouched.

Same harness as ``test_orchestrator``/``test_exits``: fakes at the edges,
everything real in between.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from audit.attribution import build_attribution
from audit.records import DecisionRecord, OutcomeRecord, StageRejectionRecord
from execution.base import BrokerPosition
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from forward.funnel import funnel_entries
from orchestrator import start
from orchestrator.golden import load_cases
from research.add_decision import ADD_HOLD_CODES, HeldPositionContext
from research.exit_review import EXIT_REVIEW_TOOL_NAME
from research.prompts import SYSTEM_PROMPT, build_user_prompt, system_prompt_for
from research.reports import ResearchReport, report_tool_definition
from signals.scanners import RawItem
from test_exits import HOLD_REVIEW, MutablePrices, RoutingLLM, build, enter_position, restart_kwargs
from test_orchestrator import (
    NOW,
    QUOTE,
    REPORT,
    FakeBroker,
    FakeClock,
    counter,
    structured,
)

#: The entry: REPORT (long NUE, 71 -> 5% of the 55,000 judged sleeve = 2,750 ->
#: 19 shares at 140 = 2,660 (55/15/30/0 allocation, rulings 2026-09-18/21).
ENTRY_SHARES = Decimal("19")
ENTRY_COST = ENTRY_SHARES * QUOTE

ADD_REPORT = {
    **REPORT,
    "thesis": "Independent confirmation of the steel thesis; the combined position is still inside the band.",
    "confidence": 86,
    "priced_in_analysis": "NUE is flat since the transaction date; the disclosure is not priced in.",
    "expected_resolution_date": "2026-10-30",
    "add_verdict": "add",
    "add_fraction": "0.5",
}
HOLD_ADD = {
    **REPORT,
    "direction": "no_position",
    "confidence": 70,
    "target_price": None,
    "priced_in_analysis": "The disclosure adds nothing the position does not already rest on.",
    "add_verdict": "hold",
    "add_fraction": None,
}


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


class AddAwareLLM:
    """Answers the entry pass, the add-decision pass (recognised by the ADD
    DECISION block in the prompt) and the review pass differently."""

    def __init__(self, entry=None, add=None, review=None) -> None:
        self.entry = entry or structured(REPORT)
        self.add = add or structured(ADD_REPORT)
        self.review = review or structured(HOLD_REVIEW)
        self.calls: list[dict] = []

    def research(self, *, system: str, user: str, tool: dict, tier: str = ""):
        self.calls.append({"system": system, "user": user, "tool": tool["name"], "tier": tier})
        if tool["name"] == EXIT_REVIEW_TOOL_NAME:
            return self.review
        # The add fields are offered ONLY on an add decision's schema.
        offered = set(tool["input_schema"]["properties"])
        assert ("add_verdict" in offered) == ("ADD DECISION" in user), offered
        if "ADD DECISION" in user:
            return self.add
        return self.entry

    @property
    def add_prompts(self) -> list[str]:
        return [c["user"] for c in self.calls if "ADD DECISION" in c["user"]]


class Feeds:
    """A fetcher whose items a test can change between ticks."""

    def __init__(self) -> None:
        from fixture_posts import PURE_FORWARD_CALL

        self.items: dict[str, list[RawItem]] = {
            "trump_posts": [RawItem("trump_posts-0", PURE_FORWARD_CALL, NOW)]
        }

    def __call__(self, source):
        return list(self.items.get(source.id, ()))


def nue_disclosure(external_id="0001111111-26-000101", amount="$265,000"):
    """A Form 4 insider CLUSTER purchase of the held name: a Class 2 filing
    family the mechanical arm never sees (so the tick stays a judged-only
    story), independent of the Trump-post family that opened the position."""
    return RawItem(
        external_id=external_id,
        content="Form 4 insider filing (SEC EDGAR, structured XML); issuer: Nucor (NUE); CLUSTER: 2 distinct insiders",
        published_at=NOW,
        fields={
            "form": "4",
            "accession": external_id,
            "ticker": "NUE",
            "issuer": "Nucor",
            "transaction": "Purchase",
            "report_date": "2026-08-15",
            "transaction_date": "2026-08-11",
            "amount_range": amount,
            "cluster": "true",
            "cluster_insiders": "2",
            "filer": "Avery Cfo; Blair Director",
        },
    )


def enter_nue(tmp_path, limits, signals_config, research_config, llm=None):
    """Tick 1: the entry fills. Returns (started, feeds, prices, clock, llm)."""
    llm = llm or AddAwareLLM()
    feeds = Feeds()
    prices = MutablePrices(NUE=str(QUOTE))
    clock = FakeClock()
    started, _, _ = enter_position(
        tmp_path, limits, signals_config, research_config,
        llm=llm, fetcher=feeds, prices=prices, clock=clock,
    )
    position = started.exits.tracked[0]
    assert position.symbol == "NUE" and position.quantity == ENTRY_SHARES
    return started, feeds, prices, clock, llm


def second_signal(started, feeds, clock, item=None):
    """Tick 2: an hourly poll delivers an insider cluster purchase of the held name."""
    feeds.items["form4_insiders"] = [item or nue_disclosure()]
    clock.advance(minutes=61)
    return started.loop.tick()


# ================================================================================
# Routing and sizing
# ================================================================================


def test_a_signal_on_a_held_name_is_an_add_decision_not_a_second_position(
    tmp_path, limits, signals_config, research_config
):
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    report = second_signal(started, feeds, clock)

    assert len(report.processed) == 1 and report.processed[0].traded
    # The add pass saw the position, not just the signal.
    prompt = llm.add_prompts[-1]
    assert "ADD DECISION" in prompt and "POSITION HELD" in prompt
    assert "symbol: NUE" in prompt and "originating source: trump_posts" in prompt
    assert "insider_filings" in prompt and "INDEPENDENT" in prompt
    # Still ONE tracked position, now with two lots.
    assert len(started.exits.tracked) == 1
    position = started.exits.tracked[0]
    assert position.decision_id == "dec-1"
    assert len(position.lots) == 2
    assert position.lots[0].decision_id == "dec-1" and position.lots[1].decision_id == "dec-2"
    # Combined cap (rule 2): confidence 86 -> 10% band = 5,500; held 2,660 ->
    # headroom 2,840 x add_fraction 0.5 = 1,420 -> 10 shares at 140.
    assert position.lots[1].quantity == Decimal("10")
    assert position.quantity == ENTRY_SHARES + 10
    assert position.entry_cost == ENTRY_COST + 10 * QUOTE
    assert position.entry_price == QUOTE  # same price both lots: the blend is 140
    decision = started.audit.trail("dec-2").decision
    assert decision.add is not None
    assert decision.add.verdict == "add"
    assert decision.add.position_decision_id == "dec-1"
    assert decision.add.family_bump is False  # 10% is already the top band
    assert decision.add.combined_cap_fraction == Decimal("0.10")
    assert decision.add.held_value == ENTRY_COST
    assert decision.add.headroom == Decimal("5500") - ENTRY_COST
    assert decision.sizing.capital == Decimal("1420.00")
    assert decision.research.add_verdict == "add"
    # The leash moved to the later resolution date (rule 3): entry stated none
    # (weeks fallback 45), the add stated 2026-10-30 = day 74 from 2026-08-17.
    assert position.leash_days == 74
    assert position.resolution_date.isoformat() == "2026-10-30"


def test_an_independent_family_bumps_the_combined_cap_one_band(
    tmp_path, limits, signals_config, research_config
):
    """Confidence 75 is the 5% band (2,750): with the position at 2,660 that is
    90 of headroom — nothing. The insider-filings family is independent of the
    Trump-post family that opened it, so the cap is the 10% band instead."""
    llm = AddAwareLLM(add=structured({**ADD_REPORT, "confidence": 75}))
    started, feeds, prices, clock, llm = enter_nue(
        tmp_path, limits, signals_config, research_config, llm=llm
    )
    report = second_signal(started, feeds, clock)
    assert report.processed[0].traded
    decision = started.audit.trail("dec-2").decision
    assert decision.add.family_bump is True
    assert decision.add.combined_cap_fraction == Decimal("0.10")
    assert decision.sizing.capital == Decimal("1420.00")
    assert "one band up" in decision.sizing.rationale


def test_the_same_family_gets_no_bump_and_a_full_band_is_no_headroom(
    tmp_path, limits, signals_config, research_config
):
    """A second Trump post on the same name: same family, no bump. At 62 the
    combined cap is the 2% band (1,100) and the position already holds 2,660 —
    no headroom, no add, convergence recorded, review owed."""
    llm = AddAwareLLM(add=structured({**ADD_REPORT, "confidence": 62}))
    started, feeds, prices, clock, llm = enter_nue(
        tmp_path, limits, signals_config, research_config, llm=llm
    )
    # The scanner extracts $NVDA from the fixture post while the report names
    # NUE, so the second post takes the UNROUTED path: the entry pass names the
    # held symbol, and the add-decision pass runs after it.
    from fixture_posts import PURE_FORWARD_CALL

    feeds.items["trump_posts"].append(RawItem("trump_posts-1", PURE_FORWARD_CALL + " Adding.", NOW))
    clock.advance(days=1, minutes=1)  # same-day repeats attach (ruling 2026-09-18)
    report = started.loop.tick()

    result = report.processed[0]
    assert not result.traded
    assert result.rejection.code == "add_no_headroom"
    assert result.rejection.add.verdict == "no_headroom"
    assert result.rejection.add.family_bump is False
    assert result.rejection.add.second_pass is True
    assert result.rejection.add.combined_cap_fraction == Decimal("0.02")
    assert len(started.exits.tracked) == 1
    position = started.exits.tracked[0]
    assert position.quantity == ENTRY_SHARES
    assert len(position.convergence) == 1
    assert position.convergence[0].verdict == "no_headroom"
    # The review it owed ran in the same tick (reviews follow dispatch).
    assert report.reviews_run == 1 and position.review_due_kind == ""


def test_a_hold_verdict_records_convergence_and_triggers_a_review_that_sees_it(
    tmp_path, limits, signals_config, research_config
):
    llm = AddAwareLLM(add=structured(HOLD_ADD))
    started, feeds, prices, clock, llm = enter_nue(
        tmp_path, limits, signals_config, research_config, llm=llm
    )
    report = second_signal(started, feeds, clock)
    result = report.processed[0]
    assert not result.traded
    assert result.rejection.code == "already_held_no_add"
    assert result.rejection.code in ADD_HOLD_CODES
    assert result.rejection.add.verdict == "hold"
    assert result.rejection.add.position_decision_id == "dec-1"
    assert result.rejection.research.add_verdict == "hold"
    position = started.exits.tracked[0]
    assert position.quantity == ENTRY_SHARES
    assert [note.source_id for note in position.convergence] == ["form4_insiders"]
    assert position.convergence[0].family == "insider_filings"
    # The owed review ran out of cadence in the same tick (reviews follow
    # dispatch), and its prompt carried the convergent signal.
    assert report.reviews_run == 1
    review = started.audit.trail("dec-1").reviews[-1]
    assert "add decision was hold" in (review.trigger_reason or "")
    review_prompt = [c for c in llm.calls if c["tool"] == EXIT_REVIEW_TOOL_NAME][-1]["user"]
    assert "WHY YOU ARE SEEING THIS NOW: a NEW signal on this name" in review_prompt
    assert "CONVERGENT SIGNALS SINCE ENTRY" in review_prompt
    assert "form4_insiders (insider_filings)" in review_prompt
    assert position.review_due_kind == ""


def test_an_add_without_a_fraction_or_off_symbol_reads_as_a_hold(
    tmp_path, limits, signals_config, research_config
):
    llm = AddAwareLLM(add=structured({**ADD_REPORT, "add_fraction": None}))
    started, feeds, prices, clock, llm = enter_nue(
        tmp_path, limits, signals_config, research_config, llm=llm
    )
    result = second_signal(started, feeds, clock).processed[0]
    assert result.rejection.code == "already_held_no_add"
    assert "without an add_fraction" in result.rejection.message
    assert started.exits.tracked[0].quantity == ENTRY_SHARES

    pipeline = started.loop.pipeline
    context = started.exits.context_for_symbol("NUE")
    off_symbol = ResearchReport.model_validate({**ADD_REPORT, "tickers": ["STLD"]})
    verdict, message = pipeline._add_problem(off_symbol, context)
    assert verdict == "hold" and "exactly the held symbol" in message


def test_adds_to_an_option_position_are_not_built(tmp_path, limits, signals_config, research_config):
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    pipeline = started.loop.pipeline
    context = started.exits.context_for_symbol("NUE")
    fields = {name: getattr(context, name) for name in HeldPositionContext.__slots__}
    option_context = HeldPositionContext(**{**fields, "instrument_kind": "option"})
    verdict, message = pipeline._add_problem(
        ResearchReport.model_validate(ADD_REPORT), option_context
    )
    assert verdict == "not_built" and "option" in message


def test_adds_never_exceed_the_hard_cap(tmp_path, limits, signals_config, research_config):
    """Confidence 100 on an independent family: the band is already the hard
    cap, the bump has nowhere to go, and the combined position is 10%."""
    llm = AddAwareLLM(add=structured({**ADD_REPORT, "confidence": 100, "add_fraction": "1"}))
    started, feeds, prices, clock, llm = enter_nue(
        tmp_path, limits, signals_config, research_config, llm=llm
    )
    second_signal(started, feeds, clock)
    decision = started.audit.trail("dec-2").decision
    assert decision.add.combined_cap_fraction == limits.sizing.hard_cap == Decimal("0.10")
    assert decision.add.family_bump is False
    position = started.exits.tracked[0]
    # 5,500 - 2,660 = 2,840 -> 20 shares at 140 = 2,800; total 5,460 <= 5,500.
    assert position.entry_cost <= Decimal("5500")
    assert position.quantity == ENTRY_SHARES + 20


# ================================================================================
# One position, many lots: stops, leash, close, restart
# ================================================================================


def test_the_stop_is_re_derived_on_add_and_only_ever_tightens(
    tmp_path, limits, signals_config, research_config
):
    """Fixed-15% regime in the harness (no ATR): the entry stop is 119. An add
    at a higher price lifts the blended entry and the re-derived stop; an add
    below the blended entry would lower it, and is refused (Constraint #6)."""
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    prices.set("NUE", "160")
    llm.add = structured({**ADD_REPORT, "target_price": "260"})  # clears reward:risk at 160
    second_signal(started, feeds, clock)
    position = started.exits.tracked[0]
    assert position.quantity > ENTRY_SHARES
    blended = position.entry_cost / position.quantity
    assert QUOTE < blended < Decimal("160")
    assert position.stop_price == blended * Decimal("0.85")
    assert position.stop_price > Decimal("119")

    # A second add, below the blend: the derived stop would fall; it does not.
    stop_before = position.stop_price
    prices.set("NUE", "120")
    feeds.items["form4_insiders"] = [nue_disclosure("0001111111-26-000102")]
    # A quarter of the headroom: the gate marks the position at the LAST
    # mark it saw (160) until the guardrail pass re-marks it, so a full-headroom
    # add here would be refused as max_single_position_exceeded — correctly.
    llm.add = structured({**ADD_REPORT, "confidence": 100, "add_fraction": "0.25", "target_price": "260"})
    clock.advance(days=1, minutes=1)  # same-day repeats attach (ruling 2026-09-18)
    tick = started.loop.tick()
    assert tick.processed and tick.processed[0].traded
    assert len(position.lots) == 3
    assert position.stop_price == stop_before


def test_a_close_resolves_every_lot_to_its_own_outcome(
    tmp_path, limits, signals_config, research_config
):
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    second_signal(started, feeds, clock)
    position = started.exits.tracked[0]
    assert len(position.lots) == 2

    prices.set("NUE", "100")  # through the 119 stop
    assert started.loop.tick().exits_started == 1
    started.loop.tick()  # settles

    assert started.exits.tracked == ()
    outcomes = {r.decision_id: r for r in started.audit.records() if isinstance(r, OutcomeRecord)}
    assert set(outcomes) == {"dec-1", "dec-2"}
    # FIFO relief of a full close: each lot's own cost against its own proceeds.
    assert outcomes["dec-1"].realised_pnl == 19 * Decimal("100") - ENTRY_COST
    assert outcomes["dec-2"].realised_pnl == 10 * Decimal("100") - 10 * QUOTE
    assert "lot 1 of 2" in outcomes["dec-1"].note and "lot 2 of 2" in outcomes["dec-2"].note
    assert started.audit.trail("dec-1").is_complete and started.audit.trail("dec-2").is_complete

    # Attribution separates the add from the lot it joined; the funnel marks it.
    report = build_attribution(started.audit.trails(), clock())
    assert report.adds is not None
    assert report.adds.adds == 1 and report.adds.adds_closed == 1
    assert report.adds.origins == 1 and report.adds.origins_closed == 1
    assert "Add decisions (ruling 2026-09-16" in report.render()
    entries = {e.decision_id: e for e in funnel_entries(started.audit.records())}
    assert entries["dec-2"].is_add is True and entries["dec-1"].is_add is False


def test_a_merged_position_is_rebuilt_from_the_log_after_a_restart(
    tmp_path, limits, signals_config, research_config
):
    """The CELH case: two open judged trails in one symbol replay as ONE
    position with two lots — blended entry, one stop, the leash on the later
    resolution date — whether or not the second was stamped as an add."""
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    second_signal(started, feeds, clock)
    live = started.exits.tracked[0]
    started.loop.shutdown()

    broker = FakeBroker(
        cash=Decimal("90000"),
        positions=[BrokerPosition("NUE", live.quantity, live.entry_cost, live.entry_cost)],
    )
    restarted = start(
        fetcher=Feeds(),
        prices=MutablePrices(NUE=str(QUOTE)),
        llm_client=RoutingLLM(),
        adapter=broker,
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    assert len(restarted.exits.tracked) == 1
    position = restarted.exits.tracked[0]
    assert position.decision_id == "dec-1"
    assert [lot.decision_id for lot in position.lots] == ["dec-1", "dec-2"]
    assert [lot.quantity for lot in position.lots] == [ENTRY_SHARES, Decimal("10")]
    assert position.quantity == live.quantity
    assert position.entry_cost == live.entry_cost
    assert position.entry_price == live.entry_price
    assert position.stop_price == live.stop_price
    assert position.leash_days == live.leash_days == 74
    assert position.resolution_date == live.resolution_date
    assert position.originating_family == "trump_posts"
    assert position.lots[1].family == "insider_filings"


def test_a_held_add_decision_survives_a_restart_as_convergence_and_an_owed_review(
    tmp_path, limits, signals_config, research_config
):
    llm = AddAwareLLM(add=structured(HOLD_ADD))
    started, feeds, prices, clock, llm = enter_nue(
        tmp_path, limits, signals_config, research_config, llm=llm
    )
    second_signal(started, feeds, clock)  # the hold's review ran in this tick
    started.loop.shutdown()

    broker = FakeBroker(
        cash=Decimal("97340"),
        positions=[BrokerPosition("NUE", ENTRY_SHARES, ENTRY_COST, ENTRY_COST)],
    )
    restarted = start(
        fetcher=Feeds(),
        prices=MutablePrices(NUE=str(QUOTE)),
        llm_client=RoutingLLM(),
        adapter=broker,
        id_factory=counter("b"),
        **restart_kwargs(tmp_path, limits, signals_config, research_config, clock),
    )
    position = restarted.exits.tracked[0]
    assert len(position.convergence) == 1
    assert position.convergence[0].verdict == "hold"
    assert position.convergence[0].source_id == "form4_insiders"
    assert not position.lots  # nothing was added
    # The review that answered it is remembered, so nothing is owed...
    assert position.last_review_at is not None and position.review_due_kind == ""
    # ...and had the process died between the hold and its review, the flag
    # would be re-armed from the record alone.
    position.last_review_at = None
    position.review_due_reason = ""
    position.convergence.clear()
    held = [r for r in restarted.audit.stage_rejections() if r.add is not None]
    restarted.exits._restore_convergence(position, held)
    assert len(position.convergence) == 1
    assert position.review_due_kind == "add_signal"
    assert "add decision was hold" in position.review_due_reason


# ================================================================================
# Schema, prompt, golden
# ================================================================================


def test_the_report_schema_offers_add_verdict_and_a_bounded_fraction():
    tool = report_tool_definition(add_decision=True)
    assert "add_verdict" in tool["input_schema"]["properties"]
    assert "add_fraction" in tool["input_schema"]["properties"]
    assert {"add_verdict", "add_fraction"} <= set(tool["input_schema"]["required"])
    assert "ADD DECISION" in tool["description"]
    report = ResearchReport.model_validate(ADD_REPORT)
    assert report.is_add and report.add_fraction == Decimal("0.5")
    assert not ResearchReport.model_validate(HOLD_ADD).is_add
    assert not ResearchReport.model_validate(REPORT).is_add
    with pytest.raises(ValidationError):
        ResearchReport.model_validate({**ADD_REPORT, "add_fraction": "1.5"})
    with pytest.raises(ValidationError):
        ResearchReport.model_validate({**ADD_REPORT, "add_fraction": "0"})
    with pytest.raises(ValidationError):
        ResearchReport.model_validate({**ADD_REPORT, "add_verdict": "double"})


def test_the_system_prompt_states_the_add_question_and_denies_the_dollars():
    prompt = system_prompt_for(True)
    assert "ADD DECISIONS" in prompt
    assert "one judged position per symbol" in prompt
    assert "combined-position cap you cannot see or move" in prompt


def test_an_ordinary_entry_pass_sends_the_pre_ruling_request_shape():
    """The add machinery must be invisible to every pass that is not an add
    decision (2026-09-16 drift review): no section in the system prompt, no
    add fields in the tool schema, nothing in the description."""
    assert system_prompt_for(False) == SYSTEM_PROMPT
    assert "ADD DECISION" not in SYSTEM_PROMPT and "add_verdict" not in SYSTEM_PROMPT
    tool = report_tool_definition()
    rendered = str(tool)
    assert "add_verdict" not in rendered and "add_fraction" not in rendered
    assert "AddVerdict" not in rendered and "ADD DECISION" not in rendered
    # Both shapes validate through the same model: an ordinary report carries
    # no add fields and still parses.
    assert ResearchReport.model_validate(REPORT).add_verdict is None


def test_the_research_pass_selects_the_shape_per_request(tmp_path, limits, signals_config, research_config):
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    second_signal(started, feeds, clock)
    entry_calls = [c for c in llm.calls if c["tool"] != EXIT_REVIEW_TOOL_NAME and "ADD DECISION" not in c["user"]]
    add_calls = [c for c in llm.calls if "ADD DECISION" in c["user"]]
    assert entry_calls and add_calls
    assert all("ADD DECISIONS" not in c["system"] for c in entry_calls)
    assert all("ADD DECISIONS" in c["system"] for c in add_calls)


def test_the_golden_set_carries_the_add_cases_and_their_contexts_render():
    cases = [case for case in load_cases() if case.kind == "add"]
    assert {case.name for case in cases} == {
        "add-celh-same-cluster-repeat-real",
        "add-celh-cross-family-congressional-synthetic",
    }
    same, cross = sorted(cases, key=lambda case: case.name, reverse=True)
    assert same.add_verdicts == ("hold",)
    context = cross.add_context()
    assert context.symbol == "CELH" and len(context.lots) == 2
    assert context.independent_family and context.leash_days == 181
    prompt = build_user_prompt(
        cross.signal(datetime(2026, 9, 18, 14, 30, tzinfo=timezone.utc)), add_context=context
    )
    assert "ADD DECISION" in prompt and "2 lot(s)" in prompt
    assert "INDEPENDENT of the position's family" in prompt
    # The lots' own signal content never re-enters through the add block: only
    # the new signal is fenced.
    assert prompt.count("-----BEGIN UNTRUSTED THIRD-PARTY CONTENT-----") == 1


def test_health_renders_lots_and_convergence(tmp_path, limits, signals_config, research_config):
    from orchestrator.ops import _fmt_position

    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    second_signal(started, feeds, clock)
    position = started.exits.tracked[0]
    rendered = _fmt_position(position, clock())
    assert "lots: dec-1 19@140.00 (trump_posts, 71) | dec-2 10@140.00 (insider_filings, 86)" in rendered


def test_the_mechanical_arm_is_untouched(tmp_path, limits, signals_config, research_config):
    """The mechanical arm observes the same disclosure on its own rules (one
    slice per name, no LLM); the judged add decision never stamps its records."""
    started, feeds, prices, clock, llm = enter_nue(tmp_path, limits, signals_config, research_config)
    second_signal(started, feeds, clock)
    assert len(started.exits.tracked) == 1
    for record in started.audit.records():
        if isinstance(record, DecisionRecord) and record.sizing.strategy == "mechanical":
            assert record.add is None
