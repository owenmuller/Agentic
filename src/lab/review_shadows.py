"""The AI trader review's shadows, scored (human rulings 2026-11-12: build
them for the 10-session read). Read-only over the AI trader's ledger; nothing
here can place, size or stop a trade.

  concentration  ideas the live cap refused, replayed as if taken, beside the
                 positions that were taken - the same simulator for both, and
                 each refused idea against the position that blocked it
  strict         positions the stricter reading (every earlier idea in the
                 pass counts) would have refused: what they actually earned
  exits          the "would exit" rows: where the old rule (a wide quote's
                 mid) and the quote rule disagreed, whether the level really
                 traded, and for a stock what the old exit would have earned
  expiry         the contract the old 2-day window would have bought, at its
                 entry ask and its marked bid, against the option we held
  move           ideas whose move since public exceeded 1x ATR (the "skip"
                 shadow) against the rest

THE SIMULATOR (replays an idea on the underlying's one-minute SIP bars): in
at the anchored price; bars from the minute AFTER the entry minute (that
minute's range holds prices from before the entry); a stop checked before a
target inside one bar; a stop that gaps fills at the bar's open, a target
fills at its level (a gap past a target is never credited); otherwise out at
the last bar before the horizon's end (intraday 15:50; days and weeks at
15:50 of the 5th / 20th session counting the entry day, as the engine
counts). The return is on the underlying in the idea's direction - an
option's is levered and this is not its P&L. Stops fill at their level: the
measured stop slippage (Learnings #2) is not charged, the same for every
group, so the comparisons stand and the levels are optimistic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import mean
from typing import Any, Callable, Iterable, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
#: the free feed refuses the consolidated tape's last 15 minutes
SIP_DELAY = timedelta(minutes=16)
BULLISH = {"stock", "etf", "call"}
_BLOCKER = re.compile(r"\(([A-Z][A-Z0-9.]*)\)$")

Bars = Callable[[str, datetime, datetime], list[dict]]


@dataclass
class Sim:
    status: str                        # done, pending, no_bars, no_levels
    reason: Optional[str] = None       # stop, target, horizon_end, intraday_close
    exit_at: Optional[str] = None
    exit_price: Optional[float] = None
    ret: Optional[float] = None        # on the underlying, in the idea's direction


def _ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _f(value: Any) -> Optional[float]:
    try:
        return float(Decimal(str(value))) if value not in (None, "") else None
    except (ArithmeticError, ValueError):
        return None


def horizon_end(opened: datetime, horizon: str, calendar: Any, intraday_close: str,
                horizon_sessions: dict[str, int]) -> Optional[datetime]:
    """When the engine closes a position of this horizon opened at ``opened``."""
    day = opened.astimezone(NY).date()
    if horizon == "intraday":
        session = calendar.session(day)
        return session.at(intraday_close) if session is not None else None
    wanted = horizon_sessions.get(horizon)
    if wanted is None:
        return None
    count = 0
    for _ in range(wanted * 3 + 10):
        if calendar.is_session(day):
            count += 1
            if count == wanted:
                return calendar.session(day).at(intraday_close)
        day += timedelta(days=1)
    return None


class Simulator:
    """Replays ideas on minute bars (see the module notes); bars are fetched
    once per symbol and window."""

    def __init__(self, bars: Bars, calendar: Any, *, intraday_close: str, horizon_sessions: dict[str, int],
                 now: datetime, report: Callable[[str, str], None] = lambda source, message: None) -> None:
        self._bars = bars
        self._calendar = calendar
        self._close = intraday_close
        self._sessions = horizon_sessions
        self._now = now
        self._report = report
        self._cache: dict[tuple, list[dict]] = {}

    def run(self, symbol: str, instrument: str, horizon: str, opened: datetime,
            anchor: Optional[float], stop: Optional[float], target: Optional[float]) -> Sim:
        if not anchor or not stop or not target:
            return Sim("no_levels")
        end = horizon_end(opened, horizon, self._calendar, self._close, self._sessions)
        if end is None or end > self._now - SIP_DELAY:
            return Sim("pending")
        first = opened.replace(second=0, microsecond=0) + timedelta(minutes=1)
        key = (symbol, first, end)
        if key not in self._cache:
            self._cache[key] = sorted(self._bars(symbol, first, end), key=lambda b: _ts(b["t"]))
        rows = [b for b in self._cache[key] if first <= _ts(b["t"]) < end]
        if not rows:
            # never silent (rule 2026-10-15): a replay with no bars is a gap, not a result
            self._report("AI trader review shadows", f"no minute bars for {symbol} {first:%Y-%m-%d %H:%M} to "
                                                     f"{end:%Y-%m-%d %H:%M}; that idea is left unscored")
            return Sim("no_bars")
        bull = instrument in BULLISH
        for bar in rows:
            o, h, l = float(bar["o"]), float(bar["h"]), float(bar["l"])
            if bull:
                if l <= stop:
                    return self._out("stop", bar, min(o, stop), anchor, bull)
                if h >= target:
                    return self._out("target", bar, target, anchor, bull)
            else:
                if h >= stop:
                    return self._out("stop", bar, max(o, stop), anchor, bull)
                if l <= target:
                    return self._out("target", bar, target, anchor, bull)
        last = rows[-1]
        return self._out("intraday_close" if horizon == "intraday" else "horizon_end", last, float(last["c"]),
                         anchor, bull)

    @staticmethod
    def _out(reason: str, bar: dict, price: float, anchor: float, bull: bool) -> Sim:
        ret = price / anchor - 1 if bull else 1 - price / anchor
        return Sim("done", reason, str(bar["t"]), price, ret)


# -- the ledger --------------------------------------------------------------------

def realized(events: list[dict]) -> dict[str, tuple[float, float]]:
    """pid -> (P&L, cost) for every round trip that is closed and flat."""
    closed = {e["pid"] for e in events if e.get("event") == "position_closed"}
    legs: dict[str, list[dict]] = {}
    for e in events:
        if e.get("event") == "settled" and (_f(e.get("filled_qty")) or 0) > 0:
            legs.setdefault(e.get("pid"), []).append(e)
    out = {}
    for pid, fills in legs.items():
        held = sum(_f(f["filled_qty"]) * (1 if f["side"] == "buy" else -1) for f in fills)
        if pid not in closed or abs(held) > 1e-9:
            continue
        value = [(_f(f["filled_qty"]) * _f(f["avg_price"]) * (_f(f.get("multiplier")) or 1), f["side"])
                 for f in fills]
        cost = sum(v for v, side in value if side == "buy")
        if cost > 0:
            out[pid] = (sum(v if side == "sell" else -v for v, side in value), cost)
    return out


def _plans(events: list[dict]) -> dict[str, dict]:
    return {e["pid"]: e for e in events if e.get("event") == "position_opened"}


def _sim_plan(sim: Simulator, plan: dict) -> Sim:
    return sim.run(plan["symbol"], plan["instrument"], plan["horizon"], _ts(plan["at"]),
                   _f(plan.get("anchor_mid")), _f(plan.get("stop")), _f(plan.get("target")))


def _group(name: str, sims: list[Sim]) -> str:
    done = [s for s in sims if s.status == "done"]
    others: dict[str, int] = {}
    for s in sims:
        if s.status != "done":
            others[s.status] = others.get(s.status, 0) + 1
    if not done:
        tail = ", ".join(f"{k} {v}" for k, v in sorted(others.items()))
        return f"  {name}: n {len(sims)}, none scored yet" + (f" ({tail})" if tail else "")
    reasons: dict[str, int] = {}
    for s in done:
        reasons[s.reason] = reasons.get(s.reason, 0) + 1
    rets = [s.ret for s in done]
    text = (f"  {name}: scored {len(done)} - mean {mean(rets):+.2%}, positive {sum(r > 0 for r in rets)}/{len(done)}, "
            + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    if others:
        text += " (" + ", ".join(f"{k} {v}" for k, v in sorted(others.items())) + ")"
    return text


def _money(rows: Iterable[tuple[float, float]]) -> str:
    rows = list(rows)
    if not rows:
        return "none closed"
    pnl, cost = sum(p for p, _ in rows), sum(c for _, c in rows)
    return f"{len(rows)} closed, ${pnl:+,.0f} on ${cost:,.0f} ({pnl / cost:+.1%})"


# -- the scorers -------------------------------------------------------------------

def concentration(events: list[dict], sim: Simulator) -> list[str]:
    refused = [e for e in events if e.get("event") == "idea_rejected" and e.get("code") == "concentration"]
    plans = _plans(events)
    lines = ["CONCENTRATION CAP - refused ideas replayed as if taken (ruling 2026-11-12, item 2)"]
    if not refused:
        return lines + ["  no refusal yet"]
    kinds: dict[str, list[Sim]] = {}
    pairs: list[float] = []
    for row in refused:
        idea, levels = row.get("idea") or {}, row.get("levels") or {}
        result = sim.run(str(row.get("symbol", "")).upper(), idea.get("instrument", ""), idea.get("horizon", ""),
                         _ts(row["at"]), _f(levels.get("anchor")), _f(levels.get("stop")), _f(levels.get("target")))
        reason = str(row.get("reason", ""))
        kind = "option per scan" if "option(s) per scan" in reason else (
            ("theme" if " theme " in reason else "sector") + (" held" if "already held" in reason else " in the pass"))
        kinds.setdefault(kind, []).append(result)
        blocker = _BLOCKER.search(reason)
        if result.status == "done" and blocker:
            earlier = [p for p in plans.values() if p["symbol"] == blocker.group(1) and p["at"] <= row["at"]]
            if earlier:
                other = _sim_plan(sim, max(earlier, key=lambda p: p["at"]))
                if other.status == "done":
                    pairs.append(result.ret - other.ret)
    for kind, sims in sorted(kinds.items()):
        lines.append(_group(f"refused ({kind})", sims))
    start = min(row["at"] for row in refused)
    taken = [_sim_plan(sim, p) for p in plans.values() if p["at"] >= start]
    lines.append(_group(f"taken since {start[:10]} (same simulator)", taken))
    if pairs:
        lines.append(f"  refused minus the position that blocked it: n {len(pairs)}, mean {mean(pairs):+.2%}, "
                     f"refused ahead {sum(d > 0 for d in pairs)}/{len(pairs)}")
    return lines


def strict(events: list[dict], sim: Simulator) -> list[str]:
    rows = [e for e in events if e.get("event") == "concentration_strict_shadow"]
    lines = ["STRICT READING - positions it would have refused (readings 2026-11-12, later)"]
    if not rows:
        return lines + ["  none: the two readings have not differed yet"]
    plans, money = _plans(events), realized(events)
    placed = [r for r in rows if r.get("pid") in plans]
    lines.append(f"  {len(rows)} idea(s) taken that the strict reading would refuse; {len(placed)} placed")
    lines.append("  what they earned: " + _money(money[r["pid"]] for r in placed if r["pid"] in money))
    lines.append(_group("replayed", [_sim_plan(sim, plans[r["pid"]]) for r in placed]))
    return lines


def _crossed(row: dict, level: str) -> Optional[bool]:
    """Did the market really trade at the old rule's level? From the last trade
    (and a long stop's trigger, the bid of a tight quote)."""
    bull = row.get("instrument") in BULLISH
    value = _f(row.get("stop") if level == "stop" else row.get("target"))
    prices = [p for p in (_f(row.get("last")), _f(row.get("trigger")) if level == "stop" and bull else None)
              if p is not None]
    if value is None or not prices:
        return None
    below = (level == "stop") == bull
    return any(p <= value for p in prices) if below else any(p >= value for p in prices)


def exits(events: list[dict]) -> list[str]:
    rows = [e for e in events if e.get("event") == "exit_shadow"]
    lines = ["WOULD EXIT - the old rule (any quote's mid) against the quote rule (ruling 2026-11-12, item 1)"]
    if not rows:
        return lines + ["  no disagreement yet"]
    old_only = [r for r in rows if r.get("old_rule") and not r.get("new_rule")]
    new_only = [r for r in rows if r.get("new_rule") and not r.get("old_rule")]
    other = len(rows) - len(old_only) - len(new_only)
    traded = [_crossed(r, r["old_rule"]) for r in old_only]
    lines.append(f"  {len(rows)} rows on {len({r['pid'] for r in rows})} positions: old rule alone fired "
                 f"{len(old_only)} (level traded {sum(t is True for t in traded)}, never traded - phantom "
                 f"{sum(t is False for t in traded)}, no last trade {sum(t is None for t in traded)}); quote rule "
                 f"alone fired {len(new_only)}" + (f"; both fired, differently {other}" if other else ""))
    # a stock's old exit priced at the bid it would have sold into, against
    # what the position really earned (first old-rule row per position)
    plans, legs = _plans(events), {}
    for e in events:
        if e.get("event") == "settled" and (_f(e.get("filled_qty")) or 0) > 0:
            legs.setdefault(e.get("pid"), []).append(e)
    money = realized(events)
    diffs, seen = [], set()
    for row in old_only:
        pid = row["pid"]
        plan = plans.get(pid, {})
        if pid in seen or plan.get("occ") or pid not in money or _f(row.get("bid")) is None:
            continue
        seen.add(pid)
        bought = sum(_f(f["filled_qty"]) for f in legs[pid] if f["side"] == "buy")
        cost = money[pid][1]
        diffs.append(bought * _f(row["bid"]) - cost - money[pid][0])
    if diffs:
        lines.append(f"  stocks closed since: the old exit would have earned ${sum(diffs):+,.0f} against what "
                     f"they did ({len(diffs)} position(s); positive = the old rule was better)")
    options = {r["pid"] for r in old_only if plans.get(r["pid"], {}).get("occ")}
    if options:
        lines.append(f"  options: {len(options)} position(s); the shadow logs the underlying's quote only, so their "
                     f"old exit is not priced")
    return lines


def expiry(events: list[dict]) -> list[str]:
    entries = [e for e in events if e.get("event") == "expiry_counterfactual" and e.get("phase") == "entry"]
    marks = {e["pid"]: e for e in events if e.get("event") == "expiry_counterfactual" and e.get("phase") == "mark"}
    lines = ["MINIMUM EXPIRY - the old window's contract beside ours (ruling 2026-11-12, item 3)"]
    if not entries:
        return lines + ["  no days option opened yet"]
    same = sum(1 for e in entries if e.get("status") == "ok" and e.get("same_contract"))
    missing = sum(1 for e in entries if e.get("status") != "ok")
    priced = [e for e in entries if e.get("status") == "ok" and not e.get("same_contract")]
    money = realized(events)
    ours, theirs, open_ = [], [], 0
    for e in priced:
        mark = marks.get(e["pid"])
        if mark is None or _f(mark.get("bid")) is None or e["pid"] not in money:
            open_ += 1
            continue
        contracts, multiplier, ask = e.get("contracts") or 0, _f(e.get("multiplier")) or 100, _f(e.get("ask"))
        if not contracts or not ask:
            open_ += 1
            continue
        cost = contracts * multiplier * ask
        theirs.append((contracts * multiplier * _f(mark["bid"]) - cost, cost))
        ours.append(money[e["pid"]])
    lines.append(f"  {len(entries)} days option(s): same contract {same}, old window had none {missing}, "
                 f"different {len(priced)} ({len(theirs)} priced, {open_} still open or unpriced)")
    if theirs:
        lines.append("  ours (7+ days): " + _money(ours))
        lines.append("  old window (bought at the ask, sold at the marked bid): " + _money(theirs))
    return lines


def move(events: list[dict], sim: Simulator) -> list[str]:
    rows = [e for e in events if e.get("event") == "move_since_public"]
    lines = ["MOVE SINCE PUBLIC - the 'skip above 1x ATR' shadow (ruling 2026-11-12, item 4)"]
    if not rows:
        return lines + ["  no row yet"]
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r.get("status", "?")] = statuses.get(r.get("status", "?"), 0) + 1
    lines.append(f"  {len(rows)} ideas: " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())))
    plans, money = _plans(events), realized(events)
    ideas: dict[tuple[str, str, str], dict] = {}
    for e in events:
        if e.get("event") == "llm_pass":
            for idea in e.get("ideas") or []:
                ideas[(str(e.get("scan")), str(e.get("at", ""))[:10], str(idea.get("symbol", "")).upper())] = idea
    groups: dict[str, list[Sim]] = {"would skip": [], "would keep": []}
    taken: dict[str, list[tuple[float, float]]] = {"would skip": [], "would keep": []}
    for r in rows:
        if r.get("status") != "ok" or "would_skip" not in r:
            continue
        group = "would skip" if r["would_skip"] else "would keep"
        plan = plans.get(r.get("pid") or "")
        if plan is not None:
            groups[group].append(_sim_plan(sim, plan))
            if plan["pid"] in money:
                taken[group].append(money[plan["pid"]])
            continue
        idea = ideas.get((str(r.get("scan")), str(r.get("at", ""))[:10], r["symbol"]), {})
        anchor = _f(r.get("price"))
        try:
            stop_pct, target_pct = float(idea["stop_pct"]) / 100, float(idea["target_pct"]) / 100
        except (KeyError, TypeError, ValueError):
            groups[group].append(Sim("no_levels"))
            continue
        bull = r.get("instrument") in BULLISH
        stop = anchor * (1 - stop_pct if bull else 1 + stop_pct) if anchor else None
        target = anchor * (1 + target_pct if bull else 1 - target_pct) if anchor else None
        groups[group].append(sim.run(r["symbol"], r.get("instrument", ""), r.get("horizon", ""), _ts(r["at"]),
                                     anchor, stop, target))
    for group, sims in groups.items():
        lines.append(_group(f"{group} (every idea, replayed)", sims))
        lines.append("    taken and closed: " + _money(taken[group]))
    return lines


def report(events: list[dict], sim: Simulator, as_of: date) -> str:
    sections = [concentration(events, sim), strict(events, sim), exits(events), expiry(events), move(events, sim)]
    head = [f"AI TRADER REVIEW SHADOWS - as of {as_of} (read-only; the 10-session read is due after 2026-11-26)",
            "Replays: the underlying on one-minute SIP bars, stops at their level (slippage not charged), "
            "an option scored on its underlying. Returns in the idea's direction.", ""]
    return "\n".join(head + [line for section in sections for line in section + [""]]).rstrip() + "\n"
