"""The golden set: frozen past decisions replayed through the current research
path (human ruling 2026-09-02).

``config/golden/golden_set.jsonl`` holds 20 graded cases — real decisions mined
from the audit log (declines the human upheld, the one taken entry) plus
synthetic adversarial fixtures (a prompt injection, a fabricated mirror relay)
and the two live-round-trip shapes (Form 4 cluster, 13D amendment). Each case
carries the verdict bands a correct pass should land in.

``python -m orchestrator golden`` replays every case through the PRODUCTION
ResearchPass — current prompt, tiers, models, two-stage screen/verify as
configured — and reports drift. CLAUDE.md requires this replay, reviewed by a
human, before ANY prompt, tier, or model change ships.

What it grades and what it cannot: direction against the allowed set,
confidence against the band, and the manipulation flag where the case demands
one. It does not grade prose. Context builders (market context, credibility,
convergence) are deliberately absent — the replay isolates prompt x schema x
model, the three things a change under test actually changes. Costs real API
dollars by design (~$0.05-0.15/case); it writes NO audit records and places no
orders. The set skews toward declines because the log does — new graded cases
join by ruling as real entries accumulate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from research.reports import ResearchReport, is_manipulation_flagged
from signals import SignalClass, SignalsConfig
from signals.records import Classification, Priority, Signal, signal_id_for

GOLDEN_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "golden" / "golden_set.jsonl"
)


@dataclass(frozen=True, slots=True)
class GoldenCase:
    name: str
    origin: str
    source_id: str
    signal_class: SignalClass
    content: str
    external_id: str
    classification: Optional[str]
    metadata: dict[str, str]
    directions: tuple[str, ...]
    confidence_band: tuple[int, int]
    must_flag_manipulation: bool
    note: str
    recorded_verdict: str
    #: The ORIGINAL observation moment, frozen into the fixture (baseline run
    #: 2026-09-02 showed why): the prompt's lag arithmetic must match the frame
    #: the grading assumed, or every time-sensitive case drifts as the calendar
    #: moves. Web search still sees the present — a stated limitation, so the
    #: set leans on time-robust declines and boundary-watch cases.
    observed_at: Optional[datetime] = None
    #: A band applied ONLY when the verdict is a tradeable direction. Lets a
    #: case say "a decline at any confidence grades; a long grades only weak"
    #: without conflating decline confidence (calibration) with position size.
    traded_confidence_band: Optional[tuple[int, int]] = None

    def signal(self, now: datetime) -> Signal:
        classification = None
        if self.classification:
            classification = Classification(self.classification)
        return Signal(
            signal_id=signal_id_for(self.source_id, self.external_id, self.content),
            source_id=self.source_id,
            signal_class=self.signal_class,
            observed_at=self.observed_at or now,
            content=self.content,
            raw_content=self.content,
            priority=Priority.for_class(self.signal_class),
            external_id=self.external_id,
            classification=classification,
            metadata=dict(self.metadata),
        )


def load_cases(path: Optional[Path] = None) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    with open(path or GOLDEN_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            expect = raw["expect"]
            cases.append(
                GoldenCase(
                    name=raw["name"],
                    origin=raw.get("origin", ""),
                    source_id=raw["source_id"],
                    signal_class=SignalClass(raw["signal_class"]),
                    content=raw["content"],
                    external_id=raw["external_id"],
                    classification=raw.get("classification"),
                    metadata=dict(raw.get("metadata") or {}),
                    directions=tuple(expect["directions"]),
                    confidence_band=(
                        int(expect["confidence"][0]),
                        int(expect["confidence"][1]),
                    ),
                    must_flag_manipulation=bool(
                        expect.get("must_flag_manipulation", False)
                    ),
                    note=expect.get("note", ""),
                    recorded_verdict=raw.get("recorded_verdict", ""),
                    observed_at=(
                        datetime.fromisoformat(
                            raw["observed_at"].replace("Z", "+00:00")
                        )
                        if raw.get("observed_at")
                        else None
                    ),
                    traded_confidence_band=(
                        (
                            int(expect["traded_confidence"][0]),
                            int(expect["traded_confidence"][1]),
                        )
                        if expect.get("traded_confidence")
                        else None
                    ),
                )
            )
    return cases


@dataclass(frozen=True, slots=True)
class GoldenResult:
    case: GoldenCase
    passed: bool
    verdict: str
    problems: tuple[str, ...]
    cost: Optional[Decimal]


def grade(case: GoldenCase, outcome, usage) -> GoldenResult:
    cost = usage.cost_usd if usage else None
    if not isinstance(outcome, ResearchReport):
        return GoldenResult(
            case,
            passed=False,
            verdict=f"REJECTION {getattr(outcome, 'code', '?')}",
            problems=(f"no report: {getattr(outcome, 'message', outcome)}",),
            cost=cost,
        )
    problems: list[str] = []
    direction = str(outcome.direction)
    if direction not in case.directions:
        problems.append(
            f"direction {direction} not in graded set {list(case.directions)}"
        )
    low, high = case.confidence_band
    if not low <= outcome.confidence <= high:
        problems.append(f"confidence {outcome.confidence} outside [{low}, {high}]")
    if case.traded_confidence_band is not None and direction != "no_position":
        traded_low, traded_high = case.traded_confidence_band
        if not traded_low <= outcome.confidence <= traded_high:
            problems.append(
                f"traded verdict confidence {outcome.confidence} outside "
                f"[{traded_low}, {traded_high}]"
            )
    if case.must_flag_manipulation and not is_manipulation_flagged(
        outcome.manipulation_assessment
    ):
        problems.append("manipulation NOT flagged where the case demands it")
    verdict = f"{direction}/{outcome.confidence}"
    if outcome.target_price is not None:
        verdict += f" target={outcome.target_price}"
    return GoldenResult(
        case,
        passed=not problems,
        verdict=verdict,
        problems=tuple(problems),
        cost=cost,
    )


def run_golden(
    research_pass,
    cases: list[GoldenCase],
    now: Optional[datetime] = None,
    echo=print,
) -> list[GoldenResult]:
    """Replay each case through the given (production) pass, grading as we go."""
    moment = now or datetime.now(timezone.utc)
    results: list[GoldenResult] = []
    for case in cases:
        outcome = research_pass.run(case.signal(moment))
        result = grade(case, outcome, research_pass.last_usage)
        results.append(result)
        status = "PASS " if result.passed else "DRIFT"
        cost = f" ${result.cost}" if result.cost is not None else ""
        echo(f"{status} {case.name}: {result.verdict}{cost}")
        for problem in result.problems:
            echo(f"      {problem}")
    return results


def render_summary(results: list[GoldenResult]) -> str:
    drifted = [r for r in results if not r.passed]
    total_cost = sum((r.cost for r in results if r.cost is not None), Decimal("0"))
    lines = [
        "",
        f"Golden set: {len(results) - len(drifted)}/{len(results)} passed, "
        f"~${total_cost:.2f} spent",
    ]
    if drifted:
        lines.append("DRIFT — a human reviews each before any change ships:")
        for result in drifted:
            lines.append(
                f"  {result.case.name}: got {result.verdict}; expected "
                f"{list(result.case.directions)} in "
                f"{list(result.case.confidence_band)}"
                + (
                    " + manipulation flag"
                    if result.case.must_flag_manipulation
                    else ""
                )
                + f" — {result.case.note}"
            )
    else:
        lines.append("No drift: every case graded inside its bands.")
    lines.append(
        "These numbers argue; humans rule. Drift is reviewable evidence, not an "
        "automatic block — but shipping a prompt/tier/model change without "
        "reviewing it violates CLAUDE.md § LLM Request-Path Changes."
    )
    return "\n".join(lines)


def build_source_tiers(signals_config: SignalsConfig) -> dict[str, str]:
    return {
        source.id: source.research_tier
        for klass in signals_config.classes.values()
        for source in klass.sources
        if source.research_tier
    }
