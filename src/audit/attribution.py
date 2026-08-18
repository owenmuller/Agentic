"""Weekly attribution by signal class.

CLAUDE.md § Audit & Attribution: "Weekly attribution report by signal class. Any
signal class with negative attribution over a rolling 60-90 day window is flagged for
human review and possible removal."

Two things this deliberately does *not* do. It does not remove a signal class — the
spec says flagged for human review, and a system that silently retires its own inputs
is one nobody is watching. And it does not treat an unresolved position as a zero: a
class with three open trades and no closes has no attribution yet, which is different
from having flat attribution, and the report says so rather than averaging the
distinction away.

Rejections are reported alongside P&L. A class whose orders are mostly rejected is
telling you something even if the ones that got through made money.

Feed costs are part of the verdict. A paid data feed is a position the class holds
permanently, and it loses every month the class does not out-earn it - so the report
carries each class's prorated feed cost, states P&L both gross and net, and the
human-review flag fires on NET. Proration is monthly_cost x window_days / 30; a
30-day month slightly overstates the cost, which errs toward flagging (Constraint #6
flavour: the doubtful direction goes against the feed, not for it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Optional

from audit.records import AuditTrail
from signals import SignalClass

ZERO = Decimal("0")
CENTS = Decimal("0.01")

#: Days a monthly feed price is spread over when prorating into a window.
PRORATION_MONTH_DAYS = 30

#: CLAUDE.md gives a 60-90 day range. The wider end is the default: a shorter window
#: flags a class on less evidence, and "possible removal" deserves the fuller sample.
DEFAULT_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class ClassAttribution:
    """One signal class's contribution over the window."""

    signal_class: SignalClass
    decisions: int
    approved: int
    rejected: int
    resolved: int
    wins: int
    #: Gross: realised trading P&L alone, before the feed bill.
    realised_pnl: Decimal
    manipulation_flags: int
    #: This class's data-feed cost, prorated to the window.
    feed_cost: Decimal = ZERO

    @property
    def hit_rate(self) -> Optional[float]:
        """None until something has actually closed."""
        if self.resolved <= 0:
            return None
        return self.wins / self.resolved

    @property
    def rejection_rate(self) -> Optional[float]:
        if self.decisions <= 0:
            return None
        return self.rejected / self.decisions

    @property
    def net_pnl(self) -> Decimal:
        """What the class actually contributed: gross P&L minus its feed bill."""
        return self.realised_pnl - self.feed_cost

    @property
    def is_negative(self) -> bool:
        """Negative attribution — only meaningful once something has resolved.

        Judged on NET: a class must out-earn its own feed, and gross-positive is not
        a pass if the subscription ate the gains. Still gated on resolved outcomes —
        a class with open trades and a feed bill has not been judged yet, only
        billed.
        """
        return self.resolved > 0 and self.net_pnl < ZERO

    def summary(self) -> str:
        if self.feed_cost > ZERO:
            pnl = (
                f"{self.realised_pnl:+.2f} gross, {self.feed_cost:.2f} feed cost, "
                f"{self.net_pnl:+.2f} net"
            )
        else:
            pnl = f"{self.realised_pnl:+.2f} realised (feed is free)"
        if self.resolved == 0:
            verdict = "no resolved outcomes yet"
        else:
            verdict = f"{self.hit_rate:.0%} hit rate over {self.resolved} closed"
        return (
            f"{self.signal_class}: {pnl}, {verdict}; "
            f"{self.approved} approved / {self.rejected} rejected of {self.decisions}"
        )


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """The weekly report. Flags are advisory — a human decides what to remove."""

    generated_at: datetime
    window_days: int
    window_start: datetime
    by_class: dict[SignalClass, ClassAttribution] = field(default_factory=dict)

    @property
    def total_pnl(self) -> Decimal:
        """Gross, across classes."""
        return sum((c.realised_pnl for c in self.by_class.values()), ZERO)

    @property
    def total_feed_cost(self) -> Decimal:
        return sum((c.feed_cost for c in self.by_class.values()), ZERO)

    @property
    def total_net_pnl(self) -> Decimal:
        return sum((c.net_pnl for c in self.by_class.values()), ZERO)

    @property
    def flagged_classes(self) -> tuple[SignalClass, ...]:
        """Classes NET-negative over the window. For human review, not auto-removal."""
        return tuple(
            signal_class
            for signal_class, attribution in sorted(self.by_class.items())
            if attribution.is_negative
        )

    def render(self) -> str:
        lines = [
            f"Attribution report — {self.window_days}d window "
            f"from {self.window_start.date()} to {self.generated_at.date()}",
            f"Total: {self.total_pnl:+.2f} gross, {self.total_feed_cost:.2f} feed "
            f"costs, {self.total_net_pnl:+.2f} net",
            "",
        ]
        for signal_class in sorted(self.by_class):
            lines.append(f"  {self.by_class[signal_class].summary()}")

        if self.flagged_classes:
            lines.extend(
                ["", "FLAGGED FOR HUMAN REVIEW (net-negative over the window):"]
            )
            for signal_class in self.flagged_classes:
                attribution = self.by_class[signal_class]
                lines.append(
                    f"  {signal_class}: {attribution.net_pnl:+.2f} net "
                    f"({attribution.realised_pnl:+.2f} gross less "
                    f"{attribution.feed_cost:.2f} feed cost) over "
                    f"{attribution.resolved} closed positions. CLAUDE.md calls for "
                    f"review and possible removal of this signal class."
                )
        else:
            lines.extend(["", "No signal class is net-negative over the window."])
        return "\n".join(lines)


def build_attribution(
    trails: list[AuditTrail],
    generated_at: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    feed_costs: Optional[Mapping[SignalClass, Decimal]] = None,
) -> AttributionReport:
    """Compute attribution from audit trails.

    A trail counts toward the window by its *decision* time — when the system committed
    — rather than when the position happened to close. Attributing a trade to the week
    it was closed would credit a signal class for timing that belongs to the exit.
    """
    if not 60 <= window_days <= 90:
        raise ValueError(
            f"window must be 60-90 days per CLAUDE.md, got {window_days}"
        )

    window_start = generated_at - timedelta(days=window_days)
    buckets: dict[SignalClass, dict[str, object]] = {}

    def empty_bucket() -> dict[str, object]:
        return {
            "decisions": 0,
            "approved": 0,
            "rejected": 0,
            "resolved": 0,
            "wins": 0,
            "pnl": ZERO,
            "flags": 0,
        }

    # A paid class appears even when it made no decisions: the bill does not wait
    # for the signals, and a silent month of feed cost belongs in the report.
    for signal_class, monthly in (feed_costs or {}).items():
        if monthly > ZERO:
            buckets.setdefault(signal_class, empty_bucket())

    for trail in trails:
        if trail.decision.recorded_at < window_start:
            continue
        bucket = buckets.setdefault(trail.signal_class, empty_bucket())
        bucket["decisions"] += 1  # type: ignore[operator]
        if trail.decision.was_approved:
            bucket["approved"] += 1  # type: ignore[operator]
        else:
            bucket["rejected"] += 1  # type: ignore[operator]
        if trail.decision.research.flagged_manipulation:
            bucket["flags"] += 1  # type: ignore[operator]
        if trail.outcome is not None:
            bucket["resolved"] += 1  # type: ignore[operator]
            bucket["pnl"] += trail.outcome.realised_pnl  # type: ignore[operator]
            if trail.outcome.won:
                bucket["wins"] += 1  # type: ignore[operator]

    def prorated_cost(signal_class: SignalClass) -> Decimal:
        monthly = (feed_costs or {}).get(signal_class, ZERO)
        if monthly <= ZERO:
            return ZERO
        cost = monthly * Decimal(window_days) / Decimal(PRORATION_MONTH_DAYS)
        return cost.quantize(CENTS)

    by_class = {
        signal_class: ClassAttribution(
            signal_class=signal_class,
            decisions=int(values["decisions"]),  # type: ignore[arg-type]
            approved=int(values["approved"]),  # type: ignore[arg-type]
            rejected=int(values["rejected"]),  # type: ignore[arg-type]
            resolved=int(values["resolved"]),  # type: ignore[arg-type]
            wins=int(values["wins"]),  # type: ignore[arg-type]
            realised_pnl=values["pnl"],  # type: ignore[arg-type]
            manipulation_flags=int(values["flags"]),  # type: ignore[arg-type]
            feed_cost=prorated_cost(signal_class),
        )
        for signal_class, values in buckets.items()
    }

    return AttributionReport(
        generated_at=generated_at,
        window_days=window_days,
        window_start=window_start,
        by_class=by_class,
    )
