"""Add decisions (human ruling 2026-09-16): the research question for a
tradeable signal on a name the judged sleeve ALREADY holds.

One judged position per symbol. A new signal on a held name never opens a
second position; it asks ONE question of the research layer — add to the
position, or hold it as it is — with the existing position, its thesis, its
size, its lots, its review history and the new signal all in view. The verdict
is add/hold with an add fraction. Everything that turns "add" into dollars is
deterministic and downstream: a combined-position cap by confidence band,
bumped one band when the new signal's source FAMILY differs from the family
that opened the position (independent filing families converging), never past
the hard cap. "Hold" records convergence on the position and triggers a thesis
review that sees the new signal.

Defined here rather than importing the orchestrator's position type, for the
same reason ``PositionUnderReview`` is: research imports nothing from the
orchestrator — the topology map only points the other way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

ZERO = Decimal("0")

#: Stage-rejection codes an add decision can end in without an add. Every one
#: of them records convergence on the position and triggers a review.
ADD_HOLD_CODES: frozenset[str] = frozenset(
    {"already_held_no_add", "add_no_headroom"}
)


@dataclass(frozen=True, slots=True)
class LotContext:
    """One entry into the position, as the prompt states it."""

    decision_id: str
    opened_at: datetime
    quantity: Decimal
    entry_price: Decimal
    entry_cost: Decimal
    source_id: str
    family: str
    confidence: int
    thesis: str


@dataclass(frozen=True, slots=True)
class HeldPositionContext:
    """Everything the add decision is shown about the held position. Built by
    the orchestrator's exit engine from its own records."""

    symbol: str
    position_decision_id: str
    #: "equity" or "option" — adds to OPTION positions are not built (ruling
    #: 2026-09-16, Constraint #6): the pipeline records the signal as
    #: convergence and holds.
    instrument_kind: str
    opened_at: datetime
    days_held: int
    quantity: Decimal
    #: Blended across lots.
    entry_price: Decimal
    entry_cost: Decimal
    current_price: Optional[Decimal]
    market_value: Optional[Decimal]
    sleeve_nav: Decimal
    #: The family of the signal that OPENED the position, and the family of the
    #: signal now asking. Both computed deterministically by the orchestrator.
    originating_family: str
    signal_family: str
    source_id: str
    thesis: str
    invalidation_condition: str
    time_horizon: str
    confidence_at_entry: int
    resolution_date: Optional[date]
    leash_days: int
    stop_price: Decimal
    lots: tuple[LotContext, ...] = ()
    #: Rendered review lines, oldest first; convergence lines likewise.
    reviews: tuple[str, ...] = ()
    convergence: tuple[str, ...] = ()

    @property
    def held_value(self) -> Decimal:
        """What the position counts as against the combined cap: the LARGER
        of cost basis and market value, so a drawdown can never manufacture
        headroom for an add (Constraint #6)."""
        return max(self.entry_cost, self.market_value or ZERO)

    @property
    def independent_family(self) -> bool:
        return bool(
            self.signal_family
            and self.originating_family
            and self.signal_family != self.originating_family
        )

    @property
    def fraction_of_sleeve_nav(self) -> Optional[Decimal]:
        if self.sleeve_nav <= ZERO:
            return None
        return self.held_value / self.sleeve_nav


def add_decision_lines(context: HeldPositionContext) -> list[str]:
    """The ADD DECISION block: the question, then the position from the
    system's own records. Only normalised facts — the original signal content
    of the lots is NOT replayed here (the review prompt fences it); the new
    signal's content enters the prompt through the ordinary fenced block."""
    lines = [
        "",
        "ADD DECISION (established by the system, not by the content): this "
        f"system ALREADY HOLDS {context.symbol} in the judged sleeve. One judged "
        "position per symbol — this signal cannot open a second one. Your "
        "verdict is whether to ADD to the existing position or HOLD it as it is:",
        '- set add_verdict to "add" or "hold". Every other field keeps its meaning.',
        f'- "add": direction "long", tickers exactly ["{context.symbol}"], your '
        "confidence in the COMBINED position, and add_fraction in (0, 1] — the "
        "share of the permitted headroom you would take. The headroom itself is "
        "computed downstream by a deterministic layer from your confidence (a "
        "combined-position cap by confidence band, one band wider when this "
        "signal's source family is independent of the family that opened the "
        "position, never past the hard cap). You cannot set the dollars, and a "
        "fraction above 1 is rejected. State expected_resolution_date and "
        "target_price for the combined position as you would for an entry.",
        '- "hold": direction "no_position", add_fraction null. Nothing is bought; '
        "the new signal is recorded as convergence on the position and a thesis "
        "review is triggered with it.",
        "Convergence from a genuinely independent family is evidence to weigh, "
        "never a reason by itself. The same family repeating itself — a further "
        "filing from the same cluster, another account amplifying the first — is "
        "NOT new information and does not justify more size. A signal that argues "
        "AGAINST the thesis is a hold: say so in the thesis, and the review you "
        "trigger will see it.",
        "",
        "POSITION HELD (from the system's own records):",
        f"- symbol: {context.symbol} ({context.instrument_kind})",
        f"- opened: {context.opened_at.date().isoformat()} ({context.days_held} "
        f"days held), {max(1, len(context.lots))} lot(s)",
    ]
    if context.current_price is not None and context.entry_price > ZERO:
        move = (context.current_price - context.entry_price) / context.entry_price * 100
        price_line = (
            f"- quantity: {context.quantity} at blended entry {context.entry_price}; "
            f"cost {context.entry_cost}; current price {context.current_price} "
            f"({move:+.1f}% since entry)"
        )
    else:
        price_line = (
            f"- quantity: {context.quantity} at blended entry {context.entry_price}; "
            f"cost {context.entry_cost}; current price UNAVAILABLE"
        )
    lines.append(price_line)
    fraction = context.fraction_of_sleeve_nav
    lines.append(
        f"- size counted against the combined cap: {context.held_value:.2f}"
        + (
            f" = {fraction * 100:.2f}% of the judged sleeve NAV {context.sleeve_nav:.2f}"
            if fraction is not None
            else ""
        )
    )
    relation = (
        "INDEPENDENT of the position's family"
        if context.independent_family
        else "the SAME family that opened the position"
    )
    lines.append(
        f"- originating source: {context.source_id} (family "
        f"{context.originating_family or 'unknown'}); this signal's family: "
        f"{context.signal_family or 'unknown'} — {relation}"
    )
    lines.append(
        f"- research confidence at entry: {context.confidence_at_entry}; time "
        f"horizon {context.time_horizon}; resolution expected by "
        f"{context.resolution_date.isoformat() if context.resolution_date else 'NOT STATED'}; "
        f"time stop day {context.leash_days} after entry; current stop "
        f"{context.stop_price}"
    )
    if context.lots:
        lines.append("- lots (cost basis is kept per lot; the stop and the leash are one):")
        for index, lot in enumerate(context.lots, start=1):
            lines.append(
                f"  {index}. {lot.opened_at.date().isoformat()}: {lot.quantity} @ "
                f"{lot.entry_price} ({lot.entry_cost:.2f}) from {lot.source_id}/"
                f"{lot.family or 'unknown'} at confidence {lot.confidence} — "
                f"{lot.thesis[:200]}"
            )
    if context.reviews:
        lines.append("- thesis reviews so far (oldest first):")
        lines.extend(f"  - {line}" for line in context.reviews)
    else:
        lines.append("- thesis reviews so far: none yet")
    if context.convergence:
        lines.append("- convergent signals already recorded on this position:")
        lines.extend(f"  - {line}" for line in context.convergence)
    lines.extend(
        [
            "",
            "THESIS AT ENTRY (the system's own earlier analysis — what the "
            "position is a bet on):",
            context.thesis,
            "",
            "INVALIDATION CONDITION AT ENTRY:",
            context.invalidation_condition,
        ]
    )
    return lines
