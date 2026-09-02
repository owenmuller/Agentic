"""SEC EDGAR Form 4 fetcher — insider cluster buying (human ruling 2026-09-02).

The recipe, deterministic end to end (every parameter is a ruling, dated):

  transaction code P only     open-market purchases; codes M/A/S/D/F/G and the
                              whole derivative table are compensation mechanics
                              or exits, not conviction
  10b5-1 excluded             via the structured ``aff10b5One`` checkbox the
                              April-2023 rule amendments added — a field read,
                              never footnote text-mining
  $50,000 per-insider floor   the filing's summed P legs (shares x price); a leg
                              with no stated price contributes zero, which errs
                              toward fewer signals (Constraint #6)
  cluster = >=2 insiders      distinct reporting owners on the same issuer whose
    within 15 days            purchases fall inside the rolling window, AND
  >=$150,000 aggregate        summed across the window's qualifying purchases
  routine excluded            an insider whose prior Form 4s land in the SAME
                              calendar month 3+ consecutive years is routine
                              (Cohen-Malloy-Pomorski); v1 PROXY: filing months
                              from the owner's data.sec.gov submissions index,
                              purchases not distinguished from sales — the proxy
                              over-suppresses, the less-risk direction, and our
                              own cache refines it once history accumulates.
                              Unknown history defaults OPPORTUNISTIC (ruling:
                              the alternative silently kills the source for
                              years, and the floors plus research remain).

Singles that fail ONLY the cluster test are still emitted, marked
``cluster: false`` — the orchestrator's prefilter records them (code
``no_cluster``) so the forward-return engine measures the cluster rule itself:
the prefiltered singles are the control group.

Access pattern (probed live 2026-09-02): EDGAR full-text search with
``forms=4`` lists filings (100 hits/page, ``from=`` paging; 549 Form 4s filed
2026-09-01 — quiet season; earnings windows run 2-3x that). Each filing is one
further request: the full-submission ``.txt``, from which the structured
``ownershipDocument`` XML is sliced. A per-poll fetch budget bounds the wall
time one poll may spend; the remainder carries in a backlog to the next poll,
newest first, and a warning is logged when the backlog grows — nothing is ever
silently dropped. Filing deadline is two business days after the transaction,
so the signal is days-fresh; hourly Class 2 cadence loses nothing.

State (window, seen accessions, routine cache, backlog) persists to a JSON
file so a restart neither re-fetches paid-for filings nor forgets a
half-formed cluster. The file is operator-wired (data/form4_state.json);
signals imports nothing first-party, so the path arrives as an argument.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import httpx

from signals.config import SourceConfig
from signals.edgar import ARCHIVES_URL, FTS_URL, EdgarFetcherBase
from signals.scanners import RawItem

SUBMISSIONS_URL = "https://data.sec.gov/submissions"

logger = logging.getLogger("signals.form4")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    # Form 4 wraps most scalars in a <value> child; take it when present.
    for child in element:
        if _local(child.tag) == "value":
            return (child.text or "").strip()
    return (element.text or "").strip()


def _find(root: ET.Element, name: str) -> Optional[ET.Element]:
    for element in root.iter():
        if _local(element.tag) == name:
            return element
    return None


@dataclass(frozen=True, slots=True)
class QualifyingPurchase:
    """One insider identity's qualifying open-market purchase from one filing.

    A multi-owner filing (spouse, trust, co-filers) is ONE identity — keyed by
    the first reporting owner's CIK, names joined — because joint filers are one
    decision unit, and counting them separately would manufacture clusters.
    """

    accession: str
    file_date: str
    issuer_cik: str
    issuer_name: str
    symbol: str
    owner_cik: str
    owner_name: str
    roles: str
    transaction_date: str  # earliest P leg, ISO date
    shares: float
    amount: float


@dataclass(frozen=True, slots=True)
class _ParsedFiling:
    issuer_cik: str
    issuer_name: str
    symbol: str
    owner_cik: str
    owner_name: str
    roles: str
    plan: bool  # aff10b5One checkbox
    purchase_date: Optional[str]
    shares: float
    amount: float
    #: The SELL side (bearish groundwork, ruling 2026-09-02): code S open-market
    #: sales, summed the same way. Parsed for measurement only — no bearish
    #: trading path exists.
    sale_date: Optional[str] = None
    sale_shares: float = 0.0
    sale_amount: float = 0.0


def parse_ownership_document(xml_text: str) -> Optional[_ParsedFiling]:
    """Structured facts from one ``ownershipDocument``; None if unparseable."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    if _local(root.tag) != "ownershipDocument":
        return None

    symbol = _text(_find(root, "issuerTradingSymbol")).upper()
    issuer_cik = _text(_find(root, "issuerCik"))
    issuer_name = _text(_find(root, "issuerName"))

    owner_ciks: list[str] = []
    owner_names: list[str] = []
    role_bits: list[str] = []
    for owner in root.iter():
        if _local(owner.tag) != "reportingOwner":
            continue
        cik = _text(_find(owner, "rptOwnerCik"))
        name = _text(_find(owner, "rptOwnerName"))
        if cik:
            owner_ciks.append(cik)
        if name:
            owner_names.append(name)
        roles = []
        if _text(_find(owner, "isDirector")) in ("1", "true"):
            roles.append("director")
        if _text(_find(owner, "isOfficer")) in ("1", "true"):
            title = _text(_find(owner, "officerTitle"))
            roles.append(f"officer ({title})" if title else "officer")
        if _text(_find(owner, "isTenPercentOwner")) in ("1", "true"):
            roles.append("10% owner")
        if roles:
            role_bits.append(", ".join(roles))
    if not owner_ciks:
        return None

    plan = _text(_find(root, "aff10b5One")) in ("1", "true")

    shares_total = 0.0
    amount_total = 0.0
    earliest: Optional[str] = None
    sale_shares_total = 0.0
    sale_amount_total = 0.0
    earliest_sale: Optional[str] = None
    for txn in root.iter():
        if _local(txn.tag) != "nonDerivativeTransaction":
            continue
        code = _text(_find(txn, "transactionCode"))
        acquired = _text(_find(txn, "transactionAcquiredDisposedCode"))
        is_purchase = code == "P" and acquired == "A"
        is_sale = code == "S" and acquired == "D"
        if not (is_purchase or is_sale):
            continue
        try:
            shares = float(_text(_find(txn, "transactionShares")) or 0)
        except ValueError:
            shares = 0.0
        try:
            price = float(_text(_find(txn, "transactionPricePerShare")) or 0)
        except ValueError:
            price = 0.0  # footnoted price: contributes zero, fewer signals
        when = _text(_find(txn, "transactionDate"))
        if is_purchase:
            shares_total += shares
            amount_total += shares * price
            if when and (earliest is None or when < earliest):
                earliest = when
        else:
            sale_shares_total += shares
            sale_amount_total += shares * price
            if when and (earliest_sale is None or when < earliest_sale):
                earliest_sale = when

    return _ParsedFiling(
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        symbol=symbol,
        owner_cik=owner_ciks[0],
        owner_name="; ".join(owner_names) or owner_ciks[0],
        roles="; ".join(role_bits),
        plan=plan,
        purchase_date=earliest,
        shares=shares_total,
        amount=amount_total,
        sale_date=earliest_sale,
        sale_shares=sale_shares_total,
        sale_amount=sale_amount_total,
    )


def is_routine_month(filed_months: set[str], transaction_month: str) -> bool:
    """The routine test: the transaction's calendar month appears in each of the
    THREE immediately prior years of the owner's Form 4 filing history."""
    try:
        year, month = transaction_month.split("-")
        year_number = int(year)
    except ValueError:
        return False
    return all(
        f"{year_number - back}-{month}" in filed_months for back in (1, 2, 3)
    )


class Form4InsiderFetcher(EdgarFetcherBase):
    """Fetches new Form 4 filings market-wide and emits cluster/single items."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        user_agent: Optional[str] = None,
        state_path: Optional[Path] = None,
        lookback_days: int = 1,
        first_poll_lookback_days: int = 3,
        window_days: int = 15,
        min_insider_usd: float = 50_000,
        min_cluster_usd: float = 150_000,
        min_insiders: int = 2,
        fetch_budget_per_poll: int = 150,
        max_list_pages: int = 40,
        min_request_interval: float = 0.25,
        timeout: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
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
        self._state_path = state_path
        self._lookback_days = lookback_days
        self._first_poll_lookback_days = first_poll_lookback_days
        self._window_days = window_days
        self._min_insider_usd = min_insider_usd
        self._min_cluster_usd = min_cluster_usd
        self._min_insiders = min_insiders
        self._fetch_budget = fetch_budget_per_poll
        self._max_list_pages = max_list_pages

        #: accession -> file_date, pruned past the lookback horizon.
        self._seen: dict[str, str] = {}
        #: qualifying opportunistic purchases inside the rolling window.
        self._window: list[QualifyingPurchase] = []
        #: the mirror image (bearish groundwork, ruling 2026-09-02): qualifying
        #: opportunistic SALES. Sell clusters are emitted MEASUREMENT-ONLY —
        #: recorded, never researched, never traded.
        self._sell_window: list[QualifyingPurchase] = []
        #: owner_cik -> {"months": [...], "fetched": iso date}; misses default
        #: opportunistic (ruling 2026-09-02).
        self._routine: dict[str, dict[str, Any]] = {}
        #: (accession, cik, file_date) awaiting an XML fetch, budget carry-over.
        self._backlog: list[tuple[str, str, str]] = []
        self._load_state()
        for accession in seen or ():
            self._seen.setdefault(accession, "")

    # -- the Fetcher protocol ----------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        user_agent = self._resolve_user_agent(source)
        today = self._clock().date()
        tally: Counter = Counter()

        self._list_new_filings(user_agent, today, tally)

        items: list[RawItem] = []
        budget = self._fetch_budget
        # Newest first: under budget pressure the freshest filings emit first.
        self._backlog.sort(key=lambda entry: entry[2], reverse=True)
        pending = self._backlog
        self._backlog = []
        for accession, cik, file_date in pending:
            if budget <= 0:
                self._backlog.append((accession, cik, file_date))
                continue
            budget -= 1
            item = self._process_filing(
                accession, cik, file_date, user_agent, today, tally
            )
            if item is not None:
                items.append(item)

        self._prune(today)
        self._save_state()
        if self._backlog:
            logger.warning(
                "form4 poll budget spent with %d filings still in the backlog; "
                "they carry to the next poll, newest first",
                len(self._backlog),
            )
        if tally:
            logger.info(
                "form4 poll: %s",
                " ".join(f"{key}={count}" for key, count in sorted(tally.items())),
            )
        return items

    # -- listing -------------------------------------------------------------------

    def _list_new_filings(
        self, user_agent: str, today: date, tally: Counter
    ) -> None:
        lookback = (
            self._first_poll_lookback_days if not self._seen else self._lookback_days
        )
        start = (today - timedelta(days=lookback)).isoformat()
        offset = 0
        for _ in range(self._max_list_pages):
            response = self._get(
                FTS_URL,
                user_agent,
                params={
                    "q": '"4"',
                    "forms": "4",
                    "startdt": start,
                    "enddt": today.isoformat(),
                    "from": str(offset),
                },
            )
            if response.status_code != 200:
                logger.warning(
                    "form4 FTS listing returned HTTP %d; polling what is "
                    "already backlogged",
                    response.status_code,
                )
                return
            hits = response.json().get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                source_data = hit.get("_source", {})
                if source_data.get("form") != "4":
                    tally["skipped_amendment"] += 1  # 4/A corrections, stated
                    continue
                accession = source_data.get("adsh") or ""
                ciks = source_data.get("ciks") or []
                if not accession or not ciks:
                    continue
                if accession in self._seen or any(
                    accession == queued for queued, _, _ in self._backlog
                ):
                    continue
                self._backlog.append(
                    (accession, str(ciks[0]), str(source_data.get("file_date", "")))
                )
                tally["listed"] += 1
            if len(hits) < 100:
                return
            offset += len(hits)
        logger.warning(
            "form4 FTS listing hit the %d-page cap; the rest lists next poll",
            self._max_list_pages,
        )

    # -- one filing ------------------------------------------------------------------

    def _process_filing(
        self,
        accession: str,
        cik: str,
        file_date: str,
        user_agent: str,
        today: date,
        tally: Counter,
    ) -> Optional[RawItem]:
        self._seen[accession] = file_date or today.isoformat()
        xml_text = self._fetch_document(accession, cik, user_agent)
        if xml_text is None:
            tally["unparseable"] += 1
            return None
        parsed = parse_ownership_document(xml_text)
        if parsed is None:
            tally["unparseable"] += 1
            return None
        if parsed.plan:
            tally["plan_10b5_1"] += 1
            return None
        if parsed.purchase_date is None or parsed.shares <= 0:
            # No open-market purchase; the SELL side may still matter — bearish
            # groundwork (ruling 2026-09-02), measurement-only.
            item = self._process_sale(
                accession, file_date, parsed, user_agent, today, tally
            )
            if item is None:
                tally["no_open_market_purchase"] += 1
            return item
        if parsed.amount < self._min_insider_usd:
            tally["below_insider_floor"] += 1
            return None
        if not parsed.symbol:
            tally["no_symbol"] += 1
            return None
        if self._is_routine(parsed, user_agent):
            tally["routine"] += 1
            return None

        purchase = QualifyingPurchase(
            accession=accession,
            file_date=file_date or today.isoformat(),
            issuer_cik=parsed.issuer_cik,
            issuer_name=parsed.issuer_name,
            symbol=parsed.symbol,
            owner_cik=parsed.owner_cik,
            owner_name=parsed.owner_name,
            roles=parsed.roles,
            transaction_date=parsed.purchase_date,
            shares=parsed.shares,
            amount=parsed.amount,
        )
        self._window.append(purchase)
        cluster = [
            event
            for event in self._window
            if event.issuer_cik == purchase.issuer_cik
            and self._within_window(event, purchase, today)
        ]
        insiders = {event.owner_cik for event in cluster}
        aggregate = sum(event.amount for event in cluster)
        clustered = (
            len(insiders) >= self._min_insiders
            and aggregate >= self._min_cluster_usd
        )
        tally["cluster" if clustered else "single"] += 1
        return self._item(purchase, cluster, clustered, aggregate, insiders)

    def _process_sale(
        self,
        accession: str,
        file_date: str,
        parsed: "_ParsedFiling",
        user_agent: str,
        today: date,
        tally: Counter,
    ) -> Optional[RawItem]:
        """The mirror recipe on the SELL side (bearish groundwork, 2026-09-02):
        same floors, same window, same routine exclusion — but a completed sell
        cluster is emitted MEASUREMENT-ONLY (the prefilter records it, research
        never sees it, nothing trades). Sell singles are not emitted at all."""
        if (
            parsed.sale_date is None
            or parsed.sale_shares <= 0
            or parsed.sale_amount < self._min_insider_usd
            or not parsed.symbol
        ):
            return None
        if self._is_routine_month_for(parsed.owner_cik, parsed.sale_date, user_agent):
            tally["routine_sale"] += 1
            return None
        sale = QualifyingPurchase(
            accession=accession,
            file_date=file_date or today.isoformat(),
            issuer_cik=parsed.issuer_cik,
            issuer_name=parsed.issuer_name,
            symbol=parsed.symbol,
            owner_cik=parsed.owner_cik,
            owner_name=parsed.owner_name,
            roles=parsed.roles,
            transaction_date=parsed.sale_date,
            shares=parsed.sale_shares,
            amount=parsed.sale_amount,
        )
        self._sell_window.append(sale)
        cluster = [
            event
            for event in self._sell_window
            if event.issuer_cik == sale.issuer_cik
            and self._within_window(event, sale, today)
        ]
        insiders = {event.owner_cik for event in cluster}
        aggregate = sum(event.amount for event in cluster)
        if (
            len(insiders) < self._min_insiders
            or aggregate < self._min_cluster_usd
        ):
            tally["sell_single"] += 1
            return None
        tally["sell_cluster"] += 1
        ordered = sorted(cluster, key=lambda event: event.transaction_date)
        lines = [
            "Form 4 insider SELL cluster — BEARISH MEASUREMENT ONLY (ruling "
            "2026-09-02: recorded for the forward engine, never researched, "
            "never traded; no bearish path exists)",
            f"issuer: {sale.issuer_name} ({sale.symbol})",
            f"{len(insiders)} distinct insiders made open-market sales within "
            f"the {self._window_days}-day window; aggregate ${aggregate:,.0f}",
        ]
        for event in ordered:
            roles = f" [{event.roles}]" if event.roles else ""
            lines.append(
                f"  - {event.owner_name}{roles}: {event.shares:,.0f} shares, "
                f"${event.amount:,.0f}, traded {event.transaction_date}, "
                f"filed {event.file_date}"
            )
        fields = {
            "form": "4",
            "accession": sale.accession,
            "ticker": sale.symbol,
            "issuer": sale.issuer_name,
            "issuer_cik": sale.issuer_cik,
            "transaction": "Sale",
            "report_date": sale.file_date,
            "transaction_date": ordered[0].transaction_date,
            "amount_range": f"${aggregate:,.0f}",
            "cluster": "true",
            "cluster_insiders": str(len(insiders)),
            "filer": "; ".join(sorted({event.owner_name for event in cluster})),
            "measurement_only": "true",
        }
        published = self._parse_date(sale.file_date) or self._clock()
        return RawItem(
            external_id=sale.accession,
            content="\n".join(lines),
            published_at=published,
            fields=fields,
        )

    def _within_window(
        self, event: QualifyingPurchase, anchor: QualifyingPurchase, today: date
    ) -> bool:
        try:
            gap = abs(
                (
                    date.fromisoformat(event.transaction_date)
                    - date.fromisoformat(anchor.transaction_date)
                ).days
            )
        except ValueError:
            return False
        return gap <= self._window_days

    def _fetch_document(
        self, accession: str, cik: str, user_agent: str
    ) -> Optional[str]:
        """The ownershipDocument XML, sliced from the one-request full submission."""
        base = f"{ARCHIVES_URL}/{int(cik)}/{accession.replace('-', '')}"
        response = self._get(f"{base}/{accession}.txt", user_agent)
        if response.status_code != 200:
            return None
        text = response.text
        start = text.find("<ownershipDocument")
        end = text.find("</ownershipDocument>")
        if start == -1 or end == -1:
            return None
        return text[start : end + len("</ownershipDocument>")]

    # -- routine vs opportunistic ---------------------------------------------------

    def _is_routine(self, parsed: _ParsedFiling, user_agent: str) -> bool:
        return self._is_routine_month_for(
            parsed.owner_cik, parsed.purchase_date or "", user_agent
        )

    def _is_routine_month_for(
        self, owner_cik: str, transaction_date: str, user_agent: str
    ) -> bool:
        cached = self._routine.get(owner_cik)
        if cached is None:
            cached = self._fetch_filing_months(owner_cik, user_agent)
            self._routine[owner_cik] = cached
        months = set(cached.get("months", ()))
        if not months:
            return False  # unknown history defaults opportunistic (ruling)
        return is_routine_month(months, transaction_date[:7])

    def _fetch_filing_months(self, owner_cik: str, user_agent: str) -> dict:
        """The owner's Form 4 filing months from their submissions index. One
        request per insider, cached in the state file; a failed fetch caches
        empty (opportunistic) and is retried on a later restart, not this run."""
        url = f"{SUBMISSIONS_URL}/CIK{int(owner_cik):010d}.json"
        try:
            payload = self._get_json(url, user_agent)
        except Exception as error:  # noqa: BLE001 - a miss must not kill the poll
            logger.warning(
                "submissions history for owner %s unavailable (%s); "
                "defaulting opportunistic",
                owner_cik,
                error,
            )
            return {"months": [], "fetched": self._clock().date().isoformat()}
        recent = payload.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        months = sorted(
            {
                str(filed)[:7]
                for form, filed in zip(forms, dates)
                if str(form).startswith("4") and filed
            }
        )
        return {"months": months, "fetched": self._clock().date().isoformat()}

    # -- rendering --------------------------------------------------------------------

    def _item(
        self,
        purchase: QualifyingPurchase,
        cluster: list[QualifyingPurchase],
        clustered: bool,
        aggregate: float,
        insiders: set[str],
    ) -> RawItem:
        ordered = sorted(cluster, key=lambda event: event.transaction_date)
        earliest = ordered[0].transaction_date
        lines = [
            "Form 4 insider filing (SEC EDGAR, structured XML)",
            f"issuer: {purchase.issuer_name} ({purchase.symbol})",
        ]
        if clustered:
            lines.append(
                f"CLUSTER: {len(insiders)} distinct insiders made open-market "
                f"purchases within the {self._window_days}-day window; "
                f"aggregate ${aggregate:,.0f}"
            )
        else:
            lines.append(
                "single qualifying purchase — no cluster in the "
                f"{self._window_days}-day window "
                f"({len(insiders)} insider(s), aggregate ${aggregate:,.0f})"
            )
        lines.append(
            "all transactions are code P (open-market purchase); Rule 10b5-1 "
            "plan trades and routine same-month-3-years buyers are excluded "
            "upstream"
        )
        for event in ordered:
            roles = f" [{event.roles}]" if event.roles else ""
            lines.append(
                f"  - {event.owner_name}{roles}: {event.shares:,.0f} shares, "
                f"${event.amount:,.0f}, traded {event.transaction_date}, "
                f"filed {event.file_date}"
            )
        detail = ""
        if not clustered:
            if len(insiders) < self._min_insiders:
                detail = (
                    f"{len(insiders)} insider(s) in the {self._window_days}-day "
                    f"window; the cluster rule requires {self._min_insiders}"
                )
            else:
                detail = (
                    f"window aggregate ${aggregate:,.0f} is below the "
                    f"${self._min_cluster_usd:,.0f} cluster floor"
                )
        fields = {
            "form": "4",
            "accession": purchase.accession,
            "ticker": purchase.symbol,
            "issuer": purchase.issuer_name,
            "issuer_cik": purchase.issuer_cik,
            "transaction": "Purchase",
            "report_date": purchase.file_date,
            "transaction_date": earliest,
            "amount_range": f"${aggregate:,.0f}",
            "cluster": "true" if clustered else "false",
            "cluster_insiders": str(len(insiders)),
            "filer": "; ".join(
                sorted({event.owner_name for event in cluster})
            ),
            "roles": purchase.roles,
        }
        if detail:
            fields["cluster_detail"] = detail
        published = self._parse_date(purchase.file_date) or self._clock()
        return RawItem(
            external_id=purchase.accession,
            content="\n".join(lines),
            published_at=published,
            fields=fields,
        )

    # -- state ------------------------------------------------------------------------

    def _prune(self, today: date) -> None:
        window_floor = today - timedelta(days=self._window_days)

        def fresh_only(events: list[QualifyingPurchase]) -> list[QualifyingPurchase]:
            kept: list[QualifyingPurchase] = []
            for event in events:
                try:
                    fresh = date.fromisoformat(event.transaction_date) >= window_floor
                except ValueError:
                    fresh = False
                if fresh:
                    kept.append(event)
            return kept

        self._window = fresh_only(self._window)
        self._sell_window = fresh_only(self._sell_window)
        seen_floor = (
            today
            - timedelta(days=max(self._first_poll_lookback_days, self._lookback_days) + 2)
        ).isoformat()
        self._seen = {
            accession: filed
            for accession, filed in self._seen.items()
            if not filed or filed >= seen_floor
        }

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._seen = dict(raw.get("seen", {}))
            self._window = [
                QualifyingPurchase(**event) for event in raw.get("window", ())
            ]
            self._sell_window = [
                QualifyingPurchase(**event) for event in raw.get("sell_window", ())
            ]
            self._routine = dict(raw.get("routine", {}))
            self._backlog = [tuple(entry) for entry in raw.get("backlog", ())]
        except Exception as error:  # noqa: BLE001 - a corrupt file must not stop polling
            logger.warning(
                "form4 state at %s unreadable (%s); starting from the "
                "first-poll lookback",
                self._state_path,
                error,
            )

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            payload = {
                "version": 1,
                "seen": self._seen,
                "window": [asdict(event) for event in self._window],
                "sell_window": [asdict(event) for event in self._sell_window],
                "routine": self._routine,
                "backlog": [list(entry) for entry in self._backlog],
            }
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(payload), encoding="utf-8"
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "form4 state could not be written to %s: %s",
                self._state_path,
                error,
            )
