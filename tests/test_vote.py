"""Self-consistency vote (human ruling 2026-10-07): the rule in isolation.

Five single-pass replays of every golden case put the noise where the trades
are; the vote treats a pass as a sample. These tests pin the rule: what
triggers, majority direction, median-down confidence, ties and incomplete
votes to no position, adds that must replicate as adds, reviews that would
close or trim, and the persisted samples with their transcript hashes.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from audit.records import VoteSnapshot
from orchestrator.vote import (
    TRIGGER_ADD,
    TRIGGER_NEAR_THRESHOLD,
    TRIGGER_REVIEW,
    TRIGGER_TRADEABLE,
    VOTE_OVERTURNED,
    VoteRules,
    run_review_vote,
    run_vote,
    trigger_for,
)
from research.reports import ResearchUsage
from test_audit import make_report

RULES = VoteRules(k=2, margin=10, thresholds=(45, 50, 70), floor=45)


class _Pass:
    """A research pass answering from a script, with per-sample usage and hashes."""

    def __init__(self, *outcomes, hashes=None):
        self._outcomes = list(outcomes)
        self._hashes = list(hashes or [])
        self.calls = 0
        self.last_usage = None
        self.last_transcript_hash = "first0000000000"

    def run(self, signal, add_context=None):
        self.calls += 1
        self.last_usage = ResearchUsage(input_tokens=100, output_tokens=10, cost_usd=Decimal("0.10"))
        self.last_transcript_hash = self._hashes.pop(0) if self._hashes else f"hash{self.calls:011d}"
        return self._outcomes.pop(0)


class _Signal:
    signal_id = "sig-1"


def _long(confidence, target="12"):
    return make_report(direction="long", confidence=confidence, target_price=target)


def _decline(confidence):
    return make_report(direction="no_position", confidence=confidence)


# ------------------------------------------------------------------ triggers

def test_what_triggers_a_vote():
    assert trigger_for(_long(85), RULES) == TRIGGER_TRADEABLE  # every long, any confidence
    assert trigger_for(_long(52), RULES) == TRIGGER_TRADEABLE
    assert trigger_for(_decline(62), RULES) == TRIGGER_NEAR_THRESHOLD  # within 10 of 70
    assert trigger_for(_decline(40), RULES) == TRIGGER_NEAR_THRESHOLD  # within 10 of 45/50
    assert trigger_for(_decline(35), RULES) == TRIGGER_NEAR_THRESHOLD  # exactly 10 from 45
    assert trigger_for(_decline(82), RULES) is None  # the confident-decline cluster buys nothing
    assert trigger_for(_decline(20), RULES) is None
    assert RULES.nearest_threshold(62) == 70 and RULES.nearest_threshold(47) == 45


def test_a_confident_decline_buys_nothing():
    research = _Pass()
    outcome = run_vote(research, _Signal(), _decline(88), rules=RULES)
    assert not outcome.ran and outcome.report.confidence == 88 and research.calls == 0
    assert outcome.snapshot is None and outcome.describe() == "no vote (no trigger)"


# ------------------------------------------------------------------ the rule

def test_a_minority_long_is_overturned():
    """pelosi-uber: long/58 once, no_position twice -> nothing sizes."""
    research = _Pass(_decline(42), _decline(38))
    outcome = run_vote(research, _Signal(), _long(58), rules=RULES, first_transcript_hash="aaaa")
    assert research.calls == 2 and outcome.ran and outcome.reversed and outcome.report is None
    assert outcome.failure and VOTE_OVERTURNED not in outcome.failure  # the code is the caller's
    snap = outcome.snapshot
    assert isinstance(snap, VoteSnapshot)
    assert snap.trigger == TRIGGER_TRADEABLE and snap.k == 2 and snap.margin == 10 and snap.floor == 45
    assert (snap.first_direction, snap.first_confidence) == ("long", 58)
    assert (snap.majority_direction, snap.median_confidence, snap.held) == ("no_position", 40, False)
    assert [s.direction for s in snap.samples] == ["long", "no_position", "no_position"]
    assert [s.confidence for s in snap.samples] == [58, 42, 38]
    assert snap.samples[0].transcript_hash == "aaaa" and snap.samples[1].transcript_hash.startswith("hash")
    assert snap.extra_passes == 2 and snap.extra_est_cost_usd == Decimal("0.20")
    assert outcome.usage is not None and outcome.usage.cost_usd == Decimal("0.20")
    assert "OVERTURNED" in outcome.describe()


def test_a_replicating_long_holds_at_the_median_rounded_down():
    """form4-intc: long 62, 63, 62 -> held at 62; 62, 67, 64 -> 64; two majority samples 60/65 -> 62."""
    research = _Pass(_long(63), _long(62))
    outcome = run_vote(research, _Signal(), _long(62), rules=RULES)
    assert outcome.upheld and outcome.report.confidence == 62 and outcome.snapshot.held

    research = _Pass(_long(67), _long(64))
    outcome = run_vote(research, _Signal(), _long(62), rules=RULES)
    assert outcome.upheld and outcome.report.confidence == 64

    research = _Pass(_decline(30), _long(65))
    outcome = run_vote(research, _Signal(), _long(60), rules=RULES)
    assert outcome.upheld and outcome.snapshot.majority_direction == "long"
    assert outcome.report.confidence == 62  # median of (60, 65) = 62.5, rounded DOWN
    assert outcome.report.target_price == Decimal("12")  # a real sample's thesis, confidence set to the median


def test_a_near_threshold_decline_can_be_voted_either_way():
    """pelosi-be: no_position/62 whose next draws were long/52 and long/55 -> long/52 (the vote
    confirms or kills; a decline that is a minority is overturned to the majority long)."""
    research = _Pass(_long(52), _long(55))
    outcome = run_vote(research, _Signal(), _decline(62), rules=RULES)
    assert outcome.ran and outcome.report is not None
    assert str(outcome.report.direction) == "long" and outcome.report.confidence == 53
    assert outcome.snapshot.trigger == TRIGGER_NEAR_THRESHOLD and not outcome.snapshot.held

    research = _Pass(_decline(72), _long(52))
    outcome = run_vote(research, _Signal(), _decline(62), rules=RULES)
    assert outcome.upheld and str(outcome.report.direction) == "no_position"
    assert outcome.report.confidence == 67  # median of the majority declines (62, 72)


def test_an_unfunded_or_failed_sample_on_a_tradeable_trigger_sizes_nothing():
    funds = iter([True, False])
    research = _Pass(_long(60), _long(60))
    outcome = run_vote(research, _Signal(), _long(60), rules=RULES, fund=lambda: next(funds))
    assert research.calls == 1 and outcome.reversed and outcome.report is None
    assert "not funded" in outcome.failure and outcome.snapshot.failure and outcome.snapshot.extra_passes == 1

    class _Rejection:
        code = "upstream_error"

    research = _Pass(_Rejection(), _long(60))
    outcome = run_vote(research, _Signal(), _long(60), rules=RULES)
    assert outcome.report is None and "failed (upstream_error)" in outcome.failure

    # The same incomplete vote on a near-threshold DECLINE leaves the decline standing.
    research = _Pass(_Rejection())
    outcome = run_vote(research, _Signal(), _decline(62), rules=RULES)
    assert outcome.ran and outcome.report.confidence == 62 and outcome.snapshot.held and outcome.snapshot.failure


def test_an_add_must_replicate_as_an_add():
    add = make_report(direction="long", confidence=66, target_price="12", add_verdict="add", add_fraction="0.5")
    hold = make_report(direction="long", confidence=85, target_price="12", add_verdict="hold")
    context = object()
    assert trigger_for(add, RULES, context) == TRIGGER_ADD
    assert trigger_for(hold, RULES, context) is None  # a confident hold on an add decision buys nothing
    research = _Pass(hold, hold)
    outcome = run_vote(research, _Signal(), add, rules=RULES, add_context=context)
    assert outcome.reversed and outcome.report is None and outcome.snapshot.majority_direction == "hold"
    research = _Pass(add, hold)
    outcome = run_vote(research, _Signal(), add, rules=RULES, add_context=context)
    assert outcome.upheld and outcome.report.is_add and outcome.report.confidence == 66


# ------------------------------------------------------------------ reviews

class _Review:
    def __init__(self, action, close_contradiction=None):
        self.action = action
        self.close_contradiction = close_contradiction

    @property
    def should_close(self):
        return self.action == "close" or self.close_contradiction is not None


def test_reviews_that_would_close_or_trim_vote_on_the_action():
    asked = []

    def ask():
        asked.append(1)
        return samples.pop(0)

    usage = lambda: ResearchUsage(input_tokens=50, output_tokens=5, cost_usd=Decimal("0.05"))  # noqa: E731
    # A stray close (review-intc-day3: close, hold, hold) -> hold.
    samples = [_Review("hold"), _Review("hold")]
    voted = run_review_vote(ask, _Review("close"), rules=RULES, usage_of=usage, hash_of=lambda: "h")
    assert voted.ran and voted.outcome.action == "hold" and not voted.snapshot.held
    assert voted.snapshot.trigger == TRIGGER_REVIEW and voted.snapshot.majority_direction == "hold"
    assert [s.direction for s in voted.snapshot.samples] == ["close", "hold", "hold"]
    # A replicating trim holds.
    samples = [_Review("trim"), _Review("hold")]
    voted = run_review_vote(ask, _Review("trim"), rules=RULES, usage_of=usage, hash_of=lambda: "h")
    assert voted.outcome.action == "trim" and voted.snapshot.held
    # Three different actions: no majority -> hold.
    samples = [_Review("trim"), _Review("hold")]
    voted = run_review_vote(ask, _Review("close"), rules=RULES, usage_of=usage, hash_of=lambda: "h")
    assert voted.outcome.action == "hold" and voted.snapshot.majority_direction == "hold"
    # A hold buys nothing.
    voted = run_review_vote(ask, _Review("hold"), rules=RULES, usage_of=usage, hash_of=lambda: "h")
    assert not voted.ran and voted.outcome.action == "hold"
    # Unfunded on a close: no hold sample exists -> outcome None (the caller holds).
    samples = []
    voted = run_review_vote(ask, _Review("close"), rules=RULES, usage_of=usage, hash_of=lambda: "h", fund=lambda: False)
    assert voted.ran and voted.outcome is None and "not funded" in voted.failure


def test_the_vote_module_is_fenced_from_the_scoreboard():
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "vote.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for forbidden in ("audit.attribution", "audit.spend", "audit.log", "forward", "orchestrator.target"):
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in names), forbidden
