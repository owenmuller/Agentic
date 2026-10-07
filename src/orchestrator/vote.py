"""Self-consistency vote (human ruling 2026-10-07) as ONE function per path.

Diagnosis: five single-pass replays of every golden case (2026-10-06) put the
noise where the trades are. Twenty cases sat in a tight confident-decline
cluster; the five noisy ones were marginal filing cases whose confidence spanned
20-40 points on identical input, and EVERY long outside the Form 4 INTC cluster
was a minority verdict - one or two runs of five. Temperature cannot pin it
(the noise is search variance; T=0 on the report call still spanned 20-82), so
a pass is treated as a noisy sample and the decision is a vote.

Rule: after the first production pass, buy ``k`` further INDEPENDENT full
passes (same path, fresh context, no knowledge of the first) when
  (i)  the verdict is a long or puts, or an add verdict on an add decision, or
  (ii) its confidence lies within ``margin`` of a live threshold - the sizing
       floor, the band edge 50, the band top 70 (configured) - a decline
       included, because the next draw of a decline at 62 was long/52.
Confident declines buy nothing. Direction = majority of the k+1 samples;
confidence = median of the samples carrying the majority direction (rounded
DOWN - Constraint #6); a long sizes only if the majority is long and the median
clears the floor. No majority, an unfunded or failed sample on a tradeable
trigger, or an add that does not replicate as an add -> no position / hold.

Exit reviews that would CLOSE or TRIM vote the same way on the action; no
majority -> hold (the vote confirms or kills an action, never invents one).

This REPLACES boundary confirmation (ruling 2026-09-02, k=1 on the floor
band, the lower sizes): one vote, one place, the pipeline and the golden replay
share it. Every sample's verdict, confidence and search-transcript hash is
persisted with the decision so vote stability is measurable per source and
band. Topology: this module reads the samples and nothing else - no
attribution, spend, P&L or target (tests/test_scoreboard_constraint.py).
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from audit.records import VoteSampleSnapshot, VoteSnapshot
from research.reports import Direction, ResearchReport, ResearchUsage

logger = logging.getLogger(__name__)

TRIGGER_TRADEABLE = "tradeable_verdict"
TRIGGER_ADD = "add_verdict"
TRIGGER_NEAR_THRESHOLD = "near_threshold"
TRIGGER_REVIEW = "review_close_or_trim"
#: The stage-rejection code when a tradeable verdict loses its vote.
VOTE_OVERTURNED = "vote_overturned"

NO_POSITION = str(Direction.NO_POSITION)


@dataclass(frozen=True, slots=True)
class VoteRules:
    """What the config says: extra samples, the margin and the thresholds."""

    k: int = 2
    margin: int = 10
    thresholds: tuple[int, ...] = (45, 50, 70)
    floor: Optional[int] = None

    def nearest_threshold(self, confidence: int) -> Optional[int]:
        near = [t for t in self.thresholds if abs(confidence - t) <= self.margin]
        return min(near, key=lambda t: abs(confidence - t)) if near else None


def trigger_for(report: ResearchReport, rules: VoteRules, add_context: Any = None) -> Optional[str]:
    """Why this verdict buys a vote, or None when it does not."""
    if add_context is not None:
        if report.is_add:
            return TRIGGER_ADD
        return TRIGGER_NEAR_THRESHOLD if rules.nearest_threshold(report.confidence) is not None else None
    if str(report.direction) != NO_POSITION:
        return TRIGGER_TRADEABLE
    if rules.nearest_threshold(report.confidence) is not None:
        return TRIGGER_NEAR_THRESHOLD
    return None


def _label(report: ResearchReport, add_context: Any) -> str:
    """The thing the vote is over: the add verdict on an add decision, else the direction."""
    if add_context is not None:
        return str(report.add_verdict) if report.add_verdict is not None else "hold"
    return str(report.direction)


def _median_down(values: Sequence[int]) -> int:
    median = statistics.median(values)
    return int(median)  # x.5 rounds DOWN (Constraint #6)


@dataclass
class VoteOutcome:
    """What the vote decided. ``report`` is the report to continue with:
    the majority sample nearest the median, its confidence set to the median.
    None = a tradeable trigger was overturned, unfunded or failed, with
    ``failure`` saying why (a typed ``vote_overturned`` rejection). ``ran``
    False = no trigger, and ``report`` is simply the first pass."""

    report: Optional[ResearchReport]
    failure: Optional[str] = None
    snapshot: Optional[VoteSnapshot] = None
    usage: Optional[ResearchUsage] = None
    ran: bool = False
    trigger: Optional[str] = None
    first: Optional[ResearchReport] = None
    samples: tuple[Any, ...] = ()
    #: Kept for the golden harness and old call sites: the second sample.
    second: Any = None

    @property
    def upheld(self) -> bool:
        """The vote ran and the first pass's verdict carried."""
        return self.ran and self.snapshot is not None and self.snapshot.held

    @property
    def reversed(self) -> bool:
        """The vote ran and did NOT carry the first pass (overturned, no
        majority, unfunded or a failed sample on a tradeable trigger)."""
        return self.ran and not self.upheld

    def describe(self) -> str:
        if not self.ran:
            return "no vote (no trigger)"
        votes = ", ".join(
            f"{s.direction}/{s.confidence}" if s.confidence is not None else s.direction for s in self.samples
        )
        if self.report is None:
            return f"samples [{votes}] -> OVERTURNED, nothing sizes ({self.failure})"
        assert self.snapshot is not None
        return (
            f"samples [{votes}] -> {'HELD' if self.snapshot.held else 'OVERTURNED to'} "
            f"{self.snapshot.majority_direction}/{self.snapshot.median_confidence}"
        )


def _sum_usage(parts: Sequence[Optional[ResearchUsage]]) -> Optional[ResearchUsage]:
    real = [p for p in parts if p is not None]
    if not real:
        return None
    cost = None
    if any(p.cost_usd is not None for p in real):
        cost = sum((p.cost_usd for p in real if p.cost_usd is not None), Decimal("0"))
    return ResearchUsage(
        input_tokens=sum(p.input_tokens for p in real),
        output_tokens=sum(p.output_tokens for p in real),
        cost_usd=cost,
    )


def run_vote(
    research,
    signal,
    report: ResearchReport,
    *,
    rules: VoteRules,
    add_context: Any = None,
    fund: Optional[Callable[[], bool]] = None,
    first_transcript_hash: str = "",
) -> VoteOutcome:
    """The entry-side vote on ``report`` (the first pass). ``research`` is the
    production ResearchPass (``run(signal, add_context=)``, ``last_usage``,
    ``last_transcript_hash``). ``fund`` is asked once per extra sample and may
    refuse (the daily research budget); an unfunded vote on a tradeable
    trigger sizes nothing."""
    trigger = trigger_for(report, rules, add_context)
    if trigger is None:
        return VoteOutcome(report, first=report)
    first_label = _label(report, add_context)
    tradeable = trigger in (TRIGGER_TRADEABLE, TRIGGER_ADD)
    logger.info(
        "vote on %s: first pass %s/%d (%s); buying %d independent samples",
        signal.signal_id, first_label, report.confidence, trigger, rules.k,
    )
    samples: list[tuple[ResearchReport, str, Optional[ResearchUsage]]] = [(report, first_transcript_hash, None)]
    usages: list[Optional[ResearchUsage]] = []
    failure: Optional[str] = None
    for index in range(rules.k):
        if fund is not None and not fund():
            failure = f"sample {index + 2} of {rules.k + 1} not funded (daily research budget)"
            break
        sample = research.run(signal, add_context=add_context)
        usage = research.last_usage
        usages.append(usage)
        if not isinstance(sample, ResearchReport):
            failure = f"sample {index + 2} of {rules.k + 1} failed ({getattr(sample, 'code', 'error')})"
            break
        samples.append((sample, getattr(research, "last_transcript_hash", "") or "", usage))
    extra_usage = _sum_usage(usages)
    snapshots = tuple(
        VoteSampleSnapshot(
            index=i,
            direction=_label(s, add_context),
            confidence=s.confidence,
            transcript_hash=h,
            est_cost_usd=(u.cost_usd if u is not None else None),
        )
        for i, (s, h, u) in enumerate(samples)
    )
    base = dict(
        trigger=trigger, k=rules.k, margin=rules.margin, thresholds=tuple(rules.thresholds), floor=rules.floor,
        first_direction=first_label, first_confidence=report.confidence, samples=snapshots,
        extra_passes=len(samples) - 1, extra_est_cost_usd=(extra_usage.cost_usd if extra_usage else None),
    )
    if failure is not None:
        if tradeable:
            snap = VoteSnapshot(**base, majority_direction=NO_POSITION, median_confidence=None, held=False, failure=failure)
            return VoteOutcome(None, f"vote on {first_label}/{report.confidence} incomplete: {failure} - a verdict that cannot be confirmed is not sized (ruling 2026-10-07)",
                               snapshot=snap, usage=extra_usage, ran=True, trigger=trigger, first=report, samples=snapshots)
        # A near-threshold decline that could not be voted stays a decline.
        snap = VoteSnapshot(**base, majority_direction=first_label, median_confidence=report.confidence, held=True, failure=failure)
        return VoteOutcome(report, snapshot=snap, usage=extra_usage, ran=True, trigger=trigger, first=report, samples=snapshots)

    labels = [_label(s, add_context) for s, _, _ in samples]
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    needed = len(samples) // 2 + 1
    majority = next((label for label, n in counts.items() if n >= needed), None)
    if majority is None:
        # No majority: no position (Constraint #6). An add decision holds.
        majority = "hold" if add_context is not None else NO_POSITION
    chosen = [s for s, _, _ in samples if _label(s, add_context) == majority]
    if not chosen:
        # Three-way split with no decline sample cannot happen (two directions
        # plus no_position exhaust the schema), but never size on a vacuum.
        snap = VoteSnapshot(**base, majority_direction=majority, median_confidence=None, held=False, failure="no majority")
        return VoteOutcome(None, f"vote on {first_label}/{report.confidence}: no majority among {labels} - nothing sizes (ruling 2026-10-07)",
                           snapshot=snap, usage=extra_usage, ran=True, trigger=trigger, first=report, samples=snapshots)
    median = _median_down([s.confidence for s in chosen])
    nearest = min(chosen, key=lambda s: (abs(s.confidence - median), s.confidence))
    decided = nearest if nearest.confidence == median else nearest.model_copy(update={"confidence": median})
    held = majority == first_label
    snap = VoteSnapshot(**base, majority_direction=majority, median_confidence=median, held=held, failure=None)
    tradeable_now = (majority not in (NO_POSITION, "hold")) if add_context is None else majority == "add"
    if add_context is not None and majority == "add" and not decided.is_add:
        tradeable_now = False
    if not tradeable_now and tradeable:
        logger.info("vote on %s OVERTURNED: %s", signal.signal_id, [f"{l}/{s.confidence}" for (s, _, _), l in zip(samples, labels)])
        return VoteOutcome(None, f"vote on {first_label}/{report.confidence} overturned: samples {[f'{l}/{s.confidence}' for (s, _, _), l in zip(samples, labels)]} -> {majority}/{median} (ruling 2026-10-07)",
                           snapshot=snap, usage=extra_usage, ran=True, trigger=trigger, first=report, samples=snapshots)
    logger.info("vote on %s %s: %s -> %s/%d", signal.signal_id, "held" if held else "overturned", labels, majority, median)
    return VoteOutcome(decided, snapshot=snap, usage=extra_usage, ran=True, trigger=trigger, first=report, samples=snapshots,
                       second=(samples[1][0] if len(samples) > 1 else None))


# ----------------------------------------------------------------- exit reviews

def review_label(outcome: Any) -> str:
    """close / trim / hold - ``should_close`` folds the contradiction rules in."""
    if getattr(outcome, "should_close", False):
        return "close"
    return str(getattr(outcome, "action", "hold"))


@dataclass
class ReviewVoteOutcome:
    outcome: Any
    snapshot: Optional[VoteSnapshot] = None
    usage: Optional[ResearchUsage] = None
    ran: bool = False
    failure: Optional[str] = None


def run_review_vote(
    ask: Callable[[], Any],
    first: Any,
    *,
    rules: VoteRules,
    usage_of: Callable[[], Optional[ResearchUsage]],
    hash_of: Callable[[], str],
    fund: Optional[Callable[[], bool]] = None,
    first_transcript_hash: str = "",
    is_review: Callable[[Any], bool] = lambda o: hasattr(o, "action"),
) -> ReviewVoteOutcome:
    """The review-side vote: a first verdict that would close or trim buys
    ``k`` more; majority action; no majority or an incomplete vote -> HOLD
    (the first HOLD sample, or the first verdict with its action read as hold
    by the caller). ``ask`` runs one fresh review."""
    first_label = review_label(first)
    if first_label not in ("close", "trim"):
        return ReviewVoteOutcome(first)
    samples: list[tuple[Any, str, Optional[ResearchUsage]]] = [(first, first_transcript_hash, None)]
    usages: list[Optional[ResearchUsage]] = []
    failure: Optional[str] = None
    for index in range(rules.k):
        if fund is not None and not fund():
            failure = f"sample {index + 2} of {rules.k + 1} not funded (daily research budget)"
            break
        sample = ask()
        usage = usage_of()
        usages.append(usage)
        if not is_review(sample):
            failure = f"sample {index + 2} of {rules.k + 1} failed ({getattr(sample, 'code', 'error')})"
            break
        samples.append((sample, hash_of() or "", usage))
    extra_usage = _sum_usage(usages)
    labels = [review_label(s) for s, _, _ in samples]
    snapshots = tuple(
        VoteSampleSnapshot(index=i, direction=l, confidence=None, transcript_hash=h, est_cost_usd=(u.cost_usd if u else None))
        for i, ((s, h, u), l) in enumerate(zip(samples, labels))
    )
    base = dict(
        trigger=TRIGGER_REVIEW, k=rules.k, margin=rules.margin, thresholds=tuple(rules.thresholds), floor=rules.floor,
        first_direction=first_label, first_confidence=None, samples=snapshots,
        extra_passes=len(samples) - 1, extra_est_cost_usd=(extra_usage.cost_usd if extra_usage else None),
    )
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    needed = len(samples) // 2 + 1
    majority = next((label for label, n in counts.items() if n >= needed), None)
    if failure is not None or majority is None:
        majority = "hold"
    held = majority == first_label
    snap = VoteSnapshot(**base, majority_direction=majority, median_confidence=None, held=held, failure=failure)
    chosen = next((s for (s, _, _), l in zip(samples, labels) if l == majority), None)
    if chosen is None:
        # Majority is hold but no sample said hold (incomplete vote): the
        # caller treats ``outcome`` None as HOLD, logged as such.
        return ReviewVoteOutcome(None, snapshot=snap, usage=extra_usage, ran=True, failure=failure or "no majority")
    return ReviewVoteOutcome(chosen, snapshot=snap, usage=extra_usage, ran=True, failure=failure)
