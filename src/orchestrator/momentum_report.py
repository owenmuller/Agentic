"""The momentum sleeve's weekly line (human ruling 2026-10-13; v1.1 from
2026-10-14): the live sleeve beside SPY and SPMO - SPMO is the binding
comparison - with every shadow in an attached table.

  live sleeve   Modified Dietz on the INVESTED book, from the audit log's
                momentum fills and daily closes: the fills' real prices (and
                so the real slippage) are in it. Cash that vol scaling holds
                back is NOT in it (flagged on the card's attachment): the
                return is on what the sleeve had invested.
  shadows       software books from the monthly ``shadows-YYYY-MM.json``
                weights, rebalanced at the rebalance day's close, charged the
                spec's 10 bp per side of turnover. "v1.1 modelled" is the live
                plan run the same way, so live minus modelled is the fill gap.
  benchmarks    SPY and SPMO closes over the same days.
Reporting only: nothing here feeds a threshold or a size. The modelled books'
since-start returns (never "v1.1 live") are rendered as reference data in the
AI trader's brief (human ruling 2026-10-15).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import datahealth

COST_PER_SIDE = 0.0010


def dietz(v0: float, v1: float, flows: Iterable[tuple[float, float]]) -> Optional[float]:
    """Modified Dietz: ``flows`` are (fraction of the period REMAINING after
    the flow, amount in; buys positive, sales negative)."""
    flows = list(flows)
    net = sum(amount for _, amount in flows)
    base = v0 + sum(weight * amount for weight, amount in flows)
    if base <= 0:
        return None
    return (v1 - v0 - net) / base


@dataclass
class Fill:
    day: str
    symbol: str
    quantity: float  # + bought, - sold
    value: float     # + paid, - received


def fills_from_trails(trails) -> list[Fill]:
    out = []
    for trail in trails:
        if trail.decision.sizing.strategy != "momentum":
            continue
        symbol = str((trail.decision.gate.order or {}).get("symbol", ""))
        for f in trail.fills:
            sign = 1 if f.side == "buy" else -1
            out.append(Fill(f.recorded_at.date().isoformat(), symbol, sign * float(f.filled_quantity),
                            sign * float(f.filled_value)))
    return sorted(out, key=lambda f: f.day)


def close_on(closes: dict[str, float], day: str) -> Optional[float]:
    """The last close on or before ``day``."""
    eligible = [d for d in closes if d <= day]
    return closes[max(eligible)] if eligible else None


def book_value(fills: list[Fill], prices: dict[str, dict[str, float]], day: str) -> Optional[float]:
    units: dict[str, float] = {}
    for f in fills:
        if f.day <= day:
            units[f.symbol] = units.get(f.symbol, 0.0) + f.quantity
    total = 0.0
    for symbol, quantity in units.items():
        if abs(quantity) < 1e-9:
            continue
        price = close_on(prices.get(symbol, {}), day)
        if price is None:
            return None
        total += quantity * price
    return total


def close_before(closes: dict[str, float], day: str) -> Optional[float]:
    """The last close strictly before ``day``."""
    eligible = [d for d in closes if d < day]
    return closes[max(eligible)] if eligible else None


def live_return(fills: list[Fill], prices: dict[str, dict[str, float]], start: str, end: str) -> Optional[float]:
    """The invested book's return from the close BEFORE ``start`` to the close
    of ``end``, by Modified Dietz. A flow on ``start`` counts at full weight,
    so an inception window (start = the first trade day) is the plain return
    on what was put in."""
    units: dict[str, float] = {}
    for f in fills:
        if f.day < start:
            units[f.symbol] = units.get(f.symbol, 0.0) + f.quantity
    v0 = 0.0
    for symbol, quantity in units.items():
        if abs(quantity) < 1e-9:
            continue
        price = close_before(prices.get(symbol, {}), start)
        if price is None:
            return None
        v0 += quantity * price
    v1 = book_value(fills, prices, end)
    if v1 is None:
        return None
    if v0 == 0.0:
        # nothing held before the window: it runs from the first flow in it
        # (a week-to-date window that opens before inception, 2026-10-15)
        inside = [f.day for f in fills if start <= f.day <= end]
        if inside:
            start = min(inside)
    span = (date.fromisoformat(end) - date.fromisoformat(start)).days or 1
    # a flow ON start counts at full weight - also in a one-day window, where
    # the day-count weight would be 0 and the first day's return n/a (2026-10-15)
    flows = [(1.0 if f.day == start else (date.fromisoformat(end) - date.fromisoformat(f.day)).days / span, f.value)
             for f in fills if start <= f.day <= end]
    return dietz(v0, v1, flows)


def window(path: dict[str, float], start: str, end: str) -> Optional[float]:
    """A path's return from the close before ``start`` (or its first point)
    to the last close on or before ``end``."""
    days = sorted(d for d in path if d <= end)
    if not days:
        return None
    before = [d for d in days if d < start]
    begin = before[-1] if before else days[0]
    if path.get(begin, 0) <= 0:
        return None
    return path[days[-1]] / path[begin] - 1


def book_path(months: list[tuple[str, dict[str, float]]], prices: dict[str, dict[str, float]],
              days: list[str], cost: float = COST_PER_SIDE) -> dict[str, float]:
    """A software book's NAV by day: rebalanced to each month's weights at the
    rebalance day's close (weights below 1 leave the rest in cash), charged
    ``cost`` per side of turnover. Starts at 1 on the first rebalance day."""
    schedule = dict(months)
    units: dict[str, float] = {}
    cash: Optional[float] = None
    path: dict[str, float] = {}

    def value_on(day: str) -> float:
        held = sum(q * (close_on(prices.get(s, {}), day) or 0.0) for s, q in units.items())
        return (cash or 0.0) + held

    for day in days:
        if day in schedule:
            nav = 1.0 if cash is None else value_on(day)
            old = {s: q * (close_on(prices.get(s, {}), day) or 0.0) for s, q in units.items()}
            target = {s: w * nav for s, w in schedule[day].items() if close_on(prices.get(s, {}), day)}
            traded = sum(abs(target.get(s, 0.0) - old.get(s, 0.0)) for s in set(target) | set(old))
            nav -= traded * cost
            scale = nav / (nav + traded * cost) if nav + traded * cost > 0 else 0.0
            units = {s: amount * scale / close_on(prices.get(s, {}), day) for s, amount in target.items()}
            cash = nav - sum(amount * scale for amount in target.values())
            path[day] = nav
        elif cash is None and day < min(schedule, default=day):
            path[day] = 1.0  # all cash before the first rebalance, so windows include entry costs
        elif cash is not None:
            path[day] = value_on(day)
    return path


@dataclass
class Report:
    started: Optional[str]
    day: dict = field(default_factory=dict)       # name -> return today (from the prior close)
    week: dict = field(default_factory=dict)      # name -> return week to date
    since: dict = field(default_factory=dict)
    holdings: int = 0
    invested: Optional[float] = None
    notes: list = field(default_factory=list)
    #: the Quiver strategy shadows (human ruling 2026-10-29): (book, started,
    #: week to date, since start, SPY and QQQ over the same days since start)
    quiver: list = field(default_factory=list)

    def table(self) -> str:
        names = sorted(self.since, key=lambda n: -(self.since[n] if self.since[n] is not None else -9e9))
        lines = [f"MOMENTUM SLEEVE - live v1.1 vs shadows and benchmarks (since {self.started})",
                 f"{'book':32s} {'today':>8s} {'week to date':>13s} {'since start':>12s}"]
        for name in names:
            d, w, s = self.day.get(name), self.week.get(name), self.since.get(name)
            lines.append(f"{name:32s} {_fmt(d):>8s} {_fmt(w):>13s} {_fmt(s):>12s}")
        if self.quiver:
            lines += ["", "QUIVER STRATEGY SHADOWS - software books on Quiver's published rules (ruling 2026-10-29)",
                      f"{'book':24s} {'since':>10s} {'week to date':>13s} {'since start':>12s} {'SPY':>8s} {'QQQ':>8s}"]
            for name, started, week, since, spy, qqq in self.quiver:
                lines.append(f"{name:24s} {started:>10s} {_fmt(week):>13s} {_fmt(since):>12s} {_fmt(spy):>8s} "
                             f"{_fmt(qqq):>8s}")
        lines += ["", "Notes:"] + [f"- {n}" for n in self.notes]
        return "\n".join(lines)


def _fmt(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def load_months(directory: Path) -> list[dict]:
    out = []
    for path in sorted(directory.glob("shadows-*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def load_plans(directory: Path) -> list[dict]:
    out = []
    for path in sorted(directory.glob("plan-*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def build(directory: Path, fills: list[Fill], prices: dict[str, dict[str, float]], days: list[str],
          week_start: str, today: str) -> Report:
    """Everything from files and closes already fetched (pure)."""
    plans = load_plans(directory)
    started = min((f.day for f in fills if f.quantity > 0), default=None)
    report = Report(started=started)
    if started is None:
        report.notes.append("the sleeve has not traded yet")
        return report
    report.day["v1.1 live"] = live_return(fills, prices, today, today)
    report.week["v1.1 live"] = live_return(fills, prices, week_start, today)
    anchor = started
    report.since["v1.1 live"] = live_return(fills, prices, anchor, today)
    modelled = [(p["rebalance_date"], {t["symbol"]: float(t["weight"]) for t in p["targets"]}) for p in plans]
    paths = {"v1.1 modelled (plan at closes)": book_path(modelled, prices, days)}
    months = load_months(directory)
    for name in sorted({n for m in months for n in m.get("books", {})}):
        schedule = [(m["books"][name].get("started", m["rebalance_date"]), m["books"][name].get("weights", {}))
                    for m in months if name in m["books"]]
        paths[name] = book_path(schedule, prices, days)
    for bench in ("SPY", "SPMO"):
        series = prices.get(bench, {})
        paths[bench] = {d: series[d] for d in days if d in series}
    report.quiver = quiver_rows(directory, prices, days, week_start, today)
    for name, path in paths.items():
        report.day[name] = window(path, today, today)
        report.week[name] = window(path, week_start, today)
        report.since[name] = window(path, anchor, today)
    value = book_value(fills, prices, today)
    report.invested = value
    units: dict[str, float] = {}
    for f in fills:
        units[f.symbol] = units.get(f.symbol, 0.0) + f.quantity
    report.holdings = sum(1 for q in units.values() if abs(q) > 1e-9)
    report.notes += [
        "live: Modified Dietz on the invested book, real fill prices; cash held back by vol scaling is not in it",
        f"shadows and 'modelled': rebalanced at the rebalance day's close, {COST_PER_SIDE:.2%} per side of turnover",
        "windows: today and week to date run from the prior close; since start from the first trade (standing rule 2026-10-14)",
        "first look at 6 months, real read at 12; SPMO is the binding comparison (ruling 2026-10-14)",
    ]
    return report


DAILY_FILE = "daily.jsonl"


def recorded(directory: Path, day: str) -> bool:
    """Whether ``daily.jsonl`` already has the line for ``day``."""
    path = directory / DAILY_FILE
    if not path.exists():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(raw).get("day") == day:
                return True
        except ValueError:
            continue
    return False


def record(directory: Path, report: Report, day: str) -> None:
    """One line per session in ``daily.jsonl`` (rewritten whole; a rerun
    replaces the day's line): every book's today / week / since-start return.
    Read by the lab - the AI trader's brief (modelled books only; "v1.1 live"
    is this account's realized result and never reaches a prompt) and the
    lab scoreboard (AI trader vs the momentum sleeve vs SPMO)."""
    import os

    path = directory / DAILY_FILE
    lines = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if entry.get("day") != day:
                lines.append(entry)
    lines.append({"day": day, "started": report.started, "today": report.day, "week": report.week,
                  "since": report.since})
    lines.sort(key=lambda e: e.get("day", ""))
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    os.replace(temporary, path)


def load_quiver_books(directory: Path) -> dict:
    """``data/quiver/books.json`` beside ``data/momentum`` (ruling 2026-10-29)."""
    try:
        return json.loads((directory.parent / "quiver" / "books.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _from_close(path: dict[str, float], start: str, end: str) -> Optional[float]:
    """From the close OF ``start`` (a book that starts at that close) to the
    last close on or before ``end``."""
    first = [d for d in sorted(path) if d >= start]
    last = [d for d in sorted(path) if d <= end]
    if not first or not last or last[-1] < first[0]:
        return None
    a, b = path[first[0]], path[last[-1]]
    return b / a - 1 if a > 0 else None


def quiver_rows(directory: Path, prices: dict[str, dict[str, float]], days: list[str], week_start: str,
                today: str) -> list:
    """Each Quiver shadow since ITS OWN start, SPY and QQQ over the same days."""
    rows = []
    for name, book in sorted(load_quiver_books(directory).items()):
        schedule = [(d, w) for d, w in book.get("schedule") or []]
        if not schedule:
            continue
        started = schedule[0][0]
        path = book_path(schedule, prices, days)
        bench = {s: {d: prices.get(s, {})[d] for d in days if d in prices.get(s, {})} for s in ("SPY", "QQQ")}
        held = [d for d in sorted(path) if started <= d <= today]
        # from the capital it started with (1.0 before its entry cost), so the cost counts
        since = path[held[-1]] - 1 if held else None
        rows.append((name, started, window(path, week_start, today), since,
                     _from_close(bench["SPY"], started, today), _from_close(bench["QQQ"], started, today)))
    return rows


def gather(directory: Path, trails, today: date, alpaca) -> Optional[Report]:
    """Fetch the closes the report needs and build it; None before the first
    plan exists (there is nothing to report)."""
    from datetime import timedelta

    plans = load_plans(directory)
    if not plans:
        return None
    fills = fills_from_trails(trails)
    first = min([p["rebalance_date"] for p in plans] + [f.day for f in fills])
    start = (date.fromisoformat(first) - timedelta(days=10)).isoformat()
    symbols = {f.symbol for f in fills} | {"SPY", "SPMO", "QQQ"}
    for book in load_quiver_books(directory).values():
        for _day, weights in book.get("schedule") or []:
            symbols |= set(weights)
    for p in plans:
        symbols |= {t["symbol"] for t in p["targets"]}
    for month in load_months(directory):
        for book in month.get("books", {}).values():
            symbols |= set(book.get("weights", {}))
    rows = alpaca.bars(sorted(symbols), start + "T00:00:00Z", today.isoformat() + "T23:59:00Z", "all")
    prices = {s: {d: float(c) for d, c in series} for s, series in rows.items()}
    days = sorted(prices.get("SPY", {}))
    if not days:
        # a plan exists, so empty closes are a data failure, never "nothing to
        # report": the card says so (2026-10-15: refused requests went silent)
        raise RuntimeError("the market data returned no SPY closes; the momentum report cannot be built")
    week_start = (today - timedelta(days=today.weekday())).isoformat()  # week to date: from the close before Monday
    report = build(directory, fills, prices, days, week_start, today.isoformat())
    missing = sorted(s for s in symbols if not prices.get(s))
    if missing:
        # a book holding a name with no closes would value it at nothing:
        # never silent (rule 2026-10-15) - the card carries it and a yellow card goes out
        why = getattr(alpaca, "last_failed", {}) or {}
        detail = ", ".join(f"{s} ({why.get(s, 'no bars')})" for s in missing[:10])
        report.notes.append(f"NO CLOSES for {len(missing)} symbol(s): {detail} - the books holding them are wrong")
        datahealth.report("momentum report", f"no closes for {len(missing)} held or planned symbol(s): {detail}")
    return report
