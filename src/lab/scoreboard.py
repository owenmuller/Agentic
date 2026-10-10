"""PAPER PUSH items 3 and 4: the weekly SCOREBOARD and its FILL-REALISM line.

  4. "One weekly table, every sleeve vs SPY and vs the leverage ladder:
     return, max drawdown, trades, win rate, average win vs loss, raw and
     slippage-adjusted. First grading at 20 sessions, real verdict at 60."
  3. "Apply the execution test's measured slippage to every intraday sleeve's
     reported P&L as an adjusted line beside raw paper P&L."

Read-only over every source: the lab ledgers (data-lab/), the execution
test's daily report (the main book's data/execution_test_daily.jsonl), the
main account's portfolio history and SPY's daily closes. Writes nothing.

The slippage line
-----------------
The execution test reports, per session, the mean adverse slippage of its
live fills against the backtest's fill model, in bp, by kind (entry, stop,
reversal, close-out). Pooled over sessions (weighted by fills) that is the
measured cost per fill. An intraday sleeve's adjusted P&L charges every one
of its fills that cost on the fill's notional: entries at the entry rate,
stop exits at the stop rate, every other exit at the close-out rate. A rate
is used only once it rests on >= ``MIN_FILLS`` measured fills; until then
the adjusted column says "pending" rather than guess. A favourable measured
mean charges zero, never a credit (the smaller-P&L reading, Constraint #6).
Caveat, stated on the table: the rates are measured on SPY/SH; option fills
are charged the same bp, which understates their wider spreads.

The leverage ladder is buy-and-hold (not intraday): its raw line is its line.
The execution test is the measurement itself: its raw line is live paper P&L
beside the modelled P&L, and it is never adjusted by its own numbers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
MIN_FILLS = 10
GRADE_AT, VERDICT_AT = 20, 60


@dataclass
class Row:
    name: str
    start: Optional[date] = None
    sessions: int = 0
    ret: Optional[float] = None
    ret_adj: Optional[float] = None
    vs_spy: Optional[float] = None
    vs_ladder: Optional[float] = None
    max_dd: Optional[float] = None
    trades: Optional[int] = None
    win_rate: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    note: str = ""


@dataclass
class Board:
    rows: list[Row]
    slippage: dict[str, tuple[int, Optional[float]]]
    notes: list[str] = field(default_factory=list)


def _day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(NY).date()


def daily_last(events: Iterable[dict], key: str) -> dict[date, float]:
    """The last snapshot of each day's ``key`` (equity, or a rung's value)."""
    out: dict[date, float] = {}
    for event in events:
        if event.get("event") != "snapshot":
            continue
        value = event.get(key) if "." not in key else (event.get(key.split(".")[0]) or {}).get(key.split(".")[1])
        if value in (None, ""):
            continue
        out[_day(event["at"])] = float(value)
    return dict(sorted(out.items()))


def total_return(series: dict[date, float]) -> Optional[float]:
    values = list(series.values())
    if len(values) < 2 or values[0] <= 0:
        return None
    return values[-1] / values[0] - 1


def max_drawdown(series: dict[date, float]) -> Optional[float]:
    peak, worst = None, 0.0
    for value in series.values():
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return worst if series else None


def window_return(series: dict[date, float], start: date, end: date) -> Optional[float]:
    """Return from the last value on or before ``start`` to the last on or
    before ``end``; None when the series does not cover ``start``."""
    before = [d for d in series if d <= start]
    upto = [d for d in series if d <= end]
    if not before or not upto:
        return None
    first, last = series[max(before)], series[max(upto)]
    return last / first - 1 if first > 0 else None


def slippage_table(lines: Iterable[dict]) -> dict[str, tuple[int, Optional[float]]]:
    """The execution test's per-kind adverse slippage, pooled over sessions
    (fill-weighted mean bp)."""
    pooled: dict[str, list[float]] = {}
    for line in lines:
        for kind, cell in (line.get("slippage_bp_vs_model") or {}).items():
            n, mean = int(cell.get("n") or 0), cell.get("mean")
            if n and mean is not None:
                pooled.setdefault(kind, []).extend([float(mean)] * n)
    table: dict[str, tuple[int, Optional[float]]] = {}
    for kind in ("entry", "stop", "reversal", "close_out"):
        values = pooled.get(kind, [])
        table[kind] = (len(values), sum(values) / len(values) if values else None)
    return table


def _rate(table: dict[str, tuple[int, Optional[float]]], kind: str) -> Optional[float]:
    n, mean = table.get(kind, (0, None))
    if n < MIN_FILLS or mean is None:
        return None
    return max(mean, 0.0) / 1e4


@dataclass
class Trade:
    pid: str
    symbol: str
    pnl: float
    cost: Optional[float]  # slippage charge at the measured rates; None = pending


def lab_trades(events: list[dict], table: dict[str, tuple[int, Optional[float]]]) -> list[Trade]:
    """Closed round trips from a lab ledger (settled fills grouped by pid),
    with each fill charged the measured slippage of its kind."""
    plans = {e["pid"]: e for e in events if e.get("event") == "position_opened"}
    closed = {e["pid"] for e in events if e.get("event") == "position_closed"}
    by_pid: dict[str, list[dict]] = {}
    for event in events:
        if event.get("event") == "settled" and event.get("pid") in plans and float(event.get("filled_qty") or 0) > 0:
            by_pid.setdefault(event["pid"], []).append(event)
    trades = []
    for pid, fills in by_pid.items():
        held = sum(float(f["filled_qty"]) * (1 if f["side"] == "buy" else -1) for f in fills)
        if pid not in closed or abs(held) > 1e-9:
            continue
        pnl, cost, pending = 0.0, 0.0, False
        for fill in fills:
            notional = float(fill["filled_qty"]) * float(fill["avg_price"]) * float(fill.get("multiplier", 1))
            pnl += notional if fill["side"] == "sell" else -notional
            kind = "entry" if fill["side"] == "buy" else ("stop" if fill.get("reason") == "stop" else "close_out")
            rate = _rate(table, kind)
            if rate is None:
                pending = True
            else:
                cost += notional * rate
        trades.append(Trade(pid, plans[pid]["symbol"], pnl, None if pending else cost))
    return trades


def _trade_stats(row: Row, trades: list[Trade]) -> None:
    row.trades = len(trades)
    if not trades:
        return
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    row.win_rate = len(wins) / len(trades)
    row.avg_win = sum(wins) / len(wins) if wins else None
    row.avg_loss = sum(losses) / len(losses) if losses else None


def _versus(row: Row, series: dict[date, float], spy: dict[date, float], ladder: dict[date, float]) -> None:
    if not series:
        return
    days = list(series)
    row.start, row.sessions = days[0], len(days)
    row.ret = total_return(series)
    row.max_dd = max_drawdown(series)
    if row.ret is not None:
        spy_ret = window_return(spy, days[0], days[-1])
        ladder_ret = window_return(ladder, days[0], days[-1])
        row.vs_spy = None if spy_ret is None else row.ret - spy_ret
        row.vs_ladder = None if ladder_ret is None else row.ret - ladder_ret


def build(
    *,
    ladder_events: list[dict],
    ai_events: list[dict],
    exec_lines: list[dict],
    spy: dict[date, float],
    main_equity: dict[date, float],
    rung_labels: dict[str, str],
) -> Board:
    table = slippage_table(exec_lines)
    ladder = daily_last(ladder_events, "equity")
    rows: list[Row] = []

    row = Row("LEVERAGE LADDER (benchmark)", note="buy-and-hold, not intraday: raw is its line")
    _versus(row, ladder, spy, ladder)
    rows.append(row)
    for rung, label in rung_labels.items():
        row = Row(f"  rung {label}")
        _versus(row, daily_last(ladder_events, f"rungs.{rung}"), spy, ladder)
        rows.append(row)

    ai = daily_last(ai_events, "equity")
    row = Row("AI TRADER")
    _versus(row, ai, spy, ladder)
    trades = lab_trades(ai_events, table)
    _trade_stats(row, trades)
    if row.ret is not None:
        if any(t.cost is None for t in trades):
            row.note = "adjusted: pending (execution test has < 10 measured fills of a needed kind)"
        else:
            first = list(ai.values())[0]
            row.ret_adj = row.ret - sum(t.cost or 0.0 for t in trades) / first
    rows.append(row)

    row = Row("B EXECUTION TEST (not a strategy)")
    if exec_lines:
        days = sorted(_parse(line["day"]) for line in exec_lines)
        live = sum(float(line.get("pnl_live") or 0) for line in exec_lines)
        modelled = sum(float(line.get("pnl_modelled_at_live_sizes") or 0) for line in exec_lines)
        row.start, row.sessions = days[0], len(days)
        row.trades = sum(int(line.get("fills") or 0) for line in exec_lines) // 2
        row.note = f"live P&L ${live:,.2f} vs modelled ${modelled:,.2f} (gap {live - modelled:+,.2f}); the measurement itself"
    rows.append(row)

    row = Row("MAIN BOOK (account)", note="trades and attribution: see the weekly report")
    _versus(row, main_equity, spy, ladder)
    rows.append(row)

    row = Row("SPY")
    window = ladder or ai or main_equity
    if window and spy:
        start, end = list(window)[0], list(window)[-1]
        sliced = {d: v for d, v in spy.items() if start <= d <= end}
        row.start, row.sessions = (min(sliced), len(sliced)) if sliced else (None, 0)
        row.ret = window_return(spy, start, end)
        row.max_dd = max_drawdown(sliced) if sliced else None
    rows.append(row)

    notes = [
        f"Grading: first at {GRADE_AT} sessions, verdict at {VERDICT_AT}. Sessions counted per row.",
        "Slippage (execution test, adverse bp vs its model, pooled): " + ", ".join(
            f"{k} n={n} {'%.2f' % m if m is not None else '-'}" for k, (n, m) in table.items()
        ) + f"; a rate is used from n >= {MIN_FILLS}. Measured on SPY/SH: option fills charged at the same bp "
            "understate their spreads.",
        "vs ladder = the row's return minus the ladder account's over the same days (n/a where the ladder had not "
        "started).",
    ]
    return Board(rows, table, notes)


def _parse(day: str) -> date:
    return date.fromisoformat(day)


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _usd(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:,.0f}"


def render(board: Board, as_of: date) -> str:
    header = (f"{'sleeve':36} {'start':>10} {'sess':>4} {'return':>8} {'adj':>8} {'vs SPY':>8} {'vs ladder':>9} "
              f"{'max DD':>8} {'trades':>6} {'win':>5} {'avg win':>8} {'avg loss':>8}")
    lines = [f"PAPER LAB SCOREBOARD - as of {as_of} (raw paper P&L; adj = after measured slippage)", header,
             "-" * len(header)]
    for r in board.rows:
        lines.append(
            f"{r.name[:36]:36} {str(r.start or '-'):>10} {r.sessions:>4} {_pct(r.ret):>8} {_pct(r.ret_adj):>8} "
            f"{_pct(r.vs_spy):>8} {_pct(r.vs_ladder):>9} {_pct(r.max_dd):>8} "
            f"{('-' if r.trades is None else r.trades):>6} "
            f"{('-' if r.win_rate is None else f'{r.win_rate:.0%}'):>5} {_usd(r.avg_win):>8} {_usd(r.avg_loss):>8}"
        )
        if r.note:
            lines.append(f"{'':38}{r.note}")
    lines.append("")
    lines.extend(board.notes)
    return "\n".join(lines)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
