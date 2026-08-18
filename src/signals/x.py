"""X (Twitter) fetcher for Class 1 — recent search under pay-per-use billing.

The billing model shapes the design. X's pay-per-use meter charges per POST RETURNED
(about half a cent each), not per request, with a 24-hour dedup window — so requests
are effectively free and the whole cost discipline is "never make the API repeat
itself". That is what ``since_id`` is for: after the first poll of a session, every
request asks only for posts newer than the newest one already seen, and a quiet
minute returns zero posts and costs zero. The first poll of a session has no
``since_id`` and uses a short ``start_time`` lookback instead, so a morning restart
reads minutes of history, not seven days of it.

Because the bill is usage-shaped, a bug is billing-shaped too: a ``since_id``
regression would quietly re-read the same posts every 60 seconds all day. The
defensive layer is a daily read counter — cumulative posts read per UTC day, logged,
with a warning (and a ``warn_sink`` callback the run loop wires into ``run.log``)
once past a configured threshold. The bill's dashboard alert is the human's backstop;
this is the one that fires first.

Fields: ``note_tweet`` is requested explicitly because the plain ``text`` field
truncates posts over 280 characters — a clipped trade call is a corrupted signal.
``entities`` carries cashtags, and ``referenced_tweets`` distinguishes originals from
quotes and replies (retweets never arrive at all: the query carries ``-is:retweet``,
because a retweet is someone else's words).

The query is built from the source's configured ``handle``. Routing a second Class 1
account (the Trump leg, pending the Truth API decision) is one route line in the
orchestrator plus the handle already sitting in ``signals.yaml``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

import httpx

from signals.config import SourceConfig
from signals.scanners import RawItem

X_SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

#: created_at for the timestamp; note_tweet for full long-post text; entities for
#: cashtags; referenced_tweets to tell originals from quotes/replies.
TWEET_FIELDS = "created_at,note_tweet,entities,referenced_tweets,author_id"

logger = logging.getLogger("signals.x")


class XError(RuntimeError):
    """A poll that could not be completed. The loop logs it and skips the cycle."""


class XRecentSearchFetcher:
    """Polls one account's new posts via recent search, since_id-disciplined."""

    _RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
    _RETRY_PAUSE_SECONDS = 2.0

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        bearer_token: Optional[str] = None,
        max_results: int = 25,
        first_poll_lookback_seconds: int = 900,
        min_request_interval: float = 0.5,
        timeout: float = 15.0,
        clock: Optional[Callable[[], datetime]] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        seen: Optional[Sequence[str]] = None,
        read_warning_threshold: int = 200,
        warn_sink: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._client = client or httpx.Client(timeout=timeout)
        self._bearer = bearer_token
        self._max_results = max_results
        self._lookback = timedelta(seconds=first_poll_lookback_seconds)
        self._interval = min_request_interval
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._last_request: Optional[float] = None
        #: Newest post id seen per handle. The next poll asks only for newer.
        self._since: dict[str, str] = {}
        #: Post ids already emitted. Seeded from the audit log across restarts.
        self._seen: set[str] = set(seen or ())
        #: The billing tripwire.
        self._read_threshold = read_warning_threshold
        self._warn_sink = warn_sink
        self._reads_today = 0
        self._read_day: Optional[date] = None
        self._warned_day: Optional[date] = None

    @property
    def posts_read_today(self) -> int:
        """Posts the API returned (and billed) so far this UTC day."""
        self._roll_read_day()
        return self._reads_today

    # -- the Fetcher protocol ------------------------------------------------------

    def __call__(self, source: SourceConfig) -> Sequence[RawItem]:
        handle = (source.handle or "").lstrip("@").strip()
        if not handle:
            raise XError(
                f"source {source.id!r} was routed to the X fetcher but has no handle "
                f"in signals.yaml; a query cannot be built without one"
            )

        params: dict[str, object] = {
            "query": f"from:{handle} -is:retweet",
            "max_results": self._max_results,
            "tweet.fields": TWEET_FIELDS,
        }
        since = self._since.get(handle)
        if since is not None:
            params["since_id"] = since
        else:
            # First poll of the session: minutes of history, not seven days of it.
            start = self._clock() - self._lookback
            params["start_time"] = start.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )

        payload = self._fetch(params)
        posts = payload.get("data") or []
        self._count_reads(len(posts), source)

        newest = (payload.get("meta") or {}).get("newest_id")
        if newest:
            self._since[handle] = str(newest)

        items: list[RawItem] = []
        for post in posts:
            post_id = str(post.get("id", ""))
            if not post_id or post_id in self._seen:
                continue
            self._seen.add(post_id)
            items.append(self._item(post, handle))
        return items

    # -- one request ------------------------------------------------------------------

    def _fetch(self, params: dict) -> dict:
        response = self._get(X_SEARCH_URL, params)
        if response.status_code in (401, 403):
            raise XError(self._auth_failure_message(response))
        if response.status_code != 200:
            raise XError(f"X recent search returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise XError(
                f"X recent search returned {type(payload).__name__}, not an object"
            )
        return payload

    @staticmethod
    def _auth_failure_message(response: httpx.Response) -> str:
        """X's 401/403 bodies carry the actual reason — surface it, because "check
        your token" is the wrong advice for most of them. The one seen in practice:
        ``client-not-enrolled``, a valid token whose App is not attached to a
        Project (v2 access, pay-per-use included, attaches at the Project level)."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        reason = body.get("reason", "")
        detail = body.get("detail", "") or body.get("title", "")
        if reason == "client-not-enrolled":
            return (
                "X rejected the request (403 client-not-enrolled): the bearer token "
                "is valid but its developer App is not attached to a Project. Fix in "
                "the X developer portal — attach the App to a Project (pay-per-use "
                "enrolls at the Project level) or regenerate the token from an app "
                "inside one, then update X_BEARER_TOKEN in .env."
            )
        suffix = f": {reason} — {detail}" if (reason or detail) else ""
        return (
            f"X refused the request (HTTP {response.status_code}){suffix}. "
            f"Check X_BEARER_TOKEN in .env."
        )

    def _item(self, post: dict, handle: str) -> RawItem:
        # note_tweet carries the FULL text of a long post; plain text truncates it.
        note = post.get("note_tweet") or {}
        full_text = str(note.get("text") or post.get("text") or "")

        cashtags = ",".join(
            str(tag.get("tag", "")).upper()
            for tag in (post.get("entities") or {}).get("cashtags", [])
            if tag.get("tag")
        )
        references = post.get("referenced_tweets") or []
        if any(ref.get("type") == "quoted" for ref in references):
            post_type = "quoted"
        elif any(ref.get("type") == "replied_to" for ref in references):
            post_type = "reply"
        else:
            post_type = "original"

        return RawItem(
            external_id=str(post["id"]),
            content=full_text,
            published_at=self._parse_timestamp(post.get("created_at")),
            fields={
                "handle": f"@{handle}",
                "post_id": str(post["id"]),
                "post_type": post_type,
                "cashtags": cashtags,
                "created_at": str(post.get("created_at", "")),
                "was_long_post": "true" if note.get("text") else "false",
            },
        )

    def _parse_timestamp(self, raw: object) -> datetime:
        if isinstance(raw, str) and raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                logger.warning("unparseable created_at from X: %r", raw)
        return self._clock()

    # -- the billing tripwire -------------------------------------------------------------

    def _roll_read_day(self) -> None:
        today = self._clock().date()
        if today != self._read_day:
            self._read_day = today
            self._reads_today = 0

    def _count_reads(self, returned: int, source: SourceConfig) -> None:
        """Every post the API returns is a post billed. Count, log, and trip loudly.

        A healthy since_id run reads only what was actually posted; a broken one
        re-reads the same posts every poll. The threshold is set well above organic
        volume, so crossing it means a bug, not a busy day — and it must show up in
        the logs before it shows up on the bill.
        """
        self._roll_read_day()
        if returned <= 0:
            return
        self._reads_today += returned
        threshold = source.daily_read_warning or self._read_threshold
        logger.info(
            "X posts read today: %d (+%d this poll, warning threshold %d)",
            self._reads_today,
            returned,
            threshold,
        )
        if self._reads_today > threshold and self._warned_day != self._read_day:
            self._warned_day = self._read_day
            message = (
                f"X reads today={self._reads_today}, past the {threshold} warning "
                f"threshold — possible since_id regression; every post read is "
                f"billed"
            )
            logger.warning("%s", message)
            if self._warn_sink is not None:
                self._warn_sink(message)

    # -- plumbing --------------------------------------------------------------------------

    def _get(self, url: str, params: dict) -> httpx.Response:
        response = self._request_once(url, params)
        if response.status_code in self._RETRY_STATUSES:
            logger.warning(
                "X returned HTTP %d; retrying once after %.0fs",
                response.status_code,
                self._RETRY_PAUSE_SECONDS,
            )
            self._sleep(self._RETRY_PAUSE_SECONDS)
            response = self._request_once(url, params)
        return response

    def _request_once(self, url: str, params: dict) -> httpx.Response:
        if self._last_request is not None:
            elapsed = self._monotonic() - self._last_request
            remaining = self._interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = self._monotonic()
        return self._client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._resolve_token()}"},
        )

    def _resolve_token(self) -> str:
        token = (self._bearer or os.environ.get("X_BEARER_TOKEN") or "").strip()
        if not token:
            raise XError(
                "X_BEARER_TOKEN is not set. Put it in .env (gitignored); the Class 1 "
                "X feed cannot be polled without it."
            )
        return token

    def close(self) -> None:
        self._client.close()
