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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from audit.records import AuditTrail
from signals import SignalClass

ZERO = Decimal("0")

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
    realised_pnl: Decimal
    manipulation_flags: int

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
    def is_negative(self) -> bool:
        """Negative attribution — only meaningful once something has resolved."""
        return self.resolved > 0 and self.realised_pnl < ZERO

    def summary(self) -> str:
        pnl = f"{self.realised_pnl:+.2f}"
        if self.resolved == 0:
            verdict = "no resolved outcomes yet"
        else:
            verdict = f"{self.hit_rate:.0%} hit rate over {self.resolved} closed"
        return (
            f"{self.signal_class}: {pnl} realised, {verdict}; "
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
        return sum((c.realised_pnl for c in self.by_class.values()), ZERO)

    @property
    def flagged_classes(self) -> tuple[SignalClass, ...]:
        """Classes negative over the window. For human review, not auto-removal."""
        return tuple(
            signal_class
            for signal_class, attribution in sorted(self.by_class.items())
            if attribution.is_negative
        )

    def render(self) -> str:
        lines = [
            f"Attribution report — {self.window_days}d window "
            f"from {self.window_start.date()} to {self.generated_at.date()}",
            f"Total realised P&L: {self.total_pnl:+.2f}",
            "",
        ]
        for signal_class in sorted(self.by_class):
            lines.append(f"  {self.by_class[signal_class].summary()}")

        if self.flagged_classes:
            lines.extend(["", "FLAGGED FOR HUMAN REVIEW (negative over the window):"])
            for signal_class in self.flagged_classes:
                attribution = self.by_class[signal_class]
                lines.append(
                    f"  {signal_class}: {attribution.realised_pnl:+.2f} over "
                    f"{attribution.resolved} closed positions. CLAUDE.md calls for "
                    f"review and possible removal of this signal class."
                )
        else:
            lines.extend(["", "No signal class is negative over the window."])
        return "\n".join(lines)


def build_attribution(
    trails: list[AuditTrail],
    generated_at: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
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

    for trail in trails:
        if trail.decision.recorded_at < window_start:
            continue
        bucket = buckets.setdefault(
            trail.signal_class,
            {
                "decisions": 0,
                "approved": 0,
                "rejected": 0,
                "resolved": 0,
                "wins": 0,
                "pnl": ZERO,
                "flags": 0,
            },
        )
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
        )
        for signal_class, values in buckets.items()
    }

    return AttributionReport(
        generated_at=generated_at,
        window_days=window_days,
        window_start=window_start,
        by_class=by_class,
    )
