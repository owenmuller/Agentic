"""Post-table sizing scalars: the drawdown ladder and the regime scalar.

Human rulings 2026-09-01 (regime scalar approved as designed) and 2026-09-02
(drawdown ladder; build BOTH at one composition point). The composed multiplier
is ``drawdown x regime``, each ≤1.0 by config validation, applied by the
pipeline to every judged proposal AFTER the confidence table — it can only
shrink, it can never lift a band, and no prompt or schema ever mentions it,
which is what LLM-unreachable means.

What each part reads:

  drawdown ladder   the gate's own ``drawdown()`` — the same number the kill
                    switch compares against 12%. Steps (default 1.0 / 0.75 at
                    ≥4% / 0.5 at ≥8%) are inclusive toward less risk: at
                    exactly a rung, the smaller multiplier applies. Stateless —
                    recovery restores the multiplier the same tick the
                    drawdown recovers, because there is nothing to reset. The
                    ladder's last rung sits below the kill switch on purpose;
                    the switch itself is not read, wrapped, or reimplemented.
  regime scalar     the last VIX CLOSE from CBOE's public daily CSV (probed
                    live 2026-09-02), via an injected callable — the fetcher
                    itself is ``execution.vix.CboeVixSource``, because this
                    package stays offline (topology rule). Default rungs 0.75
                    at ≥25, 0.5 at ≥35. A missing or stale close (beyond
                    ``max_age_days``) scales at 1.0 AND is logged loudly —
                    visible, never silent: a CDN outage must not quietly halve
                    the book's entries, and the weekly forgone-size line is
                    where a human sees the scalar's whole story either way.

New entries only: scaling happens in the pipeline's sizing step, so held
positions, exits, the mechanical arm (which never passes through the pipeline),
and the cash sweep are untouched by construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Optional

from orchestrator.config import RiskScalarsConfig
from sizing.engine import SizedProposal

ONE = Decimal("1")
CENTS = Decimal("0.01")

logger = logging.getLogger("orchestrator.scalars")


@dataclass(frozen=True, slots=True)
class ScalarReading:
    """The composed multiplier and the arithmetic behind it, for the record."""

    multiplier: Decimal
    drawdown_multiplier: Decimal
    regime_multiplier: Decimal
    drawdown: Decimal
    vix_close: Optional[Decimal]
    vix_date: Optional[date]
    detail: str


def drawdown_multiplier(drawdown: Decimal, config: RiskScalarsConfig) -> Decimal:
    """The deepest rung at or below the current drawdown. Boundaries are
    inclusive toward less risk: at exactly a rung, the rung applies."""
    multiplier = ONE
    for step in config.drawdown_steps:
        if drawdown >= step.at:
            multiplier = step.multiplier
    return multiplier


def regime_multiplier(vix_close: Decimal, config: RiskScalarsConfig) -> Decimal:
    """The highest rung at or below the VIX close. Inclusive toward less risk."""
    multiplier = ONE
    for step in config.regime.thresholds:
        if vix_close >= step.vix_at_or_above:
            multiplier = step.multiplier
    return multiplier


class SizingScalars:
    """The one composition point: ``size x regime x drawdown``, both ≤1.0."""

    def __init__(
        self,
        config: RiskScalarsConfig,
        drawdown: Callable[[], Decimal],
        vix_close: Optional[Callable[[], Optional[tuple[date, Decimal]]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config
        self._drawdown = drawdown
        self._vix_close = vix_close
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_logged: Optional[Decimal] = None

    def current(self) -> ScalarReading:
        drawdown = self._drawdown()
        ladder = drawdown_multiplier(drawdown, self._config)

        regime = ONE
        vix_value: Optional[Decimal] = None
        vix_date: Optional[date] = None
        regime_note = "regime off"
        if self._config.regime.enabled and self._vix_close is not None:
            reading = self._vix_close()
            if reading is None:
                regime_note = "VIX unavailable: regime x1.0, logged, not silent"
            else:
                vix_date, vix_value = reading
                age = (self._clock().date() - vix_date).days
                if age > self._config.regime.max_age_days:
                    vix_value = None
                    regime_note = (
                        f"VIX close from {vix_date} is {age} days old (stale "
                        f"beyond {self._config.regime.max_age_days}): regime "
                        f"x1.0, logged, not silent"
                    )
                else:
                    regime = regime_multiplier(vix_value, self._config)
                    regime_note = f"VIX {vix_value} ({vix_date}) x{regime}"

        multiplier = ladder * regime
        detail = (
            f"drawdown {drawdown:.2%} x{ladder}; {regime_note}"
        )
        reading = ScalarReading(
            multiplier=multiplier,
            drawdown_multiplier=ladder,
            regime_multiplier=regime,
            drawdown=drawdown,
            vix_close=vix_value,
            vix_date=vix_date,
            detail=detail,
        )
        if multiplier != self._last_logged:
            # One line on every CHANGE of the composed multiplier — visible in
            # the log without spamming every sized entry.
            logger.info("risk scalars now x%s (%s)", multiplier, detail)
            self._last_logged = multiplier
        return reading

    def scale(self, proposal: SizedProposal) -> SizedProposal:
        """A new proposal at ``capital x multiplier``, the table's own dollars
        preserved on the record so attribution can price what was forgone. A
        multiplier of 1 returns the proposal untouched — including its absent
        ``table_capital``, so unscaled records stay exactly as they were."""
        if not self._config.enabled or not proposal.is_tradeable:
            return proposal
        reading = self.current()
        if reading.multiplier >= ONE:
            return proposal
        scaled_fraction = proposal.fraction_of_sleeve_nav * reading.multiplier
        scaled_capital = (proposal.sleeve_nav * scaled_fraction).quantize(
            CENTS, rounding=ROUND_DOWN
        )
        return SizedProposal(
            instrument=proposal.instrument,
            sleeve=proposal.sleeve,
            confidence=proposal.confidence,
            sleeve_nav=proposal.sleeve_nav,
            fraction_of_sleeve_nav=scaled_fraction,
            capital=scaled_capital,
            rationale=(
                f"{proposal.rationale}; risk scalars x{reading.multiplier} "
                f"({reading.detail})"
            ),
            strategy=proposal.strategy,
            table_capital=proposal.capital,
        )
