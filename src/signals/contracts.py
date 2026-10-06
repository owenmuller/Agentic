"""Government contract awards (human ruling 2026-10-06, amended the same day).

Two feeds, one parser, one map, one enrichment, all deterministic:

- **DoD daily contracts digest** (defense.gov / war.gov, every business day at
  17:00 ET, after the close). The site sits behind a bot manager that refuses
  plain HTTP clients, so the body is read through a reader proxy (ruled
  2026-10-06: proxy primary, Wayback for history, one parser behind both). The
  RSS feed, which does answer plain clients, gives the digest list and the
  publish time. The first tradeable print after a digest is the NEXT OPEN.
- **FPDS public ATOM feed** (no key): structured contract actions, same-day for
  civilian agencies. DoD actions are withheld 90 days there, so the feed is
  civilian-only here — DoD comes from the digest.

Every award becomes a RawItem whose ``fields`` and labelled content lines carry
the determinants the literature says matter (relative size, new vs
modification, multiple-award / IDIQ / ceiling language, sole-source, agency,
term, awardee size), so the forward engine grades subtypes from day one.
The PRIMARY filter is relative size — award / point-in-time market cap — not
the dollar figure: a $4B award to a $20B company is the event, the same award
to a $150B prime is noise. Tiers (config ``contract_awards`` on the source):

    research   new award, single awardee, award >= research_min of market cap
    measure    everything else resolved and >= measure_min (measurement-only row)
    below_floor resolved but below measure_min (measurement-only row, the control)

Unresolved, small-business (``*``) and known-non-public awardees carry no
ticker and die at the prefilter as ``no_instrument`` with the reason written
into the content, so the funnel shows every award that arrived and why.

CONSTRAINT #5: award text is untrusted external content. It is parsed for
facts and rendered as data; nothing here reads it as an instruction.

Topology: this module imports nothing first-party outside ``signals`` and
reads credentials only from ``os.environ`` (``FINNHUB_API_KEY``), like the
X and EDGAR fetchers.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx
import yaml

from signals.config import SourceConfig
from signals.scanners import RawItem

logger = logging.getLogger("signals.contracts")

NEW_YORK = ZoneInfo("America/New_York")
SOURCE_ID = "gov_contract_awards"
FEED_DOD = "dod_digest"
FEED_FPDS = "fpds"

DOD_RSS_URL = (
    "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx"
    "?ContentType=400&Site=945&max=30"
)
DEFAULT_PROXY_PREFIX = "https://r.jina.ai/"
FPDS_ATOM_URL = "https://www.fpds.gov/ezsearch/FEEDS/ATOM"
FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{concept}.json"

#: Measurement codes written for the two non-research tiers (outside convergence).
MEASUREMENT_CODE_MEASURE = "award_measurement"
MEASUREMENT_CODE_BELOW_FLOOR = "award_below_floor"

KIND_NEW = "new"
KIND_MODIFICATION = "modification"
KIND_OPTION = "option"
KIND_CEILING = "ceiling_increase"

TIER_RESEARCH = "research"
TIER_MEASURE = "measure"
TIER_BELOW_FLOOR = "below_floor"


class ContractsError(RuntimeError):
    """A contract-awards feed or lookup failed in a way the caller should see."""


# --------------------------------------------------------------------------- parsing

_AGENCY_NAMES = (
    "ARMY",
    "NAVY",
    "AIR FORCE",
    "SPACE FORCE",
    "DEFENSE LOGISTICS AGENCY",
    "MISSILE DEFENSE AGENCY",
    "DEFENSE HEALTH AGENCY",
    "DEFENSE ADVANCED RESEARCH PROJECTS AGENCY",
    "DEFENSE INFORMATION SYSTEMS AGENCY",
    "U.S. SPECIAL OPERATIONS COMMAND",
    "WASHINGTON HEADQUARTERS SERVICES",
    "DEFENSE THREAT REDUCTION AGENCY",
    "DEFENSE COUNTERINTELLIGENCE AND SECURITY AGENCY",
    "NATIONAL GEOSPATIAL-INTELLIGENCE AGENCY",
    "DEFENSE MICROELECTRONICS ACTIVITY",
    "DEFENSE INTELLIGENCE AGENCY",
    "DEFENSE COMMISSARY AGENCY",
    "DEFENSE FINANCE AND ACCOUNTING SERVICE",
    "DEFENSE SECURITY COOPERATION AGENCY",
    "DEFENSE CONTRACT MANAGEMENT AGENCY",
    "DEFENSE HUMAN RESOURCES ACTIVITY",
    "DEFENSE MEDIA ACTIVITY",
    "DEFENSE POW/MIA ACCOUNTING AGENCY",
    "DEFENSE INNOVATION UNIT",
    "U.S. TRANSPORTATION COMMAND",
    "U.S. CYBER COMMAND",
    "NATIONAL SECURITY AGENCY",
    "UNIFORMED SERVICES UNIVERSITY OF THE HEALTH SCIENCES",
    "CHEMICAL AND BIOLOGICAL DEFENSE PROGRAM",
    "NATIONAL RECONNAISSANCE OFFICE",
    "DEFENSE COUNTERINTELLIGENCE",
)
_AGENCY_RE = re.compile(
    r"^\W*(?:U\.S\. )?(" + "|".join(re.escape(a) for a in _AGENCY_NAMES) + r")\W*$",
    re.I,
)
_AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?", re.I)
_AWARDED_SPLIT_RE = re.compile(
    r"\s*(?:,|;)?\s+(?:is|has been|was|are|have been|were)\s+(?:being\s+)?awarded\b", re.I
)
_CEILING_RE = re.compile(
    r"(?:cumulative (?:face )?value[^$]{0,80}?|if (?:all )?options? (?:are|is|were) exercised[^$]{0,80}?|"
    r"ceiling[^$]{0,40}?|maximum (?:dollar )?(?:value|amount)[^$]{0,40}?|not[- ]to[- ]exceed[^$]{0,30}?|"
    r"total (?:potential |estimated )?(?:contract )?value[^$]{0,60}?|to a total of[^$]{0,20}?|"
    r"bring(?:ing)? the (?:total|cumulative)[^$]{0,60}?)\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)?",
    re.I,
)
_SOLE_SOURCE_RE = re.compile(
    r"sole[- ]source|only one (?:responsible|responsive) (?:source|offeror)|other than full and open|"
    r"non-?competitive|not competitively (?:procured|awarded)|without competition|"
    r"10 U\.S\.C\. (?:2304|3204)|FAR 6\.302|limited sources|brand[- ]name justification",
    re.I,
)
#: Recompete / incumbent-retained language (ruling 2026-10-06 post-ship, 2d):
#: stamped and measured, never a filter. The digest rarely says it; the
#: honest stamp needs a prior-award lookup, which is a later build.
_RECOMPETE_RE = re.compile(
    r"re-?compet|follow-?on|incumbent|bridge contract|continuation of|"
    r"successor (?:contract|to)|replaces (?:the )?(?:existing|current|expiring)|"
    r"extends? (?:the )?(?:current|existing)|renewal|renews",
    re.I,
)
_OFFERS_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+"
    r"(?:offers?|bids?|proposals?|quotes?)\s+(?:was|were)\s+received"
    r"|\bwith\s+(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+"
    r"(?:(?:offers?|bids?|proposals?|quotes?)\s+)?received",
    re.I,
)
_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_COMPLETION_RE = re.compile(
    r"(?:expected to be complete[d]? (?:by|in|on)|completion date (?:of|is)|complete[d]? by|"
    r"ordering period (?:ends?|expires?|through)|through)\s+"
    r"([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4}|[A-Z][a-z]+\.?\s+\d{4}|\d{4})",
)
_CONTRACT_NO_RE = re.compile(r"\(([A-Z0-9]{2,8}[- ]?[A-Z0-9\-]{6,22})\)")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DIGEST_SLUG_RE = re.compile(r"contracts?-for-([a-z]+)-(\d{1,2})-(\d{4})")
_ARTICLE_ID_RE = re.compile(r"/Article/(\d+)/")
_TAG_RE = re.compile(r"<[^>]+>")
_PARAGRAPH_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)


@dataclass(frozen=True, slots=True)
class AwardFacts:
    """One award paragraph, parsed. ``text`` is the verbatim paragraph."""

    awardee: str
    amount: Optional[Decimal]
    ceiling: Optional[Decimal]
    kind: str
    multiple_award: bool
    idiq: bool
    sole_source: bool
    offers: Optional[int]
    completion: Optional[str]
    agency: Optional[str]
    contract_number: Optional[str]
    small_business: bool
    text: str
    #: Position within the digest, for a stable external id.
    index: int = 0
    #: Recompete / incumbent-retained language present (stamped, never a filter).
    recompete_language: bool = False

    @property
    def ceiling_stated(self) -> bool:
        return self.ceiling is not None and (self.amount is None or self.ceiling > self.amount)


def _money(raw: str, unit: Optional[str]) -> Optional[Decimal]:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    unit = (unit or "").lower()
    if unit == "billion":
        value *= Decimal(1_000_000_000)
    elif unit == "million":
        value *= Decimal(1_000_000)
    elif unit == "thousand":
        value *= Decimal(1_000)
    return value.quantize(Decimal("1"))


def html_to_paragraphs(page: str) -> list[str]:
    """Paragraph texts from a digest page (defense.gov / Wayback HTML)."""
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", page, flags=re.S)
    out = []
    for raw in _PARAGRAPH_RE.findall(body):
        text = html_module.unescape(_TAG_RE.sub(" ", raw))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append(text)
    return out


def markdown_to_paragraphs(text: str) -> list[str]:
    """Paragraphs from the reader proxy's markdown rendering of a digest."""
    body = text
    marker = "Markdown Content:"
    if marker in body:
        body = body.split(marker, 1)[1]
    out = []
    for block in re.split(r"\n\s*\n", body):
        cleaned = re.sub(r"\*\*|__|^#+\s*", "", block.strip(), flags=re.M)
        cleaned = re.sub(r"\s+", " ", html_module.unescape(cleaned)).strip()
        if cleaned:
            out.append(cleaned)
    return out


def paragraphs_of(text: str) -> list[str]:
    """Either rendering: HTML if it looks like HTML, markdown/plain otherwise."""
    if "<p" in text[:20000].lower() or "<html" in text[:2000].lower():
        return html_to_paragraphs(text)
    return markdown_to_paragraphs(text)


def _classify_kind(text: str) -> str:
    head = text[:500]
    if re.search(r"\bceiling (?:increase|is being increased)|increase[sd]? the (?:contract )?ceiling|raise[sd]? the ceiling", head, re.I):
        return KIND_CEILING
    if re.search(r"\boption\b.{0,40}\bexercis|exercis.{0,40}\boption\b|\boption (?:year|period)\b", head, re.I):
        return KIND_OPTION
    if re.search(r"\bmodification\b|\bmodif(?:ies|ied|y)\b|\bP\d{5}\b", head, re.I):
        return KIND_MODIFICATION
    return KIND_NEW


def _awardee_segments(paragraph: str) -> tuple[list[tuple[str, bool]], bool]:
    """The awardee name(s) before the "is/are awarded" verb, each with its
    small-business flag (the digest marks them "Name,* City"). Multiple
    awardees are separated by semicolons; the last often starts "and"."""
    match = _AWARDED_SPLIT_RE.search(paragraph)
    if match is None:
        return [], False
    head = paragraph[: match.start()].strip()
    # Leading digest noise.
    head = re.sub(r"^(?:UPDATE|CORRECTION|\*{1,2}CORRECTION\*{1,2})\s*:?\s*", "", head, flags=re.I)
    plural = bool(re.match(r"\s*(?:,|;)?\s+(?:are|have been|were)\s", match.group(0), re.I))
    segments = [head]
    multiple = False
    if ";" in head:
        segments = [re.sub(r"^and\s+", "", p.strip(" ;"), flags=re.I) for p in head.split(";") if p.strip(" ;")]
        multiple = len(segments) > 1
    out = []
    for segment in segments:
        name = _name_of(segment)
        if name:
            out.append((name, bool(re.match(r"^[^,]+,\s*\*", segment.strip()))))
    return out, (multiple or (plural and len(out) > 1))


def _name_of(segment: str) -> str:
    """The company name from "Name Inc., City, State (contract)": the text up
    to the first comma that is not part of a corporate suffix."""
    segment = segment.strip()
    segment = re.sub(r"\s*\([^)]*\)\s*$", "", segment)  # trailing (N00024-...)
    dba = re.split(r",?\s+doing business as\s+", segment, 1, flags=re.I)
    if len(dba) == 2:
        segment = dba[0]
    pieces = [p.strip() for p in segment.split(",")]
    if not pieces:
        return ""
    name = pieces[0]
    # "Lockheed Martin Corp., Rotary and Mission Systems, Liverpool" keeps the
    # division when the second piece is not a place (no state follows).
    return name.strip(" *").strip()


def parse_digest_paragraphs(paragraphs: Iterable[str]) -> list[AwardFacts]:
    """Every award paragraph in a digest, with the agency header in force."""
    awards: list[AwardFacts] = []
    agency: Optional[str] = None
    index = 0
    for paragraph in paragraphs:
        header = _AGENCY_RE.match(paragraph)
        if header and len(paragraph) < 80:
            agency = header.group(1).upper()
            continue
        if "awarded" not in paragraph.lower():
            continue
        amount_match = _AMOUNT_RE.search(paragraph)
        if amount_match is None:
            continue
        named, multiple = _awardee_segments(paragraph)
        if not named:
            continue
        amount = _money(amount_match.group(1), amount_match.group(2))
        ceiling = None
        for cm in _CEILING_RE.finditer(paragraph):
            candidate = _money(cm.group(1), cm.group(2))
            if candidate is not None and (ceiling is None or candidate > ceiling):
                ceiling = candidate
        kind = _classify_kind(paragraph)
        idiq = bool(re.search(r"indefinite[- ]delivery|indefinite[- ]quantity|\bIDIQ\b", paragraph, re.I))
        multiple = multiple or bool(re.search(r"multiple[- ]award|\bmultiple awardees?\b", paragraph, re.I))
        sole = bool(_SOLE_SOURCE_RE.search(paragraph))
        offers_match = _OFFERS_RE.search(paragraph)
        offers = None
        if offers_match:
            token = (offers_match.group(1) or offers_match.group(2) or "").lower()
            offers = _WORD_NUMBERS.get(token) or (int(token) if token.isdigit() else None)
        completion_match = _COMPLETION_RE.search(paragraph)
        completion = completion_match.group(1) if completion_match else None
        contract_match = None
        for cm2 in _CONTRACT_NO_RE.finditer(paragraph):
            contract_match = cm2.group(1)
        for name, small in named:
            awards.append(
                AwardFacts(
                    awardee=name,
                    amount=amount,
                    ceiling=ceiling,
                    kind=kind,
                    multiple_award=multiple,
                    idiq=idiq,
                    sole_source=sole,
                    offers=offers,
                    completion=completion,
                    agency=agency,
                    contract_number=contract_match,
                    small_business=small,
                    text=paragraph,
                    index=index,
                    recompete_language=bool(_RECOMPETE_RE.search(paragraph)),
                )
            )
            index += 1
    return awards


def parse_digest(text: str) -> list[AwardFacts]:
    """The production parser: HTML (site, Wayback) or proxy markdown in, awards out."""
    return parse_digest_paragraphs(paragraphs_of(text))


def digest_date_from(title_or_url: str) -> Optional[date]:
    """"Contracts for Oct. 5, 2026" or ".../contracts-for-oct-5-2026/" -> 2026-10-05."""
    text = re.sub(r"[.,]", "", title_or_url.lower()).replace(" ", "-")
    match = _DIGEST_SLUG_RE.search(text)
    if not match:
        return None
    key = match.group(1)
    month = _MONTHS.get(key) or _MONTHS.get(key[:4]) or _MONTHS.get(key[:3])
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def term_months(completion: Optional[str], start: date) -> Optional[int]:
    """Months from the award date to the stated completion, when stated."""
    if not completion:
        return None
    text = completion.replace(".", "").strip()
    year_only = re.fullmatch(r"\d{4}", text)
    if year_only:
        end = date(int(text), 12, 31)
    else:
        match = re.match(r"([A-Za-z]+)\s+(?:(\d{1,2}),\s+)?(\d{4})", text)
        if not match:
            return None
        month = _MONTHS.get(match.group(1).lower()[:4]) or _MONTHS.get(match.group(1).lower()[:3])
        if not month:
            return None
        day = int(match.group(2)) if match.group(2) else 28
        try:
            end = date(int(match.group(3)), month, min(day, 28))
        except ValueError:
            return None
    months = (end.year - start.year) * 12 + (end.month - start.month)
    return max(0, months)


# --------------------------------------------------------------------- contractor map

#: Corporate-form words only. Descriptive words ("technologies", "systems",
#: "services") stay: "Raytheon Technologies" and "Raytheon" were different
#: tickers, and the map's prefixes carry the distinction.
_SUFFIX_RE = re.compile(
    r"\b(the|inc|incorporated|corp|corporation|co|company|llc|l l c|ltd|limited|lp|l p|llp|plc|"
    r"usa|of america|north america)\b"
)


def normalize_name(raw: str) -> str:
    """Lower-case, strip punctuation and corporate suffixes, collapse spaces."""
    text = raw.lower()
    text = re.sub(r"\bdoing business as\b.*", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = _SUFFIX_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True, slots=True)
class MapEntry:
    prefix: str
    ticker: Optional[str]
    cik: Optional[int] = None
    from_date: Optional[date] = None
    until: Optional[date] = None
    successor: Optional[str] = None
    successor_cik: Optional[int] = None


@dataclass(frozen=True, slots=True)
class Resolution:
    ticker: Optional[str]
    #: curated | curated_successor | edgar_exact | known_nonpublic |
    #: small_business | not_yet_listed | unresolved
    how: str
    cik: Optional[int] = None
    note: str = ""


def default_contractors_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "contractors.yaml"


class ContractorMap:
    """The human-edited awardee -> parent map, with point-in-time renames."""

    def __init__(self, entries: Sequence[MapEntry], edgar_names: Optional[dict[str, tuple[str, int]]] = None) -> None:
        # Longest prefix first so "general dynamics electric boat" beats "general dynamics".
        self._entries = sorted(entries, key=lambda e: -len(e.prefix))
        self._edgar = edgar_names or {}
        #: ticker -> CIK from the EDGAR index (current registrants); the map's
        #: own `cik` field covers delisted tickers.
        self._ciks: dict[str, int] = {ticker: cik for (ticker, cik) in self._edgar.values()}

    def cik_for(self, ticker: Optional[str], entry_cik: Optional[int] = None) -> Optional[int]:
        if entry_cik:
            return entry_cik
        return self._ciks.get(ticker.upper()) if ticker else None

    @classmethod
    def load(cls, path: Optional[Path] = None, edgar_names: Optional[dict[str, tuple[str, int]]] = None) -> "ContractorMap":
        with open(path or default_contractors_path(), "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        entries = []
        for raw in payload.get("entries") or []:
            entries.append(
                MapEntry(
                    prefix=normalize_name(str(raw["prefix"])) + " ",
                    ticker=(str(raw["ticker"]).upper() if raw.get("ticker") else None),
                    cik=int(raw["cik"]) if raw.get("cik") else None,
                    from_date=date.fromisoformat(str(raw["from"])) if raw.get("from") else None,
                    until=date.fromisoformat(str(raw["until"])) if raw.get("until") else None,
                    successor=(str(raw["successor"]).upper() if raw.get("successor") else None),
                    successor_cik=int(raw["successor_cik"]) if raw.get("successor_cik") else None,
                )
            )
        return cls(entries, edgar_names)

    def resolve(self, awardee: str, on: date, small_business: bool = False) -> Resolution:
        if small_business:
            return Resolution(None, "small_business", note="small-business awardee (*) — private by construction")
        name = normalize_name(awardee) + " "
        for entry in self._entries:
            if name.startswith(entry.prefix):
                if entry.ticker is None:
                    return Resolution(None, "known_nonpublic", note=f"map: {entry.prefix.strip()!r} is non-public")
                if entry.from_date and on < entry.from_date:
                    return Resolution(None, "not_yet_listed", note=f"{entry.ticker} listed {entry.from_date}")
                if entry.until and on > entry.until:
                    if entry.successor:
                        return Resolution(entry.successor, "curated_successor", cik=self.cik_for(entry.successor, entry.successor_cik), note=f"{entry.ticker} -> {entry.successor} after {entry.until}")
                    return Resolution(None, "known_nonpublic", note=f"{entry.ticker} delisted {entry.until}")
                return Resolution(entry.ticker, "curated", cik=self.cik_for(entry.ticker, entry.cik))
        hit = self._edgar.get(name.strip())
        if hit:
            return Resolution(hit[0], "edgar_exact", cik=hit[1])
        return Resolution(None, "unresolved", note="no map entry and no exact EDGAR name match")


def load_edgar_names(client: httpx.Client, user_agent: str) -> dict[str, tuple[str, int]]:
    """Exact-normalized-name -> (ticker, cik) from SEC's company_tickers.json.
    Current registrants only; the map carries history."""
    response = client.get(SEC_TICKERS_URL, headers={"User-Agent": user_agent}, timeout=60)
    if response.status_code != 200:
        raise ContractsError(f"company_tickers.json returned HTTP {response.status_code}")
    out: dict[str, tuple[str, int]] = {}
    for entry in response.json().values():
        key = normalize_name(str(entry.get("title", "")))
        if key and key not in out:
            out[key] = (str(entry["ticker"]).upper(), int(entry["cik_str"]))
    return out


# ---------------------------------------------------------------------- enrichment

@dataclass(frozen=True, slots=True)
class AwardeeSize:
    market_cap: Optional[Decimal]
    market_cap_asof: Optional[date]
    revenue: Optional[Decimal]
    revenue_period: Optional[str]
    source: str


class SecCompanyFacts:
    """Shares outstanding and revenue from SEC companyfacts (free, no key)."""

    REVENUE_CONCEPTS = (
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
        ("us-gaap", "SalesRevenueNet"),
        ("us-gaap", "RevenuesNetOfInterestExpense"),
    )

    def __init__(self, client: Optional[httpx.Client] = None, *, user_agent: str, min_interval: float = 0.15,
                 sleeper: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=True)
        self._ua = user_agent
        self._interval = min_interval
        self._sleep = sleeper
        self._monotonic = monotonic
        self._last: Optional[float] = None
        self._cache: dict[str, Any] = {}

    def _get(self, url: str) -> Optional[dict]:
        if url in self._cache:
            return self._cache[url]
        if self._last is not None:
            wait = self._interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._monotonic()
        try:
            response = self._client.get(url, headers={"User-Agent": self._ua})
        except httpx.HTTPError as error:
            logger.warning("SEC companyfacts %s failed: %s", url, error)
            self._cache[url] = None
            return None
        payload = response.json() if response.status_code == 200 else None
        self._cache[url] = payload
        return payload

    def shares_outstanding(self, cik: int, on: date) -> Optional[tuple[Decimal, date]]:
        """dei:EntityCommonStockSharesOutstanding — the latest value whose
        period end is on or before ``on`` (point-in-time, never a later count)."""
        payload = self._get(SEC_CONCEPT_URL.format(cik=cik, taxonomy="dei", concept="EntityCommonStockSharesOutstanding"))
        if not payload:
            return None
        best: Optional[tuple[date, Decimal]] = None
        for point in (payload.get("units") or {}).get("shares", []):
            try:
                end = date.fromisoformat(str(point.get("end"))[:10])
                value = Decimal(str(point.get("val")))
            except (ValueError, InvalidOperation, TypeError):
                continue
            if end <= on and value > 0 and (best is None or end > best[0]):
                best = (end, value)
        return (best[1], best[0]) if best else None

    def revenue(self, cik: int, on: date) -> Optional[tuple[Decimal, str]]:
        """The latest full-year revenue reported on or before ``on`` (10-K FY)."""
        for taxonomy, concept in self.REVENUE_CONCEPTS:
            payload = self._get(SEC_CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, concept=concept))
            if not payload:
                continue
            best: Optional[tuple[date, Decimal, str]] = None
            for point in (payload.get("units") or {}).get("USD", []):
                if point.get("fp") != "FY" or not str(point.get("form", "")).startswith("10-K"):
                    continue
                try:
                    filed = date.fromisoformat(str(point.get("filed"))[:10])
                    end = date.fromisoformat(str(point.get("end"))[:10])
                    start = date.fromisoformat(str(point.get("start"))[:10]) if point.get("start") else None
                    value = Decimal(str(point.get("val")))
                except (ValueError, InvalidOperation, TypeError):
                    continue
                if start is not None and (end - start).days < 300:
                    continue  # not a full year
                if filed <= on and value > 0 and (best is None or end > best[0]):
                    best = (end, value, f"FY ending {end.isoformat()}")
            if best:
                return best[1], best[2]
        return None

    def close(self) -> None:
        self._client.close()


class FinnhubProfile:
    """Current market cap from Finnhub profile2 (USD millions on the wire)."""

    def __init__(self, client: Optional[httpx.Client] = None, *, api_key: Optional[str] = None, min_interval: float = 1.1,
                 sleeper: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._client = client or httpx.Client(timeout=20.0)
        self._key = api_key
        self._interval = min_interval
        self._sleep = sleeper
        self._monotonic = monotonic
        self._last: Optional[float] = None
        self._cache: dict[str, Optional[Decimal]] = {}

    def _resolve_key(self) -> str:
        key = self._key or os.environ.get("FINNHUB_API_KEY", "")
        if not key:
            raise ContractsError("FINNHUB_API_KEY is not set; market cap enrichment unavailable")
        return key

    def market_cap(self, ticker: str) -> Optional[Decimal]:
        if ticker in self._cache:
            return self._cache[ticker]
        if self._last is not None:
            wait = self._interval - (self._monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._monotonic()
        try:
            response = self._client.get(FINNHUB_PROFILE_URL, params={"symbol": ticker, "token": self._resolve_key()})
            payload = response.json() if response.status_code == 200 else {}
            raw = payload.get("marketCapitalization")
            value = (Decimal(str(raw)) * Decimal(1_000_000)).quantize(Decimal("1")) if raw else None
        except (httpx.HTTPError, InvalidOperation, ValueError, ContractsError) as error:
            logger.warning("Finnhub profile2 %s failed: %s", ticker, error)
            value = None
        self._cache[ticker] = value
        return value

    def close(self) -> None:
        self._client.close()


class AwardeeSizer:
    """Live enrichment: Finnhub cap today, SEC revenue; both optional, never guessed."""

    def __init__(self, finnhub: Optional[FinnhubProfile], sec: Optional[SecCompanyFacts],
                 ciks: Optional[Callable[[str], Optional[int]]] = None, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._finnhub = finnhub
        self._sec = sec
        self._ciks = ciks or (lambda ticker: None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple[str, Optional[int]], AwardeeSize] = {}

    def size(self, ticker: str, cik: Optional[int]) -> AwardeeSize:
        key = (ticker, cik)
        if key in self._cache:
            return self._cache[key]
        today = self._clock().date()
        cap = self._finnhub.market_cap(ticker) if self._finnhub is not None else None
        resolved_cik = cik or self._ciks(ticker)
        revenue = None
        period = None
        if self._sec is not None and resolved_cik:
            found = self._sec.revenue(resolved_cik, today)
            if found:
                revenue, period = found
        result = AwardeeSize(cap, today if cap is not None else None, revenue, period, "finnhub+sec")
        self._cache[key] = result
        return result


# ------------------------------------------------------------------ tiers and items

@dataclass(frozen=True, slots=True)
class RatioRules:
    research_min: Decimal = Decimal("0.01")
    measure_min: Decimal = Decimal("0.002")
    parser_floor: Decimal = Decimal("50000000")

    @classmethod
    def from_source(cls, source: SourceConfig) -> "RatioRules":
        cfg = getattr(source, "contract_awards", None)
        if cfg is None:
            return cls()
        return cls(
            research_min=Decimal(str(cfg.research_min)),
            measure_min=Decimal(str(cfg.measure_min)),
            parser_floor=Decimal(str(cfg.parser_floor_usd)),
        )


#: An award larger than the awardee's whole market cap is a shared-pool ceiling
#: reported per awardee, whatever the paragraph says (backtest ceiling audit,
#: 2026-10-06: TLS at 61x, GEO at 28x, JBLU at 2.7x). Measured, never researched.
CEILING_SUSPECT_RATIO = Decimal("1.0")


def size_tier(facts: AwardFacts, rel_mcap: Optional[Decimal], rules: RatioRules) -> str:
    """The pre-registered tiers. Multiple-award, non-new and ceiling-suspect
    awards never research."""
    if rel_mcap is None:
        return TIER_MEASURE  # resolved but unsized: a row to grade, not a pass to buy
    if rel_mcap > CEILING_SUSPECT_RATIO:
        return TIER_MEASURE
    if rel_mcap >= rules.research_min and facts.kind == KIND_NEW and not facts.multiple_award:
        return TIER_RESEARCH
    if rel_mcap >= rules.measure_min:
        return TIER_MEASURE
    return TIER_BELOW_FLOOR


def _fmt_money(value: Optional[Decimal]) -> str:
    return f"${int(value):,}" if value is not None else "none stated"


def _pct(value: Optional[Decimal]) -> str:
    return f"{(value * 100):.2f}%" if value is not None else "unknown"


def _military(department: str) -> bool:
    text = department.upper()
    return "DEFENSE" in text or "DEPT OF THE" in text or text in {"ARMY", "NAVY", "AIR FORCE", "SPACE FORCE"}


def build_item(
    *,
    feed: str,
    facts: AwardFacts,
    event_date: date,
    published_at: datetime,
    resolution: Resolution,
    size: Optional[AwardeeSize],
    rules: RatioRules,
    department: str,
    external_id: str,
    digest_title: str = "",
    digest_url: str = "",
    article_id: str = "",
    action_type: str = "",
    modification_reason: str = "",
    competition: str = "",
) -> RawItem:
    """One award as the scanner wants it: labelled lines the funnel can parse
    back, and the same facts as ``fields`` for dispatch and the prompt."""
    rel_mcap = None
    rel_rev = None
    if size is not None and facts.amount is not None:
        if size.market_cap:
            rel_mcap = (facts.amount / size.market_cap).quantize(Decimal("0.000001"))
        if size.revenue:
            rel_rev = (facts.amount / size.revenue).quantize(Decimal("0.000001"))
    ticker = resolution.ticker
    tier = size_tier(facts, rel_mcap, rules) if ticker else "no_instrument"
    months = term_months(facts.completion, event_date)
    military = _military(department)
    ceiling_suspect = rel_mcap is not None and rel_mcap > CEILING_SUSPECT_RATIO
    measurement_only = tier in (TIER_MEASURE, TIER_BELOW_FLOOR)
    measurement_code = (
        MEASUREMENT_CODE_BELOW_FLOOR if tier == TIER_BELOW_FLOOR else MEASUREMENT_CODE_MEASURE if tier == TIER_MEASURE else ""
    )
    feed_label = "DoD daily contracts digest" if feed == FEED_DOD else "FPDS public feed (civilian agencies)"
    lines = [
        f"Government contract award ({feed_label})",
        f"feed: {feed}",
        f"awardee: {facts.awardee}",
        f"parent resolution: {resolution.how}" + (f" — {resolution.note}" if resolution.note else ""),
    ]
    if ticker:
        lines.append(f"ticker: {ticker}")
    lines.extend(
        [
            f"agency: {facts.agency or department or 'unstated'}",
            f"department: {department or 'unstated'}",
            f"military: {'yes' if military else 'no'}",
            f"award amount: {_fmt_money(facts.amount)}",
            f"ceiling value: {_fmt_money(facts.ceiling) if facts.ceiling_stated else 'none stated'}",
            f"award kind: {facts.kind}",
            f"multiple award: {'yes' if facts.multiple_award else 'no'}",
            f"idiq: {'yes' if facts.idiq else 'no'}",
            f"sole source: {'yes' if facts.sole_source else 'unstated'}",
            f"offers received: {facts.offers if facts.offers is not None else 'unstated'}",
            f"term months: {months if months is not None else 'unstated'}",
            f"completion: {facts.completion or 'unstated'}",
            f"awardee market cap: {_fmt_money(size.market_cap) if size and size.market_cap else 'unknown'}"
            + (f" (as of {size.market_cap_asof})" if size and size.market_cap_asof else ""),
            f"award / market cap: {_pct(rel_mcap)}",
            f"awardee revenue: {_fmt_money(size.revenue) if size and size.revenue else 'unknown'}"
            + (f" ({size.revenue_period})" if size and size.revenue_period else ""),
            f"award / revenue: {_pct(rel_rev)}",
            f"ceiling suspect: {'yes' if ceiling_suspect else 'no'}",
            f"recompete: {'yes' if facts.recompete_language else 'unstated'}",
            f"size tier: {tier}",
            f"event date: {event_date.isoformat()}",
            f"published: {published_at.isoformat()}"
            + (" (the digest posts after the close; the first tradeable print is the next open)" if feed == FEED_DOD else ""),
        ]
    )
    if action_type:
        lines.append(f"action type: {action_type}")
    if modification_reason:
        lines.append(f"modification reason: {modification_reason}")
    if competition:
        lines.append(f"competition: {competition}")
    if facts.contract_number:
        lines.append(f"contract: {facts.contract_number}")
    if digest_title:
        lines.append(f"digest: {digest_title}" + (f" — {digest_url}" if digest_url else ""))
    lines.append("text: " + facts.text[:1500])
    fields = {
        "feed": feed,
        "awardee": facts.awardee,
        "ticker": ticker or "",
        "parent_how": resolution.how,
        "agency": facts.agency or department or "",
        "department": department,
        "military": "true" if military else "false",
        "award_amount": str(facts.amount) if facts.amount is not None else "",
        "ceiling_value": str(facts.ceiling) if facts.ceiling_stated else "",
        "award_kind": facts.kind,
        "multiple_award": "true" if facts.multiple_award else "false",
        "idiq": "true" if facts.idiq else "false",
        "sole_source": "true" if facts.sole_source else "false",
        "offers": str(facts.offers) if facts.offers is not None else "",
        "term_months": str(months) if months is not None else "",
        "awardee_mcap": str(size.market_cap) if size and size.market_cap else "",
        "rel_mcap": str(rel_mcap) if rel_mcap is not None else "",
        "rel_revenue": str(rel_rev) if rel_rev is not None else "",
        "size_tier": tier,
        "ceiling_suspect": "true" if ceiling_suspect else "false",
        "recompete": "true" if facts.recompete_language else "false",
        "event_date": event_date.isoformat(),
        "report_date": event_date.isoformat(),
        "published_at": published_at.isoformat(),
        "measurement_only": "true" if measurement_only else "false",
        "measurement_code": measurement_code,
        "small_business": "true" if facts.small_business else "false",
        "contract_number": facts.contract_number or "",
        "article_id": article_id,
        "digest_title": digest_title,
    }
    return RawItem(external_id=external_id, content="\n".join(lines), published_at=published_at, fields=fields)


# ------------------------------------------------------------------ DoD digest feed

@dataclass(frozen=True, slots=True)
class DigestRef:
    article_id: str
    title: str
    url: str
    published_at: datetime
    digest_date: Optional[date]


def parse_rss(body: str) -> list[DigestRef]:
    refs = []
    for item in re.findall(r"<item>(.*?)</item>", body, re.S):
        title = html_module.unescape(re.search(r"<title>(.*?)</title>", item, re.S).group(1)).strip()
        link = re.search(r"<link>(.*?)</link>", item, re.S).group(1).strip()
        pub_raw = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S).group(1).strip()
        try:
            published = datetime.strptime(pub_raw, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                published = datetime.strptime(pub_raw, "%a, %d %b %Y %H:%M:%S %z")
            except ValueError:
                continue
        aid = _ARTICLE_ID_RE.search(link)
        refs.append(DigestRef(aid.group(1) if aid else link, title, link, published, digest_date_from(title) or digest_date_from(link)))
    return refs


class DodDigestFetcher:
    """The DoD daily digest through the reader proxy (ruled primary, 2026-10-06).

    Each poll lists the RSS, takes every digest published inside ``lookback``
    that this process has not already parsed, reads its body through the proxy
    (``proxy_prefix + url``; the site itself refuses plain clients), parses it,
    resolves and sizes every awardee, and returns one RawItem per award. A
    pending file written by the evening one-shot is drained first so the open
    does not wait on the proxy. Polls inside ``min_poll_interval`` return []."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        contractors: ContractorMap,
        sizer: Optional[AwardeeSizer] = None,
        proxy_prefix: str = DEFAULT_PROXY_PREFIX,
        rss_url: str = DOD_RSS_URL,
        lookback: timedelta = timedelta(days=3),
        min_poll_interval_seconds: int = 900,
        timeout: float = 60.0,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        seen: Optional[Iterable[str]] = None,
        pending_path: Optional[Path] = None,
        user_agent: str = "Agentic trading research (omuller@brasacap.com)",
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._contractors = contractors
        self._sizer = sizer
        self._proxy = proxy_prefix
        self._rss_url = rss_url
        self._lookback = lookback
        self._min_poll = timedelta(seconds=min_poll_interval_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._seen: set[str] = set(seen or ())
        self._parsed_articles: set[str] = set()
        self._pending_path = pending_path
        self._ua = user_agent
        self._last_fetch: Optional[datetime] = None
        self.last_tally: dict[str, int] = {}

    # -- public ------------------------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        now = self._clock()
        items: list[RawItem] = list(self._drain_pending())
        if self._last_fetch is not None and now - self._last_fetch < self._min_poll:
            return items
        self._last_fetch = now
        rules = RatioRules.from_source(source)
        try:
            refs = self.list_digests()
        except ContractsError as error:
            logger.warning("DoD digest RSS unavailable: %s", error)
            return items
        for ref in refs:
            if ref.article_id in self._parsed_articles or now - ref.published_at > self._lookback:
                continue
            try:
                items.extend(self.items_for(ref, rules))
            except ContractsError as error:
                logger.warning("DoD digest %s unreadable this poll: %s", ref.title, error)
                continue
            self._parsed_articles.add(ref.article_id)
        return items

    def list_digests(self) -> list[DigestRef]:
        try:
            response = self._client.get(self._rss_url, headers={"User-Agent": self._ua})
        except httpx.HTTPError as error:
            raise ContractsError(f"RSS fetch failed: {error}") from error
        if response.status_code != 200:
            raise ContractsError(f"RSS returned HTTP {response.status_code}")
        return parse_rss(response.text)

    def fetch_digest_text(self, url: str) -> str:
        """The digest body through the proxy. Raises ContractsError on refusal."""
        try:
            response = self._client.get(self._proxy + url, headers={"User-Agent": self._ua, "Accept": "text/plain"})
        except httpx.HTTPError as error:
            raise ContractsError(f"proxy fetch failed: {error}") from error
        if response.status_code != 200 or len(response.text) < 200:
            raise ContractsError(f"proxy returned HTTP {response.status_code} ({len(response.text)} bytes)")
        if "awarded" not in response.text:
            raise ContractsError("proxy body carries no award paragraphs")
        return response.text

    def items_for(self, ref: DigestRef, rules: RatioRules, text: Optional[str] = None) -> list[RawItem]:
        """Parse one digest (fetched through the proxy unless ``text`` is given)."""
        body = text if text is not None else self.fetch_digest_text(ref.url)
        facts_list = parse_digest(body)
        event_date = ref.digest_date or ref.published_at.astimezone(NEW_YORK).date()
        items = []
        tally = {"awards": 0, "below_parser_floor": 0, "no_instrument": 0, TIER_RESEARCH: 0, TIER_MEASURE: 0, TIER_BELOW_FLOOR: 0, "seen": 0}
        for facts in facts_list:
            tally["awards"] += 1
            if facts.amount is None or facts.amount < rules.parser_floor:
                tally["below_parser_floor"] += 1
                continue
            external_id = f"dod:{ref.article_id}:{facts.index}"
            if external_id in self._seen:
                tally["seen"] += 1
                continue
            resolution = self._contractors.resolve(facts.awardee, event_date, facts.small_business)
            size = None
            if resolution.ticker and self._sizer is not None:
                size = self._sizer.size(resolution.ticker, resolution.cik)
            item = build_item(
                feed=FEED_DOD,
                facts=facts,
                event_date=event_date,
                published_at=ref.published_at,
                resolution=resolution,
                size=size,
                rules=rules,
                department="DEPT OF DEFENSE",
                external_id=external_id,
                digest_title=ref.title,
                digest_url=ref.url,
                article_id=ref.article_id,
            )
            tally[item.fields["size_tier"]] = tally.get(item.fields["size_tier"], 0) + 1
            items.append(item)
            self._seen.add(external_id)
        self.last_tally = tally
        return items

    # -- the evening handoff -----------------------------------------------------

    def stash_pending(self, items: Iterable[RawItem]) -> int:
        """Write items for the next session's first poll (evening one-shot)."""
        if self._pending_path is None:
            return 0
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with open(self._pending_path, "a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps({"external_id": item.external_id, "content": item.content, "published_at": item.published_at.isoformat(), "fields": item.fields}) + "\n")
                count += 1
        return count

    def _drain_pending(self) -> list[RawItem]:
        if self._pending_path is None or not self._pending_path.exists():
            return []
        items = []
        with open(self._pending_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    if raw["external_id"] in self._seen:
                        continue
                    items.append(RawItem(external_id=raw["external_id"], content=raw["content"], published_at=datetime.fromisoformat(raw["published_at"]), fields=dict(raw.get("fields") or {})))
                    self._seen.add(raw["external_id"])
                    aid = raw.get("fields", {}).get("article_id")
                    if aid:
                        self._parsed_articles.add(aid)
                except (KeyError, ValueError, TypeError):
                    continue
        try:
            self._pending_path.unlink()
        except OSError:
            pass
        return items

    def close(self) -> None:
        self._client.close()


# ------------------------------------------------------------------ FPDS civilian feed

def _xml_tag(entry: str, name: str) -> str:
    match = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", entry, re.S)
    return html_module.unescape(_TAG_RE.sub("", match.group(1))).strip() if match else ""


def _xml_attr(entry: str, name: str, attr: str) -> str:
    match = re.search(rf"<(?:\w+:)?{name}\b[^>]*\b{attr}=\"([^\"]*)\"", entry)
    return html_module.unescape(match.group(1)) if match else ""


@dataclass(frozen=True, slots=True)
class FpdsAction:
    piid: str
    mod_number: str
    transaction_number: str
    vendor: str
    obligated: Optional[Decimal]
    base_and_all_options: Optional[Decimal]
    department: str
    agency: str
    action_type: str
    modification_reason: str
    competition: str
    offers: Optional[int]
    signed: Optional[date]
    effective: Optional[date]
    ultimate_completion: Optional[date]
    modified_at: Optional[datetime]
    description: str

    @property
    def external_id(self) -> str:
        return f"fpds:{self.piid}:{self.mod_number or '0'}:{self.transaction_number or '0'}"


def _xml_date(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def parse_fpds_atom(body: str) -> list[FpdsAction]:
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", body, re.S):
        modified_raw = _xml_tag(entry, "modified")
        modified_at = None
        if modified_raw:
            try:
                modified_at = datetime.strptime(modified_raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=NEW_YORK).astimezone(timezone.utc)
            except ValueError:
                modified_at = None
        offers_raw = _xml_tag(entry, "numberOfOffersReceived")
        try:
            obligated = Decimal(_xml_tag(entry, "obligatedAmount") or "0")
        except InvalidOperation:
            obligated = None
        try:
            ceiling = Decimal(_xml_tag(entry, "baseAndAllOptionsValue") or "0")
        except InvalidOperation:
            ceiling = None
        out.append(
            FpdsAction(
                piid=_xml_tag(entry, "PIID"),
                mod_number=_xml_tag(entry, "modNumber"),
                transaction_number=_xml_tag(entry, "transactionNumber"),
                vendor=_xml_tag(entry, "vendorName"),
                obligated=obligated if obligated and obligated > 0 else None,
                base_and_all_options=ceiling if ceiling and ceiling > 0 else None,
                department=_xml_attr(entry, "contractingOfficeAgencyID", "departmentName") or _xml_attr(entry, "fundingRequestingAgencyID", "departmentName"),
                agency=_xml_attr(entry, "contractingOfficeAgencyID", "name"),
                action_type=_xml_attr(entry, "contractActionType", "description") or _xml_tag(entry, "contractActionType"),
                modification_reason=_xml_attr(entry, "reasonForModification", "description"),
                competition=_xml_attr(entry, "extentCompeted", "description"),
                offers=int(offers_raw) if offers_raw.isdigit() else None,
                signed=_xml_date(_xml_tag(entry, "signedDate")),
                effective=_xml_date(_xml_tag(entry, "effectiveDate")),
                ultimate_completion=_xml_date(_xml_tag(entry, "ultimateCompletionDate")),
                modified_at=modified_at,
                description=_xml_tag(entry, "descriptionOfContractRequirement"),
            )
        )
    return out


def facts_from_fpds(action: FpdsAction) -> AwardFacts:
    """An FPDS action in the digest's vocabulary, so one tiering rule serves both."""
    is_mod = bool(action.mod_number and action.mod_number not in ("0", "")) or bool(action.modification_reason)
    reason = (action.modification_reason or "").upper()
    if "OPTION" in reason:
        kind = KIND_OPTION
    elif is_mod:
        kind = KIND_MODIFICATION
    else:
        kind = KIND_NEW
    competition = (action.competition or "").upper()
    sole = competition.startswith("NOT COMPETED") or "NOT AVAILABLE FOR COMPETITION" in competition or "FOLLOW ON TO COMPETED" in competition
    text = f"{action.vendor} — {action.action_type or 'contract action'} {action.piid}"
    if is_mod:
        text += f" mod {action.mod_number}"
    if action.obligated:
        text += f": obligated ${int(action.obligated):,}"
    if action.base_and_all_options:
        text += f" (base and all options ${int(action.base_and_all_options):,})"
    if action.description:
        text += f" — {action.description}"
    return AwardFacts(
        awardee=action.vendor,
        amount=action.obligated,
        ceiling=action.base_and_all_options,
        kind=kind,
        multiple_award=("IDV" in (action.action_type or "").upper()) or ("DELIVERY ORDER" in (action.action_type or "").upper() and False),
        idiq="INDEFINITE" in (action.action_type or "").upper() or "IDV" in (action.action_type or "").upper(),
        sole_source=sole,
        offers=action.offers,
        completion=(action.ultimate_completion.strftime("%b %Y") if action.ultimate_completion else None),
        agency=action.agency or action.department,
        contract_number=action.piid,
        small_business=False,
        text=text,
        recompete_language=bool(_RECOMPETE_RE.search(f"{action.description} {action.modification_reason} {action.action_type}")),
    )


class FpdsCivilianFetcher:
    """Civilian contract actions from the public FPDS ATOM feed, same day.
    DoD departments are excluded (withheld 90 days there; the digest covers
    them). One query per poll over the last ``lookback_days`` by modification
    date, paginated to ``max_pages``."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        contractors: ContractorMap,
        sizer: Optional[AwardeeSizer] = None,
        min_obligated: Decimal = Decimal("25000000"),
        lookback_days: int = 2,
        max_pages: int = 10,
        min_poll_interval_seconds: int = 900,
        exclude_departments: Sequence[str] = ("DEPT OF DEFENSE",),
        timeout: float = 90.0,
        clock: Optional[Callable[[], datetime]] = None,
        seen: Optional[Iterable[str]] = None,
        user_agent: str = "Agentic trading research (omuller@brasacap.com)",
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._contractors = contractors
        self._sizer = sizer
        self._min_obligated = min_obligated
        self._lookback_days = lookback_days
        self._max_pages = max_pages
        self._min_poll = timedelta(seconds=min_poll_interval_seconds)
        self._exclude = tuple(d.upper() for d in exclude_departments)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._seen: set[str] = set(seen or ())
        self._ua = user_agent
        self._last_fetch: Optional[datetime] = None
        self.last_tally: dict[str, int] = {}

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        now = self._clock()
        if self._last_fetch is not None and now - self._last_fetch < self._min_poll:
            return []
        self._last_fetch = now
        rules = RatioRules.from_source(source)
        end = now.astimezone(NEW_YORK).date()
        start = end - timedelta(days=self._lookback_days)
        query = f"LAST_MOD_DATE:[{start:%Y/%m/%d},{end:%Y/%m/%d}] OBLIGATED_AMOUNT:[{int(self._min_obligated)},)"
        actions: list[FpdsAction] = []
        for page in range(self._max_pages):
            try:
                response = self._client.get(FPDS_ATOM_URL, params={"FEEDNAME": "PUBLIC", "q": query, "start": page * 10}, headers={"User-Agent": self._ua})
            except httpx.HTTPError as error:
                logger.warning("FPDS feed failed: %s", error)
                break
            if response.status_code != 200:
                logger.warning("FPDS feed returned HTTP %d", response.status_code)
                break
            batch = parse_fpds_atom(response.text)
            if not batch:
                break
            actions.extend(batch)
            if len(batch) < 10:
                break
        items = []
        tally = {"actions": len(actions), "dod_excluded": 0, "seen": 0, "no_instrument": 0}
        for action in actions:
            if any(action.department.upper().startswith(d) for d in self._exclude):
                tally["dod_excluded"] += 1
                continue
            if action.external_id in self._seen:
                tally["seen"] += 1
                continue
            facts = facts_from_fpds(action)
            event_date = action.signed or (action.modified_at or now).astimezone(NEW_YORK).date()
            resolution = self._contractors.resolve(facts.awardee, event_date)
            size = None
            if resolution.ticker and self._sizer is not None:
                size = self._sizer.size(resolution.ticker, resolution.cik)
            item = build_item(
                feed=FEED_FPDS,
                facts=facts,
                event_date=event_date,
                published_at=action.modified_at or now,
                resolution=resolution,
                size=size,
                rules=rules,
                department=action.department,
                external_id=action.external_id,
                action_type=action.action_type,
                modification_reason=action.modification_reason,
                competition=action.competition,
            )
            tally[item.fields["size_tier"]] = tally.get(item.fields["size_tier"], 0) + 1
            items.append(item)
            self._seen.add(action.external_id)
        self.last_tally = tally
        return items

    def close(self) -> None:
        self._client.close()


class CombinedAwardsFetcher:
    """Both feeds behind one source id: the router wants one callable."""

    def __init__(self, dod: DodDigestFetcher, fpds: Optional[FpdsCivilianFetcher]) -> None:
        self._dod = dod
        self._fpds = fpds

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        items = list(self._dod(source))
        if self._fpds is not None:
            items.extend(self._fpds(source))
        return items

    @property
    def last_tally(self) -> dict[str, int]:
        merged = dict(self._dod.last_tally)
        if self._fpds is not None:
            for key, value in self._fpds.last_tally.items():
                merged[f"fpds_{key}"] = value
        return merged

    def close(self) -> None:
        self._dod.close()
        if self._fpds is not None:
            self._fpds.close()
