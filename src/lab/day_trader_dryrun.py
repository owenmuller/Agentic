"""A dry run of the day-trader rules (human ruling 2026-11-16) on a day's REAL
ideas: ``python -m lab day-trader-dryrun --day YYYY-MM-DD [--confirm]``.

Read-only. For every idea the AI trader proposed that day, at the time its
pass returned:
  - the intraday context the engine would have computed (today's IEX bars, as
    live; the move since public is the row the engine logged then);
  - the verdict: refused (not measurable, or extended with no pullback level
    beyond the price), at the market, or a pullback entry at its level;
  - for a pullback: whether the order would have filled within its window
    (SIP minute bars: a resting limit fills at the first bar that trades at
    or through it, at the level or the bar's open if it opened through; an
    option trigger the same on the underlying), and the trade from that fill
    to its stop, target or horizon - beside the at-market trade it replaced
    (the chase) on the same bars. A horizon not yet ended is marked at the
    last bar available ("open").
  - with ``--confirm``, the confirm step itself on the day's real ideas and
    contexts (the production client; a few cents).

Returns are on the UNDERLYING in the idea's direction (an option's own
return is not modelled)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from lab.ai_prompts import ConfirmItem, confirm_system, confirm_tool, confirm_user
from lab.ai_trader import NY, bullish, levels
from lab.intraday import Context, pullback_level
from lab.review_shadows import SIP_DELAY, horizon_end

Bars = Callable[[str, datetime, datetime], list[dict]]


def _ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _d(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (ArithmeticError, ValueError):
        return None


def regular(rows: list[dict], calendar: Any) -> list[dict]:
    """Regular-session bars only: the engine acts only in session, so a
    pre-market or after-hours print can neither fill nor stop it."""
    out = []
    for bar in rows:
        stamp = _ts(bar["t"])
        session = calendar.session(stamp.astimezone(NY).date())
        if session is not None and session.open <= stamp < session.close:
            out.append(bar)
    return out


@dataclass
class Walk:
    status: str                       # done, open, no_bars
    reason: str = ""
    at: Optional[datetime] = None
    price: Optional[Decimal] = None
    ret: Optional[Decimal] = None


def walk(rows: list[dict], bull: bool, anchor: Decimal, stop: Decimal, target: Decimal, ended: bool) -> Walk:
    """From ``anchor`` over minute bars (stop before target within a bar, a
    gap through the stop fills at the open), to the last bar if neither."""
    if not rows:
        return Walk("no_bars")
    for bar in rows:
        o, h, l = Decimal(str(bar["o"])), Decimal(str(bar["h"])), Decimal(str(bar["l"]))
        if bull:
            if l <= stop:
                return _out("stop", bar, min(o, stop), anchor, bull)
            if h >= target:
                return _out("target", bar, max(o, target), anchor, bull)
        else:
            if h >= stop:
                return _out("stop", bar, max(o, stop), anchor, bull)
            if l <= target:
                return _out("target", bar, min(o, target), anchor, bull)
    last = rows[-1]
    out = _out("horizon_end" if ended else "marked", last, Decimal(str(last["c"])), anchor, bull)
    out.status = "done" if ended else "open"
    return out


def _out(reason: str, bar: dict, price: Decimal, anchor: Decimal, bull: bool) -> Walk:
    ret = price / anchor - 1 if bull else 1 - price / anchor
    return Walk("done", reason, _ts(bar["t"]), price, ret)


def fill(rows: list[dict], bull: bool, level: Decimal, stop_level: Decimal, kind: str) -> tuple[Optional[dict], str]:
    """The first bar that reaches the level. A resting limit fills at the
    level (or at the open when the bar opened through it); a trigger is
    cancelled instead when the bar opened through its stop level."""
    for bar in rows:
        o, h, l = Decimal(str(bar["o"])), Decimal(str(bar["h"])), Decimal(str(bar["l"]))
        touched = l <= level if bull else h >= level
        if not touched:
            continue
        through_stop = o <= stop_level if bull else o >= stop_level
        if kind == "trigger" and through_stop:
            return None, f"opened through its stop level at {_ts(bar['t']).astimezone(NY):%H:%M}"
        price = min(o, level) if bull else max(o, level)
        return {"at": _ts(bar["t"]), "price": price}, ""
    return None, "the price never came back within the window"


@dataclass
class Row:
    scan: str
    symbol: str
    idea: dict
    actual: str
    verdict: str = ""
    context: Optional[Context] = None
    level: Optional[Decimal] = None
    filled: Optional[dict] = None
    unfilled_why: str = ""
    pullback: Optional[Walk] = None
    chase: Optional[Walk] = None
    item: Optional[ConfirmItem] = None
    decision: str = ""


def run(events: list[dict], day: date, config: Any, *, context: Callable[[str, datetime, Optional[Decimal]], Context],
        bars: Bars, calendar: Any, now: datetime) -> list[Row]:
    cfg = config
    multiple = cfg.day_trader.extended_atr
    on_day = [e for e in events if _ts(e["at"]).astimezone(NY).date() == day]
    anchors = {(e.get("scan"), str(e.get("symbol", "")).upper()): _d(e.get("live_mid"))
               for e in on_day if e.get("event") == "anchor_check"}
    moves = {(e.get("scan"), str(e.get("symbol", "")).upper()): e for e in on_day if e.get("event") == "move_since_public"}
    opened = {(e.get("scan"), e["symbol"]): e for e in on_day if e.get("event") == "position_opened"}
    refused = {(e.get("scan"), str(e.get("symbol", "")).upper()): e for e in on_day if e.get("event") == "idea_rejected"}
    rows: list[Row] = []
    for event in on_day:
        if event.get("event") != "llm_pass":
            continue
        scan, as_of = event.get("scan"), _ts(event["at"])
        for idea in event.get("ideas") or []:
            symbol = str(idea.get("symbol", "")).upper().strip()
            key = (scan, symbol)
            actual = ("taken" if key in opened else
                      f"refused: {refused[key]['reason'][:90]}" if key in refused else "not placed")
            row = Row(scan, symbol, idea, actual)
            rows.append(row)
            instrument = str(idea.get("instrument"))
            bull = bullish(instrument)
            sign = 1 if bull else -1
            price = anchors.get(key)
            ctx = context(symbol, as_of, price)
            ctx.catalyst = dict(moves.get(key) or {})
            row.context = ctx
            extended = ctx.extended(sign, multiple)
            if extended is None:
                row.verdict = "REFUSED: extension not measurable"
                continue
            try:
                stop_f, target_f = Decimal(str(idea["stop_pct"])) / 100, Decimal(str(idea["target_pct"])) / 100
            except (KeyError, ArithmeticError, ValueError):
                row.verdict = "no levels"
                continue
            end = horizon_end(as_of, str(idea.get("horizon")), calendar, cfg.intraday_close,
                              dict(cfg.horizon_sessions)) or as_of
            limit_end = min(end, now - SIP_DELAY)
            minute = regular(sorted(bars(symbol, as_of + timedelta(minutes=1), limit_end), key=lambda b: _ts(b["t"])),
                             calendar) if limit_end > as_of else []
            anchor = ctx.price
            if anchor is not None:
                stop, target = levels(instrument, anchor, stop_f, target_f)
                row.chase = walk(minute, bull, anchor, stop, target, end <= now - SIP_DELAY)
            entry = "entered at the market."
            if extended:
                level, why = pullback_level(ctx, bull)
                if level is None:
                    row.verdict = f"REFUSED: extended; {why}"
                    continue
                row.level = level
                cutoff = (calendar.session(day).at(cfg.no_intraday_entries_after if idea.get("horizon") == "intraday"
                                                   else cfg.no_entries_after))
                until = min(as_of + timedelta(minutes=cfg.day_trader.pullback_minutes), cutoff)
                kind = "trigger" if instrument in ("call", "put") else "limit"
                stop_level, _ = levels(instrument, level, stop_f, target_f)
                window = [b for b in minute if _ts(b["t"]) < until]
                row.filled, row.unfilled_why = fill(window, bull, level, stop_level, kind)
                row.verdict = f"PULLBACK {kind} at {level} until {until.astimezone(NY):%H:%M}"
                entry = (f"a pullback entry at {level} ({'the higher of VWAP and the opening-range low' if bull else 'the lower of VWAP and the opening-range high'}), "
                         f"valid until {until.astimezone(NY):%H:%M} ET; no entry if the price does not come back.")
                if row.filled is not None:
                    p = row.filled["price"]
                    pstop, ptarget = levels(instrument, p, stop_f, target_f)
                    after = [b for b in minute if _ts(b["t"]) > row.filled["at"]]
                    row.pullback = walk(after, bull, p, pstop, ptarget, end <= now - SIP_DELAY)
            else:
                row.verdict = "AT THE MARKET (not extended)"
            row.item = ConfirmItem(idea, ctx.line(sign, multiple), entry)
    return rows


def confirm(rows: list[Row], config: Any, llm: Any, events: list[dict], day: date) -> float:
    """The confirm step on the day's real ideas, pass by pass; returns spend."""
    spend = 0.0
    times = {e.get("scan"): _ts(e["at"]) for e in events if e.get("event") == "llm_pass"
             and _ts(e["at"]).astimezone(NY).date() == day}
    by_scan: dict[str, list[Row]] = {}
    for row in rows:
        if row.item is not None:
            by_scan.setdefault(row.scan, []).append(row)
    for scan, group in by_scan.items():
        symbols = sorted({r.symbol for r in group})
        out = llm.confirm(system=confirm_system(config), user=confirm_user(times[scan], [r.item for r in group]),
                          tool=confirm_tool(symbols), max_tokens=config.day_trader.confirm_max_tokens)
        spend += float(out.cost_usd)
        decided = {str(d.get("symbol", "")).upper(): d for d in (out.structured or {}).get("decisions") or []}
        for row in group:
            d = decided.get(row.symbol)
            row.decision = f"{d.get('decision', '?').upper()}: {str(d.get('reason', ''))[:160]}" if d else "no decision"
    return spend


def _pct(value: Optional[Decimal]) -> str:
    return "n/a" if value is None else f"{value:+.2%}"


def _walk(w: Optional[Walk]) -> str:
    if w is None:
        return "n/a"
    if w.status == "no_bars":
        return "no bars"
    when = f" {w.at.astimezone(NY):%H:%M}" if w.at else ""
    return f"{_pct(w.ret)} ({w.reason}{when}{', still open' if w.status == 'open' else ''})"


def render(rows: list[Row], day: date) -> str:
    lines = [f"DAY-TRADER DRY RUN on the AI trader's real ideas of {day} (returns on the underlying, the idea's "
             f"direction)"]
    counts: dict[str, int] = {}
    for row in rows:
        kind = row.verdict.split(":")[0].split(" ")[0] if row.verdict else "?"
        counts[kind] = counts.get(kind, 0) + 1
        idea = row.idea
        lines.append(f"{row.scan} {row.symbol} {idea.get('instrument')} {idea.get('horizon')} conf {idea.get('confidence')}"
                     f" - actually {row.actual}")
        if row.context is not None:
            lines.append(f"    context: {row.context.line(1 if bullish(str(idea.get('instrument'))) else -1, Decimal('1'))}")
        lines.append(f"    verdict: {row.verdict}")
        if row.level is not None:
            if row.filled is not None:
                lines.append(f"    pullback FILLED {row.filled['at'].astimezone(NY):%H:%M} at {row.filled['price']}; "
                             f"then {_walk(row.pullback)}")
            else:
                lines.append(f"    pullback NOT FILLED: {row.unfilled_why}")
        if row.chase is not None:
            lines.append(f"    the at-market entry (the chase): {_walk(row.chase)}")
        if row.decision:
            lines.append(f"    confirm step: {row.decision}")
    filled = [r for r in rows if r.filled is not None]
    lines.append("")
    lines.append("SUMMARY: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) +
                 f"; pullback orders {sum(1 for r in rows if r.level is not None)}, filled {len(filled)}")
    paired = [(r.pullback.ret, r.chase.ret) for r in filled
              if r.pullback and r.chase and r.pullback.ret is not None and r.chase.ret is not None]
    if paired:
        diff = sum(p - c for p, c in paired) / len(paired)
        lines.append(f"  filled pullbacks vs the chase they replaced: n {len(paired)}, mean difference "
                     f"{diff:+.2%} a trade (pullback minus chase)")
    unfilled = [r.chase.ret for r in rows if r.level is not None and r.filled is None and r.chase and
                r.chase.ret is not None]
    if unfilled:
        lines.append(f"  unfilled pullbacks - the chase not taken: n {len(unfilled)}, mean {sum(unfilled) / len(unfilled):+.2%}")
    refused = [r.chase.ret for r in rows if r.verdict.startswith("REFUSED") and r.chase and r.chase.ret is not None]
    if refused:
        lines.append(f"  refused outright - the chase not taken: n {len(refused)}, mean {sum(refused) / len(refused):+.2%}")
    return "\n".join(lines)


def golden_cases(rows: list[Row], events: list[dict], day: date) -> list[dict]:
    """Confirm-step golden cases from the day's real ideas and contexts."""
    times = {e.get("scan"): _ts(e["at"]) for e in events if e.get("event") == "llm_pass"
             and _ts(e["at"]).astimezone(NY).date() == day}
    out = []
    by_scan: dict[str, list[Row]] = {}
    for row in rows:
        if row.item is not None:
            by_scan.setdefault(row.scan, []).append(row)
    for scan, group in by_scan.items():
        keep = ("symbol", "instrument", "horizon", "setup", "confidence", "stop_pct", "target_pct", "thesis",
                "catalyst")
        out.append({"name": f"{times[scan].astimezone(NY):%Y-%m-%d}_{scan.replace(':', '')}",
                    "time": f"{times[scan].astimezone(NY):%H:%M}",
                    "items": [{"idea": {k: r.idea.get(k) for k in keep if r.idea.get(k) is not None},
                               "context": r.item.context, "entry": r.item.entry} for r in group]})
    return out
