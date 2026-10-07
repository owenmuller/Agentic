"""The pre-registered contract-awards backtest (SESSION_NOTES, 2026-10-06).

Rule under test, fixed before any data was seen: NEW award, SINGLE awardee,
award >= 1% of the awardee's POINT-IN-TIME market cap, awardee mapped to a
public US-listed parent. Universe: every award in every archived DoD digest
2019-01-01 .. 2026-10-02, through the PRODUCTION parser. Market cap: SEC
companyfacts shares outstanding (latest period end on or before the digest
day) x the awardee's close on the digest day — never a current cap. Returns:
t0 = digest day (published after the close); the tradeable path is the NEXT
OPEN -> t+1 / t+5 / t+20 close, excess over SPY on the same path, beside
close(t0) -> close and the pre-drift close(t-5) -> close(t0). Marks absent when
no bar lands within 4 calendar days (the forward engine's rule). Comparison
groups and the success criterion are in the pre-registration; this module
only computes them. Report-only: no audit record, no order, no LLM.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional

from signals.contracts import (
    KIND_NEW,
    AwardFacts,
    ContractorMap,
    SecCompanyFacts,
    load_edgar_names,
    parse_digest,
)

logger = logging.getLogger("orchestrator.contracts_backtest")

USER_AGENT = "Agentic trading research (omuller@brasacap.com)"
PARSER_FLOOR = Decimal("50000000")
RULE_REL_MIN = Decimal("0.01")
MAX_GAP_DAYS = 4
HORIZONS = (1, 5, 20)
_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})__(\d+)\.html$")


@dataclass
class Event:
    digest_date: date
    article_id: str
    facts: AwardFacts
    ticker: Optional[str]
    how: str
    cik: Optional[int]
    shares: Optional[Decimal] = None
    shares_asof: Optional[date] = None
    close_t0: Optional[Decimal] = None
    market_cap: Optional[Decimal] = None
    rel: Optional[Decimal] = None
    pre5: Optional[float] = None
    cc: dict[int, Optional[float]] = field(default_factory=dict)
    oc: dict[int, Optional[float]] = field(default_factory=dict)
    gap_pct: Optional[float] = None
    note: str = ""
    #: Awards this ticker took in the same digest (ruling 2026-10-06 on 2a):
    #: one event per ticker-day, the largest award primary, the total stamped.
    same_day_awards: int = 1
    same_day_total: Optional[Decimal] = None
    rel_total: Optional[Decimal] = None

    @property
    def group(self) -> str:
        """The pre-registered comparison groups."""
        if self.ticker is None:
            return "unmapped"
        if self.facts.multiple_award or self.facts.idiq or self.facts.ceiling_stated:
            return "iv_multi_idiq_ceiling"
        if self.facts.kind != KIND_NEW:
            return "ii_modification"
        if self.rel is None:
            return "unsized"
        if self.rel >= RULE_REL_MIN:
            return "i_rule"
        return "iii_new_single_below_1pct"


def _load_digests(folder: Path) -> list[tuple[date, str, str]]:
    """(date, article id, html) — one digest per day, the fuller when duplicated."""
    best: dict[date, tuple[str, str]] = {}
    for path in sorted(folder.glob("*.html")):
        match = _FILE_RE.match(path.name)
        if not match:
            continue
        day = date.fromisoformat(match.group(1))
        html = path.read_text(encoding="utf-8", errors="replace")
        if day not in best or len(html) > len(best[day][1]):
            best[day] = (match.group(2), html)
    return [(day, aid, html) for day, (aid, html) in sorted(best.items())]


def _collapse_same_ticker_day(events: list["Event"]) -> tuple[list["Event"], int]:
    """One event per ticker per digest date (human ruling 2026-10-06 on 2a).
    OSK 2020-03-27 took four orders in one digest, two of them above 1% of
    cap, and the rule group counted one forward return twice. The largest
    award is the primary and decides the group (Constraint #6: the day's
    total would admit more); the count and total are stamped so the
    sum-tiered variant can be reported beside the rule."""
    by_key: dict[tuple[str, date], list[Event]] = defaultdict(list)
    kept: list[Event] = []
    for e in events:
        if e.ticker is None:
            kept.append(e)
            continue
        by_key[(e.ticker, e.digest_date)].append(e)
    removed = 0
    for members in by_key.values():
        primary = max(members, key=lambda e: (e.facts.amount or Decimal(0), -e.facts.index))
        primary.same_day_awards = len(members)
        primary.same_day_total = sum((m.facts.amount or Decimal(0)) for m in members)
        removed += len(members) - 1
        kept.append(primary)
    kept.sort(key=lambda e: (e.digest_date, e.facts.index))
    return kept, removed


class _Series:
    """Per-symbol daily bars, fetched once, with the engine's gap rule."""

    def __init__(self, bars: Callable[[str, datetime, datetime], list[dict[str, Any]]], start: date, end: date, pace: float = 0.35) -> None:
        self._bars = bars
        self._start = datetime.combine(start - timedelta(days=10), dtime.min, tzinfo=timezone.utc)
        self._end = datetime.combine(end + timedelta(days=40), dtime.min, tzinfo=timezone.utc)
        self._pace = pace
        self._cache: dict[str, list[tuple[date, Decimal, Decimal]]] = {}
        self._calls = 0

    def get(self, symbol: str) -> list[tuple[date, Decimal, Decimal]]:
        if symbol not in self._cache:
            if self._calls and self._pace:
                time.sleep(self._pace)
            self._calls += 1
            rows = []
            for bar in self._bars(symbol, self._start, self._end):
                try:
                    rows.append((date.fromisoformat(str(bar.get("t"))[:10]), Decimal(str(bar.get("o"))), Decimal(str(bar.get("c")))))
                except Exception:  # noqa: BLE001 - a bad bar is skipped, not zeroed
                    continue
            rows.sort(key=lambda r: r[0])
            self._cache[symbol] = rows
        return self._cache[symbol]

    @staticmethod
    def on_or_after(rows, day: date):
        for session, o, c in rows:
            if session >= day:
                return (session, o, c) if (session - day).days <= MAX_GAP_DAYS else None
        return None

    @staticmethod
    def strictly_after(rows, day: date):
        for session, o, c in rows:
            if session > day:
                return (session, o, c) if (session - day).days <= MAX_GAP_DAYS else None
        return None

    @staticmethod
    def on_or_before(rows, day: date):
        best = None
        for session, o, c in rows:
            if session <= day:
                best = (session, o, c)
            else:
                break
        return best if best and (day - best[0]).days <= MAX_GAP_DAYS else None


def _pct(a: Decimal, b: Decimal) -> float:
    return float((a / b - 1) * 100)


def _measure(event: Event, series: _Series, spy: list[tuple[date, Decimal, Decimal]]) -> None:
    rows = series.get(event.ticker)
    if not rows:
        event.note = "no bars"
        return
    t0 = _Series.on_or_before(rows, event.digest_date)
    spy0 = _Series.on_or_before(spy, event.digest_date)
    if t0 is None or spy0 is None:
        event.note = "no t0 close"
        return
    event.close_t0 = t0[2]
    if event.shares:
        event.market_cap = (event.shares * t0[2]).quantize(Decimal("1"))
        if event.facts.amount is not None and event.market_cap > 0:
            event.rel = (event.facts.amount / event.market_cap).quantize(Decimal("0.000001"))
        if event.same_day_awards > 1 and event.same_day_total is not None and event.market_cap > 0:
            event.rel_total = (event.same_day_total / event.market_cap).quantize(Decimal("0.000001"))
    pre = _Series.on_or_before(rows, event.digest_date - timedelta(days=5))
    spre = _Series.on_or_before(spy, event.digest_date - timedelta(days=5))
    if pre and spre:
        event.pre5 = _pct(t0[2], pre[2]) - _pct(spy0[2], spre[2])
    nxt = _Series.strictly_after(rows, event.digest_date)
    snxt = _Series.strictly_after(spy, event.digest_date)
    if nxt and snxt:
        event.gap_pct = _pct(nxt[1], t0[2]) - _pct(snxt[1], spy0[2])
    for n in HORIZONS:
        due = event.digest_date + timedelta(days=n)
        mark = _Series.on_or_after(rows, due)
        smark = _Series.on_or_after(spy, due)
        if mark is None or smark is None:
            event.cc[n] = None
            event.oc[n] = None
            continue
        event.cc[n] = _pct(mark[2], t0[2]) - _pct(smark[2], spy0[2])
        if nxt and snxt and mark[0] >= nxt[0]:
            event.oc[n] = _pct(mark[2], nxt[1]) - _pct(smark[2], snxt[1])
        else:
            event.oc[n] = None


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean": round(statistics.mean(values), 2),
        "median": round(statistics.median(values), 2),
        "hit": round(sum(1 for v in values if v > 0) / len(values), 3),
        "sd": round(statistics.pstdev(values), 2) if len(values) > 1 else 0.0,
    }


def _group_table(events: list[Event]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for n in HORIZONS:
        out[f"oc{n}"] = _stats([e.oc[n] for e in events if e.oc.get(n) is not None])
        out[f"cc{n}"] = _stats([e.cc[n] for e in events if e.cc.get(n) is not None])
    out["pre5"] = _stats([e.pre5 for e in events if e.pre5 is not None])
    out["gap"] = _stats([e.gap_pct for e in events if e.gap_pct is not None])
    out["events"] = len(events)
    out["tickers"] = len({e.ticker for e in events if e.ticker})
    out["years"] = sorted({e.digest_date.year for e in events})
    # ticker-weighted next-open -> t+5
    per = defaultdict(list)
    for e in events:
        if e.oc.get(5) is not None:
            per[e.ticker].append(e.oc[5])
    out["oc5_ticker_weighted"] = round(statistics.mean(statistics.mean(v) for v in per.values()), 2) if per else None
    return out


def run_backtest(
    digest_dir: Path,
    *,
    bars: Callable[[str, datetime, datetime], list[dict[str, Any]]],
    out_path: Optional[Path] = None,
    sec: Optional[SecCompanyFacts] = None,
    contractors: Optional[ContractorMap] = None,
    max_events: Optional[int] = None,
) -> str:
    digests = _load_digests(digest_dir)
    if not digests:
        return f"no digests found under {digest_dir}"
    sec = sec or SecCompanyFacts(user_agent=USER_AGENT)
    if contractors is None:
        try:
            names = load_edgar_names(sec._client, USER_AGENT)  # noqa: SLF001 - shared client
        except Exception as error:  # noqa: BLE001
            logger.warning("EDGAR names unavailable: %s", error)
            names = None
        contractors = ContractorMap.load(edgar_names=names)
    events: list[Event] = []
    parsed_awards = 0
    resolution_by_year: dict[int, Counter] = defaultdict(Counter)
    unresolved_names: Counter = Counter()
    unresolved_dollars: Counter = Counter()
    for day, aid, html in digests:
        for facts in parse_digest(html):
            parsed_awards += 1
            if facts.amount is None or facts.amount < PARSER_FLOOR:
                continue
            res = contractors.resolve(facts.awardee, day, facts.small_business)
            resolution_by_year[day.year][res.how] += 1
            if res.how == "unresolved":
                unresolved_names[facts.awardee] += 1
                unresolved_dollars[facts.awardee] += float(facts.amount)
            events.append(Event(day, aid, facts, res.ticker, res.how, res.cik))
    events, same_day_collapsed = _collapse_same_ticker_day(events)
    if max_events:
        events = events[:max_events]
    mapped = [e for e in events if e.ticker]
    # point-in-time shares
    cik_cache: dict[tuple[int, date], Optional[tuple[Decimal, date]]] = {}
    for e in mapped:
        if not e.cik:
            continue
        key = (e.cik, e.digest_date)
        if key not in cik_cache:
            cik_cache[key] = sec.shares_outstanding(e.cik, e.digest_date)
        found = cik_cache[key]
        if found:
            e.shares, e.shares_asof = found
    # bars
    series = _Series(bars, min(e.digest_date for e in events), max(e.digest_date for e in events))
    spy = series.get("SPY")
    for e in mapped:
        _measure(e, series, spy)
    groups: dict[str, list[Event]] = defaultdict(list)
    for e in mapped:
        groups[e.group].append(e)
    rule = groups.get("i_rule", [])
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "digests": len(digests),
        "date_range": [digests[0][0].isoformat(), digests[-1][0].isoformat()],
        "awards_parsed": parsed_awards,
        "awards_at_or_above_parser_floor": len(events),
        "mapped": len(mapped),
        "mapped_with_pit_cap": sum(1 for e in mapped if e.market_cap),
        "mapped_with_bars": sum(1 for e in mapped if e.close_t0 is not None),
        "resolution_rate_by_year": {
            str(year): {**dict(counter), "resolved_pct": round(100 * (counter.get("curated", 0) + counter.get("curated_successor", 0) + counter.get("edgar_exact", 0)) / max(1, sum(counter.values())), 1)}
            for year, counter in sorted(resolution_by_year.items())
        },
        "groups": {name: _group_table(members) for name, members in sorted(groups.items())},
        "rule_by_year": {str(y): _group_table([e for e in rule if e.digest_date.year == y]) for y in sorted({e.digest_date.year for e in rule})},
        "rule_by_agency": {a: _group_table([e for e in rule if (e.facts.agency or "") == a]) for a in sorted({e.facts.agency or "" for e in rule})},
        "rule_by_cap_band": {
            band: _group_table([e for e in rule if _band(e.market_cap) == band]) for band in ("small", "mid", "mega")
        },
        "rule_by_rel_band": {
            band: _group_table([e for e in rule if _rel_band(e.rel) == band]) for band in ("1-2%", "2-5%", "5-20%", ">=20%")
        },
        "rule_sole_source": _group_table([e for e in rule if e.facts.sole_source]),
        "rule_competed_or_unstated": _group_table([e for e in rule if not e.facts.sole_source]),
        # Ruling 2026-10-06 on 2a: one event per ticker-day. How many were
        # folded, how the rule splits on single- vs multi-award days, and what
        # tiering on the day's TOTAL (not the largest award) would have added.
        "same_day_collapsed": same_day_collapsed,
        "rule_single_award_day": _group_table([e for e in rule if e.same_day_awards == 1]),
        "rule_multi_award_day": _group_table([e for e in rule if e.same_day_awards > 1]),
        "would_join_rule_on_day_total": _group_table(
            [
                e
                for e in mapped
                if e.group == "iii_new_single_below_1pct" and e.rel_total is not None and e.rel_total >= RULE_REL_MIN
            ]
        ),
        "ceiling_audit": [
            {"date": e.digest_date.isoformat(), "ticker": e.ticker, "awardee": e.facts.awardee[:50], "amount": str(e.facts.amount), "ceiling": str(e.facts.ceiling), "rel": str(e.rel), "kind": e.facts.kind}
            for e in sorted([e for e in mapped if e.rel is not None and e.rel >= Decimal("0.2")], key=lambda e: -(e.rel or 0))[:40]
        ],
        "success_criterion": {
            "stated": "rule group next-open->t+5 excess positive, hit > 50%, n >= 100, >= 4 calendar years, beats groups ii and iii at t+5",
            "met": _criterion_met(groups),
        },
        "top_unresolved": [
            {"awardee": name, "awards": count, "dollars": round(unresolved_dollars[name])}
            for name, count in unresolved_names.most_common(80)
        ],
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
        events_path = out_path.with_suffix(".events.jsonl")
        with open(events_path, "w", encoding="utf-8") as handle:
            for e in mapped:
                handle.write(json.dumps({"date": e.digest_date.isoformat(), "ticker": e.ticker, "awardee": e.facts.awardee, "amount": str(e.facts.amount), "ceiling": str(e.facts.ceiling) if e.facts.ceiling else None, "kind": e.facts.kind, "multiple": e.facts.multiple_award, "idiq": e.facts.idiq, "sole": e.facts.sole_source, "agency": e.facts.agency, "mcap": str(e.market_cap) if e.market_cap else None, "rel": str(e.rel) if e.rel is not None else None, "group": e.group, "pre5": e.pre5, "gap": e.gap_pct, "cc": e.cc, "oc": e.oc, "note": e.note, "same_day_awards": e.same_day_awards, "same_day_total": str(e.same_day_total) if e.same_day_awards > 1 else None, "rel_total": str(e.rel_total) if e.rel_total is not None else None}) + "\n")
    return render_backtest(report)


def _band(cap: Optional[Decimal]) -> str:
    if cap is None:
        return "unknown"
    if cap < Decimal("5000000000"):
        return "small"
    if cap < Decimal("50000000000"):
        return "mid"
    return "mega"


def _rel_band(rel: Optional[Decimal]) -> str:
    if rel is None:
        return "unknown"
    if rel < Decimal("0.02"):
        return "1-2%"
    if rel < Decimal("0.05"):
        return "2-5%"
    if rel < Decimal("0.20"):
        return "5-20%"
    return ">=20%"


def _criterion_met(groups: dict[str, list[Event]]) -> dict[str, Any]:
    rule = _group_table(groups.get("i_rule", []))
    ii = _group_table(groups.get("ii_modification", []))
    iii = _group_table(groups.get("iii_new_single_below_1pct", []))
    oc5 = rule.get("oc5", {})
    verdict = bool(
        oc5.get("n", 0) >= 100
        and oc5.get("mean", 0) > 0
        and oc5.get("hit", 0) > 0.5
        and len(rule.get("years", [])) >= 4
        and oc5.get("mean", 0) > ii.get("oc5", {}).get("mean", 0)
        and oc5.get("mean", 0) > iii.get("oc5", {}).get("mean", 0)
    )
    return {"met": verdict, "rule_oc5": oc5, "ii_oc5": ii.get("oc5", {}), "iii_oc5": iii.get("oc5", {}), "years": rule.get("years", [])}


def _fmt(stats: dict[str, Any]) -> str:
    if not stats or stats.get("n", 0) == 0:
        return "--"
    return f"mean {stats['mean']:+.2f} med {stats['median']:+.2f} hit {stats['hit']:.0%} (n={stats['n']})"


def render_backtest(report: dict[str, Any]) -> str:
    lines = [
        f"CONTRACT AWARDS BACKTEST (pre-registered 2026-10-06) — {report['digests']} digests "
        f"{report['date_range'][0]}..{report['date_range'][1]}; awards parsed {report['awards_parsed']}, "
        f">= parser floor {report['awards_at_or_above_parser_floor']}, mapped {report['mapped']}, "
        f"with point-in-time cap {report['mapped_with_pit_cap']}, with bars {report['mapped_with_bars']}",
        "excess vs SPY, points; oc = NEXT-OPEN -> close (tradeable), cc = close(t0) -> close, pre5 = close(t-5)->close(t0), gap = next open vs t0 close",
        "",
        "resolution rate by year (share of >= floor awards resolved to a US-listed parent):",
    ]
    for year, counter in report["resolution_rate_by_year"].items():
        lines.append(f"  {year}: resolved {counter['resolved_pct']}% — " + ", ".join(f"{k}={v}" for k, v in sorted(counter.items()) if k != "resolved_pct"))
    lines.append("")
    lines.append("groups (pre-registered):")
    for name, table in report["groups"].items():
        lines.append(f"  {name}: {table['events']} events, {table['tickers']} tickers, years {table['years'][0] if table['years'] else '-'}..{table['years'][-1] if table['years'] else '-'}")
        lines.append(f"     pre5 {_fmt(table['pre5'])} | gap {_fmt(table['gap'])}")
        for n in HORIZONS:
            lines.append(f"     t+{n}: oc {_fmt(table[f'oc{n}'])} | cc {_fmt(table[f'cc{n}'])}")
        if table.get("oc5_ticker_weighted") is not None:
            lines.append(f"     oc5 ticker-weighted {table['oc5_ticker_weighted']:+.2f}")
    lines.append("")
    lines.append("rule group by year (oc t+5):")
    for year, table in report["rule_by_year"].items():
        lines.append(f"  {year}: {_fmt(table['oc5'])} | t+1 {_fmt(table['oc1'])} | t+20 {_fmt(table['oc20'])}")
    lines.append("rule group by awardee cap band (oc t+5):")
    for band, table in report["rule_by_cap_band"].items():
        lines.append(f"  {band}: {_fmt(table['oc5'])}")
    lines.append("rule group by relative-size band (oc t+5):")
    for band, table in report["rule_by_rel_band"].items():
        lines.append(f"  {band}: {_fmt(table['oc5'])}")
    lines.append("rule group by agency (oc t+5):")
    for agency, table in report["rule_by_agency"].items():
        if table["events"] >= 10:
            lines.append(f"  {agency or 'unstated'}: {_fmt(table['oc5'])}")
    lines.append(f"rule, sole-source language: {_fmt(report['rule_sole_source']['oc5'])} | competed/unstated: {_fmt(report['rule_competed_or_unstated']['oc5'])}")
    if "same_day_collapsed" in report:
        lines.append(
            f"same ticker, same digest: {report['same_day_collapsed']} awards folded into their day's largest (one event per ticker-day); "
            f"rule on single-award days {_fmt(report['rule_single_award_day']['oc5'])} | multi-award days {_fmt(report['rule_multi_award_day']['oc5'])}"
        )
        lines.append(f"would join the rule if tiered on the day's TOTAL instead of the largest award (not adopted, Constraint #6): {_fmt(report['would_join_rule_on_day_total']['oc5'])}")
    lines.append("")
    lines.append("ceiling audit — largest award / point-in-time cap ratios (check for ceiling or mapping errors):")
    for row in report["ceiling_audit"][:25]:
        lines.append(f"  {row['date']} {row['ticker']:6} rel {float(row['rel'])*100:7.1f}% amount ${int(Decimal(row['amount'])):,} ceiling {row['ceiling']} {row['kind']} {row['awardee']}")
    lines.append("")
    lines.append("top unresolved awardee strings (>= floor; candidates for the map, or private):")
    for row in report["top_unresolved"][:40]:
        lines.append(f"  {row['awards']:3d}x ${row['dollars']/1e6:8.0f}M  {row['awardee'][:60]}")
    crit = report["success_criterion"]
    lines.append("")
    lines.append(f"SUCCESS CRITERION ({crit['stated']}): {'MET' if crit['met']['met'] else 'NOT MET'} — rule oc5 {_fmt(crit['met']['rule_oc5'])}; ii {_fmt(crit['met']['ii_oc5'])}; iii {_fmt(crit['met']['iii_oc5'])}; years {crit['met']['years']}")
    return "\n".join(lines)
