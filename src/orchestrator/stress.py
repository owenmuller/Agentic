"""Historical stress test of the CURRENT book (human ruling 2026-09-02).
Report-only: nothing here trades, sizes, or writes.

The question is narrow and honest: if today's positions, at today's quantities,
had been held through a configured historical window, what is the worst
peak-to-trough drawdown each sleeve and the whole NAV would have shown — and
would the drawdown ladder, the kill switch, or the mechanical breaker have
fired? Cash is held flat. Anything that cannot be replayed with equity bars —
an option, a name with no history in the window — is HELD FLAT at its current
value and listed by name, because a flat-held position understates the
drawdown and the reader must see how much of the book that is.

Inputs are plain data so the module stays offline: positions, per-sleeve cash,
a ``closes(symbol, start, end)`` callable, and the windows from config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, Iterable, Optional, Sequence

ZERO = Decimal("0")
ONE = Decimal("1")

#: The post-table drawdown ladder (rulings 2026-09-01/02) and the two halts,
#: as fractions of drawdown from the running peak. The ladder and kill switch
#: read TOTAL NAV; the breaker reads the mechanical sleeve's own value.
LADDER_RUNGS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("0.04"), Decimal("0.75")),
    (Decimal("0.08"), Decimal("0.5")),
)
KILL_SWITCH_DRAWDOWN = Decimal("0.12")
MECHANICAL_BREAKER_DRAWDOWN = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class BookPosition:
    sleeve: str  # "equity" (judged) | "mechanical" | "cash_management"
    symbol: str
    quantity: Decimal
    market_value: Decimal
    is_option: bool = False


@dataclass(frozen=True, slots=True)
class StressWindowSpec:
    name: str
    start: date
    end: date


ClosesFn = Callable[[str, date, date], Sequence[tuple[date, Decimal]]]


@dataclass
class SleeveStress:
    sleeve: str
    start_value: Decimal
    trough_value: Decimal
    max_drawdown: Decimal  # fraction, 0.0-1.0
    replayed: tuple[str, ...] = ()
    flat_held: tuple[str, ...] = ()  # "SYMBOL (reason)"


@dataclass
class WindowStress:
    window: StressWindowSpec
    sleeves: dict[str, SleeveStress] = field(default_factory=dict)
    total_start: Decimal = ZERO
    total_trough: Decimal = ZERO
    total_max_drawdown: Decimal = ZERO
    ladder_rung: Optional[Decimal] = None  # the deepest multiplier reached
    kill_switch_fired: bool = False
    mechanical_breaker_fired: bool = False
    trading_days: int = 0
    note: str = ""


def _max_drawdown(path: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    """(max drawdown fraction, trough value) over a value path."""
    peak = None
    worst = ZERO
    trough = path[0] if path else ZERO
    for value in path:
        if peak is None or value > peak:
            peak = value
        if peak and peak > ZERO:
            drawdown = (peak - value) / peak
            if drawdown > worst:
                worst = drawdown
                trough = value
    return worst, trough


def stress_book(
    positions: Iterable[BookPosition],
    cash_by_sleeve: dict[str, Decimal],
    other_cash: Decimal,
    closes: ClosesFn,
    windows: Iterable[StressWindowSpec],
) -> list[WindowStress]:
    positions = list(positions)
    results: list[WindowStress] = []
    for window in windows:
        result = WindowStress(window=window)
        # Fetch once per symbol per window.
        series: dict[str, dict[date, Decimal]] = {}
        for position in positions:
            if position.is_option or position.symbol in series:
                continue
            try:
                rows = closes(position.symbol, window.start, window.end)
            except Exception:  # noqa: BLE001 - missing, never invented
                rows = []
            series[position.symbol] = {d: c for d, c in rows if c > ZERO}
        days = sorted({d for s in series.values() for d in s})
        result.trading_days = len(days)
        if not days:
            result.note = (
                "no price history for any position in this window — nothing "
                "could be replayed"
            )
            results.append(result)
            continue

        sleeve_paths: dict[str, list[Decimal]] = {}
        for sleeve in sorted({p.sleeve for p in positions} | set(cash_by_sleeve)):
            members = [p for p in positions if p.sleeve == sleeve]
            flat_value = cash_by_sleeve.get(sleeve, ZERO)
            replayed: list[str] = []
            flat_held: list[str] = []
            replayable: list[BookPosition] = []
            for position in members:
                if position.is_option:
                    flat_held.append(f"{position.symbol} (option: no equity bars)")
                    flat_value += position.market_value
                elif not series.get(position.symbol):
                    flat_held.append(f"{position.symbol} (no history in window)")
                    flat_value += position.market_value
                else:
                    replayable.append(position)
                    replayed.append(position.symbol)
            path: list[Decimal] = []
            last_close: dict[str, Decimal] = {}
            for day in days:
                value = flat_value
                for position in replayable:
                    known = series[position.symbol]
                    if day in known:
                        last_close[position.symbol] = known[day]
                    close = last_close.get(position.symbol)
                    if close is None:
                        # Before the name's first bar in the window: flat at
                        # its first available close, so it neither helps nor
                        # hurts until it trades.
                        close = known[min(known)]
                    value += position.quantity * close
                path.append(value)
            drawdown, trough = _max_drawdown(path)
            result.sleeves[sleeve] = SleeveStress(
                sleeve=sleeve,
                start_value=path[0],
                trough_value=trough,
                max_drawdown=drawdown,
                replayed=tuple(replayed),
                flat_held=tuple(flat_held),
            )
            sleeve_paths[sleeve] = path

        total_path = [
            other_cash + sum((paths[i] for paths in sleeve_paths.values()), ZERO)
            for i in range(len(days))
        ]
        total_dd, total_trough = _max_drawdown(total_path)
        result.total_start = total_path[0]
        result.total_trough = total_trough
        result.total_max_drawdown = total_dd
        for threshold, multiplier in LADDER_RUNGS:
            if total_dd >= threshold:
                result.ladder_rung = multiplier
        result.kill_switch_fired = total_dd >= KILL_SWITCH_DRAWDOWN
        mechanical = result.sleeves.get("mechanical")
        result.mechanical_breaker_fired = bool(
            mechanical and mechanical.max_drawdown > MECHANICAL_BREAKER_DRAWDOWN
        )
        results.append(result)
    return results


def render_stress_report(results: Sequence[WindowStress], generated_at) -> str:
    lines = [
        f"STRESS TEST of the current book — {generated_at.isoformat(timespec='seconds')}",
        "Report-only. Today's quantities held through each window; cash flat; "
        "options and names without history HELD FLAT (listed — they understate "
        "the drawdown). Ladder 0.75 @ >=4%, 0.5 @ >=8%; kill switch @ >=12% of "
        "total NAV; mechanical breaker @ >25% of the mechanical sleeve's own value.",
        "",
    ]
    for result in results:
        window = result.window
        lines.append(f"== {window.name}: {window.start} -> {window.end} "
                     f"({result.trading_days} trading days)")
        if result.note:
            lines.append(f"   {result.note}")
            lines.append("")
            continue
        lines.append(
            f"   TOTAL NAV: {result.total_start:.2f} -> trough {result.total_trough:.2f}, "
            f"max drawdown {result.total_max_drawdown:.2%}  |  ladder "
            f"{'x' + str(result.ladder_rung) if result.ladder_rung else 'not reached'}"
            f"  |  kill switch {'FIRED' if result.kill_switch_fired else 'not reached'}"
        )
        for sleeve, stress in sorted(result.sleeves.items()):
            flag = ""
            if sleeve == "mechanical":
                flag = "  |  breaker " + (
                    "FIRED" if result.mechanical_breaker_fired else "not reached"
                )
            lines.append(
                f"   {sleeve:<16} {stress.start_value:.2f} -> trough "
                f"{stress.trough_value:.2f}, max drawdown {stress.max_drawdown:.2%}{flag}"
            )
            if stress.replayed:
                lines.append(f"      replayed: {', '.join(stress.replayed)}")
            if stress.flat_held:
                lines.append(f"      HELD FLAT: {', '.join(stress.flat_held)}")
        lines.append("")
    lines.append(
        "These numbers argue; humans rule. A window that would have fired the kill "
        "switch is evidence about concentration and beta, not an instruction."
    )
    return "\n".join(lines)
