"""The momentum book's aligned data store: one session calendar, every
segment's raw and adjusted closes, and its SEC facts as session indexes.
Built once (restart-safe: every stage caches to disk) and loaded by the
sanity backtest, the live sleeve and the shadows alike."""
from __future__ import annotations

import json
import math
import os
from array import array
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Optional

from momentum import feeds
from momentum.feeds import Cache, Segment
from momentum.rules import ff12

#: A segment shorter than this many sessions is not a tradeable history.
MIN_SEGMENT = 30


class Store:
    def __init__(self, calendar: list[str], spy: array, benchmarks: dict, segments: list[Segment],
                 adj: dict, raw: dict) -> None:
        self.calendar = calendar
        self.index = {d: i for i, d in enumerate(calendar)}
        self.spy = spy
        self.benchmarks = benchmarks
        self.segments = segments
        self.by_key = {s.key: s for s in segments}
        self.adj = adj
        self.raw = raw

    # -- persistence -----------------------------------------------------------

    def save(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        keys = [s.key for s in self.segments]
        meta = dict(calendar=self.calendar, keys=keys, segments=[asdict(s) for s in self.segments],
                    benchmarks=list(self.benchmarks))
        with open(root / "adj.bin", "wb") as fh:
            self.spy.tofile(fh)
            for name in self.benchmarks:
                self.benchmarks[name].tofile(fh)
            for k in keys:
                self.adj[k].tofile(fh)
        with open(root / "raw.bin", "wb") as fh:
            for k in keys:
                self.raw[k].tofile(fh)
        tmp = root / "meta.json.tmp"
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, root / "meta.json")

    @classmethod
    def load(cls, root: Path) -> "Store":
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        n = len(meta["calendar"])
        segments = [Segment(**s) for s in meta["segments"]]

        def read(fh) -> array:
            a = array("f")
            a.fromfile(fh, n)
            return a

        with open(root / "adj.bin", "rb") as fh:
            spy = read(fh)
            benchmarks = {name: read(fh) for name in meta["benchmarks"]}
            adj = {k: read(fh) for k in meta["keys"]}
        with open(root / "raw.bin", "rb") as fh:
            raw = {k: read(fh) for k in meta["keys"]}
        return cls(meta["calendar"], spy, benchmarks, segments, adj, raw)


def build(cache_dir: Path, *, start: str = "2016-01-01", end: Optional[str] = None,
          log: Callable[[str], None] = print, benchmarks: tuple = ("SPMO", "RSP", "MTUM", "QMOM")) -> tuple:
    """Fetch everything and return (Store, stats). Each stage caches."""
    cache = Cache(cache_dir)
    alpaca = feeds.Alpaca(cache)
    sec = feeds.Sec(cache)
    end = end or (date.today() - timedelta(days=1)).isoformat()
    bar_end = end + "T23:59:00Z"
    stats: dict = {}

    spy_rows = alpaca.bars(["SPY"], start + "T00:00:00Z", bar_end, "all")["SPY"]
    calendar = [d for d, _ in spy_rows]
    index = {d: i for i, d in enumerate(calendar)}
    n = len(calendar)
    spy = feeds.align(spy_rows, index, n)
    bench = {}
    for sym in benchmarks:
        rows = alpaca.bars([sym], start + "T00:00:00Z", bar_end, "all").get(sym, [])
        bench[sym] = feeds.align(rows, index, n)
    log(f"calendar {calendar[0]} .. {calendar[-1]} ({n} sessions)")

    assets = alpaca.assets()
    by_symbol = {a["symbol"]: a for a in assets}
    symbols = sorted(by_symbol)
    stats["assets"] = len(symbols)
    log(f"assets (listed common-like, active + inactive): {len(symbols)}")

    adj_all: dict[str, array] = {}
    raw_all: dict[str, array] = {}
    for b in range(0, len(symbols), 100):
        batch = symbols[b:b + 100]
        name = f"bars_{b // 100:04d}"
        cached = cache.get(name)
        if cached is None:
            got_all = alpaca.bars(batch, start + "T00:00:00Z", bar_end, "all")
            got_raw = alpaca.bars(batch, start + "T00:00:00Z", bar_end, "raw")
            cached = {"all": got_all, "raw": got_raw}
            cache.put(name, cached)
        for sym in batch:
            if sym in cached["all"]:
                adj_all[sym] = feeds.align(cached["all"][sym], index, n)
                raw_all[sym] = feeds.align(cached["raw"].get(sym, []), index, n)
        if b // 100 % 10 == 0:
            log(f"bars {b + len(batch)}/{len(symbols)}")

    # dormant prints are not trading: blank them so a reused ticker splits
    blanked = sum(feeds.drop_flat_runs(raw_all[s], adj_all[s]) for s in adj_all)
    stats["flat_closes_blanked"] = blanked
    log(f"dormant (flat-run) closes blanked: {blanked}")

    # segments and identities
    current ={k.replace("-", "."): v for k, v in sec.company_ciks().items()}
    raw_segments: list[tuple[str, int, int]] = []
    for sym, closes in adj_all.items():
        for first, last in feeds.segments(sym, closes):
            if last - first + 1 >= MIN_SEGMENT:
                raw_segments.append((sym, first, last))
    wanted_names = {feeds.normalize_name(by_symbol[s]["name"] or "") for s, _, _ in raw_segments
                    if by_symbol[s]["status"] != "active"}
    names = sec.name_index(wanted_names)
    segs: list[Segment] = []
    dropped = {"reused_ticker_old_segment": 0, "no_cik": 0}
    per_symbol_last = {}
    for sym, first, last in raw_segments:
        per_symbol_last[sym] = max(per_symbol_last.get(sym, -1), last)
    for sym, first, last in raw_segments:
        asset = by_symbol[sym]
        cik = None
        if last == per_symbol_last[sym] and asset["status"] == "active":
            cik = current.get(sym)
        elif last == per_symbol_last[sym]:
            cik = names.get(feeds.normalize_name(asset["name"] or ""))
        else:
            dropped["reused_ticker_old_segment"] += 1
            continue
        if not cik:
            dropped["no_cik"] += 1
            continue
        segs.append(Segment(key=f"{sym}@{calendar[first]}", symbol=sym, first=first, last=last, cik=cik))
    stats["segments"] = len(segs)
    stats["dropped"] = dropped
    stats["inactive_segments"] = sum(1 for s in segs if by_symbol[s.symbol]["status"] != "active")
    log(f"segments with a CIK: {len(segs)} (inactive {stats['inactive_segments']}); dropped {dropped}")

    ciks = sorted({s.cik for s in segs})
    facts: dict[int, dict] = {}
    shares: dict[int, list] = {}
    for i, cik in enumerate(ciks):
        facts[cik] = sec.filings(cik) or {}
        shares[cik] = sec.shares(cik)
        if i % 250 == 0:
            log(f"SEC {i}/{len(ciks)}")

    adj: dict[str, array] = {}
    raw: dict[str, array] = {}
    count: dict[str, int] = {}
    for seg in segs:
        count[seg.symbol] = count.get(seg.symbol, 0) + 1
    total = dict(count)
    for sym in list(adj_all):
        if sym not in count:  # no identified segment: release now (memory cap)
            del adj_all[sym], raw_all[sym]
    for seg in segs:
        f = facts.get(seg.cik) or {}
        seg.sector = ff12(f.get("sic"))
        seg.us_filer = bool(f.get("us_filer"))
        seg.periodic = list(f.get("periodic") or [])  # the 18-month current-filer rule (2026-10-14)
        seg.events = sorted({i for i in (feeds.session_of(a, calendar, index) for a in f.get("earnings", []))
                             if i is not None and seg.first <= i <= seg.last})
        seg.pending = _pending_ranges(f, calendar, seg)
        seg.shares = _share_rows(shares.get(seg.cik) or [], calendar)
        a, r = adj_all[seg.symbol], raw_all[seg.symbol]
        if total[seg.symbol] == 1:
            # the symbol's only company: its arrays are the segment's (no copy)
            adj[seg.key], raw[seg.key] = a, r
        else:
            # a reused ticker: each company gets only its own sessions
            adj[seg.key] = array("f", [a[i] if seg.first <= i <= seg.last else feeds.NAN for i in range(n)])
            raw[seg.key] = array("f", [r[i] if seg.first <= i <= seg.last else feeds.NAN for i in range(n)])
        count[seg.symbol] -= 1
        if count[seg.symbol] == 0:
            del adj_all[seg.symbol], raw_all[seg.symbol]
    stats["us_filers"] = sum(1 for s in segs if s.us_filer)
    stats["with_shares"] = sum(1 for s in segs if s.shares)
    stats["with_earnings"] = sum(1 for s in segs if s.events)
    stats["with_pending"] = sum(1 for s in segs if s.pending)
    log(f"facts: US filers {stats['us_filers']}, with shares {stats['with_shares']}, "
        f"with 2.02 releases {stats['with_earnings']}, with a pending-deal window {stats['with_pending']}")
    return Store(calendar, spy, bench, segs, adj, raw), stats


def _pending_ranges(facts: dict, calendar: list[str], seg: Segment) -> list:
    """A target is pending from its deal announcement (an 8-K 1.01 within the
    150 days before its merger filing, else the filing itself) until it
    stops trading, or 365 days if the deal does not close."""
    out = []
    deals = facts.get("deal_8ks", [])
    for form, filed in facts.get("mergers", []):
        f = date.fromisoformat(filed)
        announced = [d for d in deals if f - timedelta(days=150) <= date.fromisoformat(d) <= f]
        start_day = min(announced) if announced else filed
        end_day = (date.fromisoformat(start_day) + timedelta(days=365)).isoformat()
        s = feeds.index_on_or_after(start_day, calendar)
        e = feeds.index_on_or_before(end_day, calendar)
        if s is None or e is None:
            continue
        s, e = max(s, seg.first), min(e, seg.last)
        if s <= e:
            out.append([s, e])
    return out


def _share_rows(rows: list, calendar: list[str]) -> list:
    """[[period-end index, filed index, shares]] - a filing counts only from
    the session it was filed (point in time)."""
    out = []
    for end, filed, val in rows:
        fi = feeds.index_on_or_after(filed, calendar)
        ei = feeds.index_on_or_before(end, calendar)
        if fi is None or ei is None or not val or math.isnan(val):
            continue
        out.append([ei, fi, val])
    out.sort(key=lambda x: (x[1], x[0]))
    return out
