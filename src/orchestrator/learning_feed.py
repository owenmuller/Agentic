"""The learning loop's resolved-case feed (human ruling 2026-11-04, item 2):
every resolved JUDGED decision, turned into ``learning.cases.Case`` facts for
the post-mortem writer.

Sources:
  ai-trader   closed round trips from the lab ledger (read as a FILE: the
              orchestrator imports nothing from ``lab``); and every idea its
              model proposed, graded at its own horizon - the ones not traded
              (refused, placed and unfilled, or never placed) become cases of
              kind "refusal" (human ruling 2026-11-13, item 2);
  main        judged trades with an outcome, and LLM declines (no position,
              reward:risk, an overturned vote, an unconfirmed boundary) once
              the forward engine has marked them at the verdict's horizon.

Not journaled, by design: the momentum lots, the mechanical arm, the baseline
sleeve, the cash sweep and the execution test. They are rules, not
judgments - "right for the right reason" has no subject there; their
learning runs through shadows (§ Learning Loop, item 7).

Pure: every market fact comes in through a lookup the caller builds (bars,
sectors, regime, fund names). Nothing here reads P&L totals, account value
or a target (the scoreboard fence).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable, Mapping, Optional, Union
from zoneinfo import ZoneInfo

from learning import vocabulary
from learning.cases import Case

NY = ZoneInfo("America/New_York")
#: An LLM verdict that declined a position; graded at its stated horizon.
DECLINE_CODES = frozenset({"no_position", "insufficient_reward_risk", "unconfirmed_boundary", "vote_overturned"})
HORIZON_SESSIONS = {"days": 5, "weeks": 20, "months": 60}
DEFAULT_HORIZON = 20
#: What a research pass writes in ``tickers`` when it names no instrument
#: (26 declines carry ["NONE"]). The audit log keeps the model's words as
#: written; the feed never reads them as a symbol.
PLACEHOLDERS = frozenset({"NONE", "N/A", "NA", "NULL", "?"})


def is_symbol(ticker) -> bool:
    return bool(ticker) and str(ticker).strip().upper() not in PLACEHOLDERS
#: Broad-market funds; any other fund is a sector fund.
BROAD = frozenset({"SPY", "QQQ", "IWM", "DIA", "RSP", "VTI", "VOO", "IVV", "MDY"})


@dataclass(frozen=True)
class Lookups:
    """Market facts, injected. ``bars``: symbol -> {date: (open, close)};
    ``sector``: symbol -> a vocabulary sector of a COMPANY; ``regime``: day
    -> a vocabulary regime; ``fund``: symbol -> is it a fund; ``leverage``:
    symbol -> signed factor of a leveraged/inverse fund (1 otherwise);
    ``spy_at``: an instant -> SPY's price then (a minute bar), None when not
    available (an idea is then graded from its day's close)."""
    bars: Callable[[str], Mapping[date, tuple[float, float]]]
    sector: Callable[[str], str]
    regime: Callable[[date], str]
    fund: Callable[[str], bool]
    leverage: Callable[[str], float]
    spy_at: Optional[Callable[[datetime], Optional[float]]] = None


def _day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(NY).date()


def _gap(bars: Mapping[date, tuple[float, float]], day: date) -> Optional[float]:
    """The day's open over the prior session's close."""
    days = sorted(d for d in bars if d <= day)
    if len(days) < 2 or days[-1] != day:
        return None
    previous_close = bars[days[-2]][1]
    return bars[day][0] / previous_close - 1 if previous_close > 0 else None


def _close_return(bars: Mapping[date, tuple[float, float]], start: date, end: date) -> Optional[float]:
    """Close before ``start`` to the close of ``end``, percent."""
    before = [d for d in bars if d < start]
    upto = [d for d in bars if d <= end]
    if not before or not upto:
        return None
    first, last = bars[max(before)][1], bars[max(upto)][1]
    return (last / first - 1) * 100 if first > 0 else None


def _split(underlying_pct: Optional[float], spy_pct: Optional[float],
           direction: str) -> tuple[Optional[float], Optional[float]]:
    """(the underlying's own move beyond SPY, SPY's move), both signed in the
    decision's favour: a fall helps a short (a put)."""
    if underlying_pct is None or spy_pct is None:
        return None, None
    sign = -1.0 if direction == "short" else 1.0
    return sign * (underlying_pct - spy_pct), sign * spy_pct


def _sector(symbol: str, fund: bool, factor: float, look: Lookups) -> str:
    if abs(factor) > 1 or factor < 0:
        return "leveraged etf"
    if fund:
        return "broad etf" if symbol.upper() in BROAD else "sector etf"
    return look.sector(symbol)


# -- the AI trader's closed trades ----------------------------------------------------


def ai_trader_cases(events: list[dict], look: Lookups) -> list[Case]:
    """Closed round trips from the AI trader's ledger (settled fills grouped by
    pid, held quantity back to zero, a position_closed event)."""
    opened = {e["pid"]: e for e in events if e.get("event") == "position_opened"}
    closed = {e["pid"]: e for e in events if e.get("event") == "position_closed"}
    fills: dict[str, list[dict]] = {}
    for e in events:
        if e.get("event") == "settled" and e.get("pid") in opened and float(e.get("filled_qty") or 0) > 0:
            fills.setdefault(e["pid"], []).append(e)
    spy = look.bars("SPY")
    out = []
    for pid, plan in opened.items():
        rows = fills.get(pid, [])
        held = sum(float(f["filled_qty"]) * (1 if f["side"] == "buy" else -1) for f in rows)
        if pid not in closed or not rows or abs(held) > 1e-9:
            continue
        cost = sum(float(f["filled_qty"]) * float(f["avg_price"]) * float(f.get("multiplier") or 1)
                   for f in rows if f["side"] == "buy")
        proceeds = sum(float(f["filled_qty"]) * float(f["avg_price"]) * float(f.get("multiplier") or 1)
                       for f in rows if f["side"] == "sell")
        if cost <= 0:
            continue
        symbol, instrument = plan["symbol"], plan.get("instrument") or "stock"
        option = instrument in ("call", "put")
        fund = look.fund(symbol)
        factor = 1.0 if option else look.leverage(symbol)
        if option:
            direction = "short" if instrument == "put" else "long"
        else:
            direction = "short" if factor < 0 else "long"
        entered = _day(min(f["at"] for f in rows if f["side"] == "buy"))
        exited = _day(max(f["at"] for f in rows if f["side"] == "sell"))
        if not spy or exited > max(spy):
            continue  # resolved once its exit session has a close to measure against
        bars = look.bars(symbol)
        gap = _gap(bars, entered)
        if gap is not None and factor < 0:
            gap = -gap / abs(factor)   # an inverse fund's gap, read on the market it bets against
        return_pct = (proceeds / cost - 1) * 100
        spy_pct = _close_return(spy, entered, exited)
        underlying = None
        anchor = plan.get("anchor_mid")
        if anchor and exited in bars:
            underlying = (bars[exited][1] / float(anchor) - 1) * 100
        sessions = len([d for d in spy if entered <= d <= exited]) if spy else None
        conf = plan.get("confidence")
        tags = {
            "sector": _sector(symbol, fund, factor, look),
            "regime": look.regime(entered),
            "confidence_band": vocabulary.confidence_band(conf),
            "source": "ai_trader",
            "instrument": "option" if option else ("etf" if fund or instrument == "etf" else "stock"),
            "entry": vocabulary.entry(direction, gap),
        }
        kind_text = {"call": "call option on", "put": "put option on", "etf": "fund", "stock": "shares of"}
        expected = (
            f"{direction} via {kind_text.get(instrument, instrument)} {symbol}: target {plan.get('target_pct')}% and "
            f"stop {plan.get('stop_pct')}% from entry, horizon {plan.get('horizon')}, confidence {conf}"
        )
        actual = (
            f"closed by {str(closed[pid].get('reason') or 'unknown').replace('_', ' ')} on {exited} after "
            f"{sessions if sessions is not None else '?'} session(s); "
            + (f"{symbol} moved {underlying:+.2f}% from the entry mid to that day's close; " if underlying is not None
               else "")
            + f"the position returned {return_pct:+.2f}% on cost"
        )
        notes = [f"catalyst named: {plan.get('catalyst')}" if plan.get("catalyst") else "catalyst named: none",
                 f"invalidation stated: {plan.get('invalidation') or 'none'}",
                 "SPY measured close to close, from the session before entry to the exit session"]
        if gap is not None:
            notes.append(f"opening gap on the entry day: {gap * 100:+.2f}%")
        if factor != 1.0:
            notes.append(f"leverage factor of the fund: {factor:+g}")
        # the underlying's move: an option's from the entry mid to the exit
        # close; shares' and a long fund's is the position's own (unlevered);
        # an inverse fund's is not comparable with SPY's and stays unknown
        moved = underlying if option else (return_pct / factor if factor > 0 else None)
        own, market = _split(moved, spy_pct, direction)
        out.append(Case(
            decision_id=pid, account="ai-trader", strategy="ai_trader", kind="trade", symbol=symbol,
            opened=entered.isoformat(), resolved=exited.isoformat(), direction=direction,
            thesis=str(plan.get("thesis") or ""), expected=expected, actual=actual, return_pct=return_pct,
            spy_pct=spy_pct, excess=None if spy_pct is None else return_pct - spy_pct,
            own_move=own, market_move=market, tags=tags, notes=tuple(notes)))
    return sorted(out, key=lambda c: (c.resolved, c.decision_id))


# -- every idea the AI trader proposed, graded at its own horizon ---------------------

#: Sessions an idea's horizon spans, counting the day it was proposed - the
#: engine's own count (intraday ends that day; days at the 5th session; weeks
#: at the 20th).
IDEA_SESSIONS = {"intraday": 1, "days": 5, "weeks": 20}
#: When one name is proposed twice the same day in the same direction, the
#: idea counts once, under its most consequential fate.
_FATE = {"taken": 0, "unfilled": 1, "refused": 2, "unplaced": 3}


@dataclass(frozen=True)
class IdeaGrade:
    """One idea the AI trader's model proposed, whatever the rules did with it
    (human ruling 2026-11-13, items 2 and 3), graded at its STATED horizon:
    from the live price the engine read when the idea was proposed (SPY at the
    same minute) to the close of the horizon's last session. Taken and refused
    ideas get the same measure, so they can be compared. Without the minute
    price (or SPY's), a days or weeks idea is graded from its day's close (the
    decline convention) and an intraday one is not graded. An option is graded
    on its underlying, in its direction."""
    idea_id: str                       # "<day>-<scan>-<SYMBOL>"
    day: str
    scan: str
    symbol: str
    instrument: str
    horizon: str
    confidence: Optional[int]
    #: taken | unfilled (placed, never filled) | refused (by a rule, the gate
    #: or the order path) | unplaced (no verdict recorded)
    status: str
    reason: str
    pid: Optional[str]
    direction: str                     # long | short (a put or an inverse fund)
    brief_arm: Optional[str]
    theme: Optional[str]
    thesis: str
    catalyst: str
    stop_pct: Optional[str]
    target_pct: Optional[str]
    #: the last session of the horizon; None while it has not closed
    resolved: Optional[str]
    sessions: int
    #: the underlying's own move (percent, as it moved); the grade in the idea's
    #: favour (a put gains on a fall); SPY's move signed the same way
    underlying_pct: Optional[float]
    return_pct: Optional[float]
    spy_pct: Optional[float]
    excess: Optional[float]
    #: the move already made in the idea's favour when it was proposed: the
    #: proposal price against the prior session's close, percent
    moved_since_close: Optional[float]


def _num(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ai_trader_idea_grades(events: list[dict], look: Lookups) -> list[IdeaGrade]:
    """Every idea in every model pass of the AI trader's ledger, with its fate
    and its grade (None fields while its horizon is still open)."""
    opened = [e for e in events if e.get("event") == "position_opened"]
    fate = {e["pid"]: e.get("reason") for e in events if e.get("event") == "position_closed"}
    rejected = [e for e in events if e.get("event") == "idea_rejected"]
    anchors = {(str(e.get("scan")), _day(e["at"]), str(e.get("symbol", "")).upper()): e
               for e in events if e.get("event") == "anchor_check"}
    spy = look.bars("SPY")
    sessions = sorted(spy)
    out = []
    for p in events:
        if p.get("event") != "llm_pass":
            continue
        day, scan = _day(p["at"]), str(p.get("scan") or "")
        seen: set[str] = set()
        for idea in p.get("ideas") or []:
            symbol = str(idea.get("symbol") or "").upper().strip()
            if not is_symbol(symbol) or symbol in seen:
                continue
            seen.add(symbol)
            plan = next((o for o in opened if _same_idea(o, scan, day, symbol)), None)
            refusal = next((r for r in rejected if _same_idea(r, scan, day, symbol)), None)
            if plan is not None:
                status = "unfilled" if fate.get(plan["pid"]) == "entry_unfilled" else "taken"
                reason = "entry placed and not filled" if status == "unfilled" else ""
            elif refusal is not None:
                status, reason = "refused", str(refusal.get("reason") or "")
            else:
                status, reason = "unplaced", "no verdict recorded for it in its scan"
            mark = anchors.get((scan, day, symbol))
            anchor = _num(plan.get("anchor_mid")) if plan else None
            when = datetime.fromisoformat(plan["at"]) if plan else None
            if anchor is None and mark is not None:
                anchor, when = _num(mark.get("live_mid")), datetime.fromisoformat(mark["at"])
            if anchor is None and refusal is not None:
                anchor = _num((refusal.get("levels") or {}).get("anchor"))
                when = datetime.fromisoformat(refusal["at"])
            out.append(_graded(idea, symbol, day, scan, status, reason, plan, p, anchor,
                               when or datetime.fromisoformat(p["at"]), look, spy, sessions))
    return out


def _same_idea(event: dict, scan: str, day: date, symbol: str) -> bool:
    """An event of the same scan, day and name as an idea."""
    return str(event.get("scan")) == scan and _day(event["at"]) == day and str(event.get("symbol", "")).upper() == symbol


def _graded(idea: dict, symbol: str, day: date, scan: str, status: str, reason: str, plan: Optional[dict],
            p: dict, anchor: Optional[float], when: datetime, look: Lookups,
            spy: Mapping[date, tuple[float, float]], sessions: list[date]) -> IdeaGrade:
    instrument = str(idea.get("instrument") or "stock")
    horizon = str(idea.get("horizon") or "")
    option = instrument in ("call", "put")
    factor = 1.0 if option else look.leverage(symbol)
    direction = ("short" if instrument == "put" else "long") if option else ("short" if factor < 0 else "long")
    sign = -1.0 if instrument == "put" else 1.0     # an inverse fund is HELD: its own rise is the idea's gain
    bars = look.bars(symbol)
    before = [d for d in bars if d < day]
    moved = None
    if anchor and before and bars[max(before)][1] > 0:
        moved = sign * (anchor / bars[max(before)][1] - 1) * 100
    try:
        confidence = int(idea.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    base = dict(
        idea_id=f"{day.isoformat()}-{scan}-{symbol}", day=day.isoformat(), scan=scan, symbol=symbol,
        instrument=instrument, horizon=horizon, confidence=confidence, status=status, reason=reason[:300],
        pid=plan["pid"] if plan else None, direction=direction, brief_arm=p.get("brief_arm"),
        theme=idea.get("theme"), thesis=str(idea.get("thesis") or "")[:1500],
        catalyst=str(idea.get("catalyst") or "")[:300],
        stop_pct=None if idea.get("stop_pct") is None else str(idea.get("stop_pct")),
        target_pct=None if idea.get("target_pct") is None else str(idea.get("target_pct")),
        moved_since_close=moved)
    pending = dict(resolved=None, sessions=0, underlying_pct=None, return_pct=None, spy_pct=None, excess=None)
    wanted = IDEA_SESSIONS.get(horizon)
    ahead = [d for d in sessions if d >= day]
    if wanted is None or len(ahead) < wanted:
        return IdeaGrade(**base, **pending)
    end = ahead[wanted - 1]
    if end not in bars or end not in spy:
        return IdeaGrade(**base, **pending)
    start, spy_start = anchor, (look.spy_at(when) if (anchor and look.spy_at is not None) else None)
    if start is None or spy_start is None:
        if horizon == "intraday" or day not in bars or day not in spy:
            return IdeaGrade(**base, **pending)   # no minute price: an intraday idea has nothing to grade from
        start, spy_start = bars[day][1], spy[day][1]
        if end == day:
            return IdeaGrade(**base, **pending)
    if not start or not spy_start:
        return IdeaGrade(**base, **pending)
    underlying = (bars[end][1] / start - 1) * 100
    market = (spy[end][1] / spy_start - 1) * 100
    excess = sign * (underlying - market)
    return IdeaGrade(**base, resolved=end.isoformat(), sessions=wanted, underlying_pct=underlying,
                     return_pct=sign * underlying, spy_pct=sign * underlying - excess, excess=excess)


def ai_trader_refusal_cases(grades: list[IdeaGrade], look: Lookups) -> list[Case]:
    """The ideas NOT traded, as journal cases of kind "refusal" (human ruling
    2026-11-13, item 2): refused by a rule, the gate or the order path; placed
    and never filled; or proposed and never placed. One case per name, day and
    direction (its first idea that day), and none when the same name was taken
    in that direction the same day - the trade was made, so nothing was
    skipped. A refusal is RIGHT when the idea, graded at its own horizon, did
    not beat SPY."""
    taken = {(g.day, g.symbol, g.direction) for g in grades if g.status == "taken"}
    seen: set[tuple[str, str, str]] = set()
    out = []
    for g in sorted(grades, key=lambda g: (g.day, g.scan, g.symbol)):
        key = (g.day, g.symbol, g.direction)
        if g.status == "taken" or key in taken or key in seen:
            continue
        seen.add(key)
        if g.excess is None or g.resolved is None:
            continue
        option = g.instrument in ("call", "put")
        fund = look.fund(g.symbol)
        factor = 1.0 if option else look.leverage(g.symbol)
        opened = date.fromisoformat(g.day)
        gap = _gap(look.bars(g.symbol), opened)
        if gap is not None and factor < 0:
            gap = -gap / abs(factor)
        # as the trades do: the underlying's raw move (a long fund's unlevered;
        # an inverse fund's is not comparable with SPY's) and SPY's raw move;
        # _split signs both for the idea's side
        moved = g.underlying_pct if option else (g.underlying_pct / factor if factor > 0 else None)
        market = None if g.spy_pct is None else (-g.spy_pct if g.instrument == "put" else g.spy_pct)
        own, mkt = _split(moved, market, g.direction)
        fate = {"refused": f"refused by the rules engine ({g.reason})", "unfilled": "placed and never filled",
                "unplaced": "proposed and never placed (no verdict was recorded for it)"}[g.status]
        kind_text = {"call": "call option on", "put": "put option on", "etf": "fund", "stock": "shares of"}
        out.append(Case(
            decision_id=f"idea-{g.idea_id}", account="ai-trader", strategy="ai_trader", kind="refusal",
            symbol=g.symbol, opened=g.day, resolved=g.resolved, direction=g.direction, thesis=g.thesis,
            expected=(f"{fate}: {g.direction} via {kind_text.get(g.instrument, g.instrument)} {g.symbol}, target "
                      f"{g.target_pct}% and stop {g.stop_pct}% from the proposal price, horizon {g.horizon}, "
                      f"confidence {g.confidence}"),
            actual=(f"{g.symbol} moved {g.underlying_pct:+.2f}% from the price when it was proposed to the close of {g.resolved} ({g.sessions} session(s)); the idea, "
                    f"in its own direction, {g.return_pct:+.2f}%"),
            return_pct=g.return_pct, spy_pct=g.spy_pct, excess=g.excess, own_move=own, market_move=mkt,
            tags={
                "sector": _sector(g.symbol, fund, factor, look),
                "regime": look.regime(opened),
                "confidence_band": vocabulary.confidence_band(g.confidence),
                "source": "ai_trader",
                "instrument": "option" if option else ("etf" if fund or g.instrument == "etf" else "stock"),
                "entry": vocabulary.entry(g.direction, gap),
            },
            notes=(f"why it was not traded: {g.reason or fate}",
                   f"catalyst named: {g.catalyst or 'none'}",
                   "graded from the price the engine read when the idea was proposed (SPY at the same minute) to "
                   "the close of its horizon's last session, without its stop or target; an option on its "
                   "underlying")))
    return sorted(out, key=lambda c: (c.resolved, c.decision_id))


def unique_ideas(grades: Iterable[Union[IdeaGrade, Mapping[str, Any]]]) -> list[Any]:
    """One idea per name, day and direction, under its most consequential
    fate (taken before unfilled before refused before unplaced)."""
    best: dict[tuple, Any] = {}
    for g in grades:
        key = (_get(g, "day"), _get(g, "symbol"), _get(g, "direction"))
        if key not in best or _FATE[_get(g, "status")] < _FATE[_get(best[key], "status")]:
            best[key] = g
    return list(best.values())


def _get(g: Any, key: str) -> Any:
    return g[key] if isinstance(g, Mapping) else getattr(g, key)


# -- the main book's judged decisions -------------------------------------------------


def judged_trade_cases(trails: Iterable[Any], look: Lookups) -> list[Case]:
    """Judged trades with an outcome (the decision carries research)."""
    spy = look.bars("SPY")
    out = []
    for trail in trails:
        decision = trail.decision
        research = decision.research
        strategy = decision.sizing.strategy if decision.sizing else None
        if research is None or strategy not in (None, "judged") or trail.outcome is None:
            continue
        buys = [f for f in trail.fills if f.side == "buy"]
        sells = [f for f in trail.fills if f.side == "sell"]
        if not buys or not sells:
            continue
        cost = float(sum(f.filled_value for f in buys))
        proceeds = float(sum(f.filled_value for f in sells))
        if cost <= 0:
            continue
        symbol = str((decision.gate.order or {}).get("symbol", "")) or (research.tickers or ["?"])[0]
        kind = str((decision.gate.order or {}).get("kind", ""))
        option = "option" in kind
        underlying = (research.tickers or [symbol])[0]
        direction = "short" if research.direction in ("short", "puts", "short_via_puts") else "long"
        entered = min(f.recorded_at for f in buys).astimezone(NY).date()
        exited = max(f.recorded_at for f in sells).astimezone(NY).date()
        if not spy or exited > max(spy):
            continue  # resolved once its exit session has a close to measure against
        fund = look.fund(underlying)
        return_pct = (proceeds / cost - 1) * 100
        spy_pct = _close_return(spy, entered, exited)
        reasons = [e.reason.value if hasattr(e.reason, "value") else str(e.reason) for e in trail.exits]
        tags = {
            "sector": _sector(underlying, fund, 1.0, look),
            "regime": look.regime(entered),
            "confidence_band": vocabulary.confidence_band(research.confidence),
            "source": decision.signal.source_id,
            "instrument": "option" if option else ("etf" if fund else "stock"),
            "entry": vocabulary.entry(direction, _gap(look.bars(underlying), entered)),
        }
        moved = _close_return(look.bars(underlying), entered, exited) if option else return_pct
        own, market = _split(moved, spy_pct, direction)
        out.append(Case(
            decision_id=decision.decision_id, account="main", strategy="judged", kind="trade", symbol=underlying,
            opened=entered.isoformat(), resolved=exited.isoformat(), direction=direction,
            thesis=research.thesis,
            expected=(f"{direction} {underlying}, horizon {research.time_horizon}, confidence {research.confidence}"
                      + (f", catalyst: {research.catalyst_description}" if research.catalyst_description else "")),
            actual=(f"closed on {exited} ({', '.join(r.replace('_', ' ') for r in reasons) or 'exit'}); "
                    f"returned {return_pct:+.2f}% on cost"),
            return_pct=return_pct, spy_pct=spy_pct, excess=None if spy_pct is None else return_pct - spy_pct,
            own_move=own, market_move=market, tags=tags,
            notes=(f"invalidation stated: {research.invalidation_condition}",
                   f"priced-in analysis: {research.priced_in_analysis or 'none'}",
                   f"signal source: {decision.signal.source_id}")))
    return out


def decline_cases(rejections: Iterable[Any], forward_rows: Mapping[tuple[str, date], Any],
                  look: Lookups) -> list[Case]:
    """LLM declines graded by the forward engine at the verdict's horizon.
    The trade not made is the signal's own direction: long, or short for a
    puts verdict the sizing refused. A decline is RIGHT when it would not
    have beaten SPY."""
    out = []
    for record in rejections:
        research = getattr(record, "research", None)
        if research is None or record.code not in DECLINE_CODES:
            continue
        ticker = (research.tickers or list(record.signal.tickers) or [None])[0]
        if not is_symbol(ticker):
            continue   # the model named no instrument: there is no trade not made to grade
        observed = record.signal.observed_at.date()
        row = forward_rows.get((ticker.upper(), observed))
        horizon = HORIZON_SESSIONS.get(research.time_horizon, DEFAULT_HORIZON)
        mark = row.marks.get(horizon) if row is not None else None
        if mark is None or mark.excess_pct is None:
            continue
        direction = "short" if research.direction in ("short", "puts", "short_via_puts") else "long"
        sign = -1.0 if direction == "short" else 1.0
        return_pct = sign * float(mark.return_pct)
        excess = sign * float(mark.excess_pct)
        # the gap already on the tape when it decided: the decision day's open
        gap = _gap(look.bars(ticker), observed)
        fund = look.fund(ticker)
        tags = {
            "sector": _sector(ticker, fund, 1.0, look),
            "regime": look.regime(observed),
            "confidence_band": vocabulary.confidence_band(research.confidence),
            "source": record.signal.source_id,
            "instrument": "etf" if fund else "stock",
            "entry": vocabulary.entry(direction, gap),
        }
        out.append(Case(
            decision_id=record.decision_id, account="main", strategy="judged", kind="decline", symbol=ticker,
            opened=observed.isoformat(), resolved=mark.marked_on.isoformat(), direction=direction,
            thesis=research.thesis,
            expected=(f"declined ({record.code.replace('_', ' ')}): verdict {research.direction} at confidence "
                      f"{research.confidence}, horizon {research.time_horizon}"),
            actual=(f"{ticker} returned {float(mark.return_pct):+.2f}% over the {horizon} sessions after the "
                    f"decision ({float(mark.excess_pct):+.2f} points against SPY)"),
            return_pct=return_pct, spy_pct=return_pct - excess, excess=excess,
            own_move=excess, market_move=sign * (float(mark.return_pct) - float(mark.excess_pct)), tags=tags,
            notes=(f"why it was declined: {record.message}",
                   f"priced-in analysis: {research.priced_in_analysis or 'none'}",
                   f"signal source: {record.signal.source_id}",
                   "the trade not made is graded from the close of the decision day")))
    return sorted(out, key=lambda c: (c.resolved, c.decision_id))
