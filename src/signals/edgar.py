"""SEC EDGAR fetcher for Class 3 — 13F filings via full-text search.

The first production ``Fetcher``. Free, keyless, and rate-limited by courtesy: the
SEC allows 10 requests/second and asks for a User-Agent naming a contact. This
module enforces both — a configurable minimum interval between requests (default one
request per half-second, well under the ceiling), and a hard refusal to run without
a contact in the User-Agent, because an anonymous scraper is how you get the whole
office IP blocked.

The User-Agent comes from ``config/signals.yaml`` (``user_agent`` on the source),
falling back to the ``SEC_EDGAR_USER_AGENT`` environment variable. It must contain
an ``@``: the SEC's stated expectation is a way to reach you.

What one poll does
------------------
For each fund on the source's watchlist:

  1. Full-text search for the quoted fund name, ``forms=13F-HR``, bounded to a
     lookback window. FTS matches any filing *mentioning* the phrase, so hits are
     kept only when the filer's own display name contains the fund name — a filing
     that merely cites the fund is not a filing by it. (Substring matching means a
     similarly named entity — "X Partners LP" alongside "X LP" — is swept in too;
     the fund name travels in the signal's fields, and the research layer forms its
     own view. Under-filtering here surfaces; over-filtering silently drops.)
  2. For each new accession: fetch the filing's index, read ``periodOfReport`` from
     the cover (``primary_doc.xml``), find the information-table XML, and parse the
     holdings.
  3. Emit one ``RawItem`` per filing. ``content`` is a plain-text rendering of the
     holdings — third-party data that will be fenced before any model reads it —
     and ``fields`` carries the structured facts (fund, CIK, accession, dates).

Accessions already emitted are remembered for the process lifetime, so a daily
re-poll does not re-download filings; the ``SignalQueue``'s dedup is the correctness
backstop across restarts.

Known lag, restated from CLAUDE.md: 13Fs are quarterly, +45 days, longs only — and
"longs only" describes equity short exposure, not instruments: a 13F can report
bought puts, so ``putCall`` is rendered when the filing carries it. Use for
directional conviction and sector weighting, never for timing.
"""

from __future__ import annotations

import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

import httpx

from signals.config import SourceConfig
from signals.scanners import RawItem

FTS_URL = "https://efts.sec.gov/LATEST/search-index"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"

logger = logging.getLogger("signals.edgar")


class EdgarError(RuntimeError):
    """A poll that could not be completed. The loop logs it and skips the cycle."""


def _local_name(tag: str) -> str:
    """Strip the XML namespace: '{http://…}infoTable' -> 'infoTable'."""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> Optional[str]:
    for child in element.iter():
        if _local_name(child.tag) == name and child.text is not None:
            return child.text.strip()
    return None


class Form13FFetcher:
    """Fetches new 13F-HR filings for the funds on the source's watchlist."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        user_agent: Optional[str] = None,
        lookback_days: int = 120,
        top_holdings: int = 20,
        min_request_interval: float = 0.5,
        timeout: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._user_agent = user_agent
        self._lookback = timedelta(days=lookback_days)
        self._top = top_holdings
        self._interval = min_request_interval
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._last_request: Optional[float] = None
        #: Accessions already emitted this process. The queue dedups across restarts.
        self._seen: set[str] = set()

    # -- the Fetcher protocol ------------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        user_agent = self._resolve_user_agent(source)
        items: list[RawItem] = []
        for entry in source.watchlist:
            fund = entry.get("fund") or entry.get("name")
            if not fund:
                continue
            for hit in self._search(fund, user_agent):
                item = self._item_from_hit(fund, hit, user_agent)
                if item is not None:
                    items.append(item)
        return items

    # -- search ----------------------------------------------------------------------

    def _search(self, fund: str, user_agent: str) -> list[dict]:
        """Full-text search, bounded to the lookback window, filtered to the filer."""
        today = self._clock().date()
        response = self._get(
            FTS_URL,
            user_agent,
            params={
                "q": f'"{fund}"',
                "forms": "13F-HR",
                "startdt": (today - self._lookback).isoformat(),
                "enddt": today.isoformat(),
            },
        )
        if response.status_code != 200:
            raise EdgarError(
                f"EDGAR full-text search returned HTTP {response.status_code} "
                f"for {fund!r}"
            )
        hits = response.json().get("hits", {}).get("hits", [])

        filed_by_fund = []
        for hit in hits:
            names = hit.get("_source", {}).get("display_names") or []
            if any(fund.lower() in str(name).lower() for name in names):
                filed_by_fund.append(hit)
            else:
                # A filing that mentions the fund is not a filing by it.
                logger.debug(
                    "skipping %s: mentions %r but was filed by %s",
                    hit.get("_id"),
                    fund,
                    names,
                )
        return filed_by_fund

    def _item_from_hit(
        self, fund: str, hit: dict, user_agent: str
    ) -> Optional[RawItem]:
        source_data = hit.get("_source", {})
        accession = source_data.get("adsh") or str(hit.get("_id", "")).split(":")[0]
        if not accession or accession in self._seen:
            return None
        ciks = source_data.get("ciks") or []
        cik = str(ciks[0]) if ciks else str(source_data.get("cik", "")).strip()
        if not cik:
            logger.warning("hit %s carries no CIK; skipping", accession)
            return None
        file_date = str(source_data.get("file_date", ""))

        try:
            item = self._build_item(fund, cik, accession, file_date, user_agent)
        except Exception as error:  # noqa: BLE001 - one bad filing must not hide others
            logger.warning(
                "could not build a signal from filing %s (%s): %s",
                accession,
                fund,
                error,
            )
            return None
        if item is not None:
            self._seen.add(accession)
        return item

    # -- one filing ------------------------------------------------------------------

    def _build_item(
        self, fund: str, cik: str, accession: str, file_date: str, user_agent: str
    ) -> Optional[RawItem]:
        base = f"{ARCHIVES_URL}/{int(cik)}/{accession.replace('-', '')}"
        index = self._get_json(f"{base}/index.json", user_agent)
        names = [
            str(entry.get("name", ""))
            for entry in index.get("directory", {}).get("item", [])
        ]

        period = None
        if any(name == "primary_doc.xml" for name in names):
            cover = self._get(f"{base}/primary_doc.xml", user_agent)
            if cover.status_code == 200:
                period = self._period_from_cover(cover.text)

        holdings = None
        for name in names:
            if not name.lower().endswith(".xml") or "primary_doc" in name.lower():
                continue
            document = self._get(f"{base}/{name}", user_agent)
            if document.status_code != 200:
                continue
            parsed = self._parse_information_table(document.text)
            if parsed:
                holdings = parsed
                break
        if not holdings:
            logger.warning("filing %s has no parseable information table", accession)
            return None

        published_at = self._parse_date(file_date) or self._clock()
        content = self._render(fund, accession, file_date, period, holdings)
        return RawItem(
            external_id=accession,
            content=content,
            published_at=published_at,
            fields={
                "fund": fund,
                "cik": cik,
                "accession": accession,
                "form": "13F-HR",
                "file_date": file_date,
                "period_of_report": period or "",
                "holdings_count": str(len(holdings)),
            },
        )

    @staticmethod
    def _period_from_cover(xml_text: str) -> Optional[str]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return None
        for element in root.iter():
            if _local_name(element.tag) == "periodOfReport" and element.text:
                return element.text.strip()
        return None

    @staticmethod
    def _parse_information_table(xml_text: str) -> list[dict[str, Any]]:
        """Every infoTable entry, sorted by reported value, largest first."""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        holdings: list[dict[str, Any]] = []
        for element in root.iter():
            if _local_name(element.tag) != "infoTable":
                continue
            raw_value = _child_text(element, "value") or "0"
            try:
                value = int(raw_value.replace(",", ""))
            except ValueError:
                value = 0
            holdings.append(
                {
                    "issuer": _child_text(element, "nameOfIssuer") or "UNKNOWN ISSUER",
                    "class": _child_text(element, "titleOfClass") or "",
                    "cusip": _child_text(element, "cusip") or "",
                    "value": value,
                    "amount": _child_text(element, "sshPrnamt") or "",
                    "amount_type": _child_text(element, "sshPrnamtType") or "",
                    "put_call": _child_text(element, "putCall") or "",
                }
            )
        holdings.sort(key=lambda holding: holding["value"], reverse=True)
        return holdings

    def _render(
        self,
        fund: str,
        accession: str,
        file_date: str,
        period: Optional[str],
        holdings: list[dict[str, Any]],
    ) -> str:
        """Plain-text holdings summary. Data for the research layer, fenced downstream."""
        total = sum(holding["value"] for holding in holdings)
        lines = [
            f"13F-HR filing by {fund}",
            f"accession {accession}, filed {file_date or 'unknown'}, "
            f"reporting period {period or 'unknown'}",
            f"reported positions: {len(holdings)}; "
            f"total reported value: ${total:,} (as filed)",
            f"top {min(self._top, len(holdings))} holdings by reported value:",
        ]
        for rank, holding in enumerate(holdings[: self._top], start=1):
            side = f" ({holding['put_call']})" if holding["put_call"] else ""
            amount = (
                f", {holding['amount']} {holding['amount_type']}".rstrip()
                if holding["amount"]
                else ""
            )
            lines.append(
                f"  {rank}. {holding['issuer']}{side} — ${holding['value']:,}{amount}"
                f" [CUSIP {holding['cusip']}]"
            )
        return "\n".join(lines)

    # -- plumbing ---------------------------------------------------------------------

    def _resolve_user_agent(self, source: SourceConfig) -> str:
        """The SEC-required contact header. Config first, environment as fallback."""
        candidate = (
            source.user_agent
            or self._user_agent
            or os.environ.get("SEC_EDGAR_USER_AGENT")
            or ""
        ).strip()
        if "@" not in candidate:
            raise EdgarError(
                "the SEC requires a User-Agent naming a contact (an email address). "
                "Set user_agent on the form_13f source in config/signals.yaml, or "
                "SEC_EDGAR_USER_AGENT in .env."
            )
        return candidate

    #: One retry, once, on a throttle or server blip. The class polls daily, so a
    #: single transient 503 would otherwise cost a full day of latency; more than one
    #: retry would start to look like the hammering the throttle exists to stop.
    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _RETRY_PAUSE_SECONDS = 2.0

    def _get(
        self, url: str, user_agent: str, params: Optional[dict] = None
    ) -> httpx.Response:
        """One throttled request. Conservative against the SEC's 10 req/s ceiling."""
        response = self._request_once(url, user_agent, params)
        if response.status_code in self._RETRY_STATUSES:
            logger.warning(
                "EDGAR returned HTTP %d for %s; retrying once after %.0fs",
                response.status_code,
                url,
                self._RETRY_PAUSE_SECONDS,
            )
            self._sleep(self._RETRY_PAUSE_SECONDS)
            response = self._request_once(url, user_agent, params)
        return response

    def _request_once(
        self, url: str, user_agent: str, params: Optional[dict] = None
    ) -> httpx.Response:
        if self._last_request is not None:
            elapsed = self._monotonic() - self._last_request
            remaining = self._interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._monotonic()
        return self._client.get(
            url, params=params, headers={"User-Agent": user_agent}
        )

    def _get_json(self, url: str, user_agent: str) -> dict:
        response = self._get(url, user_agent)
        if response.status_code != 200:
            raise EdgarError(f"{url} returned HTTP {response.status_code}")
        return response.json()

    @staticmethod
    def _parse_date(raw: str) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def close(self) -> None:
        self._client.close()
