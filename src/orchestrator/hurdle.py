"""The horizon-adjusted, opportunity-cost-aware reward hurdle (ADAPTIVE STANDARDS
ruling 2026-10-06). Deterministic, computed in the orchestrator, reachable by no
LLM call, and — the hard constraint — a function of the OPPORTUNITY SET only:

    annualized_return  = expected_move / (days_to_resolution / 365)
    effective_hurdle   = base_hurdle × (1 + k × u)

where ``u`` in [0, 1] is how contested judged capital is right now: the larger
of the judged sleeve's deployed fraction and today's queue pressure (positive-
scored candidates waiting over free position slots). A long passes when its
annualized expected return clears the effective hurdle AND its absolute
reward:risk clears the floor. Realized P&L, distance from any return target and
elapsed time without a trade are not inputs here and may never become inputs
(CLAUDE.md, the scoreboard constraint; ``tests/test_topology.py`` proves the
import graph).

Why annualize: a 2% move expected inside five days is worth more than a 2% move
expected in nine months — capital turns over — and the flat 1.3 reward:risk
floor treated the two identically. The replay that motivated the change: 20
reward:risk rejections since 2026-09-02 spanned −102% to +171% annualized; 7
cleared 40% with an absolute floor of 0.8, all of them the fast ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional

ZERO = Decimal("0")
ONE = Decimal("1")
YEAR_DAYS = Decimal("365")


@dataclass(frozen=True, slots=True)
class Opportunity:
    """The opportunity set at decision time — the ONLY thing allowed to move the bar."""

    #: Judged sleeve capital deployed / judged sleeve NAV, in [0, 1].
    deployed_fraction: Decimal
    #: Candidates in today's dispatch queue with a positive dispatch score.
    positive_candidates: int
    #: Judged positions currently open.
    open_positions: int
    #: The position-count target the ruling named (15–25; 20 by default).
    target_positions: int = 20

    @property
    def queue_pressure(self) -> Decimal:
        free = max(1, self.target_positions - self.open_positions)
        return min(ONE, Decimal(self.positive_candidates) / Decimal(free))

    @property
    def u(self) -> Decimal:
        deployed = min(ONE, max(ZERO, self.deployed_fraction))
        return max(deployed, self.queue_pressure)


@dataclass(frozen=True, slots=True)
class HurdleReading:
    base: Decimal
    k: Decimal
    u: Decimal
    effective: Decimal
    rr_floor: Decimal

    def line(self) -> str:
        return (
            f"hurdle {self.effective:.0%} annualized (base {self.base:.0%} × (1 + {self.k} × u={self.u:.2f})), "
            f"absolute reward:risk floor {self.rr_floor}"
        )


def effective_hurdle(base: Decimal, k: Decimal, opportunity: Optional[Opportunity], rr_floor: Decimal) -> HurdleReading:
    u = opportunity.u if opportunity is not None else ZERO
    return HurdleReading(base=base, k=k, u=u, effective=(base * (ONE + k * u)), rr_floor=rr_floor)


def days_to_resolution(
    resolution_date: Optional[date],
    observed: date,
    horizon: str,
    floor_days: int,
    ceiling_days: int,
    fallback_days: int,
) -> int:
    """Days the thesis asks for: the report's own resolution date clamped into
    the leash bounds for the horizon and class (the exit engine's rule), the
    horizon's time-stop fallback when the report named none."""
    if resolution_date is not None:
        raw = (resolution_date - observed).days
        if raw > 0:
            return max(floor_days, min(ceiling_days, raw))
    return max(floor_days, min(ceiling_days, fallback_days))


def annualized_return(target: Decimal, entry: Decimal, days: int) -> Decimal:
    """Expected move on capital, annualized by simple scaling (no compounding —
    the figure is a hurdle, not a forecast)."""
    if entry <= 0 or days <= 0:
        return ZERO
    return (target - entry) / entry * (YEAR_DAYS / Decimal(days))


@dataclass(frozen=True, slots=True)
class HurdleVerdict:
    passed: bool
    reward_risk: Decimal
    annualized: Decimal
    days: int
    reading: HurdleReading
    reason: str


def judge(
    *,
    target: Decimal,
    entry: Decimal,
    stop_fraction: Decimal,
    days: int,
    reading: HurdleReading,
) -> HurdleVerdict:
    """Both tests, veto-only: the absolute floor guards against a target so
    close to the stop that any annualization flatters it; the annualized
    hurdle prices turnover."""
    risk = entry * stop_fraction
    reward = target - entry
    rr = (reward / risk) if risk > 0 else ZERO
    ann = annualized_return(target, entry, days)
    if rr < reading.rr_floor:
        return HurdleVerdict(
            False, rr, ann, days, reading,
            f"reward:risk {rr:.2f} below the absolute floor {reading.rr_floor}: target {target} vs entry {entry} "
            f"with a {stop_fraction:.2%} stop risks {risk:.2f} to make {reward:.2f} (ruling 2026-10-06)",
        )
    if ann < reading.effective:
        return HurdleVerdict(
            False, rr, ann, days, reading,
            f"annualized expected return {ann:.0%} over {days}d below the {reading.effective:.0%} hurdle "
            f"({reading.line()}; reward:risk {rr:.2f} cleared the floor) (ruling 2026-10-06)",
        )
    return HurdleVerdict(
        True, rr, ann, days, reading,
        f"cleared: annualized {ann:.0%} over {days}d vs hurdle {reading.effective:.0%}, reward:risk {rr:.2f}",
    )
