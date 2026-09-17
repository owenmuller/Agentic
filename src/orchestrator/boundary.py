"""Boundary confirmation (human ruling 2026-09-02) as ONE function.

Diagnosis before the build: five identical-input replays of a floor-band case
spanned long/38-54 and no_position/30-72 — the sizing floor's band admits
stochastic noise, so a tradeable verdict there must REPLICATE before it sizes.
A verdict with confidence in [floor, floor + band_width) buys a SECOND
independent pass, same tier and fresh context. Confirmed = same direction at
or above the floor; the LOWER-confidence report is the one that sizes (the
second pass can only block or shrink, never enlarge). A second pass that
errors, or an add that does not replicate as an add, does not confirm: an
unconfirmable floor-band verdict is not sized (Constraint #6).

The entry pipeline and the golden replay call this same function (2026-09-17,
human approval): the golden set exercises the path production runs, including
the guard that actually stands between floor-band noise and a 2% position —
before this the replay stopped one step short of it. Whenever a second pass
ran, both verdicts and its usage come back so the record's cost is honest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from audit.records import BoundaryConfirmationSnapshot
from research.reports import ResearchReport, ResearchUsage

logger = logging.getLogger(__name__)


def in_boundary_band(confidence: int, floor: int, band_width: int) -> bool:
    """[floor, floor + band_width): the floor is IN the band (a 50 replicates),
    the top is out (a 70 sizes without a second pass)."""
    return floor <= confidence < floor + band_width


@dataclass(frozen=True, slots=True)
class BoundaryConfirmation:
    """What the boundary confirmation decided: the report to size (None = not
    confirmed, with ``failure`` saying why), the snapshot for the decision record
    when a second pass ran, and that pass's usage. ``ran`` is False when the
    verdict sat outside the band and ``report`` is simply the first pass."""

    report: Optional[ResearchReport]
    failure: Optional[str] = None
    snapshot: Optional[BoundaryConfirmationSnapshot] = None
    usage: Optional[ResearchUsage] = None
    ran: bool = False
    #: The second pass's outcome when one ran — a report or a typed rejection.
    second: Any = None

    @property
    def upheld(self) -> bool:
        """A second pass ran and replicated: ``report`` sizes."""
        return self.ran and self.report is not None

    @property
    def reversed(self) -> bool:
        """A second pass ran and did NOT replicate: nothing sizes."""
        return self.ran and self.report is None

    def describe(self) -> str:
        """One clause for a run record or a golden line."""
        if not self.ran:
            return "outside the band"
        if self.second is None or not isinstance(self.second, ResearchReport):
            return f"second pass failed ({getattr(self.second, 'code', 'error')}) -> REVERSED"
        second = f"{self.second.direction}/{self.second.confidence}"
        if self.reversed:
            return f"second {second} -> REVERSED, nothing sizes"
        assert self.snapshot is not None
        return (
            f"second {second} -> UPHELD, the {self.snapshot.sized_from} pass sizes "
            f"({self.report.direction}/{self.report.confidence})"  # type: ignore[union-attr]
        )


def confirm_boundary(
    research,
    signal,
    report: ResearchReport,
    *,
    floor: int,
    band_width: int,
    add_context=None,
) -> BoundaryConfirmation:
    """Run the boundary confirmation on ``report`` if its confidence sits in the
    band; otherwise hand the report straight back (``ran`` False).

    ``research`` is the production ResearchPass (``run(signal, add_context=)``
    and ``last_usage``). The caller decides whether the verdict is TRADEABLE —
    a no_position never comes here.
    """
    if not in_boundary_band(report.confidence, floor, band_width):
        return BoundaryConfirmation(report)
    logger.info(
        "boundary confirmation on %s: %s/%d sits in the floor band "
        "[%d, %d); buying a second independent pass",
        signal.signal_id,
        report.direction,
        report.confidence,
        floor,
        floor + band_width,
    )
    second = research.run(signal, add_context=add_context)
    second_usage = research.last_usage
    if not isinstance(second, ResearchReport):
        return BoundaryConfirmation(
            None,
            f"boundary verdict {report.direction}/{report.confidence} could "
            f"not be confirmed: the second pass failed "
            f"({getattr(second, 'code', 'error')}) — an unconfirmable "
            f"floor-band verdict is not sized (ruling 2026-09-02)",
            usage=second_usage,
            ran=True,
            second=second,
        )
    if add_context is not None and not second.is_add:
        # An add decision must REPLICATE as an add (ruling 2026-09-16): a
        # second pass that holds is a hold, whatever the first said.
        return BoundaryConfirmation(
            None,
            f"boundary add verdict NOT confirmed: first pass add/"
            f"{report.confidence}, second independent pass "
            f"{second.add_verdict or 'unstated'}/{second.direction}/"
            f"{second.confidence} — an add that does not replicate is not "
            f"sized (rulings 2026-09-02, 2026-09-16)",
            usage=second_usage,
            ran=True,
            second=second,
        )
    if second.direction is not report.direction or second.confidence < floor:
        return BoundaryConfirmation(
            None,
            f"boundary verdict NOT confirmed: first pass "
            f"{report.direction}/{report.confidence}, second independent "
            f"pass {second.direction}/{second.confidence} — the floor band "
            f"is stochastic there, and a verdict that does not replicate "
            f"is not sized (ruling 2026-09-02)",
            usage=second_usage,
            ran=True,
            second=second,
        )
    sized_from = "second" if second.confidence < report.confidence else "first"
    logger.info(
        "boundary confirmed on %s: first %s/%d, second %s/%d; the %s pass sizes",
        signal.signal_id,
        report.direction,
        report.confidence,
        second.direction,
        second.confidence,
        sized_from,
    )
    return BoundaryConfirmation(
        second if sized_from == "second" else report,
        snapshot=BoundaryConfirmationSnapshot(
            floor=floor,
            band_width=band_width,
            first_direction=str(report.direction),
            first_confidence=report.confidence,
            second_direction=str(second.direction),
            second_confidence=second.confidence,
            sized_from=sized_from,
            second_est_cost_usd=(second_usage.cost_usd if second_usage else None),
        ),
        usage=second_usage,
        ran=True,
        second=second,
    )
