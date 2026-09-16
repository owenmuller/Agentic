"""SEC EDGAR Form 8-K fetcher — item-filtered current reports (human ruling
2026-09-15; design reviewed 2026-09-02).

What an 8-K is: the "current report" an issuer must file within FOUR business
days of a material event — usually the same day. Hundreds a day market-wide,
so the source is unusable unfiltered; a whitelist of ITEMS with documented
post-filing drift makes it tractable (signals.yaml ``items_whitelist``):

  5.02  departure or appointment of directors / certain officers
  4.02  non-reliance on previously issued financial statements (restatement)
  2.05  costs associated with exit or disposal activities (restructuring)
  1.01  entry into a material definitive agreement
  1.05  material cybersecurity incident

The announcement pop is public before this system polls and is forfeit by
design; the claim is item-specific drift, and an 8-K hands the judged arm a
DATED catalyst — exactly what the catalyst gate and the options doors are
built around. Bearish items (``bearish_items``: 4.02, 1.05) are emitted
MEASUREMENT-ONLY until a bearish trading path exists; a filing carrying both a
bearish and a researchable item is measurement-only (Constraint #6).

Access pattern (probed live 2026-09-15): EDGAR full-text search hits for form
8-K carry the filing's ``items`` list, the issuer's ticker inside
``display_names``, ``period_ending`` (the event date) and ``file_date``. One
throttled listing request per whitelisted item per poll, unioned by accession;
the filing's primary document is fetched from the Archives index and an
excerpt of its text rides the signal content (fenced as untrusted content by
the prompt builder like every source's words). Class 1 polls every 60-120s;
the fetcher self-throttles to ``min_poll_interval_seconds`` because the FTS
index itself updates in minutes, not seconds.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional, Sequence

from signals.config import SourceConfig
from signals.edgar import ARCHIVES_URL, FTS_URL, EdgarFetcherBase
from signals.form13d import tickers_from_display
from signals.scanners import RawItem

logger = logging.getLogger("signals.form8k")

FORM_NAMES = ("8-K", "8-K/A")

#: Plain-English item names for the content block (the number is the fact).
ITEM_NAMES = {
    "1.01": "entry into a material definitive agreement",
    "1.05": "material cybersecurity incident",
    "2.05": "costs associated with exit or disposal activities (restructuring)",
    "4.02": "non-reliance on previously issued financial statements (restatement)",
    "5.02": "departure or appointment of directors or certain officers",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def strip_html(text: str, limit: int) -> str:
    """Tags out, entities decoded, whitespace collapsed, truncated. Never raises."""
    plain = html.unescape(_TAG.sub(" ", text or ""))
    plain = _SPACE.sub(" ", plain).strip()
    return plain[:limit]


class Form8KFetcher(EdgarFetcherBase):
    """New 8-Ks carrying a whitelisted item, market-wide."""

    def __init__(
        self,
        client=None,
        *,
        user_agent: Optional[str] = None,
        lookback_days: int = 1,
        min_poll_interval_seconds: int = 300,
        excerpt_chars: int = 2500,
        min_request_interval: float = 0.5,
        timeout: float = 15.0,
        clock=None,
        sleeper=None,
        monotonic=None,
        seen: Optional[Sequence[str]] = None,
    ) -> None:
        super().__init__(
            client,
            user_agent=user_agent,
            min_request_interval=min_request_interval,
            timeout=timeout,
            clock=clock,
            sleeper=sleeper,
            monotonic=monotonic,
        )
        self._lookback = timedelta(days=lookback_days)
        self._min_poll = timedelta(seconds=min_poll_interval_seconds)
        self._excerpt_chars = excerpt_chars
        self._seen: set[str] = set(seen or ())
        self._last_fetch: Optional[datetime] = None
        #: Per-poll funnel, for the CLASSIFY line: listed / off-whitelist /
        #: no ticker / measurement / emitted.
        self.last_tally: dict[str, int] = {}

    # -- the Fetcher protocol ----------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        whitelist = tuple(source.items_whitelist)
        if not whitelist:
            return []
        now = self._clock()
        if self._last_fetch is not None and now - self._last_fetch < self._min_poll:
            return []  # the FTS index moves in minutes; Class 1 polls in seconds
        self._last_fetch = now
        user_agent = self._resolve_user_agent(source)
        today = now.date()
        bearish = set(source.bearish_items)
        tally = {"listed": 0, "off_whitelist": 0, "no_ticker": 0, "measurement": 0, "emitted": 0}
        hits_by_accession: dict[str, dict] = {}
        for item in whitelist:
            for hit in self._list(item, user_agent, today):
                accession = (hit.get("_source") or {}).get("adsh") or ""
                if accession and accession not in hits_by_accession:
                    hits_by_accession[accession] = hit
        items: list[RawItem] = []
        for accession, hit in hits_by_accession.items():
            tally["listed"] += 1
            item = self._item_from_hit(hit, whitelist, bearish, user_agent, tally)
            if item is not None:
                items.append(item)
        self.last_tally = tally
        return items

    def _list(self, item: str, user_agent: str, today: date) -> list[dict]:
        response = self._get(
            FTS_URL,
            user_agent,
            params={
                "q": f'"Item {item}"',
                "forms": "8-K",
                "startdt": (today - self._lookback).isoformat(),
                "enddt": today.isoformat(),
            },
        )
        if response.status_code != 200:
            logger.warning(
                "8-K listing (item %s) returned HTTP %d; skipping this cycle",
                item,
                response.status_code,
            )
            return []
        try:
            return response.json().get("hits", {}).get("hits", [])
        except ValueError:
            return []

    # -- one filing ------------------------------------------------------------------

    def _item_from_hit(
        self,
        hit: dict,
        whitelist: tuple[str, ...],
        bearish: set[str],
        user_agent: str,
        tally: dict[str, int],
    ) -> Optional[RawItem]:
        source_data = hit.get("_source", {})
        accession = str(source_data.get("adsh") or "")
        if not accession or accession in self._seen:
            return None
        form = str(source_data.get("form") or "8-K")
        if form not in FORM_NAMES:
            return None
        items = tuple(str(i) for i in (source_data.get("items") or []))
        matched = tuple(i for i in items if i in whitelist)
        if not matched:
            tally["off_whitelist"] += 1
            return None
        names = [str(n) for n in (source_data.get("display_names") or [])]
        subject = next((n for n in names if tickers_from_display(n)), "")
        tickers = tickers_from_display(subject)
        if not tickers:
            tally["no_ticker"] += 1
            self._seen.add(accession)  # nothing to trade; never re-list it
            return None
        self._seen.add(accession)
        ciks = [str(c) for c in (source_data.get("ciks") or [])]
        issuer = subject.split("  (")[0].strip() or "unknown issuer"
        file_date = str(source_data.get("file_date") or "")
        event_date = str(source_data.get("period_ending") or "")
        measurement_only = any(i in bearish for i in matched)
        if measurement_only:
            tally["measurement"] += 1
        else:
            tally["emitted"] += 1

        excerpt = self._excerpt(ciks[0] if ciks else "", accession, user_agent)
        item_lines = [
            f"  - Item {i}: {ITEM_NAMES.get(i, 'see filing')}"
            + (" [WHITELISTED]" if i in whitelist else "")
            + (" [BEARISH — measurement only]" if i in bearish else "")
            for i in items
        ]
        lines = [
            f"Form {form} current report (SEC EDGAR)",
            f"issuer: {issuer} ({tickers[0]})",
            f"items: {', '.join(items)}",
            *item_lines,
            f"event date (period of report): {event_date or 'not stated'}; filed: {file_date}",
            "filing deadline is 4 business days after the event; the announcement is "
            "public before this system reads it",
        ]
        if form.endswith("/A"):
            lines.append("AMENDMENT of an earlier 8-K — restates or completes a prior report")
        if measurement_only:
            lines.append(
                "MEASUREMENT ONLY (ruling 2026-09-15): a bearish item is present and no "
                "bearish trading path exists; recorded for the forward engine to grade"
            )
        url = self._filing_url(ciks[0] if ciks else "", accession)
        lines.append(f"filing index: {url}")
        if excerpt:
            lines.append("primary document excerpt:")
            lines.append(excerpt)

        fields = {
            "form": form,
            "accession": accession,
            "ticker": tickers[0],
            "issuer": issuer,
            "issuer_cik": ciks[0] if ciks else "",
            "items": ",".join(items),
            "whitelisted_items": ",".join(matched),
            "report_date": file_date,
            "event_date": event_date,
            "amendment": "true" if form.endswith("/A") else "false",
            "measurement_only": "true" if measurement_only else "false",
            "filing_url": url,
        }
        published = self._parse_date(file_date) or self._clock()
        return RawItem(
            external_id=accession,
            content="\n".join(lines),
            published_at=published,
            fields=fields,
        )

    @staticmethod
    def _filing_url(cik: str, accession: str) -> str:
        folder = accession.replace("-", "")
        cik_int = cik.lstrip("0") or cik
        return f"{ARCHIVES_URL}/{cik_int}/{folder}/"

    def _excerpt(self, cik: str, accession: str, user_agent: str) -> str:
        """The primary document's text, stripped and truncated; "" on any failure.
        Degrade, never drop: the facts from the hit stand on their own."""
        if not cik:
            return ""
        base = self._filing_url(cik, accession)
        try:
            index = self._get_json(base + "index.json", user_agent)
            entries = (index.get("directory") or {}).get("item") or []
            names = [str(e.get("name") or "") for e in entries]
            primary = next(
                (
                    n
                    for n in names
                    if n.lower().endswith((".htm", ".html"))
                    and not n.lower().startswith("ex")
                    and "index" not in n.lower()
                ),
                "",
            )
            if not primary:
                return ""
            response = self._get(base + primary, user_agent)
            if response.status_code != 200:
                return ""
            return strip_html(response.text, self._excerpt_chars)
        except Exception as error:  # noqa: BLE001 - the excerpt is a courtesy
            logger.warning("8-K excerpt for %s failed: %s", accession, error)
            return ""
