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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from lab import leverage

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
    #: the row's return minus SPMO's over the same days (ruling 2026-10-15)
    vs_spmo: Optional[float] = None
    max_dd: Optional[float] = None
    trades: Optional[int] = None
    win_rate: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    #: The execution test only: live and modelled dollars.
    live_pnl: Optional[float] = None
    model_pnl: Optional[float] = None
    note: str = ""


@dataclass
class Board:
    rows: list[Row]
    slippage: dict[str, tuple[int, Optional[float]]]
    notes: list[str] = field(default_factory=list)
    #: the research brief's A/B by arm, and its flags (ruling 2026-10-15, second)
    ab_lines: list[str] = field(default_factory=list)
    ab_flags: list[str] = field(default_factory=list)


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
    #: leveraged_etf / inverse_etf / None (ruling 2026-10-14, second, item 2)
    tag: Optional[str] = None


def lab_trades(events: list[dict], table: dict[str, tuple[int, Optional[float]]],
               leverage_table: Optional[dict] = None) -> list[Trade]:
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
        plan = plans[pid]
        tag = plan.get("tag") if "tag" in plan else leverage.tag(
            leverage.factor(plan["symbol"], "etf", leverage_table or {}) if plan.get("instrument") in ("stock", "etf")
            else leverage.ONE)
        trades.append(Trade(pid, plan["symbol"], pnl, None if pending else cost, tag))
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


def _versus(row: Row, series: dict[date, float], spy: dict[date, float], ladder: dict[date, float],
            spmo: Optional[dict[date, float]] = None) -> None:
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
        spmo_ret = window_return(spmo, days[0], days[-1]) if spmo else None
        row.vs_spmo = None if spmo_ret is None else row.ret - spmo_ret


def build(
    *,
    ladder_events: list[dict],
    ai_events: list[dict],
    exec_lines: list[dict],
    spy: dict[date, float],
    main_equity: dict[date, float],
    rung_labels: dict[str, str],
    stop_floor: Optional[float] = None,
    leverage_table: Optional[dict] = None,
    momentum: Optional[dict[date, float]] = None,
    spmo: Optional[dict[date, float]] = None,
) -> Board:
    table = slippage_table(exec_lines)
    ladder = daily_last(ladder_events, "equity")
    rows: list[Row] = []

    row = Row("LEVERAGE LADDER (benchmark)", note="buy-and-hold, not intraday: raw is its line")
    _versus(row, ladder, spy, ladder, spmo)
    rows.append(row)
    for rung, label in rung_labels.items():
        row = Row(f"  rung {label}")
        _versus(row, daily_last(ladder_events, f"rungs.{rung}"), spy, ladder, spmo)
        rows.append(row)

    ai = daily_last(ai_events, "equity")
    row = Row("AI TRADER")
    _versus(row, ai, spy, ladder, spmo)
    trades = lab_trades(ai_events, table, leverage_table)
    _trade_stats(row, trades)
    if row.ret is not None:
        if any(t.cost is None for t in trades):
            row.note = "adjusted: pending (execution test has < 10 measured fills of a needed kind)"
        else:
            first = list(ai.values())[0]
            row.ret_adj = row.ret - sum(t.cost or 0.0 for t in trades) / first
    rows.append(row)
    # Leveraged and inverse ETFs apart from the other picks (ruling 2026-10-14,
    # second, item 2): closed-trade statistics only; the account has one equity line.
    for label, subset in (("  AI trader: leveraged / inverse ETFs", [t for t in trades if t.tag]),
                          ("  AI trader: stocks, plain ETFs, options", [t for t in trades if not t.tag])):
        sub = Row(label)
        _trade_stats(sub, subset)
        sub.note = "closed-trade statistics only" + (f"; net ${sum(t.pnl for t in subset):+,.0f}" if subset else "")
        rows.append(sub)

    # The control beside the AI trader (ruling 2026-10-15, item 4): the main
    # book's mechanical momentum sleeve (v1.1, its realized fills) and SPMO.
    row = Row("MOMENTUM SLEEVE (main book; control)", note="v1.1 live, Modified Dietz on the invested book")
    _versus(row, momentum or {}, spy, ladder, spmo)
    rows.append(row)
    row = Row("SPMO", note="over the AI trader's days")
    if ai and spmo:
        start, end = list(ai)[0], list(ai)[-1]
        sliced = {d: v for d, v in spmo.items() if start <= d <= end}
        row.start, row.sessions = (min(sliced), len(sliced)) if sliced else (None, 0)
        row.ret = window_return(spmo, start, end)
        row.max_dd = max_drawdown(sliced) if sliced else None
        spy_ret = window_return(spy, start, end)
        row.vs_spy = None if row.ret is None or spy_ret is None else row.ret - spy_ret
    rows.append(row)

    row = Row("B EXECUTION TEST (not a strategy)")
    if exec_lines:
        days = sorted(_parse(line["day"]) for line in exec_lines)
        live = sum(float(line.get("pnl_live") or 0) for line in exec_lines)
        modelled = sum(float(line.get("pnl_modelled_at_live_sizes") or 0) for line in exec_lines)
        row.start, row.sessions = days[0], len(days)
        row.trades = sum(int(line.get("fills") or 0) for line in exec_lines) // 2
        row.live_pnl, row.model_pnl = live, modelled
        row.note = f"live P&L ${live:,.2f} vs modelled ${modelled:,.2f} (gap {live - modelled:+,.2f}); the measurement itself"
    rows.append(row)

    row = Row("MAIN BOOK (account)", note="trades and attribution: see the weekly report")
    _versus(row, main_equity, spy, ladder, spmo)
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
        stop_floor_note(table, stop_floor),
        "Slippage (execution test, adverse bp vs its model, pooled): " + ", ".join(
            f"{k} n={n} {'%.2f' % m if m is not None else '-'}" for k, (n, m) in table.items()
        ) + f"; a rate is used from n >= {MIN_FILLS}. Measured on SPY/SH: option fills charged at the same bp "
            "understate their spreads.",
        "vs ladder = the row's return minus the ladder account's over the same days (n/a where the ladder had not "
        "started). vs SPMO likewise against the SPMO ETF (the momentum sleeve's binding comparison).",
    ]
    ab_lines, ab_flags = brief_ab(ai_events)
    return Board(rows, table, notes, ab_lines, ab_flags)


def stop_floor_note(table: dict[str, tuple[int, Optional[float]]], floor: Optional[float]) -> str:
    """Human ruling 2026-10-10 (second): the AI trader's minimum stop distance
    is 1% until the execution test has measured stop slippage on >= 10 fills;
    then a floor of at least 3x the measured stop slippage is PROPOSED here.
    The floor itself changes only by a human ruling - never automatically."""
    n, mean = table.get("stop", (0, None))
    current = "-" if floor is None else f"{floor:.2%}"
    if n < MIN_FILLS or mean is None:
        return (f"AI trader stop floor: {current} holds (ruling 2026-10-10) until stop slippage is measured on "
                f">= {MIN_FILLS} fills (n = {n}).")
    proposed = 3 * max(mean, 0.0) / 1e4
    return (f"AI trader stop floor PROPOSAL (for a human ruling): measured stop slippage {mean:.2f} bp over n = {n}; "
            f"3x = {proposed:.2%}; current floor {current}. A floor of at least {proposed:.2%} is proposed; "
            f"nothing changes until a human rules.")


def _parse(day: str) -> date:
    return date.fromisoformat(day)


def _pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2%}"


def _usd(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:,.0f}"


def render(board: Board, as_of: date) -> str:
    header = (f"{'sleeve':36} {'start':>10} {'sess':>4} {'return':>8} {'adj':>8} {'vs SPY':>8} {'vs ladder':>9} "
              f"{'vs SPMO':>8} "
              f"{'max DD':>8} {'trades':>6} {'win':>5} {'avg win':>8} {'avg loss':>8}")
    lines = [f"PAPER LAB SCOREBOARD - as of {as_of} (raw paper P&L; adj = after measured slippage)", header,
             "-" * len(header)]
    for r in board.rows:
        lines.append(
            f"{r.name[:36]:36} {str(r.start or '-'):>10} {r.sessions:>4} {_pct(r.ret):>8} {_pct(r.ret_adj):>8} "
            f"{_pct(r.vs_spy):>8} {_pct(r.vs_ladder):>9} {_pct(r.vs_spmo):>8} {_pct(r.max_dd):>8} "
            f"{('-' if r.trades is None else r.trades):>6} "
            f"{('-' if r.win_rate is None else f'{r.win_rate:.0%}'):>5} {_usd(r.avg_win):>8} {_usd(r.avg_loss):>8}"
        )
        if r.note:
            lines.append(f"{'':38}{r.note}")
    lines.append("")
    if board.ab_lines:
        lines.extend(board.ab_lines + [""])
    lines.extend(board.notes)
    return "\n".join(lines)


#: The research brief's A/B (human ruling 2026-10-15, second). Flags: the
#: brief arm proposing notably fewer ideas (under this share of the control
#: arm's ideas per pass), or mostly shortlist names (over this share of its
#: ideas), once each arm has the minimum sample.
AB_FEWER = 0.75
AB_SHORTLIST = 0.50
AB_MIN_PASSES, AB_MIN_IDEAS = 10, 10


@dataclass
class Arm:
    name: str
    passes: int = 0
    ideas: int = 0
    on_shortlist: int = 0
    scored: int = 0            # ideas whose shortlist membership was logged
    taken: int = 0
    closed: int = 0
    wins: int = 0
    returns: list = field(default_factory=list)  # closed entries: pnl / cost
    uses: dict = field(default_factory=dict)     # brief_use -> count (brief arm)

    @property
    def per_pass(self) -> Optional[float]:
        return self.ideas / self.passes if self.passes else None

    @property
    def off_shortlist(self) -> Optional[float]:
        return 1 - self.on_shortlist / self.scored if self.scored else None


def brief_ab(events: list[dict]) -> tuple[list[str], list[str]]:
    """(lines, flags): the A/B by arm from the AI trader's ledger - passes,
    ideas proposed, ideas taken, the share of ideas from outside the brief's
    shortlist (logged in BOTH arms), and the forward results of entries
    (closed round trips: wins and mean return on cost)."""
    arms = {"brief": Arm("brief"), "control": Arm("control")}
    start = None
    for e in events:
        if e.get("event") != "llm_pass" or e.get("brief_arm") not in arms:
            continue
        start = start or str(e.get("at", ""))[:10]
        arm = arms[e["brief_arm"]]
        ideas = e.get("ideas") or []
        arm.passes += 1
        arm.ideas += len(ideas)
        flags = e.get("ideas_on_shortlist") or []
        arm.scored += len(flags)
        arm.on_shortlist += sum(1 for f in flags if f)
        for idea in ideas:
            if e["brief_arm"] == "brief":
                use = str(idea.get("brief_use") or "missing")
                arm.uses[use] = arm.uses.get(use, 0) + 1
    if start is None:
        return [], []
    opened = {e["pid"]: e for e in events if e.get("event") == "position_opened" and e.get("brief_arm") in arms}
    closed = {e["pid"] for e in events if e.get("event") == "position_closed"}
    fills: dict[str, list[dict]] = {}
    for e in events:
        if e.get("event") == "settled" and e.get("pid") in opened and float(e.get("filled_qty") or 0) > 0:
            fills.setdefault(e["pid"], []).append(e)
    for pid, plan in opened.items():
        arm = arms[plan["brief_arm"]]
        arm.taken += 1
        legs = fills.get(pid, [])
        held = sum(float(f["filled_qty"]) * (1 if f["side"] == "buy" else -1) for f in legs)
        if pid not in closed or not legs or abs(held) > 1e-9:
            continue
        cost = sum(float(f["filled_qty"]) * float(f["avg_price"]) * float(f.get("multiplier", 1))
                   for f in legs if f["side"] == "buy")
        pnl = sum(float(f["filled_qty"]) * float(f["avg_price"]) * float(f.get("multiplier", 1))
                  * (1 if f["side"] == "sell" else -1) for f in legs)
        if cost > 0:
            arm.closed += 1
            arm.wins += pnl > 0
            arm.returns.append(pnl / cost)
    lines = [f"RESEARCH BRIEF A/B (since {start}; arm drawn at random per pass):"]
    for arm in arms.values():
        mean = sum(arm.returns) / len(arm.returns) if arm.returns else None
        lines.append(
            f"  {arm.name:8} passes {arm.passes:3}  ideas {arm.ideas:3} ({_ratio(arm.per_pass)}/pass)  taken "
            f"{arm.taken:3}  off the shortlist {_share(arm.off_shortlist)}  closed {arm.closed} "
            f"(won {arm.wins}, mean {_pct(mean)} on cost)"
            + (f"  brief use: {', '.join(f'{k} {v}' for k, v in sorted(arm.uses.items()))}" if arm.uses else ""))
    flags = []
    b, c = arms["brief"], arms["control"]
    if b.passes >= AB_MIN_PASSES and c.passes >= AB_MIN_PASSES and c.per_pass and b.per_pass is not None \
            and b.per_pass < AB_FEWER * c.per_pass:
        flags.append(f"FLAG: the brief arm proposes notably fewer ideas ({b.per_pass:.2f} vs {c.per_pass:.2f} a pass)")
    if b.scored >= AB_MIN_IDEAS and b.off_shortlist is not None and 1 - b.off_shortlist > AB_SHORTLIST:
        flags.append(f"FLAG: the brief arm's ideas are mostly shortlist names ({1 - b.off_shortlist:.0%})")
    if not flags:
        lines.append(f"  no flag (fewer ideas: under {AB_FEWER:.0%} of control's rate at {AB_MIN_PASSES}+ passes "
                     f"each; mostly shortlist: over {AB_SHORTLIST:.0%} at {AB_MIN_IDEAS}+ ideas)")
    return lines + [f"  {f}" for f in flags], flags


def _ratio(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _share(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.0%}"


def momentum_series(lines: Iterable[dict], book: str = "v1.1 live") -> dict[date, float]:
    """The momentum sleeve as a value index from the main book's
    ``data/momentum/daily.jsonl`` (one line per session: each book's
    since-start return): 1.0 at the close before its first trade, then
    1 + since-start at each session."""
    out: dict[date, float] = {}
    started = None
    for line in lines:
        value = (line.get("since") or {}).get(book)
        if value is None or not line.get("day"):
            continue
        started = started or line.get("started")
        out[_parse(line["day"])] = 1.0 + float(value)
    if out and started:
        out[_parse(started) - timedelta(days=1)] = 1.0  # the close before the first trade (on-or-before lookups)
    return dict(sorted(out.items()))


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
