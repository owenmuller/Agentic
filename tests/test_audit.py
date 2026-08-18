"""Audit log tests.

The properties that matter: a decision record cannot be written with a stage missing,
nothing already written can be changed, an outcome credits the source that called it,
and attribution over fixture history flags a losing signal class.
"""

import ast
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

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
def log(tmp_path) -> AuditLog:
    return AuditLog(
        path=tmp_path / "audit.jsonl",
        clock=FakeClock(),
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
# The log observes, it never participates
# ================================================================================


def test_nothing_in_src_imports_audit():
    """An audit trail other modules can call into is part of the machinery.

    Only a top-level orchestrator may import it, and there isn't one yet — so today
    the answer is nothing at all.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []

    for path in sorted(src.rglob("*.py")):
        if path.parts[-2] == "audit":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] == "audit":
                    offenders.append(f"{path.name}:{node.lineno}: imports {name}")

    assert offenders == [], f"audit must observe, not participate: {offenders}"


def test_audit_may_read_types_from_everywhere():
    """The inverse direction is allowed and expected — it has to describe everything."""
    package = Path(__file__).resolve().parents[1] / "src" / "audit"
    seen: set[str] = set()

    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                seen.add(node.module.split(".")[0])

    assert {"signals", "research", "sizing", "risk_gate"} <= seen
