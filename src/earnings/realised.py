"""What actually happened, from daily bars.

The realised post-earnings move is the close before the print against the close
after it — which session that is depends on whether the company reported before
the open or after the close, so the session matters and an unknown session is
handled by widening rather than guessing.

This is a *realised-volatility* reference. It is worth being precise that it is
not an IV rank: comparing implied against realised history tells you whether this
name's options have been cheap or dear relative to what the stock did, which is
a real question, but ranking today's IV against its own past needs stored IV, and
the only source of that is the daily snapshot the shadow logger writes going
forward.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Sequence

ZERO = Decimal("0")


def realised_move_pct(
    bars: Sequence[dict[str, Any]],
    report_date: date,
    session: str = "",
) -> Optional[Decimal]:
    """Signed close-to-close move across the print, in percent, or None.

    ``bmo`` reports move the session OF the report date, so the reference close is
    the session before it. ``amc`` reports move the session AFTER, so the reference
    is the report date's own close. An unknown session takes the wider span — the
    last close on or before the report date to the first close after it — which
    cannot understate the move, and understating it is the direction that would
    flatter the strategy under test.
    """
    closes = _closes_by_date(bars)
    if len(closes) < 2:
        return None
    days = sorted(closes)

    if session == "bmo":
        # Reported before the open: the report date's OWN session carries the move.
        before = _last_before(days, report_date)
        after = _first_on_or_after(days, report_date)
    elif session == "amc":
        # Reported after the close: the NEXT session carries it.
        before = _last_on_or_before(days, report_date)
        after = _first_after(days, report_date)
    else:
        # Session unknown: span both candidates. This cannot understate the move,
        # and understating it is the direction that would flatter the strategy.
        before = _last_before(days, report_date)
        after = _first_after(days, report_date)
    if before is None or after is None or after <= before:
        return None
    start, end = closes[before], closes[after]
    if start <= ZERO:
        return None
    return ((end / start - 1) * 100).quantize(Decimal("0.01"))


def _closes_by_date(bars: Sequence[dict[str, Any]]) -> dict[date, Decimal]:
    out: dict[date, Decimal] = {}
    for bar in bars:
        when = _date_of(bar.get("t"))
        close = _decimal_or_none(bar.get("c"))
        if when is not None and close is not None and close > ZERO:
            out[when] = close
    return out


def _last_on_or_before(days: Sequence[date], when: date) -> Optional[date]:
    candidates = [day for day in days if day <= when]
    return candidates[-1] if candidates else None


def _last_before(days: Sequence[date], when: date) -> Optional[date]:
    candidates = [day for day in days if day < when]
    return candidates[-1] if candidates else None


def _first_on_or_after(days: Sequence[date], when: date) -> Optional[date]:
    candidates = [day for day in days if day >= when]
    return candidates[0] if candidates else None


def _first_after(days: Sequence[date], when: date) -> Optional[date]:
    candidates = [day for day in days if day > when]
    return candidates[0] if candidates else None


def _date_of(raw: object) -> Optional[date]:
    if not raw:
        return None
    text = str(raw)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _decimal_or_none(raw: object) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
