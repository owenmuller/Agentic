"""Confidence-weighted sizing.

Sizing proposes, the gate disposes
---------------------------------
This module turns a ``ResearchReport`` into a ``SizedProposal``: a number of dollars
it would be reasonable to deploy. That is all it is — a proposal. A ``SizedProposal``
is not an order, is not an ``ApprovedOrder``, and is not accepted by any broker
adapter. The only route to execution runs through ``RiskGate.submit``, which applies
the caps again against live account state that sizing cannot see: current positions,
today's deployment, aggregate option premium, sleeve drift, the kill switch.

That duplication is deliberate. Sizing works from a NAV figure it was handed; the gate
works from the account. When they disagree, the gate wins, because the gate is the one
holding the money.

Two ways to get nothing
-----------------------
A confidence below the floor produces no trade. So does ``direction == no_position``,
at every confidence score including 100 — a model saying "there is no trade here" is
stating a verdict, not hedging one, and reading it through the confidence table alone
would invert it into the largest position the table allows.

Determinism
-----------
No LLM, no network, no clock. The only inputs from the research layer are an integer
confidence score and a direction drawn from a closed enum — the prose in a report
cannot reach this module, because nothing here reads it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Optional

from research.reports import Direction, ResearchReport
from risk_gate.limits import RiskLimits
from risk_gate.state import Sleeve

ZERO = Decimal("0")
CENTS = Decimal("0.01")


class InstrumentKind(StrEnum):
    EQUITY = "equity"
    OPTION = "option"
    EVENT_CONTRACT = "event_contract"


class EventStrategy(StrEnum):
    """Mirrors the order schema's strategy tag; the caps differ sharply."""

    ARB = "arb"
    DIRECTIONAL = "directional"


@dataclass(frozen=True, slots=True)
class SizedProposal:
    """A proposed deployment. Still has to pass the risk gate to become an order."""

    instrument: InstrumentKind
    sleeve: Sleeve
    confidence: int
    sleeve_nav: Decimal
    #: Final fraction of sleeve NAV, after every cap that applies to this instrument.
    fraction_of_sleeve_nav: Decimal
    #: Dollars to deploy. For options this is premium at risk, not notional.
    capital: Decimal
    rationale: str
    strategy: Optional[EventStrategy] = None

    @property
    def is_tradeable(self) -> bool:
        return self.capital > ZERO

    def __str__(self) -> str:  # pragma: no cover - convenience for logs
        if not self.is_tradeable:
            return f"no trade ({self.rationale})"
        return (
            f"{self.instrument}: {self.capital} "
            f"({self.fraction_of_sleeve_nav:%} of {self.sleeve} sleeve NAV)"
        )


class SizingEngine:
    """Applies the confidence table from ``config/risk_limits.yaml``."""

    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    # -- entry points ---------------------------------------------------------------

    def propose_equity(
        self, report: ResearchReport, equity_sleeve_nav: Decimal
    ) -> SizedProposal:
        """Straight table lookup against the equity sleeve."""
        fraction, rationale = self._report_fraction(report)
        return self._build(
            InstrumentKind.EQUITY,
            Sleeve.EQUITY,
            report.confidence,
            equity_sleeve_nav,
            fraction,
            rationale,
        )

    def propose_option(
        self, report: ResearchReport, equity_sleeve_nav: Decimal
    ) -> SizedProposal:
        """Table applied to premium at risk, then halved.

        CLAUDE.md: "options carry embedded leverage — the confidence table must not
        double it." A bought option's premium buys far more exposure than the same
        dollars of stock, so the same confidence buys half the dollars.
        """
        fraction, rationale = self._report_fraction(report)
        if fraction > ZERO:
            fraction = fraction * self._limits.sizing.options.multiplier
            rationale = f"{rationale}, halved for embedded option leverage"
        return self._build(
            InstrumentKind.OPTION,
            Sleeve.EQUITY,
            report.confidence,
            equity_sleeve_nav,
            fraction,
            rationale,
        )

    def propose_event_contract(
        self,
        report: ResearchReport,
        prediction_sleeve_nav: Decimal,
        strategy: EventStrategy,
    ) -> SizedProposal:
        """Table result, capped by the strategy's own per-position limit.

        Arbitrage is micro-unit and high-turnover (0.5%); directional divergence
        positions are larger (2%). Confidence still gates entry — below the floor there
        is no trade regardless of strategy — but the strategy cap is what binds above
        it, since both caps sit well under every band in the table.
        """
        fraction, rationale = self._report_fraction(report)
        if fraction > ZERO:
            cap = self._strategy_cap(strategy)
            if cap < fraction:
                fraction = cap
                rationale = f"{rationale}, capped at the {strategy} limit"
        return self._build(
            InstrumentKind.EVENT_CONTRACT,
            Sleeve.PREDICTION,
            report.confidence,
            prediction_sleeve_nav,
            fraction,
            rationale,
            strategy=strategy,
        )

    # -- internals ------------------------------------------------------------------

    def _report_fraction(self, report: ResearchReport) -> tuple[Decimal, str]:
        """Resolve a report to a fraction of sleeve NAV.

        ``direction == no_position`` short-circuits to zero before the confidence table
        is consulted at all, at every score including 100. The two fields answer
        different questions — direction is *whether, and which way*; confidence is *how
        sure* — so a report that is certain nothing should be traded must not be read as
        a certain position. Sizing that consulted only the number would turn the most
        emphatic "there is no trade here" the model can express into the largest
        position the table allows.

        Structurally the outcome is the same as a sub-floor confidence: no trade, with
        a rationale saying why. Every entry point reaches the table through here, so
        there is no path on which the verdict is skipped.
        """
        if report.recommends_no_position:
            return ZERO, (
                f"research returned direction {Direction.NO_POSITION}: no position at "
                f"any confidence, and this report scored {report.confidence}"
            )
        return self._table_fraction(report.confidence)

    def _table_fraction(self, confidence: int) -> tuple[Decimal, str]:
        """Look up the band. Rejects an out-of-range score rather than clamping.

        Clamping 150 to 100 would silently size at the maximum — the most dangerous
        direction to fail in. A score outside 0-100 means something upstream is broken,
        and a broken sizing input should stop the pipeline, not quietly buy the most it
        is allowed to.
        """
        if not 0 <= confidence <= 100:
            raise ValueError(
                f"confidence must be between 0 and 100, got {confidence}; refusing to "
                f"clamp an out-of-range score into a position size"
            )
        sizing = self._limits.sizing
        fraction = sizing.size_for(confidence)
        if fraction <= ZERO:
            return ZERO, f"confidence {confidence} is below the {sizing.no_trade_below} floor"
        return fraction, f"confidence {confidence} maps to {fraction:%} of sleeve NAV"

    def _strategy_cap(self, strategy: EventStrategy) -> Decimal:
        prediction = self._limits.prediction_sleeve
        if strategy is EventStrategy.ARB:
            return prediction.arbitrage.max_position
        return prediction.directional.max_position

    def _build(
        self,
        instrument: InstrumentKind,
        sleeve: Sleeve,
        confidence: int,
        sleeve_nav: Decimal,
        fraction: Decimal,
        rationale: str,
        strategy: Optional[EventStrategy] = None,
    ) -> SizedProposal:
        if sleeve_nav < ZERO:
            raise ValueError(f"sleeve NAV cannot be negative, got {sleeve_nav}")

        # The hard cap applies last and unconditionally. Every path above already sits
        # under it; this is the backstop that makes that a property rather than a
        # coincidence of the current config.
        fraction = min(fraction, self._limits.sizing.hard_cap)

        # Round the dollars down. Rounding must never increase exposure.
        capital = (sleeve_nav * fraction).quantize(CENTS, rounding=ROUND_DOWN)

        return SizedProposal(
            instrument=instrument,
            sleeve=sleeve,
            confidence=confidence,
            sleeve_nav=sleeve_nav,
            fraction_of_sleeve_nav=fraction,
            capital=capital,
            rationale=rationale,
            strategy=strategy,
        )
