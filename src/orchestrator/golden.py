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
one. It does not grade prose. Since 2026-09-17 (human approval) a tradeable
verdict landing in the boundary-confirmation band buys the PRODUCTION second
pass through the same function the pipeline calls, and the case is graded on
what would SIZE — the lower-confidence report when the second pass replicates,
no position when it does not. Both passes stay on the line, and the summary
tallies upheld against reversed: the concurrence rate is the single most useful
input to the 2026-10-15 floor review. Context builders (market context, credibility,
convergence) are deliberately absent — the replay isolates prompt x schema x
model, the three things a change under test actually changes. Costs real API
dollars by design (~$0.05-0.15/case); it writes NO audit records and places no
orders. The set skews toward declines because the log does — new graded cases
join by ruling as real entries accumulate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from orchestrator.boundary import BoundaryConfirmation, confirm_boundary
from research.exit_review import ExitReview, PositionUnderReview
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
    #: "entry" (the research pass) or "review" (the exit review, ruling
    #: 2026-09-02): review cases replay a frozen PositionUnderReview and are
    #: graded on the REASONING STRUCTURE — both cases argued, a verdict reason
    #: naming the winner, would_open_today answered — not only the outcome.
    kind: str = "entry"
    position: Optional[dict[str, Any]] = None
    #: Review cases: the verdicts a correct review may reach.
    actions: tuple[str, ...] = ()
    #: Review cases: the structural bar each case must clear, in characters.
    min_case_chars: int = 120
    expect_would_open: Optional[bool] = None
    #: Review cases: the validity labels a correct review may report (2026-09-15).
    #: Empty = ungraded. The INTC day-9 case grades on this alone: "displaced" is
    #: a mislabel when the move came from exactly what the thesis predicted.
    expect_validity: tuple[str, ...] = ()
    #: Add cases (ruling 2026-09-16): the frozen held position the signal lands
    #: on, and the add verdicts a correct decision may reach. Graded on
    #: STRUCTURE — a verdict stated, an add naming exactly the held symbol with
    #: a fraction, a hold with direction no_position — plus the verdict set.
    add_position: Optional[dict[str, Any]] = None
    add_verdicts: tuple[str, ...] = ()
    #: Entry cases (ruling 2026-09-16, Class 1 staleness): the report must carry
    #: a priced_in_analysis that MEASURES — non-blank and containing a number.
    #: Grades whether a stale relay's analysis engaged the move since the post.
    expect_priced_in: bool = False

    def add_context(self):
        """The frozen held position an add case replays."""
        from research.add_decision import HeldPositionContext, LotContext

        raw = dict(self.add_position or {})

        def dec(key):
            return Decimal(str(raw[key])) if raw.get(key) is not None else None

        def when(value):
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        return HeldPositionContext(
            symbol=raw["symbol"],
            position_decision_id=raw["position_decision_id"],
            instrument_kind=raw.get("instrument_kind", "equity"),
            opened_at=when(raw["opened_at"]),
            days_held=int(raw["days_held"]),
            quantity=dec("quantity"),
            entry_price=dec("entry_price"),
            entry_cost=dec("entry_cost"),
            current_price=dec("current_price"),
            market_value=dec("market_value"),
            sleeve_nav=dec("sleeve_nav"),
            originating_family=raw.get("originating_family", ""),
            signal_family=raw.get("signal_family", ""),
            source_id=raw["source_id"],
            thesis=raw["thesis"],
            invalidation_condition=raw["invalidation_condition"],
            time_horizon=raw["time_horizon"],
            confidence_at_entry=int(raw["confidence_at_entry"]),
            resolution_date=(
                date.fromisoformat(raw["resolution_date"])
                if raw.get("resolution_date")
                else None
            ),
            leash_days=int(raw["leash_days"]),
            stop_price=dec("stop_price"),
            lots=tuple(
                LotContext(
                    decision_id=lot["decision_id"],
                    opened_at=when(lot["opened_at"]),
                    quantity=Decimal(str(lot["quantity"])),
                    entry_price=Decimal(str(lot["entry_price"])),
                    entry_cost=Decimal(str(lot["entry_cost"])),
                    source_id=lot["source_id"],
                    family=lot.get("family", ""),
                    confidence=int(lot["confidence"]),
                    thesis=lot["thesis"],
                )
                for lot in raw.get("lots", ())
            ),
            reviews=tuple(raw.get("reviews", ())),
            convergence=tuple(raw.get("convergence", ())),
        )

    def under_review(self) -> PositionUnderReview:
        """The frozen position a review case replays."""
        raw = dict(self.position or {})

        def dec(key):
            return Decimal(str(raw[key])) if raw.get(key) is not None else None

        return PositionUnderReview(
            symbol=raw["symbol"],
            entry_price=dec("entry_price"),
            current_price=dec("current_price"),
            opened_at=datetime.fromisoformat(raw["opened_at"].replace("Z", "+00:00")),
            days_held=int(raw["days_held"]),
            time_horizon=raw["time_horizon"],
            confidence_at_entry=int(raw["confidence_at_entry"]),
            source_id=raw["source_id"],
            thesis=raw["thesis"],
            invalidation_condition=raw["invalidation_condition"],
            original_content=raw["original_content"],
            expected_resolution_date=(
                date.fromisoformat(raw["expected_resolution_date"])
                if raw.get("expected_resolution_date")
                else None
            ),
            leash_days=raw.get("leash_days"),
            leash_ceiling_days=raw.get("leash_ceiling_days"),
            stop_price=dec("stop_price"),
            sizing_floor=raw.get("sizing_floor"),
            min_reward_risk=dec("min_reward_risk"),
            already_trimmed=bool(raw.get("already_trimmed", False)),
            spread_pct=dec("spread_pct"),
            opportunity_context=raw.get("opportunity_context"),
        )

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
            if raw.get("kind") == "add":
                cases.append(
                    GoldenCase(
                        name=raw["name"],
                        origin=raw.get("origin", ""),
                        source_id=raw["source_id"],
                        signal_class=SignalClass(raw["signal_class"]),
                        content=raw["content"],
                        external_id=raw.get("external_id", raw["name"]),
                        classification=raw.get("classification"),
                        metadata=dict(raw.get("metadata") or {}),
                        directions=(),
                        confidence_band=(0, 100),
                        must_flag_manipulation=False,
                        note=expect.get("note", ""),
                        recorded_verdict=raw.get("recorded_verdict", ""),
                        observed_at=(
                            datetime.fromisoformat(
                                raw["observed_at"].replace("Z", "+00:00")
                            )
                            if raw.get("observed_at")
                            else None
                        ),
                        kind="add",
                        add_position=raw["position"],
                        add_verdicts=tuple(expect.get("add_verdicts", ("add", "hold"))),
                        min_case_chars=int(expect.get("min_thesis_chars", 120)),
                    )
                )
                continue
            if raw.get("kind") == "review":
                position = raw["position"]
                cases.append(
                    GoldenCase(
                        name=raw["name"],
                        origin=raw.get("origin", ""),
                        source_id=position["source_id"],
                        signal_class=SignalClass(raw.get("signal_class", "class_2")),
                        content=position["original_content"],
                        external_id=raw.get("external_id", raw["name"]),
                        classification=None,
                        metadata={},
                        directions=(),
                        confidence_band=(0, 100),
                        must_flag_manipulation=False,
                        note=expect.get("note", ""),
                        recorded_verdict=raw.get("recorded_verdict", ""),
                        kind="review",
                        position=position,
                        actions=tuple(expect.get("actions", ("hold", "trim", "close"))),
                        min_case_chars=int(expect.get("min_case_chars", 120)),
                        expect_would_open=expect.get("would_open_today"),
                        expect_validity=tuple(expect.get("validity", ())),
                    )
                )
                continue
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
                    expect_priced_in=bool(expect.get("priced_in_required", False)),
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
    #: The boundary confirmation, when the first pass's verdict was tradeable
    #: and the harness had the band wired (2026-09-17). ``ran`` False = the
    #: verdict sat outside the band; None = not tradeable, or not wired.
    confirmation: Optional[BoundaryConfirmation] = None


@dataclass(frozen=True, slots=True)
class BoundaryBand:
    """The floor and width production confirms inside — from risk_limits.yaml
    ``sizing.no_trade_below`` and orchestrator.yaml ``boundary_confirmation``."""

    floor: int
    band_width: int


def _total_cost(usage, confirmation: Optional[BoundaryConfirmation]) -> Optional[Decimal]:
    first = usage.cost_usd if usage else None
    second = (
        confirmation.usage.cost_usd
        if confirmation is not None and confirmation.ran and confirmation.usage
        else None
    )
    if first is None:
        return second
    return first if second is None else first + second


def _confirmed_line(first: str, confirmation: Optional[BoundaryConfirmation]) -> str:
    if confirmation is None or not confirmation.ran:
        return first
    return f"{first} | boundary: {confirmation.describe()}"


def grade(
    case: GoldenCase,
    outcome,
    usage,
    confirmation: Optional[BoundaryConfirmation] = None,
) -> GoldenResult:
    """Grade an entry case on what would SIZE. Without a confirmation (or one
    that did not run) that is the report itself. When the production second
    pass ran: upheld -> the lower-confidence report is graded for direction and
    confidence; reversed -> the effective verdict is no_position and no
    confidence is graded (nothing would trade). The manipulation flag and the
    priced-in analysis are graded on the FIRST report — the observation the
    case is about — and both passes are rendered on the line."""
    cost = _total_cost(usage, confirmation)
    if not isinstance(outcome, ResearchReport):
        return GoldenResult(
            case,
            passed=False,
            verdict=f"REJECTION {getattr(outcome, 'code', '?')}",
            problems=(f"no report: {getattr(outcome, 'message', outcome)}",),
            cost=cost,
        )
    problems: list[str] = []
    first_verdict = f"{outcome.direction}/{outcome.confidence}"
    if outcome.target_price is not None:
        first_verdict += f" target={outcome.target_price}"
    sized: Optional[ResearchReport] = outcome
    if confirmation is not None and confirmation.ran:
        sized = confirmation.report  # None when reversed: nothing sizes
    direction = str(sized.direction) if sized is not None else "no_position"
    if direction not in case.directions:
        problems.append(
            f"direction {direction} not in graded set {list(case.directions)}"
        )
    if sized is not None:
        low, high = case.confidence_band
        if not low <= sized.confidence <= high:
            problems.append(f"confidence {sized.confidence} outside [{low}, {high}]")
        if case.traded_confidence_band is not None and direction != "no_position":
            traded_low, traded_high = case.traded_confidence_band
            if not traded_low <= sized.confidence <= traded_high:
                problems.append(
                    f"traded verdict confidence {sized.confidence} outside "
                    f"[{traded_low}, {traded_high}]"
                    + (" (the confirmed, lower pass)" if sized is not outcome else "")
                )
    if case.must_flag_manipulation and not is_manipulation_flagged(
        outcome.manipulation_assessment
    ):
        problems.append("manipulation NOT flagged where the case demands it")
    if case.expect_priced_in:
        analysis = (outcome.priced_in_analysis or "").strip()
        if not analysis:
            problems.append("priced_in_analysis absent on a stale Class 1 relay")
        elif not any(ch.isdigit() for ch in analysis):
            problems.append(
                "priced_in_analysis carries no number — suspicion, not measurement"
            )
    return GoldenResult(
        case,
        passed=not problems,
        verdict=_confirmed_line(first_verdict, confirmation),
        problems=tuple(problems),
        cost=cost,
        confirmation=confirmation,
    )


def grade_review(case: GoldenCase, outcome, usage) -> GoldenResult:
    """Grade a review case on its REASONING STRUCTURE (ruling 2026-09-02): the
    verdict must be one the case allows, both cases must be argued past the
    structural bar and differ from each other, the verdict reason must name a
    winner, and would_open_today must carry its arithmetic."""
    cost = usage.cost_usd if usage else None
    if not isinstance(outcome, ExitReview):
        return GoldenResult(
            case,
            passed=False,
            verdict=f"REJECTION {getattr(outcome, 'code', '?')}",
            problems=(f"no verdict: {getattr(outcome, 'message', outcome)}",),
            cost=cost,
        )
    problems: list[str] = []
    action = str(outcome.action)
    if case.actions and action not in case.actions:
        problems.append(f"verdict {action} not in graded set {list(case.actions)}")
    validity = str(outcome.validity)
    if case.expect_validity and validity not in case.expect_validity:
        problems.append(
            f"validity {validity} not in graded set {list(case.expect_validity)}"
        )
    holding = outcome.case_for_holding.strip()
    selling = outcome.case_for_selling.strip()
    if len(holding) < case.min_case_chars:
        problems.append(
            f"case_for_holding argued in {len(holding)} chars, below the "
            f"{case.min_case_chars} structural bar"
        )
    if len(selling) < case.min_case_chars:
        problems.append(
            f"case_for_selling argued in {len(selling)} chars, below the "
            f"{case.min_case_chars} structural bar"
        )
    if holding and holding == selling:
        problems.append("the two cases are identical text — one side was not argued")
    if not outcome.verdict_reason.strip():
        problems.append("verdict_reason blank")
    if not outcome.would_open_today_reason.strip():
        problems.append("would_open_today answered without its arithmetic")
    if (
        case.expect_would_open is not None
        and outcome.would_open_today is not case.expect_would_open
    ):
        problems.append(
            f"would_open_today={outcome.would_open_today}, expected "
            f"{case.expect_would_open}"
        )
    verdict = (
        f"{action} validity={validity} would_open={outcome.would_open_today} | "
        f"hold-case {len(holding)}c, "
        f"sell-case {len(selling)}c | {outcome.verdict_reason.strip()[:90]}"
    )
    return GoldenResult(
        case, passed=not problems, verdict=verdict, problems=tuple(problems), cost=cost
    )


def grade_add(
    case: GoldenCase,
    outcome,
    usage,
    confirmation: Optional[BoundaryConfirmation] = None,
) -> GoldenResult:
    """Grade an add case (ruling 2026-09-16) on STRUCTURE and the verdict set:
    add_verdict stated; an add names exactly the held symbol, direction long,
    with an add_fraction; a hold carries direction no_position; the thesis is
    argued past the structural bar. An in-band add that the production second
    pass did not replicate (2026-09-17) is graded as the hold production would
    record — the first pass's structure is still checked, and both are on the line."""
    cost = _total_cost(usage, confirmation)
    if not isinstance(outcome, ResearchReport):
        return GoldenResult(
            case,
            passed=False,
            verdict=f"REJECTION {getattr(outcome, 'code', '?')}",
            problems=(f"no report: {getattr(outcome, 'message', outcome)}",),
            cost=cost,
        )
    problems: list[str] = []
    symbol = str((case.add_position or {}).get("symbol", "")).upper()
    verdict = str(outcome.add_verdict) if outcome.add_verdict is not None else "unstated"
    effective = verdict
    if confirmation is not None and confirmation.reversed:
        effective = "hold"  # production: unconfirmed_boundary, no add, review triggered
    if outcome.add_verdict is None:
        problems.append("add_verdict null on an add decision")
    elif case.add_verdicts and effective not in case.add_verdicts:
        problems.append(f"add verdict {effective} not in graded set {list(case.add_verdicts)}")
    direction = str(outcome.direction)
    if verdict == "add":
        if outcome.add_fraction is None:
            problems.append("add without add_fraction")
        if direction != "long":
            problems.append(f"add with direction {direction}")
        if [t.upper() for t in outcome.tickers] != [symbol]:
            problems.append(f"add names {outcome.tickers}, not exactly [{symbol}]")
    elif verdict == "hold" and direction != "no_position":
        problems.append(f"hold with direction {direction} (expected no_position)")
    if len(outcome.thesis.strip()) < case.min_case_chars:
        problems.append(
            f"thesis argued in {len(outcome.thesis.strip())} chars, below the "
            f"{case.min_case_chars} structural bar"
        )
    fraction = f" fraction={outcome.add_fraction}" if outcome.add_fraction is not None else ""
    rendered = (
        f"{verdict}/{outcome.confidence}{fraction} {direction} "
        f"tickers={list(outcome.tickers)} | {outcome.thesis.strip()[:90]}"
    )
    return GoldenResult(
        case,
        passed=not problems,
        verdict=_confirmed_line(rendered, confirmation),
        problems=tuple(problems),
        cost=cost,
        confirmation=confirmation,
    )


def _confirm(research_pass, signal, outcome, band, add_context=None):
    """The production boundary confirmation on a TRADEABLE first-pass verdict
    (a long or puts; an add on an add case). None when the band is not wired
    or the verdict is not tradeable; ``ran`` False when it sat outside the band."""
    if band is None or not isinstance(outcome, ResearchReport):
        return None
    tradeable = outcome.is_add if add_context is not None else str(outcome.direction) != "no_position"
    if not tradeable:
        return None
    return confirm_boundary(
        research_pass,
        signal,
        outcome,
        floor=band.floor,
        band_width=band.band_width,
        add_context=add_context,
    )


def run_golden(
    research_pass,
    cases: list[GoldenCase],
    now: Optional[datetime] = None,
    echo=print,
    review_pass=None,
    band: Optional[BoundaryBand] = None,
) -> list[GoldenResult]:
    """Replay each case through the given (production) passes, grading as we go.
    Review cases need ``review_pass``; without one they grade as drift, loudly.
    With ``band`` wired, a tradeable verdict inside it buys the production
    second pass (2026-09-17); without it the replay is single-pass, as before."""
    moment = now or datetime.now(timezone.utc)
    results: list[GoldenResult] = []
    for case in cases:
        if case.kind == "review":
            if review_pass is None:
                result = GoldenResult(
                    case, False, "not run", ("no review pass wired",), None
                )
            else:
                outcome = review_pass.run(case.under_review())
                result = grade_review(case, outcome, review_pass.last_usage)
            results.append(result)
            status = "PASS " if result.passed else "DRIFT"
            cost = f" ${result.cost}" if result.cost is not None else ""
            echo(f"{status} {case.name}: {result.verdict}{cost}")
            for problem in result.problems:
                echo(f"      {problem}")
            continue
        if case.kind == "add":
            signal, context = case.signal(moment), case.add_context()
            outcome = research_pass.run(signal, add_context=context)
            usage = research_pass.last_usage
            confirmation = _confirm(research_pass, signal, outcome, band, context)
            result = grade_add(case, outcome, usage, confirmation)
        else:
            signal = case.signal(moment)
            outcome = research_pass.run(signal)
            usage = research_pass.last_usage
            confirmation = _confirm(research_pass, signal, outcome, band)
            result = grade(case, outcome, usage, confirmation)
        results.append(result)
        status = "PASS " if result.passed else "DRIFT"
        cost = f" ${result.cost}" if result.cost is not None else ""
        echo(f"{status} {case.name}: {result.verdict}{cost}")
        for problem in result.problems:
            echo(f"      {problem}")
    return results


def confirmation_tally(results: list[GoldenResult]) -> tuple[int, int, int]:
    """(in-band second passes run, upheld, reversed) across the results."""
    ran = [r.confirmation for r in results if r.confirmation is not None and r.confirmation.ran]
    upheld = sum(1 for c in ran if c.upheld)
    return len(ran), upheld, len(ran) - upheld


def render_summary(results: list[GoldenResult], band: Optional[BoundaryBand] = None) -> str:
    drifted = [r for r in results if not r.passed]
    total_cost = sum((r.cost for r in results if r.cost is not None), Decimal("0"))
    lines = [
        "",
        f"Golden set: {len(results) - len(drifted)}/{len(results)} passed, "
        f"~${total_cost:.2f} spent",
    ]
    if band is not None:
        ran, upheld, reversed_ = confirmation_tally(results)
        tradeable = sum(1 for r in results if r.confirmation is not None)
        lines.append(
            f"Boundary confirmation [{band.floor}, {band.floor + band.band_width}): "
            f"{tradeable} tradeable first-pass verdicts, {ran} in the band -> "
            f"{upheld} upheld, {reversed_} reversed"
            + (
                f" ({upheld / ran:.0%} concurrence)" if ran else ""
            )
        )
    if drifted:
        lines.append("DRIFT — a human reviews each before any change ships:")
        for result in drifted:
            if result.case.kind == "add":
                lines.append(
                    f"  {result.case.name}: got {result.verdict}; expected an add "
                    f"verdict in {list(result.case.add_verdicts)}, structurally "
                    f"complete — {result.case.note}"
                )
                continue
            if result.case.kind == "review":
                lines.append(
                    f"  {result.case.name}: got {result.verdict}; expected a "
                    f"verdict in {list(result.case.actions)} with both cases "
                    f"argued (>= {result.case.min_case_chars} chars each)"
                    + (
                        f", validity in {list(result.case.expect_validity)}"
                        if result.case.expect_validity
                        else ""
                    )
                    + f" — {result.case.note}"
                )
                continue
            lines.append(
                f"  {result.case.name}: got {result.verdict}; expected "
                f"{list(result.case.directions)} in "
                f"{list(result.case.confidence_band)}"
                + (
                    f", a traded verdict only in {list(result.case.traded_confidence_band)}"
                    if result.case.traded_confidence_band is not None
                    else ""
                )
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
