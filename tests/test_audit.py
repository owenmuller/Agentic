"""Audit log tests.

The properties that matter: a decision record cannot be written with a stage missing,
nothing already written can be changed, an outcome credits the source that called it,
and attribution over fixture history flags a losing signal class.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from audit import (
    AuditLog,
    AuditLogError,
    AuditTrail,
    CorrectionRecord,
    DecisionRecord,
    FillRecord,
    OutcomeRecord,
    RejectedStage,
    build_attribution,
)
from research import CredibilityTracker, ResearchReport
from risk_gate import (
    AccountState,
    EquityBuyOrder,
    LimitExecution,
    RiskGate,
    RiskLimits,
    Sleeve,
)
from signals import Classification, CredibilityLog, Priority, Signal, SignalClass
from sizing import SizingEngine

NOW = datetime(2026, 8, 17, 14, 30, tzinfo=timezone.utc)
START_CASH = Decimal("100000")

REPORT_PAYLOAD = {
    "thesis": "Tariff headline lifts domestic steel.",
    "tickers": ["NUE"],
    "direction": "long",
    "time_horizon": "weeks",
    "priced_in_analysis": None,
    "confidence": 71,
    "invalidation_condition": "Exemption granted.",
    "manipulation_assessment": "none detected",
}


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def make_signal(
    signal_id: str = "sig-1",
    source_id: str = "nolimitgains",
    signal_class: SignalClass = SignalClass.CLASS_1_REALTIME,
    content: str = "Buying $NUE here, entry: 140.",
    raw_content: str | None = None,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        source_id=source_id,
        signal_class=signal_class,
        observed_at=NOW,
        content=content,
        raw_content=raw_content or content,
        priority=Priority.for_class(signal_class),
        external_id=f"{signal_id}-post",
        classification=Classification.FORWARD_CALL,
    )


def make_report(**overrides) -> ResearchReport:
    return ResearchReport.model_validate({**REPORT_PAYLOAD, **overrides})


@pytest.fixture(scope="session")
def limits() -> RiskLimits:
    return RiskLimits.load()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def log(tmp_path, clock) -> AuditLog:
    return AuditLog(
        path=tmp_path / "audit.jsonl",
        clock=clock,
        id_factory=_counter(),
    )


def _counter():
    state = {"n": 0}

    def next_id() -> str:
        state["n"] += 1
        return f"dec-{state['n']}"

    return next_id


def gate_for(limits, cash: Decimal = START_CASH) -> RiskGate:
    return RiskGate(limits, AccountState(cash=cash, high_water_mark=cash), FakeClock())


def equity_order(symbol: str = "NUE", qty: int = 10, price: str = "140.00"):
    return EquityBuyOrder(
        symbol=symbol,
        quantity=qty,
        execution=LimitExecution(limit_price=Decimal(price)),
    )


def full_decision(log, limits, gate=None, signal=None, report=None, order=None):
    """Write one decision through the real pipeline: research -> sizing -> gate."""
    gate = gate if gate is not None else gate_for(limits)
    signal = signal if signal is not None else make_signal()
    report = report if report is not None else make_report()

    proposal = SizingEngine(limits).propose_equity(
        report, gate.sleeve_nav(Sleeve.EQUITY)
    )
    decision = gate.submit(order if order is not None else equity_order())
    return log.record_decision(signal, report, proposal, decision), gate


# ================================================================================
# Record completeness
# ================================================================================


def test_a_complete_decision_record_carries_every_stage(log, limits):
    record, _ = full_decision(log, limits)

    assert record.signal.signal_id == "sig-1"
    assert record.research.confidence == 71
    assert record.sizing.capital > 0
    assert record.gate.approved is True
    assert record.gate.approval_sequence == 1


@pytest.mark.parametrize("missing", ["signal", "research", "sizing", "gate"])
def test_a_decision_record_missing_any_stage_fails_validation(log, limits, missing):
    record, _ = full_decision(log, limits)
    payload = json.loads(record.model_dump_json())
    del payload[missing]

    with pytest.raises(ValidationError):
        DecisionRecord.model_validate(payload)


def test_the_raw_original_post_is_in_the_record(log, limits):
    """A mixed post's stripped half must still be recoverable from one record."""
    signal = make_signal(
        content="Now loading $SOFI calls here.",
        raw_content="Members banked 240% on $AMD. Now loading $SOFI calls here.",
    )
    record, _ = full_decision(log, limits, signal=signal)

    assert "240%" not in record.signal.content
    assert "240%" in record.signal.raw_content


def test_the_manipulation_assessment_is_carried_into_the_record(log, limits):
    report = make_report(manipulation_assessment="Author is already positioned.")
    record, _ = full_decision(log, limits, report=report)

    assert record.research.manipulation_assessment == "Author is already positioned."
    assert record.research.flagged_manipulation is True


def test_a_rejected_order_is_a_first_class_record(log, limits):
    """Rejections are signal, not noise — they get the same complete record."""
    gate = gate_for(limits)
    # $14,000 against a $4,500 single-position cap — affordable, but too concentrated,
    # so the cap is what rejects it rather than the cash balance.
    decision = gate.submit(equity_order(qty=100, price="140.00"))
    assert not decision.is_approved

    proposal = SizingEngine(limits).propose_equity(make_report(), Decimal("90000"))
    record = log.record_decision(make_signal(), make_report(), proposal, decision)

    assert record.was_approved is False
    assert record.gate.rejection_code == "max_single_position_exceeded"
    assert record.gate.rejection_limit is not None
    assert record.gate.rejection_observed is not None
    # And it is in the log alongside the approvals.
    assert len(log.decisions()) == 1


# ================================================================================
# Append-only
# ================================================================================


def test_the_log_exposes_no_way_to_change_the_past(log):
    surface = {name for name in dir(log) if not name.startswith("_")}
    for forbidden in ("update", "delete", "remove", "edit", "overwrite", "truncate"):
        assert not any(forbidden in name for name in surface), (
            f"audit log exposes {forbidden!r}"
        )


def test_appending_never_rewrites_an_earlier_line(log, limits):
    first, gate = full_decision(log, limits)
    before = log.path.read_text(encoding="utf-8")

    log.record_fill(first.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))
    after = log.path.read_text(encoding="utf-8")

    assert after.startswith(before), "an earlier line changed"
    assert after.count("\n") == before.count("\n") + 1


def test_a_correction_is_a_new_record_that_names_the_original(log, limits):
    record, _ = full_decision(log, limits)
    correction = log.record_correction(
        record.decision_id,
        supersedes_sequence=0,
        reason="fill price was mis-keyed",
        fill_price="141.00",
    )

    assert isinstance(correction, CorrectionRecord)
    assert correction.supersedes_sequence == 0
    # The original is still there, unchanged.
    trail = log.trail(record.decision_id)
    assert trail.decision == record
    assert trail.corrections == (correction,)


def test_records_are_frozen(log, limits):
    record, _ = full_decision(log, limits)
    with pytest.raises(ValidationError):
        record.decision_id = "tampered"


def test_a_fill_against_a_rejected_decision_is_refused(log, limits):
    """A fill on a rejected decision means something bypassed the gate."""
    gate = gate_for(limits)
    decision = gate.submit(equity_order(qty=1000))
    proposal = SizingEngine(limits).propose_equity(make_report(), Decimal("90000"))
    record = log.record_decision(make_signal(), make_report(), proposal, decision)

    with pytest.raises(AuditLogError, match="bypassed the gate"):
        log.record_fill(record.decision_id, "brk-1", Decimal("1"), Decimal("140"))


def test_resolving_the_same_position_twice_is_refused(log, limits):
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))
    log.record_outcome(record.decision_id, Decimal("120"))

    with pytest.raises(AuditLogError, match="CorrectionRecord"):
        log.record_outcome(record.decision_id, Decimal("130"))


# ================================================================================
# Trail assembly
# ================================================================================


def test_a_trail_assembles_the_full_lifecycle(log, limits):
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("139.50"))
    log.record_outcome(record.decision_id, Decimal("250"), note="target hit")

    trail = log.trail(record.decision_id)
    assert isinstance(trail, AuditTrail)
    assert trail.is_complete
    assert trail.realised_pnl == Decimal("250")
    assert trail.fills[0].fill_price == Decimal("139.50")
    assert trail.outcome.won is True


def test_a_rejected_trail_is_complete_immediately(log, limits):
    gate = gate_for(limits)
    decision = gate.submit(equity_order(qty=1000))
    proposal = SizingEngine(limits).propose_equity(make_report(), Decimal("90000"))
    record = log.record_decision(make_signal(), make_report(), proposal, decision)

    assert log.trail(record.decision_id).is_complete


def test_an_approved_but_unresolved_trail_is_not_complete(log, limits):
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))
    assert log.trail(record.decision_id).is_complete is False


def test_the_log_survives_a_reopen(tmp_path, limits):
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path=path, clock=FakeClock(), id_factory=_counter())
    record, _ = full_decision(first, limits)

    reopened = AuditLog(path=path, clock=FakeClock())
    assert [d.decision_id for d in reopened.decisions()] == [record.decision_id]


# ================================================================================
# Outcome resolution feeds credibility
# ================================================================================


def test_a_winning_outcome_credits_the_source(log, limits):
    tracker = CredibilityTracker(CredibilityLog())
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))

    log.record_outcome(record.decision_id, Decimal("300"), credibility=tracker)

    summary = tracker.summary_for("nolimitgains")
    assert summary.resolved_calls == 1
    assert summary.winning_calls == 1
    assert summary.hit_rate == 1.0


def test_a_losing_outcome_debits_the_source(log, limits):
    tracker = CredibilityTracker(CredibilityLog())
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))

    log.record_outcome(record.decision_id, Decimal("-180"), credibility=tracker)

    summary = tracker.summary_for("nolimitgains")
    assert summary.resolved_calls == 1
    assert summary.winning_calls == 0
    assert summary.hit_rate == 0.0


def test_a_flat_close_is_not_a_win(log, limits):
    """Ties go against the source — Constraint #6."""
    tracker = CredibilityTracker(CredibilityLog())
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))

    log.record_outcome(record.decision_id, Decimal("0"), credibility=tracker)
    assert tracker.summary_for("nolimitgains").winning_calls == 0


def test_outcomes_turn_a_none_hit_rate_into_a_number(log, limits):
    """The whole point of the outcome step."""
    tracker = CredibilityTracker(CredibilityLog())
    assert tracker.summary_for("nolimitgains").hit_rate is None

    for i, pnl in enumerate(["200", "-50", "175"], start=1):
        signal = make_signal(signal_id=f"sig-{i}")
        record, _ = full_decision(log, limits, signal=signal)
        log.record_fill(record.decision_id, f"brk-{i}", Decimal("10"), Decimal("140"))
        log.record_outcome(record.decision_id, Decimal(pnl), credibility=tracker)

    summary = tracker.summary_for("nolimitgains")
    assert summary.hit_rate == pytest.approx(2 / 3)
    assert "67%" in summary.as_context()


def test_the_outcome_is_attributed_to_the_signal_that_called_it(log, limits):
    """Two sources, one loses — credibility must not smear across them."""
    tracker = CredibilityTracker(CredibilityLog())

    winner, _ = full_decision(log, limits, signal=make_signal(source_id="trump_posts"))
    log.record_fill(winner.decision_id, "brk-1", Decimal("10"), Decimal("140"))
    log.record_outcome(winner.decision_id, Decimal("400"), credibility=tracker)

    loser, _ = full_decision(
        log, limits, signal=make_signal(signal_id="sig-2", source_id="nolimitgains")
    )
    log.record_fill(loser.decision_id, "brk-2", Decimal("10"), Decimal("140"))
    log.record_outcome(loser.decision_id, Decimal("-90"), credibility=tracker)

    assert tracker.summary_for("trump_posts").hit_rate == 1.0
    assert tracker.summary_for("nolimitgains").hit_rate == 0.0


# ================================================================================
# Attribution
# ================================================================================


def seed_history(log, limits, clock) -> None:
    """A fixture history: class 1 profitable, class 2 losing, class 3 unresolved."""
    plan = [
        (SignalClass.CLASS_1_REALTIME, ["500", "-120", "340"]),
        (SignalClass.CLASS_2_MOMENTUM, ["-400", "-260", "90"]),
        (SignalClass.CLASS_3_THESIS, [None, None]),
    ]
    n = 0
    for signal_class, pnls in plan:
        for pnl in pnls:
            n += 1
            clock.advance(days=1)
            signal = make_signal(signal_id=f"sig-{n}", signal_class=signal_class)
            record, _ = full_decision(log, limits, signal=signal)
            log.record_fill(record.decision_id, f"brk-{n}", Decimal("10"), Decimal("140"))
            if pnl is not None:
                log.record_outcome(record.decision_id, Decimal(pnl))


def test_attribution_splits_pnl_by_signal_class(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    report = build_attribution(log.trails(), generated_at=clock.now)

    assert report.by_class[SignalClass.CLASS_1_REALTIME].realised_pnl == Decimal("720")
    assert report.by_class[SignalClass.CLASS_2_MOMENTUM].realised_pnl == Decimal("-570")
    assert report.total_pnl == Decimal("150")


def test_attribution_reports_hit_rates_per_class(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    report = build_attribution(log.trails(), generated_at=clock.now)
    assert report.by_class[SignalClass.CLASS_1_REALTIME].hit_rate == pytest.approx(2 / 3)
    assert report.by_class[SignalClass.CLASS_2_MOMENTUM].hit_rate == pytest.approx(1 / 3)


def test_a_negative_class_is_flagged_for_human_review(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    report = build_attribution(log.trails(), generated_at=clock.now)

    assert report.flagged_classes == (SignalClass.CLASS_2_MOMENTUM,)
    rendered = report.render()
    assert "FLAGGED FOR HUMAN REVIEW" in rendered
    assert "possible removal" in rendered


def test_an_unresolved_class_is_not_flagged_as_flat(tmp_path, limits):
    """No closes is different from breaking even, and must not read as a pass."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    unresolved = build_attribution(log.trails(), generated_at=clock.now).by_class[
        SignalClass.CLASS_3_THESIS
    ]
    assert unresolved.resolved == 0
    assert unresolved.hit_rate is None
    assert unresolved.is_negative is False
    assert "no resolved outcomes yet" in unresolved.summary()


def test_decisions_outside_the_window_are_excluded(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    later = clock.now + timedelta(days=200)
    report = build_attribution(log.trails(), generated_at=later, window_days=60)
    assert report.by_class == {}
    assert report.flagged_classes == ()


def test_rejections_are_counted_alongside_pnl(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    gate = gate_for(limits)
    proposal = SizingEngine(limits).propose_equity(make_report(), Decimal("90000"))

    log.record_decision(make_signal(), make_report(), proposal, gate.submit(equity_order()))
    log.record_decision(
        make_signal(signal_id="sig-2"),
        make_report(),
        proposal,
        gate.submit(equity_order(qty=1000)),
    )

    attribution = build_attribution(log.trails(), generated_at=clock.now).by_class[
        SignalClass.CLASS_1_REALTIME
    ]
    assert attribution.decisions == 2
    assert attribution.approved == 1
    assert attribution.rejected == 1
    assert attribution.rejection_rate == 0.5


@pytest.mark.parametrize("bad_window", [30, 59, 91, 365])
def test_the_window_must_stay_within_the_spec_range(bad_window):
    with pytest.raises(ValueError, match="60-90 days"):
        build_attribution([], generated_at=NOW, window_days=bad_window)



# ================================================================================
# Stage rejections — a signal that never reached the gate still leaves a trail
# ================================================================================


def test_a_research_rejection_is_a_record_with_the_whole_signal_in_it(log):
    """Nothing else about this signal exists, so this record has to be the trail."""
    signal = make_signal(content="Loading $NUE here.", raw_content="Loading $NUE here.")
    record = log.record_stage_rejection(
        "dec-99",
        RejectedStage.RESEARCH,
        "no_structured_output",
        "model did not return a structured report",
        signal,
    )

    assert record.stage is RejectedStage.RESEARCH
    assert record.signal.raw_content == "Loading $NUE here."
    assert record.research is None and record.sizing is None
    assert log.rejections_for("dec-99") == [record]


def test_a_sizing_rejection_carries_the_report_that_was_declined(log, limits):
    """"Research liked it and sizing said no" is a different fact from "no report"."""
    report = make_report(direction="no_position", confidence=95)
    proposal = SizingEngine(limits).propose_equity(report, Decimal("90000"))
    record = log.record_stage_rejection(
        "dec-1",
        RejectedStage.SIZING,
        "no_position",
        proposal.rationale,
        make_signal(),
        report=report,
        proposal=proposal,
    )

    assert record.research.confidence == 95
    assert record.research.direction == "no_position"
    assert record.sizing.capital == Decimal("0")


def test_stage_rejections_survive_a_round_trip_through_the_file(log):
    log.record_stage_rejection(
        "dec-7", RejectedStage.INTERNAL_ERROR, "KeyError", "boom", make_signal()
    )
    replayed = log.stage_rejections()

    assert len(replayed) == 1
    assert replayed[0].decision_id == "dec-7"
    assert replayed[0].stage is RejectedStage.INTERNAL_ERROR


def test_an_execution_rejection_joins_the_trail_of_its_decision(log, limits):
    """Same decision_id, so the approval and the broker's refusal read as one story."""
    record, _ = full_decision(log, limits)
    log.record_stage_rejection(
        record.decision_id,
        RejectedStage.EXECUTION,
        "BrokerRejected",
        "insufficient buying power at the broker",
        make_signal(),
    )

    trail = log.trail(record.decision_id)
    assert trail.decision.was_approved
    assert len(trail.stage_rejections) == 1
    assert trail.never_executed


def test_an_approved_order_the_broker_never_executed_is_complete(log, limits):
    """Path-dependent completeness: nothing is coming, so nothing is outstanding."""
    record, _ = full_decision(log, limits)
    log.record_stage_rejection(
        record.decision_id,
        RejectedStage.EXECUTION,
        "canceled",
        "order terminated canceled without filling; reservation released",
        make_signal(),
    )

    assert log.trail(record.decision_id).is_complete


def test_an_approved_order_that_filled_still_needs_an_outcome(log, limits):
    """The unchanged case, restated so the new branch cannot swallow it."""
    record, _ = full_decision(log, limits)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))

    trail = log.trail(record.decision_id)
    assert not trail.never_executed
    assert not trail.is_complete


def test_the_log_still_refuses_a_fill_against_a_rejected_decision(log, limits):
    """Unchanged by the new record kind: a fill there means an order bypassed the gate."""
    gate = gate_for(limits, cash=Decimal("100"))
    record, _ = full_decision(log, limits, gate=gate)
    assert not record.gate.approved

    with pytest.raises(AuditLogError):
        log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))


# ================================================================================
# Research budget replay
# ================================================================================


def test_research_passes_are_counted_per_day_from_the_log(log, limits):
    """One decision_id per research pass, whichever stage the signal died at."""
    full_decision(log, limits)
    log.record_stage_rejection(
        "dec-a", RejectedStage.RESEARCH, "upstream_error", "timeout", make_signal()
    )

    assert log.research_passes_on(NOW.date()) == 2
    assert log.research_passes_on((NOW + timedelta(days=1)).date()) == 0


def test_a_later_record_against_an_earlier_decision_is_not_a_second_pass(
    log, limits, clock
):
    """A fill tomorrow does not re-buy the research that was paid for today."""
    record, _ = full_decision(log, limits)

    clock.advance(days=1)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140.00"))

    assert log.research_passes_on(NOW.date()) == 1
    assert log.research_passes_on((NOW + timedelta(days=1)).date()) == 0


# ================================================================================
# Feed costs — a signal class must out-earn its own feed
# ================================================================================


def test_feed_costs_are_prorated_into_the_window(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    report = build_attribution(
        log.trails(),
        generated_at=clock.now,
        window_days=90,
        feed_costs={SignalClass.CLASS_2_MOMENTUM: Decimal("30")},
    )
    class_2 = report.by_class[SignalClass.CLASS_2_MOMENTUM]

    assert class_2.feed_cost == Decimal("90.00")  # 30/mo x 90d / 30
    assert class_2.net_pnl == class_2.realised_pnl - Decimal("90.00")
    # Free classes are untouched: gross == net.
    class_1 = report.by_class[SignalClass.CLASS_1_REALTIME]
    assert class_1.feed_cost == Decimal("0")
    assert class_1.net_pnl == class_1.realised_pnl


def test_gross_positive_but_net_negative_fires_the_flag(tmp_path, limits):
    """The verdict the field exists for: the class made money, the feed ate it."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    gate = gate_for(limits)

    for n, pnl in enumerate(["50", "20"], start=1):
        clock.advance(days=1)
        signal = make_signal(
            signal_id=f"sig-{n}", signal_class=SignalClass.CLASS_2_MOMENTUM
        )
        record, _ = full_decision(log, limits, gate=gate, signal=signal)
        log.record_fill(record.decision_id, f"brk-{n}", Decimal("10"), Decimal("140"))
        log.record_outcome(record.decision_id, Decimal(pnl))

    report = build_attribution(
        log.trails(),
        generated_at=clock.now,
        window_days=90,
        feed_costs={SignalClass.CLASS_2_MOMENTUM: Decimal("30")},
    )
    class_2 = report.by_class[SignalClass.CLASS_2_MOMENTUM]

    assert class_2.realised_pnl == Decimal("70")  # gross-positive
    assert class_2.feed_cost == Decimal("90.00")
    assert class_2.net_pnl == Decimal("-20.00")  # net-negative
    assert class_2.is_negative
    assert report.flagged_classes == (SignalClass.CLASS_2_MOMENTUM,)

    rendered = report.render()
    assert "FLAGGED FOR HUMAN REVIEW (net-negative over the window)" in rendered
    assert "+70.00 gross" in rendered
    assert "-20.00 net" in rendered


def test_a_class_that_out_earns_its_feed_is_not_flagged(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    record, _ = full_decision(log, limits, signal=make_signal(
        signal_class=SignalClass.CLASS_2_MOMENTUM
    ))
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140"))
    log.record_outcome(record.decision_id, Decimal("500"))

    report = build_attribution(
        log.trails(),
        generated_at=clock.now,
        window_days=90,
        feed_costs={SignalClass.CLASS_2_MOMENTUM: Decimal("30")},
    )
    class_2 = report.by_class[SignalClass.CLASS_2_MOMENTUM]
    assert class_2.net_pnl == Decimal("410.00")
    assert not class_2.is_negative
    assert report.flagged_classes == ()


def test_a_paid_but_silent_class_still_shows_its_bleed(tmp_path, limits):
    """No decisions in the window, but the bill ran anyway: the class appears in the
    report with its cost visible — and is not FLAGGED, because nothing has resolved
    to judge it by. Visible bleed, withheld verdict."""
    report = build_attribution(
        [],
        generated_at=NOW,
        window_days=60,
        feed_costs={SignalClass.CLASS_2_MOMENTUM: Decimal("30")},
    )
    class_2 = report.by_class[SignalClass.CLASS_2_MOMENTUM]

    assert class_2.decisions == 0
    assert class_2.feed_cost == Decimal("60.00")  # 30/mo x 60d / 30
    assert class_2.net_pnl == Decimal("-60.00")
    assert not class_2.is_negative  # resolved-gated, unchanged
    assert report.flagged_classes == ()
    assert report.total_feed_cost == Decimal("60.00")
    assert "60.00 feed cost" in report.render()


def test_without_feed_costs_the_report_is_unchanged(tmp_path, limits):
    """Backwards compatibility: no costs given means zero costs, net == gross."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    seed_history(log, limits, clock)

    report = build_attribution(log.trails(), generated_at=clock.now)
    for attribution in report.by_class.values():
        assert attribution.feed_cost == Decimal("0")
        assert attribution.net_pnl == attribution.realised_pnl
    assert report.total_net_pnl == report.total_pnl


def test_the_config_supplies_the_costs_the_report_consumes():
    """The wiring: signals.yaml owns the numbers, keyed by class."""
    from signals import SignalsConfig

    costs = SignalsConfig.load().monthly_feed_costs()
    assert costs["class_2"] == Decimal("30")
    assert costs["class_1"] == Decimal("10")  # X pay-per-use budget figure
    assert costs["class_3"] == Decimal("0")
    # Class keys are SignalClass values, so the mapping into the report is direct.
    assert {SignalClass(key) for key in costs} == set(SignalClass)


# ================================================================================
# Research costs — a class must out-earn its model bill too (cost pass, 2026-08-19)
# ================================================================================

from audit.records import ReviewOutcome  # noqa: E402 - section-local import
from research.reports import ResearchUsage  # noqa: E402


def usage_of(cost: str, tokens_in: int = 10_000, tokens_out: int = 500):
    return ResearchUsage(
        input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=Decimal(cost)
    )


def test_research_costs_aggregate_by_class(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    gate = gate_for(limits)

    # Class 1 entry pass, accepted.
    record, _ = full_decision(log, limits, gate=gate)
    # (full_decision doesn't thread usage; stamp a review under it instead.)
    log.record_fill(record.decision_id, "brk-1", Decimal("10"), Decimal("140"))
    log.record_thesis_review(
        record.decision_id, ReviewOutcome.HOLD, assessment="fine",
        invalidation_triggered=False, usage=usage_of("0.10"),
    )
    # Class 2 research-stage rejection: the pass was billed even though it failed.
    log.record_stage_rejection(
        "dec-c2", RejectedStage.RESEARCH, "no_structured_output", "prose",
        make_signal(signal_id="sig-c2", signal_class=SignalClass.CLASS_2_MOMENTUM),
        usage=usage_of("0.25"),
    )

    costs = log.research_costs_by_class(window_start=clock.now - timedelta(days=1))
    assert costs[SignalClass.CLASS_1_REALTIME] == Decimal("0.10")
    assert costs[SignalClass.CLASS_2_MOMENTUM] == Decimal("0.25")


def test_an_entry_pass_is_billed_once_even_when_two_records_share_its_id(
    tmp_path, limits
):
    """A decision record and a later execution rejection share a decision_id and
    the same usage estimate — one research call, one bill."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    gate = gate_for(limits)
    signal = make_signal()

    record, _ = full_decision(log, limits, gate=gate, signal=signal)
    # Simulate the broker refusing after the decision was recorded. The pipeline
    # threads the same usage into both records; here we stamp the rejection and
    # prove the aggregator does not double-bill the shared id.
    log.record_stage_rejection(
        record.decision_id, RejectedStage.EXECUTION, "BrokerError", "refused",
        signal, usage=usage_of("0.50"),
    )

    costs = log.research_costs_by_class(window_start=clock.now - timedelta(days=1))
    assert costs[SignalClass.CLASS_1_REALTIME] == Decimal("0.50")


def test_costs_outside_the_window_do_not_bill(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    log.record_stage_rejection(
        "dec-old", RejectedStage.RESEARCH, "no_structured_output", "prose",
        make_signal(signal_id="sig-old"), usage=usage_of("0.75"),
    )
    costs = log.research_costs_by_class(window_start=clock.now + timedelta(days=1))
    assert costs == {}


def test_records_without_an_estimate_bill_nothing(tmp_path, limits):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    full_decision(log, limits)  # no usage threaded
    costs = log.research_costs_by_class(window_start=clock.now - timedelta(days=1))
    assert costs == {}


def test_gross_positive_but_net_negative_after_research_costs_fires_the_flag(
    tmp_path, limits
):
    """The verdict this line exists for: the class made money, the FREE feed cost
    nothing, and the model bill still ate the gains. The flag fires on net of ALL
    costs."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    gate = gate_for(limits)

    for n, pnl in enumerate(["30", "25"], start=1):
        clock.advance(days=1)
        signal = make_signal(
            signal_id=f"sig-{n}", signal_class=SignalClass.CLASS_3_THESIS
        )
        record, _ = full_decision(log, limits, gate=gate, signal=signal)
        log.record_fill(record.decision_id, f"brk-{n}", Decimal("10"), Decimal("140"))
        log.record_outcome(record.decision_id, Decimal(pnl))

    report = build_attribution(
        log.trails(),
        generated_at=clock.now,
        window_days=90,
        research_costs={SignalClass.CLASS_3_THESIS: Decimal("60.00")},
    )
    class_3 = report.by_class[SignalClass.CLASS_3_THESIS]

    assert class_3.realised_pnl == Decimal("55")  # gross-positive
    assert class_3.feed_cost == Decimal("0")  # the feed is free
    assert class_3.research_cost == Decimal("60.00")
    assert class_3.net_pnl == Decimal("-5.00")  # net-negative on research alone
    assert class_3.is_negative
    assert report.flagged_classes == (SignalClass.CLASS_3_THESIS,)

    rendered = report.render()
    assert "research cost" in rendered
    assert "60.00" in rendered
    assert "FLAGGED FOR HUMAN REVIEW" in rendered


def test_a_class_with_only_research_spend_still_appears_in_the_report(
    tmp_path, limits
):
    """Every pass rejected pre-gate leaves no trail, but the bill is real."""
    report = build_attribution(
        [],
        generated_at=NOW,
        window_days=90,
        research_costs={SignalClass.CLASS_2_MOMENTUM: Decimal("12.50")},
    )
    class_2 = report.by_class[SignalClass.CLASS_2_MOMENTUM]
    assert class_2.research_cost == Decimal("12.50")
    assert class_2.decisions == 0
    # Billed, but not judged: no resolved outcomes means no flag yet.
    assert not class_2.is_negative
    assert report.total_research_cost == Decimal("12.50")


# ================================================================================
# Benchmark-relative attribution — a bull market must not flatter a signal class
# ================================================================================


def test_gross_positive_but_spy_underperforming_shows_negative_excess(
    tmp_path, limits
):
    """The fixture the feature exists for: +55 on 2,800 deployed is +1.96%%, and
    against a +10%% SPY window that is negative alpha, stated as such."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    gate = gate_for(limits)

    for n, pnl in enumerate(["30", "25"], start=1):
        clock.advance(days=1)
        signal = make_signal(signal_id=f"sig-{n}")
        record, _ = full_decision(log, limits, gate=gate, signal=signal)
        log.record_fill(record.decision_id, f"brk-{n}", Decimal("10"), Decimal("140"))
        log.record_outcome(record.decision_id, Decimal(pnl))

    report = build_attribution(
        log.trails(),
        generated_at=clock.now,
        window_days=90,
        benchmark_return_pct=Decimal("10.00"),
    )
    class_1 = report.by_class[SignalClass.CLASS_1_REALTIME]

    assert class_1.realised_pnl == Decimal("55")  # gross-positive
    assert class_1.deployed == Decimal("2800")
    assert class_1.return_pct == Decimal("1.96")
    assert class_1.excess_return_pct == Decimal("-8.04")  # negative alpha
    assert report.total_excess_return_pct == Decimal("-8.04")

    rendered = report.render()
    assert "SPY +10.00% over the window" in rendered
    assert "-8.04% vs SPY" in rendered
    # Beating the market is not the flag's business: net P&L still decides that.
    assert not class_1.is_negative


def test_without_a_benchmark_the_report_says_so_instead_of_guessing(
    tmp_path, limits
):
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    full_decision(log, limits)

    report = build_attribution(log.trails(), generated_at=clock.now, window_days=90)
    assert report.benchmark_return_pct is None
    assert report.total_excess_return_pct is None
    assert "SPY return unavailable" in report.render()


def test_excess_return_needs_resolved_capital(tmp_path, limits):
    """A class with no resolved fills has no return to compare — None, not 0%%."""
    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    full_decision(log, limits)  # approved, never filled or resolved

    report = build_attribution(
        log.trails(),
        generated_at=clock.now,
        window_days=90,
        benchmark_return_pct=Decimal("10.00"),
    )
    class_1 = report.by_class[SignalClass.CLASS_1_REALTIME]
    assert class_1.return_pct is None
    assert class_1.excess_return_pct is None


def test_attribution_with_zero_deployment_renders_without_a_return():
    """A class (or an inactive sleeve) with nothing deployed must render as
    unresolved — never as a 0% return, a NaN, or a divide-by-zero."""
    from audit.attribution import ClassAttribution

    empty = ClassAttribution(
        signal_class=SignalClass.CLASS_1_REALTIME,
        decisions=0,
        approved=0,
        rejected=0,
        resolved=0,
        wins=0,
        realised_pnl=Decimal("0"),
        manipulation_flags=0,
        benchmark_return_pct=Decimal("2.10"),  # a benchmark alone must not force a %
    )
    assert empty.return_pct is None
    assert empty.excess_return_pct is None
    assert "no resolved outcomes yet" in empty.summary()
    assert "vs SPY" not in empty.summary()
