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

The instrument is the point too (2026-08-27)
--------------------------------------------
A "Purchase" of stock and a "Purchase" of calls are materially different claims, and
until now both rendered as ``transaction: Purchase``. Quiver carries the difference in
two fields this module used to discard: ``TickerType`` (``OP`` / ``Stock Option`` for
options) and ``Description`` — free text, present on only ~5% of rows, where strike,
expiry, side and contract count live when they live anywhere at all.

Detection is the OR of both, never the type alone: the feed types option rows as plain
stock (a BAC row of 2026-07-19 reads "CALL OPTION CONTRACTS." under ``TickerType: ST``).
Everything the description does not state renders as "not stated by the filing" — an
absent strike is a fact about the filing, and no field here is ever inferred.

Dedup across restarts
---------------------
Disclosure rows have no stable id and reappear across pulls, so each gets a
deterministic identity: a hash of (representative, ticker, transaction, both dates,
amount) — plus the description WHEN THERE IS ONE. That last clause is load-bearing in
both directions. Without the description, the two Pelosi BE rows of 2026-08-21 —
10,000 shares and 100 calls, same day, same amount band — collide and one is silently
dropped as a duplicate (36 such collisions across the current feed, 42 rows lost).
Appending it unconditionally would instead change the identity of every row that has
no description, re-emitting the whole feed; appending it only when present leaves the
95% untouched and re-emits exactly the described rows.

Within a process a seen-set suppresses re-emits; across restarts the caller
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
import re
import time
from datetime import datetime, timezone
from typing import Callable, NamedTuple, Optional, Sequence

import httpx

from signals.config import SourceConfig
from signals.scanners import RawItem

QUIVER_CONGRESS_URL = "https://api.quiverquant.com/beta/live/congresstrading"

logger = logging.getLogger("signals.quiver")

#: What the filing did not say. One phrase, used everywhere, so a missing strike
#: reads as a fact about the disclosure rather than as an oversight or a zero.
NOT_STATED = "not stated by the filing"

#: TickerType values that declare an option outright. Necessary, not sufficient —
#: see _instrument_of.
_OPTION_TICKER_TYPES = frozenset({"op", "stock option"})
#: TickerType values that declare ordinary equity.
_EQUITY_TICKER_TYPES = frozenset({"st", "stock"})

#: The word "option(s)" in the filing's own text. Deliberately narrower than a bare
#: call/put match: every option row observed in the feed spells "OPTION" out, and a
#: loose pattern would relabel prose like "shares put into trust" as an option.
_OPTION_TEXT = re.compile(r"\boptions?\b", re.IGNORECASE)
_SIDE = re.compile(r"\b(call|put)\b", re.IGNORECASE)
#: Three formats seen in the wild: "STRIKE PRICE OF $100", "STRIKE PRICE $325",
#: "Strike price: $75.00".
_STRIKE = re.compile(
    r"strike\s*price(?:\s+of)?\s*[:\s]*\$?\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE
)
#: "EXPIRATION DATE OF 6/17/27", "EXPIRES 06/18/2026", "Expires: 2026-08-21".
_EXPIRY = re.compile(
    r"expir\w*(?:\s+date)?(?:\s+of)?\s*[:\s]*"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_CONTRACTS = re.compile(r"([\d,]+)\s+(?:call|put)\s+options?", re.IGNORECASE)


class OptionTerms(NamedTuple):
    """What the filing disclosed about the instrument. Every field independently
    optional: the feed states side far more often than it states strike."""

    #: "option", "stock", or "" when neither the type nor the text says.
    instrument: str
    side: Optional[str] = None
    strike: Optional[str] = None
    expiry: Optional[str] = None
    contracts: Optional[str] = None

    @property
    def is_option(self) -> bool:
        return self.instrument == "option"


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
        ticker_type = str(row.get("TickerType") or "").strip()
        description = str(row.get("Description") or "").strip()
        terms = _terms_of(ticker_type, description)

        if not (representative and ticker and transaction and transaction_date):
            logger.warning(
                "disclosure row is missing core fields; skipping: %r",
                {k: row.get(k) for k in ("Representative", "Ticker", "Transaction")},
            )
            return None

        identity = _identity(
            representative,
            ticker,
            transaction,
            transaction_date,
            report_date,
            amount,
            description,
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
            terms,
            description,
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
                # Instrument type (2026-08-27). Normalised values only — the
                # filing's own prose stays inside the fenced content block; what
                # travels as structured metadata is a word, a number and a date.
                "instrument": terms.instrument,
                "ticker_type": ticker_type,
                "option_side": terms.side or "",
                "option_strike": terms.strike or "",
                "option_expiry": terms.expiry or "",
                "option_contracts": terms.contracts or "",
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
        terms: "OptionTerms",
        description: str,
    ) -> str:
        """The disclosure as plain facts. Both dates, labelled, and the gap between
        them — the staleness the research layer's priced-in analysis has to reason
        about is stated, not left to be inferred. Same for the instrument: a
        purchase of calls says something a purchase of stock does not, and what the
        filing withheld says so in those words."""
        who = f"{representative} ({chamber})" if chamber else representative
        lines = [
            "Congressional trading disclosure (STOCK Act filing)",
            f"representative: {who}",
            f"ticker: {ticker}",
            f"transaction: {transaction}",
            f"instrument: {terms.instrument or NOT_STATED}",
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
        if terms.is_option:
            # Every term stated separately, and every one the filing withheld
            # saying so. Options disclosures state a side far more often than
            # they state a strike; a blank line would read as an absent field
            # rather than an absent disclosure.
            lines.extend(
                [
                    f"option side: {terms.side or NOT_STATED}",
                    f"option strike: "
                    f"{'$' + terms.strike if terms.strike else NOT_STATED}",
                    f"option expiry: {terms.expiry or NOT_STATED}",
                    f"contracts: {terms.contracts or NOT_STATED}",
                    "note: for an options trade the disclosed amount range is the "
                    "PREMIUM paid, not the notional value of the underlying the "
                    "contracts control",
                ]
            )
        if description:
            lines.append(f"filing description (the filer's own words): {description}")
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


def _terms_of(ticker_type: str, description: str) -> OptionTerms:
    """What instrument the filing describes, and on what terms.

    The type field alone is not enough (a BAC row reads "CALL OPTION CONTRACTS."
    under TickerType "ST"), and the description alone is not enough (12 of 13
    option rows carry TickerType "OP" but the description is absent on 95% of the
    feed). So: either establishes an option, and only an explicit equity type with
    no option text in the description establishes stock. Anything else is "" — an
    instrument this feed did not state, which is not the same as equity.
    """
    normalised = ticker_type.strip().lower()
    if normalised in _OPTION_TICKER_TYPES or _OPTION_TEXT.search(description):
        return OptionTerms(
            instrument="option",
            side=_first_group(_SIDE, description, lower=True),
            strike=_strike_of(description),
            expiry=_expiry_of(description),
            contracts=_first_group(_CONTRACTS, description, strip_commas=True),
        )
    if normalised in _EQUITY_TICKER_TYPES:
        return OptionTerms(instrument="stock")
    return OptionTerms(instrument="")


def _first_group(
    pattern: "re.Pattern[str]",
    text: str,
    *,
    lower: bool = False,
    strip_commas: bool = False,
) -> Optional[str]:
    match = pattern.search(text)
    if match is None:
        return None
    value = match.group(1)
    if strip_commas:
        value = value.replace(",", "")
    return value.lower() if lower else value


def _strike_of(description: str) -> Optional[str]:
    raw = _first_group(_STRIKE, description, strip_commas=True)
    if raw is None:
        return None
    # "75.00" and "75" are the same strike; trailing zeros are formatting.
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def _expiry_of(description: str) -> Optional[str]:
    """An ISO date, or None. A two-digit year is the 21st century — the alternative
    reading puts every disclosed option a hundred years out."""
    raw = _first_group(_EXPIRY, description)
    if raw is None:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    logger.warning("unparseable option expiry in a filing description: %r", raw)
    return None


def _identity(
    representative: str,
    ticker: str,
    transaction: str,
    transaction_date,
    report_date,
    amount: str,
    description: str = "",
) -> str:
    """Deterministic identity for a disclosure that has no id of its own.

    Everything that distinguishes two real trades participates; the same row seen in
    two pulls (or two processes) hashes the same, which is what makes the audit-log
    seeding work.

    The description is appended ONLY when the filing has one (2026-08-27). It has to
    participate — without it Pelosi's 10,000 BE shares and her 100 BE calls, filed the
    same day in the same amount band, are one signal and the second is dropped as a
    duplicate. It cannot participate unconditionally — an empty component still changes
    the digest, which would re-emit the entire feed rather than the ~5% of rows that
    carry a description.
    """
    parts = [
        representative.lower(),
        ticker,
        transaction.lower(),
        transaction_date.isoformat(),
        report_date.isoformat() if report_date else "",
        amount,
    ]
    if description:
        parts.append(description)
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
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
