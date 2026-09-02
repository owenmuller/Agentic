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
carries each class's feed cost for the window, states P&L both gross and net, and the
human-review flag fires on NET.

The proration itself belongs to ``signals.config`` (2026-08-28), because it needs
each source's start date: a subscription that has existed for two weeks must not be
charged for a 90-day window, or the keep-or-cut flag fires on months the experiment
was never running. This module takes dollars already computed for the window and
never re-derives them from a monthly rate it cannot date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping, Optional

from audit.records import AuditTrail, ExitReason
from signals import SignalClass

ZERO = Decimal("0")
CENTS = Decimal("0.01")

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
    #: This class's data-feed cost over the window, billed from each source's
    #: start date (signals.config.feed_cost_for_window).
    feed_cost: Decimal = ZERO
    #: Estimated LLM research spend attributed to this class over the window —
    #: entry passes and thesis reviews, from the audit records' cost estimates.
    research_cost: Decimal = ZERO
    #: Cost basis actually deployed into RESOLVED positions (buy-fill value), the
    #: denominator that turns P&L into a return comparable to a benchmark.
    deployed: Decimal = ZERO
    #: The benchmark's (SPY) total return over the same window, in percent.
    #: None when the benchmark could not be fetched — absent, never guessed.
    benchmark_return_pct: Optional[Decimal] = None

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
    def return_pct(self) -> Optional[Decimal]:
        """Gross return on deployed capital, percent. None until capital resolved."""
        if self.deployed <= ZERO:
            return None
        return (self.realised_pnl / self.deployed * 100).quantize(CENTS)

    @property
    def excess_return_pct(self) -> Optional[Decimal]:
        """Return over the benchmark — alpha, not a bull market. None when either
        side is unknown: an excess return needs both a return and a benchmark."""
        ours = self.return_pct
        if ours is None or self.benchmark_return_pct is None:
            return None
        return (ours - self.benchmark_return_pct).quantize(CENTS)

    @property
    def net_pnl(self) -> Decimal:
        """What the class actually contributed: gross P&L minus its feed bill and
        the research spend it caused. Net of ALL costs — the keep/cut flag fires
        on this number."""
        return self.realised_pnl - self.feed_cost - self.research_cost

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
        if self.feed_cost > ZERO or self.research_cost > ZERO:
            pnl = (
                f"{self.realised_pnl:+.2f} gross, {self.feed_cost:.2f} feed cost, "
                f"{self.research_cost:.2f} research cost, {self.net_pnl:+.2f} net"
            )
        else:
            pnl = f"{self.realised_pnl:+.2f} realised (no feed or research costs)"
        if self.resolved == 0:
            verdict = "no resolved outcomes yet"
        else:
            verdict = f"{self.hit_rate:.0%} hit rate over {self.resolved} closed"
        excess = self.excess_return_pct
        if excess is not None:
            verdict += (
                f"; {self.return_pct:+.2f}% on {self.deployed:.2f} deployed, "
                f"{excess:+.2f}% vs SPY"
            )
        return (
            f"{self.signal_class}: {pnl}, {verdict}; "
            f"{self.approved} approved / {self.rejected} rejected of {self.decisions}"
        )


@dataclass(frozen=True, slots=True)
class MechanicalAttribution:
    """The mechanical sleeve's own bucket (ruling 2026-08-27) — never mixed
    into a signal class, so the experiment's arms stay comparable."""

    entries: int
    approved: int
    rejected: int
    resolved: int
    wins: int
    realised_pnl: Decimal
    deployed: Decimal
    open_positions: int
    #: Symbols both sleeves bought inside the window — overlap is allowed by
    #: ruling, and this is where its cost is measured rather than assumed.
    overlap_symbols: tuple[str, ...] = ()

    @property
    def hit_rate(self) -> Optional[float]:
        if self.resolved <= 0:
            return None
        return self.wins / self.resolved

    @property
    def return_pct(self) -> Optional[Decimal]:
        if self.deployed <= ZERO:
            return None
        return (self.realised_pnl / self.deployed * 100).quantize(CENTS)

    def summary(self) -> str:
        if self.resolved == 0:
            verdict = f"no resolved outcomes yet; {self.open_positions} open"
        else:
            verdict = (
                f"{self.hit_rate:.0%} hit rate over {self.resolved} closed; "
                f"{self.return_pct:+.2f}% on {self.deployed:.2f} deployed; "
                f"{self.open_positions} open"
            )
        overlap = (
            f"; overlap with judged: {', '.join(self.overlap_symbols)}"
            if self.overlap_symbols
            else "; no judged overlap"
        )
        return (
            f"mechanical: {self.realised_pnl:+.2f} realised, {verdict}; "
            f"{self.approved} approved / {self.rejected} rejected of "
            f"{self.entries}{overlap}"
        )


@dataclass(frozen=True, slots=True)
class ExitReasonAttribution:
    """P&L of the judged sleeve grouped by WHY each position closed (2026-08-31).

    The adaptive exit layer made the exit a decision rather than a clock, so the
    experiment has to be able to ask whether that decision paid. If review-driven
    exits underperform the time stop, this is where it shows.
    """

    reason: str
    closed: int
    wins: int
    realised_pnl: Decimal
    deployed: Decimal

    @property
    def hit_rate(self) -> Optional[float]:
        return self.wins / self.closed if self.closed else None

    @property
    def return_pct(self) -> Optional[Decimal]:
        if self.deployed <= ZERO:
            return None
        return (self.realised_pnl / self.deployed * 100).quantize(CENTS)

    def summary(self) -> str:
        verdict = (
            f"{self.hit_rate:.0%} of {self.closed}" if self.closed else "none closed"
        )
        ret = f", {self.return_pct:+.2f}% on {self.deployed:.2f}" if self.return_pct is not None else ""
        return f"{self.reason}: {self.realised_pnl:+.2f} over {verdict}{ret}"


@dataclass(frozen=True, slots=True)
class CashManagementAttribution:
    """The sweep's own line (ruling 2026-09-02): what parked cash earned.

    Never a signal class and never alpha — the counterfactual is idle cash at
    exactly $0.00, so the accrual is the whole story. Realised P&L from lots
    sold flat plus the mark-to-market on what is still parked, against cost."""

    symbol: str
    #: Cost of every sweep buy fill in the log.
    deployed: Decimal
    #: Proceeds of every unsweep sell fill.
    proceeds: Decimal
    #: Units still parked.
    open_units: Decimal
    #: A recent close for the open units. None = no mark available.
    mark: Optional[Decimal]

    @property
    def accrual(self) -> Optional[Decimal]:
        """Yield captured: (proceeds + open value) - cost. None when open units
        exist but no mark does — absent, never guessed."""
        if self.open_units > ZERO and self.mark is None:
            return None
        open_value = (
            self.open_units * self.mark if self.mark is not None else ZERO
        )
        return (self.proceeds + open_value - self.deployed).quantize(CENTS)

    def summary(self) -> str:
        if self.accrual is None:
            return (
                f"cash management ({self.symbol}): {self.deployed:.2f} parked, "
                f"accrual unavailable (no recent mark for {self.open_units} "
                f"open units)"
            )
        return (
            f"cash management ({self.symbol}): {self.accrual:+.2f} accrued on "
            f"{self.deployed:.2f} parked ({self.open_units} units still held; "
            f"idle-cash counterfactual is exactly 0.00)"
        )


#: Below this many resolved positions a calibration cell is noise, and the report
#: says "insufficient" instead of printing a hit rate someone might tune on
#: (human ruling 2026-09-01).
CALIBRATION_MIN_N = 20

#: The sizing table's own band edges (risk_limits.yaml), labelled the way the
#: table reads: the floor band is lower-inclusive, every band above is
#: (lower, upper]. Calibration buckets match the table because the question is
#: whether THE TABLE's bands are honest about their hit rates.
_CALIBRATION_BANDS = (
    ("<50 (no-trade zone)", lambda c: c < 50),
    ("50-70 (1% band)", lambda c: 50 <= c <= 70),
    ("70-85 (2.5% band)", lambda c: 70 < c <= 85),
    ("85+ (7% band)", lambda c: c > 85),
)


@dataclass(frozen=True, slots=True)
class ConfidenceBandCalibration:
    """Hit rate of one confidence band, over all resolved judged positions.

    Calibration of the EXISTING confidence field (ruling 2026-09-01, upgrade 2a):
    does 85+ actually win more often than 50-70? Computed since inception rather
    than windowed — calibration is a property of the scorer, not of a quarter —
    and rendered "insufficient" below ``CALIBRATION_MIN_N`` so nobody tunes the
    sizing table on four data points.
    """

    band: str
    resolved: int
    wins: int
    realised_pnl: Decimal

    @property
    def hit_rate(self) -> Optional[float]:
        return self.wins / self.resolved if self.resolved else None

    def summary(self) -> str:
        if self.resolved < CALIBRATION_MIN_N:
            detail = f"insufficient (n={self.resolved}"
            if self.resolved:
                detail += f", {self.wins} won, {self.realised_pnl:+.2f} realised"
            return f"{self.band}: {detail})"
        return (
            f"{self.band}: {self.hit_rate:.0%} hit rate over {self.resolved} "
            f"closed, {self.realised_pnl:+.2f} realised"
        )


def _calibration(trails: list[AuditTrail]) -> tuple[ConfidenceBandCalibration, ...]:
    """Judged, resolved, research-carrying trails only — the mechanical arm has no
    confidence to calibrate, and an open position has no outcome to score."""
    rows: list[ConfidenceBandCalibration] = []
    for band, contains in _CALIBRATION_BANDS:
        resolved = wins = 0
        pnl = ZERO
        for trail in trails:
            research = trail.decision.research
            if research is None or trail.outcome is None:
                continue
            if not contains(research.confidence):
                continue
            resolved += 1
            pnl += trail.outcome.realised_pnl
            if trail.outcome.won:
                wins += 1
        if resolved:
            rows.append(
                ConfidenceBandCalibration(
                    band=band, resolved=resolved, wins=wins, realised_pnl=pnl
                )
            )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class CounterfactualHold:
    """What one judged exit would have returned held to the mechanical arm's clock.

    The two-arm experiment varies judgement and exits. This isolates the second
    half: for every judged position that closed, what the same entry would have
    made held 365 days, the way the mechanical sleeve holds. ``partial`` marks the
    ones whose 365th day has not arrived — those compare against today's price and
    are a progress report, not a verdict.
    """

    decision_id: str
    symbol: str
    exit_reason: str
    realised_return_pct: Optional[Decimal]
    held_return_pct: Optional[Decimal]
    partial: bool

    @property
    def difference_pct(self) -> Optional[Decimal]:
        """Judged exit minus hold-to-365. Positive means the exit added value."""
        if self.realised_return_pct is None or self.held_return_pct is None:
            return None
        return (self.realised_return_pct - self.held_return_pct).quantize(CENTS)

    def summary(self) -> str:
        if self.difference_pct is None:
            return (
                f"{self.symbol} ({self.exit_reason}): comparison unavailable "
                f"(no price history for the counterfactual)"
            )
        marker = " [partial: 365 days not yet elapsed]" if self.partial else ""
        return (
            f"{self.symbol} ({self.exit_reason}): exited {self.realised_return_pct:+.2f}%, "
            f"holding would have made {self.held_return_pct:+.2f}%, "
            f"exit {self.difference_pct:+.2f}%{marker}"
        )


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """The weekly report. Flags are advisory — a human decides what to remove."""

    generated_at: datetime
    window_days: int
    window_start: datetime
    by_class: dict[SignalClass, ClassAttribution] = field(default_factory=dict)
    #: SPY total return over the window, percent. None when unavailable.
    benchmark_return_pct: Optional[Decimal] = None
    #: Estimated research spend this calendar month, across all classes — a
    #: shorter horizon than the window costs above, for bill anticipation.
    mtd_research_cost: Optional[Decimal] = None
    #: The mechanical sleeve's bucket; None when the sleeve has no history.
    mechanical: Optional[MechanicalAttribution] = None
    #: The idle-cash sweep's own line (2026-09-02); None when nothing swept.
    cash_management: Optional[CashManagementAttribution] = None
    #: Judged-sleeve P&L grouped by exit reason (2026-08-31).
    by_exit_reason: tuple["ExitReasonAttribution", ...] = ()
    #: Hit rate per sizing-table confidence band, since inception (2026-09-01).
    calibration: tuple["ConfidenceBandCalibration", ...] = ()
    #: Per judged exit: realised vs held-to-365-days, the mechanical arm's clock.
    counterfactuals: tuple["CounterfactualHold", ...] = ()
    #: One line per paid source: what it costs monthly, when its bill started,
    #: and how many days of it this window actually bought. Rendered so the feed
    #: total is auditable rather than asserted (2026-08-28).
    feed_cost_detail: tuple[str, ...] = ()
    #: Dollars the post-table risk scalars (drawdown ladder x regime, rulings
    #: 2026-09-01/02) shaved off judged entries this window, and how many
    #: entries they touched. The weekly forgone-size line.
    scalar_forgone: Decimal = ZERO
    scalar_scaled_entries: int = 0

    @property
    def total_pnl(self) -> Decimal:
        """Gross, across classes."""
        return sum((c.realised_pnl for c in self.by_class.values()), ZERO)

    @property
    def total_feed_cost(self) -> Decimal:
        return sum((c.feed_cost for c in self.by_class.values()), ZERO)

    @property
    def total_research_cost(self) -> Decimal:
        return sum((c.research_cost for c in self.by_class.values()), ZERO)

    @property
    def total_deployed(self) -> Decimal:
        return sum((c.deployed for c in self.by_class.values()), ZERO)

    @property
    def total_excess_return_pct(self) -> Optional[Decimal]:
        """Overall alpha vs SPY. None without a benchmark or deployed capital."""
        if self.benchmark_return_pct is None or self.total_deployed <= ZERO:
            return None
        ours = (self.total_pnl / self.total_deployed * 100).quantize(CENTS)
        return (ours - self.benchmark_return_pct).quantize(CENTS)

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
            f"costs, {self.total_research_cost:.2f} research costs, "
            f"{self.total_net_pnl:+.2f} net",
        ]
        if self.mtd_research_cost is not None:
            lines.append(
                f"Research cost month-to-date: ${self.mtd_research_cost:.2f} "
                f"(estimates; console bill is truth)"
            )
        if self.by_exit_reason:
            lines.extend(
                [
                    "",
                    "Judged exits by reason (did the exit decision pay?):",
                    *(f"  {row.summary()}" for row in self.by_exit_reason),
                ]
            )
        if self.calibration:
            lines.extend(
                [
                    "",
                    "Confidence calibration (all resolved judged positions since "
                    f"inception; cells under n={CALIBRATION_MIN_N} are insufficient "
                    "and must not tune the sizing table):",
                    *(f"  {row.summary()}" for row in self.calibration),
                ]
            )
        if self.counterfactuals:
            lines.extend(
                [
                    "",
                    "Counterfactual: each judged exit vs holding 365 days (the "
                    "mechanical arm's clock):",
                    *(f"  {row.summary()}" for row in self.counterfactuals),
                ]
            )
            comparable = [
                row.difference_pct
                for row in self.counterfactuals
                if row.difference_pct is not None
            ]
            if comparable:
                total = sum(comparable, ZERO) / Decimal(len(comparable))
                verdict = "added" if total > ZERO else "cost"
                lines.append(
                    f"  mean: exiting {verdict} {abs(total):.2f} percentage points "
                    f"per position over {len(comparable)} comparisons"
                )
        if self.mechanical is not None and self.mechanical.overlap_symbols:
            lines.extend(
                [
                    "",
                    "Sleeve overlap (both arms hold these; allowed by ruling and "
                    "measured here rather than assumed):",
                    f"  {', '.join(self.mechanical.overlap_symbols)}",
                ]
            )
        lines.extend(
            [
                "",
                "Risk scalars (drawdown ladder x regime, rulings 2026-09-01/02): "
                + (
                    f"forgone ${self.scalar_forgone:.2f} across "
                    f"{self.scalar_scaled_entries} scaled judged entries this window"
                    if self.scalar_scaled_entries
                    else "no judged entry was scaled this window (x1.0 throughout)"
                ),
            ]
        )
        if self.feed_cost_detail:
            lines.extend(
                [
                    "",
                    "Feed costs, billed from each source's start date (a source "
                    "younger than the window is charged only for the days it ran):",
                    *(f"  {detail}" for detail in self.feed_cost_detail),
                ]
            )
        if self.benchmark_return_pct is not None:
            benchmark_line = (
                f"Benchmark: SPY {self.benchmark_return_pct:+.2f}% over the window"
            )
            excess = self.total_excess_return_pct
            if excess is not None:
                benchmark_line += (
                    f"; portfolio excess return {excess:+.2f}% "
                    f"(a bull market must not flatter a signal class)"
                )
            lines.append(benchmark_line)
        else:
            lines.append(
                "Benchmark: SPY return unavailable for this window — excess "
                "returns not computed"
            )
        lines.append("")
        for signal_class in sorted(self.by_class):
            lines.append(f"  {self.by_class[signal_class].summary()}")
        if self.mechanical is not None:
            lines.append(f"  {self.mechanical.summary()}")
        if self.cash_management is not None:
            lines.append(f"  {self.cash_management.summary()}")

        if self.flagged_classes:
            lines.extend(
                ["", "FLAGGED FOR HUMAN REVIEW (net-negative over the window):"]
            )
            for signal_class in self.flagged_classes:
                attribution = self.by_class[signal_class]
                lines.append(
                    f"  {signal_class}: {attribution.net_pnl:+.2f} net "
                    f"({attribution.realised_pnl:+.2f} gross less "
                    f"{attribution.feed_cost:.2f} feed cost less "
                    f"{attribution.research_cost:.2f} research cost) over "
                    f"{attribution.resolved} closed positions. CLAUDE.md calls for "
                    f"review and possible removal of this signal class."
                )
        else:
            lines.extend(["", "No signal class is net-negative over the window."])
        return "\n".join(lines)


#: The mechanical sleeve's holding period, and the counterfactual's horizon.
#: 365 -> 367 with the tax ruling (2026-09-02), in step with risk_limits.yaml.
MECHANICAL_HOLD_DAYS = 367


def _closing_reason(trail: AuditTrail) -> str:
    """Why this position actually closed.

    The LAST exit attempt that the broker took is the one that closed it; earlier
    records are retries and refusals, which are part of the story but not the
    answer. A trail with no submitted exit closed some other way (a manual close,
    or a fill reconciliation) and says so rather than guessing.
    """
    for exit_record in reversed(trail.exits):
        if exit_record.submitted:
            return str(exit_record.reason)
    if trail.exits:
        return str(trail.exits[-1].reason)
    return "unrecorded"


def _by_exit_reason(trails: list[AuditTrail]) -> tuple[ExitReasonAttribution, ...]:
    buckets: dict[str, dict[str, object]] = {}
    for trail in trails:
        reason = _closing_reason(trail)
        bucket = buckets.setdefault(
            reason, {"closed": 0, "wins": 0, "pnl": ZERO, "deployed": ZERO}
        )
        bucket["closed"] += 1  # type: ignore[operator]
        if trail.outcome is not None:
            bucket["pnl"] += trail.outcome.realised_pnl  # type: ignore[operator]
            if trail.outcome.won:
                bucket["wins"] += 1  # type: ignore[operator]
        bucket["deployed"] += sum(  # type: ignore[operator]
            (f.filled_value for f in trail.fills if f.side == "buy"), ZERO
        )
    return tuple(
        ExitReasonAttribution(
            reason=reason,
            closed=int(values["closed"]),  # type: ignore[arg-type]
            wins=int(values["wins"]),  # type: ignore[arg-type]
            realised_pnl=values["pnl"],  # type: ignore[arg-type]
            deployed=values["deployed"],  # type: ignore[arg-type]
        )
        for reason, values in sorted(buckets.items())
    )


def _counterfactuals(
    trails: list[AuditTrail], generated_at: datetime, price_on
) -> tuple[CounterfactualHold, ...]:
    """What each judged exit would have made held to the mechanical arm's clock.

    ``price_on(symbol, when)`` returns a close on or near a date, or None. Without
    it — no market data wired, or an outage — every comparison is simply absent.
    An absent comparison is reported as absent; none of this is worth a guessed
    price.
    """
    if price_on is None:
        return ()
    out: list[CounterfactualHold] = []
    for trail in trails:
        buys = [f for f in trail.fills if f.side == "buy"]
        if not buys or trail.outcome is None:
            continue
        symbol = str((trail.decision.gate.order or {}).get("symbol") or "")
        if not symbol:
            continue
        opened = buys[0].recorded_at
        cost = sum((f.filled_value for f in buys), ZERO)
        realised = (
            (trail.outcome.realised_pnl / cost * 100).quantize(CENTS)
            if cost > ZERO
            else None
        )
        target = opened + timedelta(days=MECHANICAL_HOLD_DAYS)
        partial = target > generated_at
        entry_price = price_on(symbol, opened)
        later_price = price_on(symbol, min(target, generated_at))
        held = None
        if entry_price and later_price and entry_price > ZERO:
            held = ((later_price / entry_price - 1) * 100).quantize(CENTS)
        out.append(
            CounterfactualHold(
                decision_id=trail.decision.decision_id,
                symbol=symbol,
                exit_reason=_closing_reason(trail),
                realised_return_pct=realised,
                held_return_pct=held,
                partial=partial,
            )
        )
    return tuple(out)


def build_attribution(
    trails: list[AuditTrail],
    generated_at: datetime,
    window_days: int = DEFAULT_WINDOW_DAYS,
    feed_costs_for_window: Optional[Mapping[SignalClass, Decimal]] = None,
    research_costs: Optional[Mapping[SignalClass, Decimal]] = None,
    benchmark_return_pct: Optional[Decimal] = None,
    mtd_research_cost: Optional[Decimal] = None,
    feed_cost_detail: tuple[str, ...] = (),
    price_on=None,
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
    # The mechanical sleeve's trails are partitioned out first: they carry no
    # research snapshot and belong to their own bucket, never a signal class.
    # Same for the cash sweep (2026-09-02) — parked cash is not a signal.
    mechanical_trails = [
        t for t in trails if t.decision.sizing.strategy == "mechanical"
    ]
    sweep_trails = [
        t for t in trails if t.decision.sizing.strategy == "cash_sweep"
    ]
    trails = [
        t
        for t in trails
        if t.decision.sizing.strategy not in ("mechanical", "cash_sweep")
    ]
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
            "deployed": ZERO,
        }

    # A paid class appears even when it made no decisions: the bill does not wait
    # for the signals, and a silent month of feed cost belongs in the report.
    for signal_class, billed in (feed_costs_for_window or {}).items():
        if billed > ZERO:
            buckets.setdefault(signal_class, empty_bucket())
    # Likewise research spend: a class whose every pass was rejected pre-gate has
    # no trails, but its LLM bill is real and belongs in its column.
    for signal_class, spent in (research_costs or {}).items():
        if spent > ZERO:
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
        if trail.decision.research is not None and trail.decision.research.flagged_manipulation:
            bucket["flags"] += 1  # type: ignore[operator]
        if trail.outcome is not None:
            bucket["resolved"] += 1  # type: ignore[operator]
            bucket["pnl"] += trail.outcome.realised_pnl  # type: ignore[operator]
            if trail.outcome.won:
                bucket["wins"] += 1  # type: ignore[operator]
            bucket["deployed"] += sum(  # type: ignore[operator]
                (f.filled_value for f in trail.fills if f.side == "buy"), ZERO
            )

    # Risk-scalar forgone size (rulings 2026-09-01/02): every judged proposal a
    # post-table scalar shrank carries the table's own dollars, and the weekly
    # line prices the difference. Judged trails only — the scalars never touch
    # the mechanical arm or the sweep by construction.
    scalar_forgone = ZERO
    scalar_scaled_entries = 0
    for trail in trails:
        if trail.decision.recorded_at < window_start:
            continue
        sizing = trail.decision.sizing
        if sizing.table_capital is not None and sizing.table_capital > sizing.capital:
            scalar_forgone += sizing.table_capital - sizing.capital
            scalar_scaled_entries += 1

    mechanical = None
    windowed = [
        t for t in mechanical_trails if t.decision.recorded_at >= window_start
    ]
    if windowed or mechanical_trails:
        judged_bought = {
            str((t.decision.gate.order or {}).get("symbol", ""))
            for t in trails
            if t.decision.recorded_at >= window_start and t.decision.was_approved
        }
        mech_bought = {
            str((t.decision.gate.order or {}).get("symbol", ""))
            for t in windowed
            if t.decision.was_approved
        }
        open_now = sum(
            1
            for t in mechanical_trails
            if t.decision.was_approved
            and t.outcome is None
            and any(f.side == "buy" for f in t.fills)
        )
        mechanical = MechanicalAttribution(
            entries=len(windowed),
            approved=sum(1 for t in windowed if t.decision.was_approved),
            rejected=sum(1 for t in windowed if not t.decision.was_approved),
            resolved=sum(1 for t in windowed if t.outcome is not None),
            wins=sum(
                1 for t in windowed if t.outcome is not None and t.outcome.won
            ),
            realised_pnl=sum(
                (t.outcome.realised_pnl for t in windowed if t.outcome is not None),
                ZERO,
            ),
            deployed=sum(
                (
                    f.filled_value
                    for t in windowed
                    if t.outcome is not None
                    for f in t.fills
                    if f.side == "buy"
                ),
                ZERO,
            ),
            open_positions=open_now,
            overlap_symbols=tuple(
                sorted((judged_bought & mech_bought) - {""})
            ),
        )

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
            feed_cost=(feed_costs_for_window or {}).get(signal_class, ZERO),
            research_cost=(research_costs or {}).get(signal_class, ZERO),
            deployed=values["deployed"],  # type: ignore[arg-type]
            benchmark_return_pct=benchmark_return_pct,
        )
        for signal_class, values in buckets.items()
    }

    cash_management = None
    if sweep_trails:
        deployed = proceeds = units = ZERO
        symbol = ""
        for trail in sweep_trails:
            symbol = str((trail.decision.gate.order or {}).get("symbol") or symbol)
            for fill in trail.fills:
                if fill.side == "buy":
                    deployed += fill.filled_value
                    units += fill.filled_quantity
                else:
                    proceeds += fill.filled_value
                    units -= fill.filled_quantity
        mark = None
        if price_on is not None and symbol:
            # The freshest close available: try near today first, walk back.
            for days_back in (1, 2, 4, 6):
                mark = price_on(symbol, generated_at - timedelta(days=days_back))
                if mark is not None:
                    break
        cash_management = CashManagementAttribution(
            symbol=symbol or "?",
            deployed=deployed,
            proceeds=proceeds,
            open_units=units,
            mark=mark,
        )

    judged_closed = [
        t
        for t in trails
        if t.outcome is not None and t.decision.recorded_at >= window_start
    ]
    return AttributionReport(
        by_exit_reason=_by_exit_reason(judged_closed),
        # Since inception on purpose: calibration measures the scorer, not the
        # quarter, and windowing it would reset the sample every 90 days.
        calibration=_calibration(trails),
        counterfactuals=_counterfactuals(judged_closed, generated_at, price_on),
        generated_at=generated_at,
        window_days=window_days,
        window_start=window_start,
        by_class=by_class,
        benchmark_return_pct=benchmark_return_pct,
        mtd_research_cost=mtd_research_cost,
        mechanical=mechanical,
        cash_management=cash_management,
        feed_cost_detail=feed_cost_detail,
        scalar_forgone=scalar_forgone,
        scalar_scaled_entries=scalar_scaled_entries,
    )
