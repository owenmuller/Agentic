"""The momentum book's data (human rulings 2026-10-13): read-only, cached.

Alpaca
  ``assets()``       every exchange-listed US equity, active AND inactive
  ``bars()``         daily closes, raw and split+dividend adjusted, from 2016

SEC (User-Agent per SEC policy; at most ~8 requests a second)
  ``company_ciks()``     current ticker -> CIK (company_tickers_exchange)
  ``name_index()``       normalized company name -> CIK (cik-lookup-data),
                         for delisted names whose ticker the SEC no longer maps
  ``filings(cik)``       SIC, whether it files 10-K/10-Q (a US filer), every
                         8-K 2.02 acceptance timestamp, merger filings
  ``shares(cik)``        shares outstanding as filed: (period end, filed, n),
                         share classes summed per filing

TICKER REUSE (the BBBY problem): a symbol's daily series is split into
segments at any gap of more than ``GAP_SESSIONS`` sessions; each segment is
its own company. The segment still trading today is the company the symbol
belongs to now (SEC ticker map); an ended segment of an INACTIVE symbol is
matched to its CIK by the asset's own name; an ended segment of a symbol
that was later reused cannot be identified and is dropped (counted).
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import re
import time
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import httpx

from execution.environment import load_environment

NY = ZoneInfo("America/New_York")
GAP_SESSIONS = 20
NAN = float("nan")
SEC_UA = "Agentic research omuller@brasacap.com"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
_NOT_COMMON = re.compile(r"\b(warrants?|units?|rights?|preferred|depositary shares?|etf|etn|fund|notes?|"
                         r"debentures?|trust units|%)\b", re.I)
MERGER_FORMS = {"DEFM14A", "PREM14A", "DEFM14C", "PREM14C", "SC 14D9", "SC TO-T", "SC 13E3"}


# -- caching -----------------------------------------------------------------------

class Cache:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def get(self, name: str):
        path = self.root / f"{name}.json.gz"
        if path.exists():
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        return None

    def put(self, name: str, value) -> None:
        path = self.root / f"{name}.json.gz"
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(value, fh)
        os.replace(tmp, path)


# -- SEC ---------------------------------------------------------------------------

class Sec:
    def __init__(self, cache: Cache, min_interval: float = 0.13) -> None:
        self.cache = cache
        self._client = httpx.Client(timeout=60, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip"})
        self._interval = min_interval
        self._last = 0.0

    def _get(self, url: str):
        for attempt in range(5):
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self._client.get(url)
            except httpx.HTTPError:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            time.sleep(5 * (attempt + 1))  # 403/429/5xx: back off
        return None

    def company_ciks(self) -> dict[str, int]:
        cached = self.cache.get("sec_company_tickers")
        if cached is None:
            r = self._get("https://www.sec.gov/files/company_tickers_exchange.json")
            data = r.json() if r else {"fields": [], "data": []}
            fields = data["fields"]
            cached = {row[fields.index("ticker")].upper(): int(row[fields.index("cik")]) for row in data["data"]}
            self.cache.put("sec_company_tickers", cached)
        return cached

    def name_index(self, wanted: Iterable[str]) -> dict[str, int]:
        """normalized name -> CIK, only for the names asked about (the full
        file is ~1M lines; it is streamed, never held)."""
        wanted = set(wanted)
        cached = self.cache.get("sec_name_index") or {}
        missing = wanted - set(cached)
        if missing:
            found: dict[str, int] = {}
            time.sleep(self._interval)
            with self._client.stream("GET", "https://www.sec.gov/Archives/edgar/cik-lookup-data.txt") as r:
                if r.status_code == 200:
                    for line in r.iter_lines():
                        name, _, rest = line.rpartition(":")[0].rpartition(":")
                        if not name:
                            continue
                        key = normalize_name(name)
                        if key in missing and key not in found:
                            try:
                                found[key] = int(rest)
                            except ValueError:
                                continue
            cached.update(found)
            cached.update({k: -1 for k in missing - set(found)})  # remembered as unmatched
            self.cache.put("sec_name_index", cached)
        return {k: v for k, v in cached.items() if k in wanted and v > 0}

    def filings(self, cik: int, since: str = "2015-01-01") -> Optional[dict]:
        key = f"sec_filings_{cik}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        r = self._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        if r is None:
            return None
        data = r.json()
        pages = [data["filings"]["recent"]]
        for extra in data["filings"].get("files", []):
            if extra.get("filingTo", "9999") < since:
                continue
            r2 = self._get(f"https://data.sec.gov/submissions/{extra['name']}")
            if r2 is not None:
                pages.append(r2.json())
        earnings, mergers, deal_8ks, forms = [], [], [], set()
        for page in pages:
            n = len(page.get("form", []))
            for i in range(n):
                form = page["form"][i]
                filed = page["filingDate"][i]
                if filed < since:
                    continue
                forms.add(form)
                items = (page.get("items") or [""] * n)[i] or ""
                accepted = (page.get("acceptanceDateTime") or [""] * n)[i] or ""
                if form == "8-K" and "2.02" in items:
                    earnings.append(accepted or filed)
                if form == "8-K" and "1.01" in items:
                    deal_8ks.append(filed)
                if form in MERGER_FORMS:
                    mergers.append([form, filed])
        sic = data.get("sic")
        summary = dict(
            cik=cik, name=data.get("name"), sic=int(sic) if str(sic or "").isdigit() else None,
            us_filer=bool(forms & {"10-K", "10-Q", "10-K/A", "10-KT"}), earnings=sorted(earnings),
            mergers=sorted(mergers, key=lambda x: x[1]), deal_8ks=sorted(deal_8ks),
            tickers=data.get("tickers") or [],
        )
        self.cache.put(key, summary)
        return summary

    def shares(self, cik: int) -> list:
        key = f"sec_shares_{cik}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        rows: list = []
        for taxonomy, concept in (("dei", "EntityCommonStockSharesOutstanding"),
                                  ("us-gaap", "CommonStockSharesOutstanding")):
            r = self._get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{concept}.json")
            if r is None:
                continue
            by_filing: dict[tuple, float] = {}
            for fact in (r.json().get("units", {}).get("shares") or []):
                k = (fact.get("accn"), fact.get("end"), fact.get("filed"))
                by_filing[k] = by_filing.get(k, 0.0) + float(fact.get("val") or 0)  # classes summed
            rows = sorted([[end, filed, val] for (_, end, filed), val in by_filing.items() if val > 0],
                          key=lambda x: (x[1], x[0]))
            if rows:
                break
        self.cache.put(key, rows)
        return rows


def normalize_name(name: str) -> str:
    text = name.lower()
    text = re.sub(r"\b(common stock|ordinary shares|class [a-z]|new|the|\(.*?\))\b", " ", text)
    text = re.sub(r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|plc|holdings?|group|lp|llc|sa|nv)\b",
                  " ", text)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


# -- Alpaca ------------------------------------------------------------------------

class Alpaca:
    def __init__(self, cache: Cache, api_key: Optional[str] = None, api_secret: Optional[str] = None) -> None:
        load_environment()
        headers = {"APCA-API-KEY-ID": api_key or os.environ["ALPACA_API_KEY"],
                   "APCA-API-SECRET-KEY": api_secret or os.environ["ALPACA_API_SECRET"]}
        self.cache = cache
        self._trading = httpx.Client(base_url="https://paper-api.alpaca.markets", timeout=120, headers=headers)
        self._data = httpx.Client(base_url="https://data.alpaca.markets", timeout=120, headers=headers)

    def assets(self) -> list[dict]:
        cached = self.cache.get("alpaca_assets")
        if cached is None:
            cached = []
            for status in ("active", "inactive"):
                rows = self._trading.get("/v2/assets", params={"status": status, "asset_class": "us_equity"}).json()
                for row in rows:
                    if row.get("exchange") in EXCHANGES and not _NOT_COMMON.search(row.get("name") or ""):
                        cached.append({k: row.get(k) for k in ("id", "symbol", "name", "status", "exchange")})
            self.cache.put("alpaca_assets", cached)
        return cached

    def bars(self, symbols: list[str], start: str, end: str, adjustment: str) -> dict[str, list]:
        """{symbol: [[date, close], ...]} - multi-symbol pages, a failing
        batch split in half until the bad symbol is isolated."""
        out: dict[str, list] = {}
        self._fetch(symbols, start, end, adjustment, out)
        return out

    def _fetch(self, symbols, start, end, adjustment, out) -> None:
        token = None
        collected: dict[str, list] = {}
        for _ in range(10000):
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                      "adjustment": adjustment, "feed": "sip", "limit": 10000}
            if token:
                params["page_token"] = token
            r = None
            for attempt in range(5):
                try:
                    r = self._data.get("/v2/stocks/bars", params=params)
                except httpx.HTTPError:
                    time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code == 429:
                    time.sleep(10)
                    continue
                break
            if r is None or r.status_code != 200:
                if len(symbols) > 1:
                    half = len(symbols) // 2
                    self._fetch(symbols[:half], start, end, adjustment, out)
                    self._fetch(symbols[half:], start, end, adjustment, out)
                return
            body = r.json()
            for sym, rows in (body.get("bars") or {}).items():
                collected.setdefault(sym, []).extend([[b["t"][:10], float(b["c"])] for b in rows])
            token = body.get("next_page_token")
            if not token:
                break
        out.update(collected)


# -- the aligned store ---------------------------------------------------------------

@dataclass
class Segment:
    key: str            # "SYMBOL@first-date"
    symbol: str
    first: int          # calendar index of the first close
    last: int           # calendar index of the last close
    cik: Optional[int] = None
    sector: int = 12
    us_filer: bool = False
    events: list = field(default_factory=list)      # session indexes of 2.02 releases
    pending: list = field(default_factory=list)     # [start, end] session index ranges
    shares: list = field(default_factory=list)      # [[end_idx, filed_idx, n], ...]


def align(series: list, calendar_index: dict[str, int], length: int) -> array:
    out = array("f", [NAN]) * length
    for day, close in series:
        i = calendar_index.get(day)
        if i is not None and close > 0:
            out[i] = close
    return out


def segments(symbol: str, closes: array, gap: int = GAP_SESSIONS) -> list[tuple[int, int]]:
    """[first, last] index ranges of a symbol's series, split at gaps of more
    than ``gap`` missing sessions (a reused ticker is a different company)."""
    present = [i for i, c in enumerate(closes) if not math.isnan(c)]
    if not present:
        return []
    out, start, prev = [], present[0], present[0]
    for i in present[1:]:
        if i - prev > gap:
            out.append((start, prev))
            start = i
        prev = i
    out.append((start, prev))
    return out


def session_of(accepted: str, calendar: list[str], calendar_index: dict[str, int]) -> Optional[int]:
    """The session an 8-K belongs to: accepted at/after 16:00 ET (or on a
    non-session day) counts to the next session (ruling 2026-10-13)."""
    if not accepted:
        return None
    try:
        when = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(NY)
    day = local.date().isoformat()
    after_close = (local.hour, local.minute) >= (16, 0)
    i = calendar_index.get(day)
    if i is not None and not after_close:
        return i
    # the next session strictly after the local date
    j = bisect.bisect_right(calendar, day)
    return j if j < len(calendar) else None


def index_on_or_after(day: str, calendar: list[str]) -> Optional[int]:
    j = bisect.bisect_left(calendar, day)
    return j if j < len(calendar) else None


def index_on_or_before(day: str, calendar: list[str]) -> Optional[int]:
    j = bisect.bisect_right(calendar, day) - 1
    return j if j >= 0 else None
