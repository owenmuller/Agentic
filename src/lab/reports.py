"""The paper lab's Discord cards (human ruling 2026-10-12), built from the
ledger and the engines: plain English, every return beside SPY's, the
detail attached. Pure over their inputs; the CLI sends them."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from execution import cards

NY = ZoneInfo("America/New_York")
SpyBetween = Callable[[date, date], Optional[float]]


def _day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(NY).date()


def start_of(events: list[dict]) -> tuple[Optional[date], Optional[float]]:
    """The account's first recorded value and its day: the start line every
    'since start' return is measured from."""
    for event in events:
        if event.get("event") == "snapshot" and event.get("equity"):
            return _day(event["at"]), float(event["equity"])
    return None, None


def ladder_report(engine: Any, events: list[dict], labels: dict[str, str], spy_between: SpyBetween,
                  today: date) -> Optional[cards.Card]:
    """After a run that rebalanced (or traded), the rebalance card; None on
    a snapshot-only run."""
    if engine.rebalance is None and not engine.fills:
        return None
    bought: dict[str, float] = {}
    sold: dict[str, float] = {}
    for fill in engine.fills:
        value = float(Decimal(str(fill["quantity"])) * Decimal(str(fill["price"])))
        book = bought if fill["side"] == "buy" else sold
        book[fill["symbol"]] = book.get(fill["symbol"], 0.0) + value
    value = float(engine.equity or 0)
    start_day, start_value = start_of(events)
    since = value / start_value - 1 if start_value else None
    spy = spy_between(start_day, today) if start_day else None
    faults = [e for e in events if e.get("event") == "fault" and _day(e["at"]) == today]
    month_name = datetime.strptime(engine.month or today.strftime("%Y-%m"), "%Y-%m").strftime("%B")
    detail = "\n".join(
        ["Fills this run (rung, side, symbol, quantity, price, limit, quote midpoint at the plan):"]
        + [f"  {labels.get(f['rung'], f['rung'])}: {f['side']} {f['symbol']} {f['quantity']} at {f['price']} "
           f"(limit {f['limit']}, midpoint {Decimal(str(f['modelled'])):.4f})" for f in engine.fills]
        + ["", "Rung values after the run:"]
        + [f"  {labels.get(name, name)}: ${value_}" for name, value_ in (engine.books or {}).items()]
        + (["", "Problems:"] + [f"  {e['message']}" for e in faults] if faults else [])
    )
    return cards.ladder_card(
        month=month_name, complete=engine.rebalance == "complete", bought=bought, sold=sold, value=value,
        since_start=since, spy_since=spy,
        rungs={labels.get(name, name).split(" (")[0]: float(v) for name, v in (engine.books or {}).items()},
        faults=[e["message"] for e in faults], detail=detail,
    )


def ai_day_report(events: list[dict], snapshot: dict, open_positions: list[Any], spy_today: Optional[float],
                  cap: float, halted: bool, today: date) -> cards.Card:
    """The AI trader's end-of-day card from today's ledger."""
    todays = [e for e in events if _day(e["at"]) == today]
    plans = {e["pid"]: e for e in events if e.get("event") == "position_opened"}
    fills_by_pid: dict[str, list[dict]] = {}
    for e in events:
        if e.get("event") == "settled" and float(e.get("filled_qty") or 0) > 0:
            fills_by_pid.setdefault(e["pid"], []).append(e)
    opened = [p for p in plans.values() if _day(p["at"]) == today and p["pid"] in fills_by_pid]
    closed_today = [e for e in todays if e.get("event") == "position_closed" and e.get("reason") != "entry_unfilled"]
    results = []
    for close in closed_today:
        pnl = 0.0
        for fill in fills_by_pid.get(close["pid"], []):
            notional = float(fill["filled_qty"]) * float(fill["avg_price"]) * float(fill.get("multiplier", 1))
            pnl += notional if fill["side"] == "sell" else -notional
        results.append((close.get("symbol"), close.get("reason"), pnl))
    wins = sum(1 for _, _, pnl in results if pnl > 0)
    passes = [e for e in todays if e.get("event") == "llm_pass"]
    ideas = sum(len(e.get("ideas") or []) for e in passes)
    spend = sum(float(e.get("cost_usd") or 0) for e in passes)
    faults = [e for e in todays if e.get("event") == "fault"]
    rejected = [e for e in todays if e.get("event") == "idea_rejected"]
    anchors = [e for e in todays if e.get("event") == "anchor_check" and e.get("price_seen_gap")]
    equity = float(snapshot.get("equity") or 0)
    prior = float(snapshot.get("last_equity") or 0)
    holding = [f"{p.symbol} {p.instrument}" for p in open_positions]
    # Leverage is measured (ruling 2026-10-14, second, item 2).
    snaps = [e for e in events if e.get("event") == "snapshot" and e.get("gross_face") is not None]
    gross_line = (f"Gross exposure (latest): face {float(snaps[-1]['gross_face']):.0%} of equity, "
                  f"leverage-adjusted {float(snaps[-1]['gross_leverage_adjusted']):.0%}"
                  if snaps else "Gross exposure: not measured yet")
    levered = [p for p in opened if p.get("tag")]
    levered_line = f"Leveraged or inverse ETF entries today: {len(levered)}" + (
        " (" + ", ".join(f"{p['symbol']} {p.get('leverage')}x" for p in levered) + ")" if levered else "")
    detail = "\n".join(
        [f"Account value: ${equity:,.2f} (previous close ${prior:,.2f})", gross_line, levered_line, "",
         "Positions closed today (symbol, why, result):"]
        + [f"  {s}: {r.replace('_', ' ')}, ${pnl:,.2f}" for s, r, pnl in results] + ([] if results else ["  none"])
        + ["", "Positions opened today:"]
        + [f"  {p['symbol']} {p['instrument']} ({p['horizon']}): stop {p.get('stop')}, target {p.get('target')}; "
           f"{str(p.get('thesis', ''))[:160]}" for p in opened] + ([] if opened else ["  none"])
        + ["", "Ideas the rules turned down:"]
        + [f"  {e.get('symbol')}: {e.get('reason')}" for e in rejected] + ([] if rejected else ["  none"])
        + ["", "Prices the model cited vs the live price (gap):"]
        + [f"  {e.get('symbol')}: saw {e.get('price_seen')}, live {e.get('live_mid')} "
           f"({float(e['price_seen_gap']) * 100:+.1f}%)" for e in anchors] + ([] if anchors else ["  none cited"])
        + ["", f"Model passes: {len(passes)}; spend ${spend:.2f} (token estimate plus searches)"]
        + (["", "Problems:"] + [f"  {e['message']}" for e in faults] if faults else [])
    )
    return cards.ai_day_card(
        today=(equity / prior - 1) if prior > 0 else None, today_usd=(equity - prior) if prior > 0 else None,
        spy_today=spy_today, opened=len(opened), closed=len(results), wins=wins, losses=len(results) - wins,
        holding=holding, ideas=ideas, taken=len(opened), spend=spend, cap=cap,
        faults=[e["message"] for e in faults], halted=halted,
        detail=detail,
    )


def scoreboard_report(board: Any, as_of: date, table: str, grade_at: int, verdict_at: int) -> cards.Card:
    """Summary line per account plus whether it is too early to judge; the
    full table attached."""
    rows = {r.name.strip(): r for r in board.rows}
    lines = []

    def since(row) -> Optional[str]:
        return f"{row.start:%b} {row.start.day}" if row.start else None

    ladder = rows.get("LEVERAGE LADDER (benchmark)")
    if ladder is not None:
        lines.append(cards.BoardLine("Leverage ladder", ladder.ret, _spy(ladder), since(ladder)))
    ai = rows.get("AI TRADER")
    if ai is not None:
        extra = ""
        if ai.trades:
            extra = f"{ai.trades} trades, {round((ai.win_rate or 0) * ai.trades)} won"
        if ai.ret_adj is not None:
            extra += (", " if extra else "") + f"{cards.pct(ai.ret_adj)} after measured fill costs"
        if _spmo(ai) is not None:
            extra += (", " if extra else "") + f"SPMO {cards.pct(_spmo(ai))} over the same days"
        lines.append(cards.BoardLine("AI trader", ai.ret, _spy(ai), since(ai), extra))
    momentum = rows.get("MOMENTUM SLEEVE (main book; control)")
    if momentum is not None and momentum.ret is not None:
        lines.append(cards.BoardLine(
            "Momentum sleeve (control)", momentum.ret, _spy(momentum), since(momentum),
            f"SPMO {cards.pct(_spmo(momentum))} over the same days" if _spmo(momentum) is not None else ""))
    if getattr(board, "ab_lines", None):
        # the research brief's A/B (ruling 2026-10-15, second): flags up front, the arms in the table
        flags = [f.replace("FLAG: ", "") for f in board.ab_flags]
        lines.append(cards.BoardLine("Research brief A/B", None, None, None,
                                     ("FLAG: " + "; ".join(flags)) if flags else "no flag; by-arm numbers in the table"))
    test = rows.get("B EXECUTION TEST (not a strategy)")
    if test is not None and test.sessions:
        lines.append(cards.BoardLine(
            "Execution test", None, None, None,
            f"{test.sessions} session{'' if test.sessions == 1 else 's'}, {cards.usd(test.live_pnl, signed=True)} live vs "
            f"{cards.usd(test.model_pnl, signed=True)} in the backtest model (a plumbing test, not a strategy)"))
    main = rows.get("MAIN BOOK (account)")
    if main is not None:
        lines.append(cards.BoardLine("Main book", main.ret, _spy(main), since(main)))
    sessions = max([r.sessions for key, r in rows.items() if key in ("AI TRADER", "LEVERAGE LADDER (benchmark)")]
                   or [0])
    return cards.scoreboard_card(f"{as_of:%b} {as_of.day}", lines, sessions, grade_at, verdict_at, table,
                                 file_stamp=as_of.isoformat())


def _spmo(row: Any) -> Optional[float]:
    """SPMO over the row's own window (ruling 2026-10-15, item 4)."""
    if row.ret is None or getattr(row, "vs_spmo", None) is None:
        return None
    return row.ret - row.vs_spmo


def _spy(row: Any) -> Optional[float]:
    """SPY over the row's own window: its return minus its excess."""
    if row.ret is None or row.vs_spy is None:
        return None
    return row.ret - row.vs_spy
