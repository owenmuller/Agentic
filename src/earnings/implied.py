"""The implied move, from the ATM straddle.

Pure arithmetic over a chain this system already fetches for the options selector:
find the first expiry that outlives the print, take the strike nearest spot, and
the two mids at that strike are what the market charges to own the event.

    implied move = (call_mid + put_mid) / spot

That is the standard approximation, and it is deliberately the naive one. A more
careful estimate (interpolating between strikes, or backing the move out of the
IV surface) would be a model, and the point of the shadow log is to compare the
market's price against what happened — not to compare two models.

Nothing here fills a gap. A missing bid, a chain with no strike near spot, an
illiquid pair: all return None, and the log records the absence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Sequence

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ImpliedMove:
    """One ATM straddle, and what it says the market expects."""

    expiry: date
    strike: Decimal
    call_symbol: str
    put_symbol: str
    call_mid: Decimal
    put_mid: Decimal
    spot: Decimal
    #: Average of the two legs' implied vols, when both carry one.
    atm_iv: Optional[Decimal] = None
    open_interest: int = 0
    #: The wider of the two legs' spreads, as a fraction of mid — the honest one
    #: to record, since both legs have to be crossed.
    worst_spread_pct: Optional[Decimal] = None

    @property
    def straddle_cost(self) -> Decimal:
        return self.call_mid + self.put_mid

    @property
    def implied_move_pct(self) -> Decimal:
        """What the straddle prices, as a percentage of spot."""
        return (self.straddle_cost / self.spot * 100).quantize(Decimal("0.01"))


def atm_straddle(
    chain: Sequence[object],
    spot: Decimal,
    *,
    earliest_expiry: date,
    latest_expiry: date,
    min_open_interest: int = 0,
    max_spread_pct_of_mid: Optional[Decimal] = None,
) -> Optional[ImpliedMove]:
    """The nearest-the-money straddle on the first qualifying expiry.

    ``chain`` is any sequence of quotes shaped like ``sizing.selection.OptionQuote``
    — the same structural typing the selector uses, so this package needs no import
    from the one that fetches them.
    """
    if spot is None or spot <= ZERO:
        return None
    usable = [
        quote
        for quote in chain
        if earliest_expiry <= _expiry(quote) <= latest_expiry
        and _mid(quote) is not None
    ]
    if not usable:
        return None

    for expiry in sorted({_expiry(quote) for quote in usable}):
        legs = [quote for quote in usable if _expiry(quote) == expiry]
        strikes = {_strike(quote) for quote in legs}
        if not strikes:
            continue
        # Nearest strike to spot; a tie goes to the LOWER strike, deterministically,
        # so the same chain always yields the same straddle.
        strike = min(strikes, key=lambda value: (abs(value - spot), value))
        call = _leg(legs, strike, "call")
        put = _leg(legs, strike, "put")
        if call is None or put is None:
            continue
        interest = min(_open_interest(call), _open_interest(put))
        if interest < min_open_interest:
            continue
        spreads = [
            spread
            for spread in (_spread_pct(call), _spread_pct(put))
            if spread is not None
        ]
        worst = max(spreads) if spreads else None
        if (
            max_spread_pct_of_mid is not None
            and worst is not None
            and worst > max_spread_pct_of_mid
        ):
            continue
        ivs = [iv for iv in (_iv(call), _iv(put)) if iv is not None]
        return ImpliedMove(
            expiry=expiry,
            strike=strike,
            call_symbol=_symbol(call),
            put_symbol=_symbol(put),
            call_mid=_mid(call),  # type: ignore[arg-type]
            put_mid=_mid(put),  # type: ignore[arg-type]
            spot=spot,
            atm_iv=(sum(ivs) / Decimal(len(ivs))) if ivs else None,
            open_interest=interest,
            worst_spread_pct=worst,
        )
    return None


def _expiry(quote: object) -> date:
    return getattr(quote, "expiration")


def _strike(quote: object) -> Decimal:
    return getattr(quote, "strike")


def _symbol(quote: object) -> str:
    return getattr(quote, "occ_symbol")


def _mid(quote: object) -> Optional[Decimal]:
    return getattr(quote, "mid", None)


def _iv(quote: object) -> Optional[Decimal]:
    return getattr(quote, "implied_volatility", None)


def _open_interest(quote: object) -> int:
    return int(getattr(quote, "open_interest", 0) or 0)


def _spread_pct(quote: object) -> Optional[Decimal]:
    return getattr(quote, "spread_pct", None)


def _leg(legs: Sequence[object], strike: Decimal, right: str) -> Optional[object]:
    for quote in legs:
        if _strike(quote) == strike and str(getattr(quote, "right", "")).lower() == right:
            return quote
    return None
