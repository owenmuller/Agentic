"""Cost instrumentation and model tiering (cost-efficiency pass, 2026-08-19).

The claims: each class resolves to the model tier research.yaml names for it, and
Class 1 stays on the flagship; the real client sends the tier's model/effort and
sums token usage across every API call in a pass; the pipeline stamps the usage
estimate onto the audit record whether the pass was accepted or rejected; and the
exit-review pass carries its own usage the same way.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from execution.environment import LIVE_CONFIRMATION_VARIABLE
from research.client import AnthropicResearchClient, LLMResult
from research.config import ResearchConfig
from research.exit_review import ExitReviewPass, PositionUnderReview
from research.reports import REPORT_TOOL_NAME
from risk_gate import RiskLimits
from signals import SignalsConfig
from test_orchestrator import (
    REPORT,
    FakeLLM,
    build,
    structured,
)


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def limits():
    return RiskLimits.load()


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    return ResearchConfig.load()


# ================================================================================
# Tier resolution — research.yaml is the contract
# ================================================================================


def test_class_1_stays_on_the_flagship(research_config):
    tier = research_config.tier_for("class_1")
    assert tier.model == research_config.model == "claude-opus-5"
    assert tier.effort == research_config.effort == "high"


@pytest.mark.parametrize("name", ["class_2", "class_3", "exit_review"])
def test_lagged_classes_and_reviews_run_on_the_cheaper_tier(research_config, name):
    tier = research_config.tier_for(name)
    assert tier.model == "claude-sonnet-4-6"
    assert tier.effort == "medium"


def test_an_unknown_tier_is_a_bug_not_a_fallback(research_config):
    with pytest.raises(ValueError, match="unknown research tier"):
        research_config.tier_for("class_4")


def test_an_unpriced_model_estimates_nothing(research_config):
    assert research_config.estimate_cost_usd("never-heard-of-it", 1000, 1000) is None


def test_cost_estimate_math(research_config):
    # sonnet at 3.00/15.00 per mtok: 200k in + 10k out = 0.60 + 0.15
    cost = research_config.estimate_cost_usd("claude-sonnet-4-6", 200_000, 10_000)
    assert cost == Decimal("0.750000")


# ================================================================================
# The real client: per-tier model selection and usage accounting
# ================================================================================


def _response(usage_in: int, usage_out: int, stop_reason: str = "tool_use"):
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use", name=REPORT_TOOL_NAME, input={"ok": True}
            )
        ],
        stop_reason=stop_reason,
        model="whatever-the-api-echoes",
        usage=SimpleNamespace(input_tokens=usage_in, output_tokens=usage_out),
    )


class RecordingAPI:
    """Stands in for the anthropic SDK client."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.create_calls: list[dict[str, Any]] = []
        self.messages = self

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._responses.pop(0)


def _config(web_search: bool) -> ResearchConfig:
    return ResearchConfig.model_validate(
        {
            "version": 1,
            "model": "claude-opus-5",
            "max_tokens": 8000,
            "effort": "high",
            "web_search": {"enabled": web_search, "max_uses": 2},
            "max_search_continuations": 1,
            "tiers": {"class_2": {"model": "claude-sonnet-4-6", "effort": "medium"}},
            "pricing": {
                "claude-sonnet-4-6": {
                    "input_per_mtok": "3.00",
                    "output_per_mtok": "15.00",
                }
            },
        }
    )


def test_the_client_sends_the_tiers_model_and_effort():
    api = RecordingAPI(_response(1000, 200))
    client = AnthropicResearchClient(_config(web_search=False), client=api)

    result = client.research(system="s", user="u", tool={"name": "t"}, tier="class_2")

    call = api.create_calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["output_config"] == {"effort": "medium"}
    assert result.input_tokens == 1000
    assert result.output_tokens == 200
    assert result.est_cost_usd == Decimal("0.006000")


def test_the_default_tier_is_class_1_on_the_top_level_model():
    api = RecordingAPI(_response(10, 10))
    client = AnthropicResearchClient(_config(web_search=False), client=api)

    result = client.research(system="s", user="u", tool={"name": "t"})

    assert api.create_calls[0]["model"] == "claude-opus-5"
    assert api.create_calls[0]["output_config"] == {"effort": "high"}
    # opus is deliberately unpriced in this fixture: tokens recorded, cost absent.
    assert result.input_tokens == 10
    assert result.est_cost_usd is None


def test_usage_is_summed_across_the_search_phase_and_the_report_phase():
    api = RecordingAPI(
        _response(5_000, 700, stop_reason="end_turn"),  # search phase
        _response(6_000, 300),  # forced report
    )
    client = AnthropicResearchClient(_config(web_search=True), client=api)

    result = client.research(system="s", user="u", tool={"name": "t"}, tier="class_2")

    assert len(api.create_calls) == 2
    assert result.input_tokens == 11_000
    assert result.output_tokens == 1_000
    # 11k in * 3.00/M + 1k out * 15.00/M
    assert result.est_cost_usd == Decimal("0.048000")


# ================================================================================
# Through the pipeline: the estimate lands on the audit record
# ================================================================================


def usage_result(payload: dict) -> LLMResult:
    return LLMResult(
        structured=payload,
        text="",
        stop_reason="tool_use",
        input_tokens=42_000,
        output_tokens=2_000,
        est_cost_usd=Decimal("0.660000"),
    )


def test_an_accepted_pass_stamps_its_cost_on_the_decision_record(
    tmp_path, limits, signals_config, research_config
):
    started = build(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=FakeLLM(usage_result(REPORT)),
    )
    started.loop.tick()

    decision = started.audit.decisions()[0]
    # Two-stage (2026-08-25): screen + verification, both billed on the record.
    assert decision.est_input_tokens == 84_000
    assert decision.est_output_tokens == 4_000
    assert decision.est_cost_usd == Decimal("1.320000")
    assert decision.screen_est_cost_usd == Decimal("0.660000")
    assert decision.screen_research is not None


def test_a_rejected_pass_still_bills_its_tokens(
    tmp_path, limits, signals_config, research_config
):
    """A pass that came back malformed was still paid for; the rejection record
    carries the estimate so attribution charges the class either way."""
    started = build(
        tmp_path,
        limits,
        signals_config,
        research_config,
        llm=FakeLLM(usage_result({"not": "a report"})),
    )
    started.loop.tick()

    rejections = started.audit.stage_rejections()
    assert len(rejections) == 1
    assert rejections[0].est_input_tokens == 42_000
    assert rejections[0].est_cost_usd == Decimal("0.660000")


def test_the_entry_pass_names_the_signals_class_as_its_tier(
    tmp_path, limits, signals_config, research_config
):
    llm = FakeLLM(structured(REPORT))
    started = build(tmp_path, limits, signals_config, research_config, llm=llm)
    started.loop.tick()

    # Two-stage: the screen leads, the class tier verifies.
    assert [call["tier"] for call in llm.calls] == ["screen", "class_1"]


# ================================================================================
# Exit reviews: same accounting, their own tier
# ================================================================================


def _position() -> PositionUnderReview:
    from datetime import datetime, timezone

    return PositionUnderReview(
        symbol="NUE",
        entry_price=Decimal("140"),
        current_price=Decimal("150"),
        opened_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        days_held=3,
        time_horizon="weeks",
        confidence_at_entry=80,
        source_id="nolimitgains",
        thesis="steel demand",
        invalidation_condition="closes below 130",
        original_content="Buying $NUE here.",
    )


class TierRecordingLLM:
    def __init__(self, result: LLMResult) -> None:
        self._result = result
        self.tiers: list[str] = []

    def research(self, *, system, user, tool, tier=""):
        self.tiers.append(tier)
        return self._result

    def last_usage(self):  # pragma: no cover - never called; protocol only
        raise AssertionError


def test_the_review_pass_uses_the_exit_review_tier_and_carries_usage():
    llm = TierRecordingLLM(
        LLMResult(
            structured={
                "action": "hold",
                "assessment": "thesis intact, price above invalidation",
                "invalidation_triggered": False,
                "case_for_holding": "thesis intact, the catalyst is still ahead and price sits well above the stop",
                "case_for_selling": "little has moved and the slot could be redeployed to a fresher candidate",
                "verdict_reason": "holding wins: nothing has changed since entry",
            },
            text="",
            stop_reason="tool_use",
            input_tokens=9_000,
            output_tokens=400,
            est_cost_usd=Decimal("0.033000"),
        )
    )
    review = ExitReviewPass(llm)

    review.run(_position())

    assert llm.tiers == ["exit_review"]
    assert review.last_usage is not None
    assert review.last_usage.input_tokens == 9_000
    assert review.last_usage.cost_usd == Decimal("0.033000")


def test_a_review_that_never_reached_the_model_has_no_usage():
    class Exploding:
        def research(self, **kwargs):
            raise TimeoutError("down")

    review = ExitReviewPass(Exploding())
    review.run(_position())
    assert review.last_usage is None


# ================================================================================
# Daily cost visibility (2026-08-20): spend is a number you read, not a surprise
# ================================================================================


def test_cost_summation_counts_passes_once_and_null_costs_as_zero(tmp_path):
    from datetime import timedelta

    from audit import AuditLog, RejectedStage
    from research.reports import ResearchUsage
    from test_audit import FakeClock, NOW, _counter, full_decision, make_signal

    clock = FakeClock()
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())

    # An entry pass whose decision AND execution rejection share one estimate.
    signal = make_signal()
    record, _ = full_decision(log, RiskLimits.load(), signal=signal)
    usage = ResearchUsage(10_000, 500, Decimal("2.12"))
    log.record_stage_rejection(
        record.decision_id, RejectedStage.EXECUTION, "BrokerError", "refused",
        signal, usage=usage,
    )
    # Rewrite the decision's cost is not possible (append-only) — instead assert
    # the shared-id rule with the rejection carrying the estimate: bills once.
    # A review under the same decision bills separately.
    from audit.records import ReviewOutcome

    log.record_fill(record.decision_id, "brk", Decimal("10"), Decimal("140"))
    log.record_thesis_review(
        record.decision_id, ReviewOutcome.HOLD, assessment="fine",
        invalidation_triggered=False,
        usage=ResearchUsage(9_000, 400, Decimal("0.03")),
    )
    # A pre_filter rejection: decision_id exists, usage is None — contributes 0.
    log.record_stage_rejection(
        "dec-pref", RejectedStage.PRE_FILTER, "pre_filter", "no theme",
        make_signal(signal_id="sig-pref"),
    )

    day_start = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    total = log.research_cost_between(day_start)
    assert total == Decimal("2.15")  # 2.12 once + 0.03 review; pre_filter adds 0


def test_month_to_date_excludes_last_month(tmp_path):
    from datetime import timedelta

    from audit import AuditLog, RejectedStage
    from research.reports import ResearchUsage
    from test_audit import FakeClock, NOW, _counter, make_signal

    clock = FakeClock(NOW - timedelta(days=40))  # last month
    log = AuditLog(path=tmp_path / "a.jsonl", clock=clock, id_factory=_counter())
    log.record_stage_rejection(
        "dec-old", RejectedStage.RESEARCH, "no_structured_output", "prose",
        make_signal(signal_id="sig-old"),
        usage=ResearchUsage(10_000, 500, Decimal("5.00")),
    )
    clock.now = NOW  # this month
    log.record_stage_rejection(
        "dec-new", RejectedStage.RESEARCH, "no_structured_output", "prose",
        make_signal(signal_id="sig-new"),
        usage=ResearchUsage(10_000, 500, Decimal("1.25")),
    )

    month_start = NOW.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    assert log.research_cost_between(month_start) == Decimal("1.25")
    assert log.research_cost_between(
        month_start - timedelta(days=31), month_start
    ) == Decimal("5.00")


def test_the_cost_tripwire_warns_once_per_day_and_resets_at_midnight():
    from datetime import datetime, timedelta, timezone

    from orchestrator.ops import CostMeter

    class Clock:
        def __init__(self):
            self.now = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)

        def __call__(self):
            return self.now

    clock = Clock()
    warnings: list[str] = []
    meter = CostMeter(Decimal("10"), warn_sink=warnings.append, clock=clock)

    meter.add(Decimal("6.00"))
    meter.add(None)  # unpriced pass: zero, never a crash
    assert warnings == []
    meter.add(Decimal("5.00"))  # 11.00 crosses 10
    assert len(warnings) == 1
    assert "crossed" in warnings[0] and "$11.00" in warnings[0]
    meter.add(Decimal("4.00"))  # still the same day: no second warning
    assert len(warnings) == 1

    clock.now += timedelta(days=1)  # midnight passed: fresh day, fresh tripwire
    assert meter.today_spent == Decimal("0")
    meter.add(Decimal("12.00"))
    assert len(warnings) == 2


def test_the_meter_seed_survives_a_restart():
    from datetime import datetime, timezone

    from orchestrator.ops import CostMeter

    clock = lambda: datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)  # noqa: E731
    warnings: list[str] = []
    meter = CostMeter(
        Decimal("10"), warn_sink=warnings.append, clock=clock,
        initial_spent=Decimal("9.50"),
    )
    meter.add(Decimal("1.00"))  # 10.50 crosses on the first post-restart pass
    assert len(warnings) == 1


def test_health_output_carries_the_cost_line(tmp_path, limits, signals_config, research_config):
    from orchestrator.ops import RunLog, health_report

    started = build(
        tmp_path, limits, signals_config, research_config,
        llm=FakeLLM(usage_result(REPORT)),
    )
    started.loop.tick()
    started.loop.shutdown()

    report = health_report(
        started.preflight, started.exits.tracked, RunLog(tmp_path / "run.log")
    )
    assert "est. research cost: today $1.32" in report
    assert "yesterday $0.00" in report
    assert "month-to-date $1.32" in report


def test_attribution_renders_the_mtd_research_cost():
    from datetime import datetime, timezone

    from audit.attribution import build_attribution

    report = build_attribution(
        [],
        generated_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        window_days=90,
        mtd_research_cost=Decimal("12.34"),
    )
    assert "Research cost month-to-date: $12.34" in report.render()


# ================================================================================
# Two-stage research (cost architecture 2026-08-25)
# ================================================================================

NO_POSITION_SCREEN = {
    **REPORT,
    "direction": "no_position",
    "confidence": 20,
    "thesis": "Nothing tradeable here; commentary only.",
}

LOW_CONFIDENCE_SCREEN = {**REPORT, "confidence": 40}


def test_an_unactionable_screen_verdict_is_the_record_and_stage_two_never_runs(
    tmp_path, limits, signals_config, research_config
):
    """Rejections get cheap: one Sonnet call, done, that is the record."""
    for payload, code in (
        (NO_POSITION_SCREEN, "no_position"),
        (LOW_CONFIDENCE_SCREEN, "below_floor"),
    ):
        llm = FakeLLM(structured(payload))
        started = build(
            tmp_path / code, limits, signals_config, research_config, llm=llm
        )
        started.loop.tick()
        assert [call["tier"] for call in llm.calls] == ["screen"]
        rejection = started.audit.stage_rejections()[-1]
        assert rejection.code == code
        assert rejection.research.confidence == payload["confidence"]
        # Stage one WAS the record: no separate screen draft is stored.
        assert rejection.screen_research is None


def test_an_actionable_screen_graduates_and_the_verification_report_proceeds(
    tmp_path, limits, signals_config, research_config
):
    screen = {**REPORT, "confidence": 60, "thesis": "Screen draft thesis."}
    verified = {**REPORT, "confidence": 82, "thesis": "Verified thesis."}
    llm = FakeLLM(structured(screen), structured(verified))
    started = build(tmp_path, limits, signals_config, research_config, llm=llm)
    result = started.loop.tick().processed[0]
    assert result.traded

    assert [call["tier"] for call in llm.calls] == ["screen", "class_1"]
    # The verification prompt carries the draft, fenced as data.
    assert "FIRST-PASS DRAFT" in llm.calls[1]["user"]
    assert "Screen draft thesis." in llm.calls[1]["user"]

    decision = started.audit.trail(result.decision_id).decision
    assert decision.research.confidence == 82  # the VERIFIED report is the record
    assert decision.research.thesis == "Verified thesis."
    assert decision.screen_research is not None  # and the draft is preserved
    assert decision.screen_research.confidence == 60
    assert decision.sizing.confidence == 82  # sizing consumed the verifier


def test_an_opus_override_wins(tmp_path, limits, signals_config, research_config):
    """Screen says trade; the verifier says no_position. Nothing trades, and the
    record shows exactly that conversation."""
    screen = {**REPORT, "confidence": 71}
    override = {
        **REPORT,
        "direction": "no_position",
        "confidence": 15,
        "thesis": "Verification found the move already priced in.",
    }
    llm = FakeLLM(structured(screen), structured(override))
    started = build(tmp_path, limits, signals_config, research_config, llm=llm)
    result = started.loop.tick().processed[0]
    assert not result.traded

    rejection = started.audit.rejections_for(result.decision_id)[0]
    assert rejection.code == "no_position"
    assert rejection.research.direction == "no_position"  # the override is the record
    assert rejection.screen_research is not None
    assert rejection.screen_research.confidence == 71  # the outvoted draft, preserved


def test_no_trade_ever_sizes_on_the_screen_alone(
    tmp_path, limits, signals_config, research_config
):
    """A stage-two upstream failure is a rejection — never a fallback to the
    unverified screen report."""

    class GraduatesThenDies:
        def __init__(self):
            self.calls = []

        def research(self, *, system, user, tool, tier=""):
            self.calls.append(tier)
            if tier == "screen":
                return structured({**REPORT, "confidence": 90})
            raise ConnectionError("verification tier is down")

    llm = GraduatesThenDies()
    started = build(tmp_path, limits, signals_config, research_config, llm=llm)
    result = started.loop.tick().processed[0]
    assert not result.traded
    rejection = started.audit.rejections_for(result.decision_id)[0]
    assert rejection.code == "upstream_error"
    assert rejection.screen_research is not None  # the draft that died waiting


def test_per_source_tier_overrides_the_class_default():
    """A structured-callout source verifies on its declared tier, not class_1."""
    from research.research_pass import ResearchPass
    from research.reports import ResearchReport
    from test_audit import make_signal
    from test_orchestrator import FakeLLM as _FakeLLM

    llm = _FakeLLM(structured({**REPORT, "confidence": 70}), structured(REPORT))
    research = ResearchPass(
        llm,
        source_tiers={"unusual_whales": "class_2"},
        screen_graduation=55,
    )
    outcome = research.run(
        make_signal(source_id="unusual_whales", content="UW callout: $NUE calls")
    )
    assert isinstance(outcome, ResearchReport)
    assert [call["tier"] for call in llm.calls] == ["screen", "class_2"]
