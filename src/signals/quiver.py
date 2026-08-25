"""Quiver Quantitative fetcher for Class 2 — congressional trading disclosures.

Paid feed (Hobbyist tier), Bearer-authenticated against ``api.quiverquant.com``. Same
citizenship discipline as the EDGAR fetcher: a conservative minimum interval between
requests (one per half-second — far under Quiver's documented per-minute allowance,
and this fetcher makes exactly one request per poll regardless of watchlist size),
plus one logged retry on a 429/5xx so a transient blip does not cost an hourly poll
its whole hour.

The two dates are the point
---------------------------
The STOCK Act allows up to 45 days between a trade and its disclosure. What the
research layer must evaluate is the gap: what has already been priced in since the
TRANSACTION date, not since the report date. Every signal's content therefore carries
both dates explicitly, labelled, plus the computed lag in days — the staleness is put
in front of the model rather than left for it to infer.

Dedup across restarts
---------------------
Disclosure rows have no stable id and reappear across pulls, so each gets a
deterministic identity: a hash of (representative, ticker, transaction, both dates,
amount). Within a process a seen-set suppresses re-emits; across restarts the caller
seeds that set from the audit log (``AuditLog.researched_external_ids``) — the same
replay-from-the-log philosophy as the budget and the kill switch, and it has the
right edge behaviour for free: a signal that was queued but never researched (budget
exhaustion, crash) left no record, so it re-emits and gets its research pass after
all.

Watchlist matching: a disclosure counts when every token of the watchlist name
appears in the API's Representative field, case-insensitively — so "Nancy Pelosi"
matches "Pelosi, Nancy" and "Nancy Pelosi" both. Adding a member to the watchlist
requires human approval (CLAUDE.md); this module reads the list, it never widens it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

import httpx

from signals.config import SourceConfig
from signals.scanners import RawItem

QUIVER_CONGRESS_URL = "https://api.quiverquant.com/beta/live/congresstrading"

logger = logging.getLogger("signals.quiver")


class QuiverError(RuntimeError):
    """A poll that could not be completed. The loop logs it and skips the cycle."""


class QuiverCongressFetcher:
    """Fetches new congressional disclosures for the members on the watchlist."""

    #: One retry, once, on a throttle or server blip — same posture as EDGAR.
    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _RETRY_PAUSE_SECONDS = 2.0

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        api_key: Optional[str] = None,
        min_request_interval: float = 0.5,
        timeout: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        seen: Optional[Sequence[str]] = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout)
        self._api_key = api_key
        self._interval = min_request_interval
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._last_request: Optional[float] = None
        #: Identities already emitted. Seed from the audit log to survive restarts.
        self._seen: set[str] = set(seen or ())

    # -- the Fetcher protocol ----------------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        names = [
            entry.get("name") or entry.get("fund", "")
            for entry in source.watchlist
        ]
        names = [name for name in names if name]

        rows = self._fetch_recent()
        items: list[RawItem] = []
        for row in rows:
            representative = str(row.get("Representative", ""))
            # Full roster (human ruling 2026-08-25): an EMPTY watchlist means
            # every filer. The deterministic pre-filters ($15K floor, lag rule,
            # held-sale rule) do the triage; per-member credibility ranks the
            # roster empirically from zero. A non-empty watchlist still narrows.
            if names and not any(
                _matches_name(representative, name) for name in names
            ):
                continue
            item = self._item_from_row(row)
            if item is not None:
                items.append(item)
        return items

    # -- one pull ------------------------------------------------------------------------

    def _fetch_recent(self) -> list[dict]:
        """One request for the recent disclosure feed; the watchlist filters locally.

        One request per poll however long the watchlist grows — per-member queries
        would multiply calls for data the live feed already returns in one.
        """
        response = self._get(QUIVER_CONGRESS_URL)
        if response.status_code == 401 or response.status_code == 403:
            raise QuiverError(
                f"Quiver refused the API key (HTTP {response.status_code}); "
                f"check QUIVER_API_KEY in .env"
            )
        if response.status_code != 200:
            raise QuiverError(
                f"Quiver congress feed returned HTTP {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise QuiverError(
                f"Quiver congress feed returned {type(payload).__name__}, not a list"
            )
        return payload

    def _item_from_row(self, row: dict) -> Optional[RawItem]:
        representative = str(row.get("Representative", "")).strip()
        ticker = str(row.get("Ticker", "")).strip().upper()
        transaction = str(row.get("Transaction", "")).strip()
        amount = str(
            row.get("Range") or row.get("Trade_Size_USD") or row.get("Amount") or ""
        ).strip()
        transaction_date = _date_of(row.get("TransactionDate"))
        report_date = _date_of(row.get("ReportDate"))
        chamber = str(row.get("House") or row.get("Chamber") or "").strip()

        if not (representative and ticker and transaction and transaction_date):
            logger.warning(
                "disclosure row is missing core fields; skipping: %r",
                {k: row.get(k) for k in ("Representative", "Ticker", "Transaction")},
            )
            return None

        identity = _identity(
            representative, ticker, transaction, transaction_date, report_date, amount
        )
        if identity in self._seen:
            return None
        self._seen.add(identity)

        lag_days = (
            (report_date - transaction_date).days if report_date else None
        )
        content = self._render(
            representative,
            chamber,
            ticker,
            transaction,
            amount,
            transaction_date,
            report_date,
            lag_days,
        )
        published_at = datetime.combine(
            report_date or transaction_date, datetime.min.time(), tzinfo=timezone.utc
        )
        return RawItem(
            external_id=identity,
            content=content,
            published_at=published_at,
            fields={
                # Per-member credibility (2026-08-25): outcomes, reports, and
                # research-context priors key on the member, not the firehose.
                "credibility_key": f"congressional_disclosures/{representative}",
                "representative": representative,
                "chamber": chamber,
                "ticker": ticker,
                "transaction": transaction,
                "amount_range": amount,
                "transaction_date": transaction_date.isoformat(),
                "report_date": report_date.isoformat() if report_date else "",
                "disclosure_lag_days": str(lag_days) if lag_days is not None else "",
            },
        )

    @staticmethod
    def _render(
        representative: str,
        chamber: str,
        ticker: str,
        transaction: str,
        amount: str,
        transaction_date,
        report_date,
        lag_days: Optional[int],
    ) -> str:
        """The disclosure as plain facts. Both dates, labelled, and the gap between
        them — the staleness the research layer's priced-in analysis has to reason
        about is stated, not left to be inferred."""
        who = f"{representative} ({chamber})" if chamber else representative
        lines = [
            "Congressional trading disclosure (STOCK Act filing)",
            f"representative: {who}",
            f"ticker: {ticker}",
            f"transaction: {transaction}",
            f"amount range: {amount or 'not stated'}",
            f"transaction date: {transaction_date.isoformat()} (when the trade was "
            f"executed)",
            f"report date: "
            f"{report_date.isoformat() if report_date else 'not stated'} "
            f"(when it became public)",
        ]
        if lag_days is not None:
            lines.append(
                f"disclosure lag: {lag_days} days between the trade and its "
                f"disclosure"
            )
        return "\n".join(lines)

    # -- plumbing --------------------------------------------------------------------------

    def _get(self, url: str) -> httpx.Response:
        response = self._request_once(url)
        if response.status_code in self._RETRY_STATUSES:
            logger.warning(
                "Quiver returned HTTP %d; retrying once after %.0fs",
                response.status_code,
                self._RETRY_PAUSE_SECONDS,
            )
            self._sleep(self._RETRY_PAUSE_SECONDS)
            response = self._request_once(url)
        return response

    def _request_once(self, url: str) -> httpx.Response:
        if self._last_request is not None:
            elapsed = self._monotonic() - self._last_request
            remaining = self._interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._monotonic()
        return self._client.get(
            url, headers={"Authorization": f"Bearer {self._resolve_key()}"}
        )

    def _resolve_key(self) -> str:
        key = (self._api_key or os.environ.get("QUIVER_API_KEY") or "").strip()
        if not key:
            raise QuiverError(
                "QUIVER_API_KEY is not set. Put it in .env (gitignored); the Class 2 "
                "feed cannot be polled without it."
            )
        return key

    def close(self) -> None:
        self._client.close()


def _matches_name(representative: str, watchlist_name: str) -> bool:
    """Every token of the watchlist name appears in the Representative field.

    Token containment rather than exact match, because the API is not consistent
    about name order ("Nancy Pelosi" vs "Pelosi, Nancy") — and a watchlist name is a
    person, not a string format.
    """
    haystack = {token.strip(",.") for token in representative.lower().split()}
    needles = {token.strip(",.") for token in watchlist_name.lower().split()}
    return bool(needles) and needles <= haystack


def _identity(
    representative: str,
    ticker: str,
    transaction: str,
    transaction_date,
    report_date,
    amount: str,
) -> str:
    """Deterministic identity for a disclosure that has no id of its own.

    Everything that distinguishes two real trades participates; the same row seen in
    two pulls (or two processes) hashes the same, which is what makes the audit-log
    seeding work.
    """
    digest = hashlib.sha256(
        "\x00".join(
            [
                representative.lower(),
                ticker,
                transaction.lower(),
                transaction_date.isoformat(),
                report_date.isoformat() if report_date else "",
                amount,
            ]
        ).encode("utf-8")
    ).hexdigest()
    return digest[:32]


def _date_of(raw: object):
    """A date from whatever the API sent, or None. Handles date and datetime forms."""
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        logger.warning("unparseable date from Quiver: %r", raw)
        return None
