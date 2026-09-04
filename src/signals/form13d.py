"""SEC EDGAR Schedule 13D fetcher — activist stakes (human ruling 2026-09-02).

What a 13D is: a beneficial-ownership filing due within FIVE business days of
crossing 5% of a company (amendments within two business days of a material
change). The well-documented announcement pop happens ON filing day, mostly
within hours of the EDGAR hit — at this system's polling latency that pop is
forfeit, deliberately and honestly. What is traded on is the slower story:
post-filing drift on prominent activists' campaigns, and the amendment trail
where campaigns actually unfold (stake changes, intent changes).

Access pattern (probed live 2026-09-02): since the SEC's structured-data rule
(Dec 2024) these file as form ``SCHEDULE 13D`` / ``SCHEDULE 13D/A`` with a
machine-readable ``primary_doc.xml`` — issuer name/CIK/CUSIP, date of event,
per-person ``percentOfClass`` and ``aggregateAmountOwned``, amendment number.
The FTS hit's ``display_names`` even carries the subject's ticker. Volume is
~50/day market-wide across both forms, so the fetcher lists market-wide (two
throttled requests per poll) and filters client-side against the watchlist —
the same substring-match-on-filer-display-name convention as the 13F fetcher.

The watchlist lives in ``config/signals.yaml`` and adding a name is a human
ruling, per source, like every watchlist. Signals attribute to ``form_13d``
with ``credibility_key`` per activist, so attribution ranks activists
empirically the way it ranks congressional filers.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Optional, Sequence

from signals.config import SourceConfig
from signals.classification import is_us_listed_symbol
from signals.edgar import ARCHIVES_URL, FTS_URL, EdgarFetcherBase
from signals.scanners import RawItem

logger = logging.getLogger("signals.form13d")

FORM_NAMES = ("SCHEDULE 13D", "SCHEDULE 13D/A")

#: The ticker(s) EDGAR renders into a subject company's display name:
#: "SIEBERT FINANCIAL CORP  (SIEB)  (CIK 0000065596)".
_DISPLAY_TICKERS = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9}(?:,\s*[A-Z][A-Z0-9.\-]{0,9})*)\)")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if _local(element.tag) == name and element.text:
            return element.text.strip()
    return ""


def tickers_from_display(display: str) -> tuple[str, ...]:
    """Tickers from a subject company's display name; CIK parens don't match,
    and only US-listed symbol shapes survive (ruling 2026-09-04)."""
    for match in _DISPLAY_TICKERS.finditer(display):
        group = match.group(1)
        if group.startswith("CIK"):
            continue
        return tuple(
            part.strip() for part in group.split(",") if is_us_listed_symbol(part.strip())
        )
    return ()


class Form13DFetcher(EdgarFetcherBase):
    """New 13Ds and amendments by the activists on the source's watchlist."""

    def __init__(
        self,
        client=None,
        *,
        user_agent: Optional[str] = None,
        lookback_days: int = 3,
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
        self._seen: set[str] = set(seen or ())

    # -- the Fetcher protocol ----------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        user_agent = self._resolve_user_agent(source)
        activists = [
            str(entry.get("activist") or entry.get("name") or "")
            for entry in source.watchlist
        ]
        activists = [name for name in activists if name]
        if not activists:
            return []
        today = self._clock().date()
        items: list[RawItem] = []
        for form_name in FORM_NAMES:
            for hit in self._list(form_name, user_agent, today):
                item = self._item_from_hit(hit, activists, user_agent)
                if item is not None:
                    items.append(item)
        return items

    def _list(self, form_name: str, user_agent: str, today: date) -> list[dict]:
        response = self._get(
            FTS_URL,
            user_agent,
            params={
                "q": '"13D"',
                "forms": form_name,
                "startdt": (today - self._lookback).isoformat(),
                "enddt": today.isoformat(),
            },
        )
        if response.status_code != 200:
            logger.warning(
                "13D listing (%s) returned HTTP %d; skipping this cycle",
                form_name,
                response.status_code,
            )
            return []
        return response.json().get("hits", {}).get("hits", [])

    # -- one filing ------------------------------------------------------------------

    def _item_from_hit(
        self, hit: dict, activists: list[str], user_agent: str
    ) -> Optional[RawItem]:
        source_data = hit.get("_source", {})
        accession = source_data.get("adsh") or ""
        if not accession or accession in self._seen:
            return None
        names = [str(name) for name in source_data.get("display_names") or []]
        # Watchlist filter, same convention as the 13F fetcher: the ACTIVIST
        # must substring-match a filer display name. The subject company also
        # appears in display_names; matching it too would be wrong only if an
        # activist's name were inside a company's name, which the curated
        # watchlist avoids by construction.
        matched = next(
            (
                activist
                for activist in activists
                for name in names
                if activist.lower() in name.lower()
            ),
            None,
        )
        if matched is None:
            return None
        self._seen.add(accession)

        subject = next((n for n in names if tickers_from_display(n)), "")
        tickers = tickers_from_display(subject)
        ciks = source_data.get("ciks") or []
        form = str(source_data.get("form", "SCHEDULE 13D"))
        file_date = str(source_data.get("file_date", ""))

        facts = self._facts_from_xml(accession, str(ciks[0]) if ciks else "", user_agent)
        issuer = facts.get("issuer_name") or subject.split("  (")[0]

        lines = [
            f"{form} filing (SEC EDGAR, structured XML) — activist beneficial "
            f"ownership",
            f"subject company: {issuer}" + (f" ({tickers[0]})" if tickers else ""),
            f"filed by: {matched} (watchlist activist)",
        ]
        if form.endswith("/A"):
            lines.append(
                f"amendment no. {facts.get('amendment_no') or 'not stated'} — an "
                f"amendment updates an EXISTING campaign (stake or intent changed)"
            )
        else:
            lines.append("INITIAL 13D — a new activist stake crossing 5%")
        lines.append(
            f"date of event requiring filing: "
            f"{facts.get('date_of_event') or 'not stated'}; filed: {file_date}"
        )
        stake = facts.get("percent_of_class")
        shares = facts.get("aggregate_shares")
        lines.append(
            "stake: "
            + (f"{stake}% of class" if stake else "percent not stated")
            + (f", {shares} shares" if shares else "")
            + (f" of {facts['class_title']}" if facts.get("class_title") else "")
        )

        fields = {
            "form": form,
            "accession": accession,
            "ticker": tickers[0] if tickers else "",
            "issuer": issuer,
            "issuer_cik": facts.get("issuer_cik", ""),
            "filer": matched,
            "credibility_key": f"form_13d/{matched}",
            "report_date": file_date,
            "date_of_event": facts.get("date_of_event", ""),
            "percent_of_class": facts.get("percent_of_class", ""),
            "aggregate_shares": facts.get("aggregate_shares", ""),
            "amendment_no": facts.get("amendment_no", ""),
        }
        published = self._parse_date(file_date) or self._clock()
        return RawItem(
            external_id=accession,
            content="\n".join(lines),
            published_at=published,
            fields=fields,
        )

    def _facts_from_xml(
        self, accession: str, cik: str, user_agent: str
    ) -> dict[str, str]:
        """Structured facts from primary_doc.xml; empty facts when unparseable —
        under-filtering surfaces to research, a dropped filing would not."""
        if not cik:
            return {}
        base = f"{ARCHIVES_URL}/{int(cik)}/{accession.replace('-', '')}"
        response = self._get(f"{base}/primary_doc.xml", user_agent)
        if response.status_code != 200:
            return {}
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            return {}
        percents = []
        for element in root.iter():
            if _local(element.tag) in ("percentOfClass", "percentageOfClassSecurities"):
                try:
                    percents.append(float((element.text or "").strip()))
                except ValueError:
                    continue
        return {
            "issuer_name": _first_text(root, "issuerName"),
            "issuer_cik": _first_text(root, "issuerCIK"),
            "date_of_event": _first_text(root, "dateOfEvent"),
            "amendment_no": _first_text(root, "amendmentNo"),
            "class_title": _first_text(root, "securitiesClassTitle"),
            "aggregate_shares": _first_text(root, "aggregateAmountOwned"),
            # The largest reported percent is the activist's own stake; group
            # members report smaller slices of the same position.
            "percent_of_class": (
                f"{max(percents):g}" if percents else ""
            ),
        }
