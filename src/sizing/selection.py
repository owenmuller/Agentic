"""Deterministic option instrument selection. No LLM, no network, no clock.

The research layer decides WHETHER leverage is earned (the catalyst gate); this
module decides WHICH contract expresses it, from a chain snapshot and the
thresholds in ``config/risk_limits.yaml``. Same inputs, same contract, always:
selection is a pure function with a total ordering, so an audit record's chosen
contract can be reproduced from the chain it was chosen from.

Everything here fails toward the less-levered expression: any gate that cannot
be evaluated (no chain, no greeks, no IV) is a fallback to equity, never a
guess. A fallback carries the reason and — where one exists — the near-miss
contract that would have been picked, so "are our liquidity gates too tight?"
is answerable from records later (ruling 2026-08-24).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Optional, Protocol, Union

from risk_gate.limits import DeltaBand, OptionsSelectionLimits

ZERO = Decimal("0")
TWO = Decimal("2")


class OptionQuote(Protocol):
    """One contract, as a chain source saw it — a structural type, on purpose.

    The concrete dataclass lives in ``execution.options_data`` (the layer that
    fetches it); this module types against the shape only, so the deterministic
    layer imports nothing from the network layer and the network layer imports
    nothing from here. The topology DAG stays a DAG.
    Missing data stays None — the gates judge absence; nothing invents a number.
    """

    occ_symbol: str
    underlying: str
    right: str  # "call" | "put"
    expiration: date
    strike: Decimal
    bid: Optional[Decimal]
    ask: Optional[Decimal]
    delta: Optional[Decimal]
    implied_volatility: Optional[Decimal]
    open_interest: int
    multiplier: int

    @property
    def mid(self) -> Optional[Decimal]: ...

    @property
    def spread_pct(self) -> Optional[Decimal]: ...


class FallbackReason(StrEnum):
    """One vocabulary for every way an options expression does not happen.

    The first three are decided in the pipeline before any chain is fetched;
    the rest are the selector's own gates, in evaluation order.
    """

    NO_CATALYST = "no_catalyst"
    NO_CATALYST_FOR_PUTS = "no_catalyst_for_puts"
    SELECTOR_UNWIRED = "selector_unwired"
    CHAIN_UNAVAILABLE = "chain_unavailable"
    NO_EXPIRY_IN_RANGE = "no_expiry_in_range"
    NO_GREEKS = "no_greeks"
    NO_STRIKE_IN_BAND = "no_strike_in_band"
    ILLIQUID_CHAIN = "illiquid_chain"
    IV_UNAVAILABLE = "iv_unavailable"
    IV_EXTREME = "iv_extreme"
    PREMIUM_EXCEEDS_SIZE = "premium_exceeds_size"


@dataclass(frozen=True, slots=True)
class NearMiss:
    """The contract that would have been picked, and the gate that killed it."""

    occ_symbol: str
    delta: Optional[Decimal]
    open_interest: int
    spread_pct: Optional[Decimal]
    killed_by: str


@dataclass(frozen=True, slots=True)
class SelectedOption:
    """A contract that passed every gate."""

    quote: OptionQuote
    band: DeltaBand
    #: The pick's IV rank within the fetched chain's own IV population.
    iv_percentile: Decimal


@dataclass(frozen=True, slots=True)
class OptionFallback:
    """Why the position expresses as stock (or, for puts, not at all)."""

    reason: FallbackReason
    detail: str
    near_miss: Optional[NearMiss] = None


SelectionResult = Union[SelectedOption, OptionFallback]


class OptionSelector:
    """Applies the selection rules from ``options_selection`` in risk_limits.yaml."""

    def __init__(self, config: OptionsSelectionLimits) -> None:
        self._config = config

    def select(
        self,
        *,
        direction: str,
        time_horizon: str,
        confidence: int,
        chain: Optional[list[OptionQuote]],
        today: date,
    ) -> SelectionResult:
        config = self._config

        if not chain:
            return OptionFallback(
                FallbackReason.CHAIN_UNAVAILABLE,
                "no chain data; the less-levered expression wins when the "
                "levered one cannot be priced",
            )

        band = config.band_for(confidence)
        if band is None:  # pragma: no cover - sizing floor rejects these earlier
            return OptionFallback(
                FallbackReason.NO_STRIKE_IN_BAND,
                f"no delta band configured for confidence {confidence}",
            )

        right = "put" if direction == "short_via_puts" else "call"

        # 1. Expiry floor: shortest expiry the thesis has room to be slow in.
        min_days = config.min_expiry_days.for_horizon(time_horizon)
        qualifying = sorted(
            {
                quote.expiration
                for quote in chain
                if (quote.expiration - today).days >= min_days
            }
        )
        if not qualifying:
            return OptionFallback(
                FallbackReason.NO_EXPIRY_IN_RANGE,
                f"no expiry at least {min_days} days out for a "
                f"{time_horizon} horizon",
            )
        expiry = qualifying[0]
        pool = [
            quote
            for quote in chain
            if quote.expiration == expiry and quote.right == right
        ]

        # 2. Delta band by confidence, with the absolute floor as backstop —
        # "never OTM lottery strikes" is a property here, not a coincidence
        # of the current band table.
        with_delta = [quote for quote in pool if quote.delta is not None]
        if not with_delta:
            return OptionFallback(
                FallbackReason.NO_GREEKS,
                f"no {right} at {expiry} carries a delta; a strike chosen "
                "without one is a guess",
                near_miss=_near_miss(pool, band, "no_greeks"),
            )
        in_band = [
            quote
            for quote in with_delta
            if band.delta_min <= abs(quote.delta) <= band.delta_max  # type: ignore[arg-type]
            and abs(quote.delta) >= config.min_delta_floor  # type: ignore[arg-type]
        ]
        if not in_band:
            return OptionFallback(
                FallbackReason.NO_STRIKE_IN_BAND,
                f"no {right} at {expiry} with |delta| in "
                f"[{band.delta_min}, {band.delta_max}]",
                near_miss=_near_miss(with_delta, band, "no_strike_in_band"),
            )

        # 3. Liquidity gates.
        liquid = [
            quote
            for quote in in_band
            if quote.open_interest >= config.min_open_interest
            and quote.spread_pct is not None
            and quote.spread_pct <= config.max_spread_pct_of_mid
        ]
        if not liquid:
            return OptionFallback(
                FallbackReason.ILLIQUID_CHAIN,
                f"no in-band {right} at {expiry} clears OI >= "
                f"{config.min_open_interest} and spread <= "
                f"{config.max_spread_pct_of_mid:%} of mid",
                near_miss=_near_miss(in_band, band, "illiquid_chain"),
            )

        # 4. The pick: closest to the band's midpoint, then the total ordering
        # that makes selection reproducible from the chain alone.
        pick = min(liquid, key=lambda quote: _order_key(quote, band))

        # 5. IV gate — the pick's rank within THIS chain's own IV population.
        # LIMITATION, accepted 2026-08-24: chain-internal percentile cannot
        # detect a uniformly panic-priced chain — when every contract's IV is
        # elevated together, the rank stays unremarkable and the gate passes.
        # It is the only history-free method; IV-rank-vs-history is the noted
        # future lever if attribution shows systematic overpaying.
        if pick.implied_volatility is None:
            return OptionFallback(
                FallbackReason.IV_UNAVAILABLE,
                f"{pick.occ_symbol} carries no implied volatility; premium "
                "that cannot be sanity-checked is not bought",
                near_miss=_as_near_miss(pick, "iv_unavailable"),
            )
        population = [
            quote.implied_volatility
            for quote in chain
            if quote.implied_volatility is not None
        ]
        percentile = Decimal(
            sum(1 for value in population if value < pick.implied_volatility)
        ) / Decimal(len(population))
        if percentile > config.max_iv_percentile:
            return OptionFallback(
                FallbackReason.IV_EXTREME,
                f"{pick.occ_symbol} IV sits at the {percentile:.0%} percentile "
                f"of its own chain, above the {config.max_iv_percentile:%} "
                "ceiling; panic-priced premium is not bought",
                near_miss=_as_near_miss(
                    pick, f"iv_extreme at {percentile:.2f} percentile"
                ),
            )

        return SelectedOption(quote=pick, band=band, iv_percentile=percentile)


def _order_key(quote: OptionQuote, band: DeltaBand):
    """The total ordering: delta-gap, then OI (desc), spread, strike."""
    midpoint = (band.delta_min + band.delta_max) / TWO
    gap = abs(abs(quote.delta) - midpoint)  # type: ignore[arg-type]
    spread = quote.spread_pct
    return (
        gap,
        -quote.open_interest,
        spread if spread is not None else Decimal("Infinity"),
        quote.strike,
        quote.occ_symbol,
    )


def _near_miss(
    pool: list[OptionQuote], band: DeltaBand, killed_by: str
) -> Optional[NearMiss]:
    """The best candidate the killing gate saw, by the same total ordering
    (falling back to OI/spread/strike when deltas are absent)."""
    if not pool:
        return None
    with_delta = [quote for quote in pool if quote.delta is not None]
    if with_delta:
        best = min(with_delta, key=lambda quote: _order_key(quote, band))
    else:
        best = min(
            pool,
            key=lambda quote: (
                -quote.open_interest,
                quote.spread_pct if quote.spread_pct is not None else Decimal("Infinity"),
                quote.strike,
                quote.occ_symbol,
            ),
        )
    return _as_near_miss(best, killed_by)


def _as_near_miss(quote: OptionQuote, killed_by: str) -> NearMiss:
    return NearMiss(
        occ_symbol=quote.occ_symbol,
        delta=quote.delta,
        open_interest=quote.open_interest,
        spread_pct=quote.spread_pct,
        killed_by=killed_by,
    )
