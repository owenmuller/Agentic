"""Append-only audit storage.

There is no update. There is no delete. There is no method on ``AuditLog`` that can
change a byte already written — the file is opened for append and nothing else, and
a correction is a new ``CorrectionRecord`` naming the record it supersedes. That is
the whole design: a log you can edit is a log that cannot be trusted about the past,
and the reason to keep one is precisely to be able to reconstruct what was believed at
the moment a decision was made.

Storage is JSONL under ``DATA_DIR`` (``./data``, gitignored). One record per line,
appended and flushed. A reader replays the file in order.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterator, Optional

from pydantic import TypeAdapter

from audit.records import (
    ConvergenceSnapshot,
    ExpressionSnapshot,
    AuditRecord,
    MechanicalSnapshot,
    AuditTrail,
    CorrectionRecord,
    DecisionRecord,
    ExitReason,
    ExitRecord,
    FilerEventRecord,
    FillRecord,
    GateSnapshot,
    OutcomeRecord,
    RejectedStage,
    long_term_boundary,
    ResearchSnapshot,
    ReviewOutcome,
    SignalSnapshot,
    SizingSnapshot,
    StageRejectionRecord,
    ThesisReviewRecord,
)
from research.reports import ResearchReport, ResearchUsage
from signals import Signal, SignalClass
from sizing.engine import SizedProposal

_ADAPTER: TypeAdapter = TypeAdapter(AuditRecord)


class AuditLogError(RuntimeError):
    """A write the log refuses to make."""


def default_data_dir() -> Path:
    configured = os.environ.get("DATA_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data"


class AuditLog:
    """Append-only JSONL record of every order the system considered."""

    def __init__(
        self,
        path: Optional[Path] = None,
        clock: Optional[Callable[[], datetime]] = None,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._path = path or (default_data_dir() / "audit.jsonl")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex[:16])

    @property
    def path(self) -> Path:
        return self._path

    # -- writing -------------------------------------------------------------------

    def record_decision(
        self,
        signal: Signal,
        report: ResearchReport,
        proposal: SizedProposal,
        gate_decision: object,
        decision_id: Optional[str] = None,
        usage: Optional[ResearchUsage] = None,
        expression: Optional["ExpressionSnapshot"] = None,
        screen_report: Optional[ResearchReport] = None,
        screen_usage: Optional[ResearchUsage] = None,
        convergence: Optional["ConvergenceSnapshot"] = None,
    ) -> DecisionRecord:
        """Write the complete decision-time record. Approved or rejected, both land."""
        record = DecisionRecord(
            decision_id=decision_id or self._id_factory(),
            recorded_at=self._clock(),
            signal=SignalSnapshot.of(signal),
            research=ResearchSnapshot.of(report),
            sizing=SizingSnapshot.of(proposal),
            gate=GateSnapshot.of(gate_decision),  # type: ignore[arg-type]
            expression=expression,
            convergence=convergence,
            screen_research=(
                ResearchSnapshot.of(screen_report)
                if screen_report is not None
                else None
            ),
            screen_est_cost_usd=(screen_usage.cost_usd if screen_usage else None),
            est_input_tokens=usage.input_tokens if usage else None,
            est_output_tokens=usage.output_tokens if usage else None,
            est_cost_usd=usage.cost_usd if usage else None,
        )
        self._append(record)
        return record

    def record_mechanical_entry(
        self,
        signal: Signal,
        gate_decision: object,
        capital: Decimal,
        sleeve_nav: Decimal,
        ruleset_version: str,
        max_positions: int,
        decision_id: Optional[str] = None,
    ) -> DecisionRecord:
        """A mechanical sleeve entry (ruling 2026-08-27): a DecisionRecord with
        no research snapshot — no LLM ran, and the record says so instead of
        carrying a fabricated verdict. Fills, exits, and outcomes then ride the
        ordinary per-decision machinery."""
        fraction = (capital / sleeve_nav) if sleeve_nav > 0 else Decimal("0")
        metadata = signal.metadata
        record = DecisionRecord(
            decision_id=decision_id or self._id_factory(),
            recorded_at=self._clock(),
            signal=SignalSnapshot.of(signal),
            research=None,
            sizing=SizingSnapshot(
                instrument="equity",
                sleeve="mechanical",
                confidence=0,  # not applicable: no research ran, by design
                sleeve_nav=sleeve_nav,
                fraction_of_sleeve_nav=fraction.quantize(Decimal("0.0001")),
                capital=capital,
                rationale=(
                    f"mechanical equal-weight slice (sleeve NAV / {max_positions}); "
                    f"no LLM in the path, ruleset {ruleset_version}"
                ),
                strategy="mechanical",
            ),
            mechanical=MechanicalSnapshot(
                ruleset_version=ruleset_version,
                filer=metadata.get("representative") or None,
                ticker=metadata.get("ticker") or None,
                amount_range=metadata.get("amount_range") or None,
                report_date=metadata.get("report_date") or None,
            ),
            gate=GateSnapshot.of(gate_decision),  # type: ignore[arg-type]
        )
        self._append(record)
        return record

    def record_sweep(
        self,
        *,
        side: str,
        detail: str,
        gate_decision: object,
        capital: Decimal,
        decision_id: Optional[str] = None,
    ) -> DecisionRecord:
        """A cash-management sweep order (human ruling 2026-09-02): a
        DecisionRecord with no research snapshot and ``strategy="cash_sweep"``.
        The signal snapshot is synthetic — the system's own deterministic buffer
        arithmetic, stated as content — because a sweep has no external signal,
        and CLAUDE.md still wants every order written down. Carries no
        external_id, so it can never seal anything, and every counter that
        matters (research passes, source caps, class attribution, the funnel,
        the registry) partitions it out by strategy."""
        now = self._clock()
        record_id = decision_id or self._id_factory()
        snapshot = SignalSnapshot(
            signal_id=f"sweep-{record_id}",
            source_id="cash_management",
            signal_class=SignalClass.CLASS_3_THESIS,
            observed_at=now,
            content=detail,
            raw_content=detail,
        )
        record = DecisionRecord(
            decision_id=record_id,
            recorded_at=now,
            signal=snapshot,
            research=None,
            sizing=SizingSnapshot(
                instrument="equity",
                sleeve="cash_management",
                confidence=0,  # not applicable: no research ran, by design
                sleeve_nav=capital,
                fraction_of_sleeve_nav=Decimal("1"),
                capital=capital,
                rationale=(
                    f"idle-cash yield sweep ({side}): deterministic buffer "
                    f"arithmetic, no LLM in the path — {detail}"
                ),
                strategy="cash_sweep",
            ),
            gate=GateSnapshot.of(gate_decision),  # type: ignore[arg-type]
        )
        self._append(record)
        return record

    def record_stage_rejection(
        self,
        decision_id: str,
        stage: RejectedStage,
        code: str,
        message: str,
        signal: Optional[Signal] = None,
        signal_snapshot: Optional[SignalSnapshot] = None,
        report: Optional[ResearchReport] = None,
        proposal: Optional[SizedProposal] = None,
        usage: Optional[ResearchUsage] = None,
        expression: Optional["ExpressionSnapshot"] = None,
        screen_report: Optional[ResearchReport] = None,
        screen_usage: Optional[ResearchUsage] = None,
    ) -> StageRejectionRecord:
        """Record a signal that stopped before the gate, or an order the broker refused.

        Takes whichever stages completed. A research-stage rejection has neither report
        nor proposal; a sizing-stage one has a report; an execution-stage one has both
        and shares its ``decision_id`` with the ``DecisionRecord`` already written.
        """
        record = StageRejectionRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            stage=stage,
            code=code,
            message=message,
            # A snapshot may be supplied directly (startup recovery replays a
            # record whose Signal object is long gone); rebuilding one from a
            # reconstructed Signal would silently drop fields.
            signal=(
                signal_snapshot
                if signal_snapshot is not None
                else SignalSnapshot.of(signal)  # type: ignore[arg-type]
            ),
            research=ResearchSnapshot.of(report) if report is not None else None,
            sizing=SizingSnapshot.of(proposal) if proposal is not None else None,
            expression=expression,
            screen_research=(
                ResearchSnapshot.of(screen_report)
                if screen_report is not None
                else None
            ),
            screen_est_cost_usd=(screen_usage.cost_usd if screen_usage else None),
            est_input_tokens=usage.input_tokens if usage else None,
            est_output_tokens=usage.output_tokens if usage else None,
            est_cost_usd=usage.cost_usd if usage else None,
        )
        self._append(record)
        return record

    def record_thesis_review(
        self,
        decision_id: str,
        outcome: ReviewOutcome,
        assessment: Optional[str] = None,
        invalidation_triggered: Optional[bool] = None,
        code: Optional[str] = None,
        message: str = "",
        usage: Optional[ResearchUsage] = None,
        validity: Optional[str] = None,
        progress: Optional[str] = None,
        resolution: Optional[str] = None,
        revised_resolution_date=None,
        continuation_thesis: Optional[str] = None,
        close_contradiction: Optional[str] = None,
        trigger_reason: Optional[str] = None,
        leash_days_after: Optional[int] = None,
    ) -> ThesisReviewRecord:
        """Record one thesis review of an open position.

        Validated against the entry decision like a fill is: a review of a position
        that was never approved means the review layer is looking at a phantom.
        """
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position to review"
            )
        record = ThesisReviewRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            outcome=outcome,
            assessment=assessment,
            invalidation_triggered=invalidation_triggered,
            validity=validity,
            progress=progress,
            resolution=resolution,
            revised_resolution_date=revised_resolution_date,
            continuation_thesis=continuation_thesis,
            close_contradiction=close_contradiction,
            trigger_reason=trigger_reason,
            leash_days_after=leash_days_after,
            code=code,
            message=message,
            est_input_tokens=usage.input_tokens if usage else None,
            est_output_tokens=usage.output_tokens if usage else None,
            est_cost_usd=usage.cost_usd if usage else None,
        )
        self._append(record)
        return record

    def record_exit(
        self,
        decision_id: str,
        reason: ExitReason,
        detail: str,
        gate_decision: object,
        submitted: Optional[bool] = None,
        broker_order_id: Optional[str] = None,
        broker_error: Optional[str] = None,
    ) -> ExitRecord:
        """Record one attempt to close a position - exits are decisions too.

        Written whether the gate approved the sell-to-close or rejected it, and
        whether the broker took it or refused: every attempt is a fact about the
        position, and a retry writes its own record rather than editing this one.
        """
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position to exit"
            )
        record = ExitRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            reason=reason,
            detail=detail,
            gate=GateSnapshot.of(gate_decision),  # type: ignore[arg-type]
            submitted=submitted,
            broker_order_id=broker_order_id,
            broker_error=broker_error,
        )
        self._append(record)
        return record

    def record_fill(
        self,
        decision_id: str,
        broker_order_id: str,
        filled_quantity: Decimal,
        fill_price: Decimal,
        filled_value: Optional[Decimal] = None,
        side: str = "buy",
        intended_price: Optional[Decimal] = None,
        spread_pct_at_submission: Optional[Decimal] = None,
        seconds_to_fill: Optional[Decimal] = None,
    ) -> FillRecord:
        """Record a fill. Refused for a decision that was never approved."""
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected by the risk gate and cannot have a fill; "
                f"a fill against a rejected decision means an order bypassed the gate"
            )
        record = FillRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            side=side,
            broker_order_id=broker_order_id,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            filled_value=(
                filled_value
                if filled_value is not None
                else filled_quantity * fill_price
            ),
            intended_price=intended_price,
            spread_pct_at_submission=spread_pct_at_submission,
            seconds_to_fill=seconds_to_fill,
        )
        self._append(record)
        return record

    def record_outcome(
        self,
        decision_id: str,
        realised_pnl: Decimal,
        closed_at: Optional[datetime] = None,
        note: str = "",
        credibility: Optional[object] = None,
    ) -> OutcomeRecord:
        """Resolve a position, and credit or debit the source that suggested it.

        This is the step that turns a source's hit rate from "not yet available" into
        a number: the outcome is attributed back to the signal that produced the
        decision, so credibility accrues to whoever actually called it.
        """
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position to resolve"
            )
        if self._outcome_for(decision_id) is not None:
            raise AuditLogError(
                f"{decision_id} already has an outcome; correct it with a "
                f"CorrectionRecord rather than resolving it twice"
            )

        # Tax character (2026-09-02): long-term iff the close lands on or after
        # the boundary derived from the FIRST buy fill. None when no buy fill is
        # findable — absent, never guessed.
        closed = closed_at or self._clock()
        first_buy = None
        for record in self.records():
            if (
                isinstance(record, FillRecord)
                and record.decision_id == decision_id
                and record.side == "buy"
            ):
                first_buy = record.recorded_at
                break
        long_term = (
            closed.date() >= long_term_boundary(first_buy.date())
            if first_buy is not None
            else None
        )

        record = OutcomeRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            closed_at=closed,
            realised_pnl=realised_pnl,
            note=note,
            long_term=long_term,
        )
        self._append(record)

        if credibility is not None:
            credibility.record_outcome(  # type: ignore[attr-defined]
                decision.signal.credibility_key or decision.signal.source_id,
                won=record.won,
            )
        return record

    def record_filer_event(
        self,
        decision_id: str,
        *,
        arm: str,
        filer: str,
        symbol: str,
        transaction: str,
        disclosure_source_id: str,
        disclosure_external_id: Optional[str] = None,
        transaction_date: Optional[str] = None,
        report_date: Optional[str] = None,
        amount_range: Optional[str] = None,
        detail: str = "",
    ) -> FilerEventRecord:
        """Record a new disclosure by the filer who originated a held position
        (ruling 2026-09-01). Validated like a review: an event against a decision
        that never held a position is an event against a phantom."""
        decision = self._decision(decision_id)
        if not decision.was_approved:
            raise AuditLogError(
                f"{decision_id} was rejected and never held a position for a "
                f"filer event to land on"
            )
        record = FilerEventRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            arm=arm,
            filer=filer,
            symbol=symbol,
            transaction=transaction,
            disclosure_source_id=disclosure_source_id,
            disclosure_external_id=disclosure_external_id,
            transaction_date=transaction_date,
            report_date=report_date,
            amount_range=amount_range,
            detail=detail,
        )
        self._append(record)
        return record

    def filer_event_keys(self) -> set[tuple[str, str]]:
        """(decision_id, disclosure_external_id) of every filer event written.

        Unresearched disclosures re-emit at startup (that is the dedup design),
        so the engines seed from this set: one disclosure writes one event per
        position, ever, no matter how many times the signal is drained.
        """
        keys: set[tuple[str, str]] = set()
        for record in self.records():
            if isinstance(record, FilerEventRecord) and record.disclosure_external_id:
                keys.add((record.decision_id, record.disclosure_external_id))
        return keys

    def triggered_reviews_on(self, day) -> int:
        """Out-of-cadence reviews recorded on a UTC day (ruling 2026-08-31).

        Replayed from the log, like the research budget, so a restart cannot hand
        the day a second allowance of triggered reviews.
        """
        return sum(
            1
            for record in self.records()
            if isinstance(record, ThesisReviewRecord)
            and record.trigger_reason
            and record.recorded_at.date() == day
        )

    def record_correction(
        self, decision_id: str, supersedes_sequence: int, reason: str, **fields: object
    ) -> CorrectionRecord:
        """Supersede an earlier record. The original is never touched."""
        record = CorrectionRecord(
            decision_id=decision_id,
            recorded_at=self._clock(),
            supersedes_sequence=supersedes_sequence,
            reason=reason,
            corrected_fields=dict(fields),
        )
        self._append(record)
        return record

    def _append(self, record: object) -> None:
        """The only write path. Append mode, one line, flushed."""
        line = record.model_dump_json()  # type: ignore[attr-defined]
        if "\n" in line:  # pragma: no cover - pydantic never emits newlines
            raise AuditLogError("record serialised to multiple lines")
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    # -- reading -------------------------------------------------------------------

    def records(self) -> Iterator[AuditRecord]:
        """Replay the log in write order."""
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield _ADAPTER.validate_python(json.loads(line))

    def decisions(self) -> list[DecisionRecord]:
        return [r for r in self.records() if isinstance(r, DecisionRecord)]

    def stage_rejections(self) -> list[StageRejectionRecord]:
        return [r for r in self.records() if isinstance(r, StageRejectionRecord)]

    def rejections_for(self, decision_id: str) -> list[StageRejectionRecord]:
        """Every stage rejection recorded against one decision id.

        For a signal that never reached the gate this *is* the whole trail — there is
        no ``AuditTrail`` to assemble, because nothing was decided.
        """
        return [r for r in self.stage_rejections() if r.decision_id == decision_id]

    def researched_external_ids(self) -> set[tuple[str, str]]:
        """(source_id, external_id) for every signal that entered the pipeline.

        Every path through the pipeline writes a record carrying the signal snapshot
        — a decision, or a stage rejection — so this is the set of signals research
        was already spent on. Fetchers seed their dedup sets from it at startup, so a
        restart does not re-emit (and re-pay for) what the log already answers. A
        signal that was queued but never researched left no record, which is the
        right edge: it re-emits and finally gets its pass.
        """
        seen: set[tuple[str, str]] = set()
        for record in self.records():
            sizing = getattr(record, "sizing", None)
            if sizing is not None and sizing.strategy == "mechanical":
                # Mechanical records never seal a signal for the JUDGED path
                # (ruling 2026-08-27): the experiment's arms are independent,
                # and a disclosure the mechanical sleeve bought must remain
                # researchable by the judged system.
                continue
            if getattr(record, "code", None) in (
                "source_cap",
                "upstream_error",
                "mechanical_capacity",
                "mechanical_halted",
                "mechanical_disabled",
                "entries_disabled",
            ):
                # Ruling 2026-08-26: never permanently discard a signal the
                # system didn't pay to evaluate. Two codes qualify:
                #   source_cap      the per-source daily cap declined to spend
                #                   a pass (same exclusion
                #                   research_passes_by_source_on makes).
                #   upstream_error  the research CALL failed — an outage or a
                #                   broken request shape produced no verdict.
                #                   Origin: the elision-400 window sealed both
                #                   Pelosi Bloom Energy tranches on 2026-08-24,
                #                   hours before that fix shipped.
                # Both re-emit at the next startup and compete for that day's
                # slots until a verdict exists or the staleness prefilter
                # retires them for free. Self-limiting: a successful research
                # pass writes a sealing record.
                continue
            signal = getattr(record, "signal", None)
            if signal is not None and signal.external_id:
                seen.add((signal.source_id, signal.external_id))
        return seen

    def mechanical_trails(self) -> list[AuditTrail]:
        """Per-decision trails of the mechanical sleeve alone."""
        return [
            self.trail(d.decision_id)
            for d in self.decisions()
            if d.sizing.strategy == "mechanical"
        ]

    def mechanical_open_positions(self) -> dict[str, tuple[Decimal, Decimal]]:
        """symbol -> (net quantity, net cost) of open mechanical positions."""
        return self.strategy_open_positions("mechanical")

    def strategy_open_positions(
        self, strategy: str
    ) -> dict[str, tuple[Decimal, Decimal]]:
        """symbol -> (net quantity, net cost) of a strategy's open positions,
        replayed from the log. Startup uses this to split the broker's single
        per-symbol holding between the sleeves — the broker stays authoritative
        on totals; the log alone knows which sleeve owns what."""
        open_positions: dict[str, tuple[Decimal, Decimal]] = {}
        trails = [
            self.trail(d.decision_id)
            for d in self.decisions()
            if d.sizing.strategy == strategy
        ]
        for trail in trails:
            decision = trail.decision
            if not decision.was_approved or trail.outcome is not None:
                continue
            order = decision.gate.order or {}
            symbol = str(order.get("symbol", ""))
            if not symbol:
                continue
            buys = [f for f in trail.fills if f.side == "buy"]
            sells = [f for f in trail.fills if f.side == "sell"]
            quantity = sum((f.filled_quantity for f in buys), Decimal("0")) - sum(
                (f.filled_quantity for f in sells), Decimal("0")
            )
            if quantity <= 0:
                continue
            cost = sum((f.filled_value for f in buys), Decimal("0"))
            held_quantity, held_cost = open_positions.get(
                symbol, (Decimal("0"), Decimal("0"))
            )
            open_positions[symbol] = (held_quantity + quantity, held_cost + cost)
        return open_positions

    def capped_external_ids(self) -> set[tuple[str, str]]:
        """(source_id, external_id) of every source_cap rejection — seeds the
        loop's memory of signals that lost a slot, so a later staleness kill
        can carry code aged_out_capped: the cap cost an evaluation, a tuning
        signal the human wants visible rather than inferred (2026-08-26)."""
        capped: set[tuple[str, str]] = set()
        for record in self.records():
            if getattr(record, "code", None) != "source_cap":
                continue
            signal = getattr(record, "signal", None)
            if signal is not None and signal.external_id:
                capped.add((signal.source_id, signal.external_id))
        return capped

    def last_record_at(self) -> Optional[datetime]:
        """Newest ``recorded_at`` in the log — when the system was last alive.
        Sizes the X fetchers' session-gap first-poll lookback (ruling
        2026-08-26), so overnight posts are fetched instead of lost."""
        latest: Optional[datetime] = None
        for record in self.records():
            recorded = getattr(record, "recorded_at", None)
            if recorded is not None and (latest is None or recorded > latest):
                latest = recorded
        return latest

    def first_seen(self) -> dict[str, datetime]:
        """Earliest record time per decision id, in write order."""
        seen: dict[str, datetime] = {}
        for record in self.records():
            recorded_at = record.recorded_at
            existing = seen.get(record.decision_id)
            if existing is None or recorded_at < existing:
                seen[record.decision_id] = recorded_at
        return seen

    def research_passes_by_source_on(self, day: date) -> dict[str, int]:
        """Research passes dispatched per source on ``day`` — the seed for the
        per-source daily caps (2026-08-25), so a restart cannot reset a cap.
        Same counting rule as ``research_passes_on``: pre-filter and triage
        rejections spent nothing and do not count."""
        counts: dict[str, int] = {}
        for record in self.records():
            if getattr(record, "recorded_at", None) is None:
                continue
            if record.recorded_at.date() != day:
                continue
            if isinstance(record, DecisionRecord):
                if record.sizing.strategy in ("mechanical", "cash_sweep"):
                    # No LLM ran (defect fix 2026-09-02): a mechanical entry or
                    # a cash sweep spent no pass, and counting it here was
                    # quietly consuming the judged source cap on every restart.
                    continue
                source = record.signal.source_id
            elif isinstance(record, StageRejectionRecord):
                if record.stage in (RejectedStage.PRE_FILTER, RejectedStage.TRIAGE):
                    continue
                source = record.signal.source_id
            else:
                continue
            counts[source] = counts.get(source, 0) + 1
        return counts

    def research_passes_on(self, day: date) -> int:
        """How many signals were researched on ``day`` (UTC).

        The research budget has to survive a restart, or a crash loop would spend it
        without limit. Rather than keep a counter somewhere that can drift from
        reality, this derives the figure from the log: the pipeline allocates one
        ``decision_id`` per signal it researches and every path from there writes at
        least one record, so distinct ids first recorded on a day *is* the number of
        passes bought that day. A later record against an earlier decision — a fill, an
        outcome — does not count again, because the id is not new.

        Thesis reviews of open positions spend the budget too, and they run under the
        *entry's* decision_id — an id that is not new — so they are counted by record
        rather than by id: every ``ThesisReviewRecord`` stamped on the day is one pass,
        failed reviews included, because the call was still made.
        """
        first_by_id: dict[str, AuditRecord] = {}
        reviews = 0
        for record in self.records():
            if record.decision_id not in first_by_id:
                first_by_id[record.decision_id] = record
            if (
                isinstance(record, ThesisReviewRecord)
                and record.recorded_at.date() == day
            ):
                reviews += 1
        new_ids = sum(
            1
            for record in first_by_id.values()
            if record.recorded_at.date() == day
            # A pre-filtered or triage-gated signal got a decision_id and a
            # record but no FULL research pass — neither spends the pass budget,
            # so neither may be replayed as spent. (Triage spends dollars, which
            # the COST meter tracks; this counter is passes, not dollars.)
            and not (
                isinstance(record, StageRejectionRecord)
                and record.stage in (RejectedStage.PRE_FILTER, RejectedStage.TRIAGE)
            )
            # Mechanical entries and cash sweeps have no LLM in their paths
            # (defect fix 2026-09-02): replaying them as spent passes was
            # quietly shrinking the day's research budget after every restart.
            and not (
                isinstance(record, DecisionRecord)
                and record.sizing.strategy in ("mechanical", "cash_sweep")
            )
        )
        return new_ids + reviews

    def _decision(self, decision_id: str) -> DecisionRecord:
        for record in self.records():
            if isinstance(record, DecisionRecord) and record.decision_id == decision_id:
                return record
        raise AuditLogError(f"no decision recorded for {decision_id}")

    def _outcome_for(self, decision_id: str) -> Optional[OutcomeRecord]:
        for record in self.records():
            if isinstance(record, OutcomeRecord) and record.decision_id == decision_id:
                return record
        return None

    def trail(self, decision_id: str) -> AuditTrail:
        """Assemble the full per-order view: decision, fills, outcome, corrections."""
        decision: Optional[DecisionRecord] = None
        fills: list[FillRecord] = []
        outcome: Optional[OutcomeRecord] = None
        corrections: list[CorrectionRecord] = []
        rejections: list[StageRejectionRecord] = []
        reviews: list[ThesisReviewRecord] = []
        exits: list[ExitRecord] = []
        filer_events: list[FilerEventRecord] = []

        for record in self.records():
            if getattr(record, "decision_id", None) != decision_id:
                continue
            if isinstance(record, DecisionRecord):
                decision = record
            elif isinstance(record, FillRecord):
                fills.append(record)
            elif isinstance(record, OutcomeRecord):
                outcome = record
            elif isinstance(record, CorrectionRecord):
                corrections.append(record)
            elif isinstance(record, StageRejectionRecord):
                rejections.append(record)
            elif isinstance(record, ThesisReviewRecord):
                reviews.append(record)
            elif isinstance(record, ExitRecord):
                exits.append(record)
            elif isinstance(record, FilerEventRecord):
                filer_events.append(record)

        if decision is None:
            raise AuditLogError(f"no decision recorded for {decision_id}")
        return AuditTrail(
            decision=decision,
            fills=tuple(fills),
            outcome=outcome,
            corrections=tuple(corrections),
            stage_rejections=tuple(rejections),
            reviews=tuple(reviews),
            exits=tuple(exits),
            filer_events=tuple(filer_events),
        )

    def research_costs_by_class(
        self, window_start: datetime
    ) -> dict[SignalClass, Decimal]:
        """Estimated LLM research spend per signal class inside the window.

        The entry pass is counted ONCE per decision_id — a decision record and a
        later execution-stage rejection share an id and the same usage estimate,
        and double-billing a class for one call would overstate its costs. Thesis
        reviews are counted per record: each review is its own pass under the
        entry's id. Records with no estimate contribute nothing (an absent
        estimate is absent, never zero-priced-as-free — the weekly console
        reconciliation is where unpriced spend gets caught).
        """
        totals: dict[SignalClass, Decimal] = {}
        entry_counted: set[str] = set()
        class_of: dict[str, SignalClass] = {}
        for record in self.records():
            if isinstance(record, (DecisionRecord, StageRejectionRecord)):
                class_of.setdefault(record.decision_id, record.signal.signal_class)
        for record in self.records():
            cost = getattr(record, "est_cost_usd", None)
            if cost is None or record.recorded_at < window_start:
                continue
            if isinstance(record, (DecisionRecord, StageRejectionRecord)):
                if record.decision_id in entry_counted:
                    continue
                entry_counted.add(record.decision_id)
                signal_class = record.signal.signal_class
            elif isinstance(record, ThesisReviewRecord):
                signal_class = class_of.get(record.decision_id)
                if signal_class is None:
                    continue
            else:
                continue
            totals[signal_class] = totals.get(signal_class, Decimal("0")) + cost
        return totals

    def research_cost_between(
        self, start: datetime, end: Optional[datetime] = None
    ) -> Decimal:
        """Total estimated LLM spend recorded in [start, end). Same counting rules
        as ``research_costs_by_class``: an entry pass bills once per decision_id
        (a decision record and a later execution rejection share one call);
        thesis reviews bill per record; records with no estimate — pre_filter
        rejections, unpriced models — contribute exactly zero.
        """
        total = Decimal("0")
        entry_counted: set[str] = set()
        for record in self.records():
            cost = getattr(record, "est_cost_usd", None)
            if cost is None:
                continue
            if record.recorded_at < start:
                continue
            if end is not None and record.recorded_at >= end:
                continue
            if isinstance(record, (DecisionRecord, StageRejectionRecord)):
                if record.decision_id in entry_counted:
                    continue
                entry_counted.add(record.decision_id)
            elif not isinstance(record, ThesisReviewRecord):
                continue
            total += cost
        return total

    def trails(self) -> list[AuditTrail]:
        return [self.trail(d.decision_id) for d in self.decisions()]
