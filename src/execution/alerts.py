"""Email alerting over Gmail SMTP (human ruling 2026-09-02, ops hardening).

Two tiers, two subject prefixes a mail filter can key on:

  [AGENTIC URGENT]   the 10am tier: kill switch, mechanical breaker, unmanaged
                     positions, ERROR/COST/READS/settlement lines, any exit.
  [AGENTIC DAILY]    the awareness tier: first entry of the day per arm, the
                     close summary, the Friday report.

Design constraints, all from the ruling:

  never blocking     sends run on one daemon worker thread over a bounded
                     queue; the trading loop's only cost is a queue put.
  never raising      a send failure is LOGGED and dropped — alerting is a
                     window onto the system, not a load-bearing wall. A full
                     queue drops the message with a log line rather than
                     waiting on SMTP.
  rate-limited       the same alert key is not re-sent inside
                     ``resend_window_minutes`` (default 4h), so an error that
                     repeats every tick is one email, not three hundred.
  credentials        ALERT_SMTP_USER / ALERT_SMTP_PASSWORD / ALERT_TO from the
                     environment — a Gmail App Password, never the account
                     password. Missing credentials disable the alerter with
                     one log line; nothing else changes.

Lives in ``execution`` because it talks to the network and the orchestrator
package stays offline (topology rule); the operator layer (``__main__``) wires
it to run-log events and tick reports.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Optional

from execution.cards import COLORS, LOOK, Card
from execution.logredact import redact

logger = logging.getLogger("execution.alerts")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL
#: HTTPS transport (ruling 2026-09-28): the droplet's SMTP egress is blocked at
#: the provider, and every alert since 2026-09-04 had failed silently. With
#: SENDGRID_API_KEY set, mail goes over HTTPS first; SMTP stays as the second
#: transport. ALERT_FROM (else ALERT_SMTP_USER) must be a sender SendGrid has
#: verified. Credentials are the operator's to set, never this code's.
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"
#: Push transport (human ruling 2026-10-12): ntfy, public server by default.
NTFY_SERVER = "https://ntfy.sh"
NTFY_MAX_BYTES = 3900
#: Discord message content limit (characters).
DISCORD_LIMIT = 2000

URGENT = "URGENT"
DAILY = "DAILY"

_PREFIX = {URGENT: "[AGENTIC URGENT]", DAILY: "[AGENTIC DAILY]"}


@dataclass(frozen=True, slots=True)
class _Outbound:
    subject: str
    body: str
    #: URGENT or DAILY: picks the Discord channel (and the ntfy priority).
    tier: str = DAILY
    #: (filename, text) sent as a file where the transport supports it
    #: (Discord); the text-only transports send ``body``.
    attachment: Optional[tuple[str, str]] = None
    #: A plain-English card (ruling 2026-10-12): Discord renders it as an
    #: embed, the text transports as its text. None = a legacy text alert.
    card: Optional[Card] = None
    #: The caller's dedupe key (e.g. "weekly-2026-10-16"): the ledger's type.
    key: str = ""


class Alerter:
    """Queue-and-worker email sender. Safe to construct unconfigured."""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        resend_window_minutes: int = 240,
        sender: Optional[Callable[[str, str], None]] = None,
        max_queued: int = 50,
        http_post: Optional[Callable[..., Any]] = None,
        status_path: Optional[Path] = None,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._user = (os.environ.get("ALERT_SMTP_USER") or "").strip()
        self._password = (os.environ.get("ALERT_SMTP_PASSWORD") or "").strip()
        self._to = (os.environ.get("ALERT_TO") or "").strip()
        self._sendgrid_key = (os.environ.get("SENDGRID_API_KEY") or "").strip()
        self._from = (os.environ.get("ALERT_FROM") or self._user).strip()
        #: Push notifications (human ruling 2026-10-12): an ntfy topic the
        #: operator subscribes to on their phone. Tried FIRST - the droplet's
        #: SMTP egress is blocked and SendGrid is not configured. The topic
        #: name is the only secret on a public server: keep it unguessable.
        self._ntfy_topic = (os.environ.get("NTFY_TOPIC") or "").strip()
        self._ntfy_server = (os.environ.get("NTFY_SERVER") or NTFY_SERVER).strip().rstrip("/")
        #: Discord (human ruling 2026-10-12): the PRIMARY channel. URGENT to
        #: #urgent, DAILY (daily and weekly reports) to #reports. The webhook
        #: URLs are secrets: never logged, never in an error message.
        self._discord = {
            URGENT: (os.environ.get("DISCORD_WEBHOOK_URGENT") or "").strip(),
            DAILY: (os.environ.get("DISCORD_WEBHOOK_REPORTS") or "").strip(),
        }
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._window = timedelta(minutes=resend_window_minutes)
        #: Injectable transport for tests; None = the real transports.
        self._sender = sender
        #: Injectable HTTPS poster (``httpx.post`` shape) for tests.
        self._http_post = http_post
        #: Delivery status file the health check reads (ruling 2026-09-28:
        #: 28 silent failures in three weeks reached neither run.log nor health).
        self._status_path = status_path
        #: Alert ledger (human ruling 2026-10-14, audit item 4): one JSON line
        #: per card ATTEMPT - delivered, failed or refused - beside the status
        #: file; a process with no status file writes the scratch ledger. Not
        #: written when a test injects its transport.
        if ledger_path is None and sender is None and http_post is None:
            if status_path is not None:
                ledger_path = status_path.parent / "alerts_ledger.jsonl"
            else:
                import datasafety

                ledger_path = datasafety.scratch_data_dir() / "alerts_ledger.jsonl"
        self._ledger_path = ledger_path
        self._last_sent: dict[tuple[str, str], datetime] = {}
        self._queue: queue.Queue[Optional[_Outbound]] = queue.Queue(maxsize=max_queued)
        self._worker: Optional[threading.Thread] = None
        if not self.enabled:
            logger.info(
                "alerting disabled: no NTFY_TOPIC, no SENDGRID_API_KEY and no ALERT_SMTP_USER / "
                "ALERT_SMTP_PASSWORD is set with ALERT_TO; the run log remains the "
                "operator"
            )

    @property
    def smtp_configured(self) -> bool:
        return bool(self._user and self._password and self._to)

    @property
    def sendgrid_configured(self) -> bool:
        return bool(self._sendgrid_key and self._from and self._to)

    @property
    def ntfy_configured(self) -> bool:
        return bool(self._ntfy_topic)

    @property
    def discord_configured(self) -> bool:
        return any(self._discord.values())

    @property
    def transports(self) -> tuple[str, ...]:
        """In the order tried: Discord, ntfy push, SendGrid HTTPS, SMTP. A
        message whose tier has no Discord webhook skips straight to ntfy."""
        order = []
        if self.discord_configured:
            order.append("discord")
        if self.ntfy_configured:
            order.append("ntfy")
        if self.sendgrid_configured:
            order.append("sendgrid")
        if self.smtp_configured:
            order.append("smtp")
        return tuple(order)

    @property
    def enabled(self) -> bool:
        return bool(self.transports) or self._sender is not None

    # -- the two tiers ----------------------------------------------------------------

    def urgent(self, key: str, subject: str, body: str = "",
               attachment: Optional[tuple[str, str]] = None) -> bool:
        return self._alert(URGENT, key, subject, body, attachment)

    def daily(self, key: str, subject: str, body: str = "",
              attachment: Optional[tuple[str, str]] = None) -> bool:
        return self._alert(DAILY, key, subject, body, attachment)

    def card(self, key: str, card: Card) -> bool:
        """Queue one plain-English card (ruling 2026-10-12) on its channel's
        tier. Same rate limit, queue and never-raise contract as every alert."""
        attachment = card.attachment
        return self._alert(card.channel, key, card.title, card.text(), attachment, card=card)

    def _alert(self, tier: str, key: str, subject: str, body: str,
               attachment: Optional[tuple[str, str]] = None, card: Optional[Card] = None) -> bool:
        """Queue one alert. Returns whether it was queued (False = disabled,
        rate-limited, or the queue is full). Never blocks, never raises."""
        message = _Outbound(
            subject=f"{_PREFIX[tier]} {subject}",
            body=body or subject,
            tier=tier,
            attachment=attachment,
            card=card,
            key=key,
        )
        if not self.enabled:
            self._ledger(message, False, "none", "alerting not configured")
            return False
        now = self._clock()
        identity = (tier, key)
        last = self._last_sent.get(identity)
        if last is not None and now - last < self._window:
            self._ledger(message, False, "none", "rate-limited: the same card was sent less than the resend window ago")
            return False
        self._last_sent[identity] = now
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            logger.error("alert queue full; dropping %r", message.subject)
            self._ledger(message, False, "none", "queue full; dropped")
            return False
        self._ensure_worker()
        return True

    def send_test(self, tier: str = DAILY) -> bool:
        """One synchronous test message on ``tier``'s route, for delivery
        verification and filters. Bypasses the rate limiter; setup only."""
        if not self.enabled:
            logger.error("cannot send test message: alerting is not configured")
            return False
        try:
            self._send(
                _Outbound(
                    key="test-message",
                    tier=tier,
                    subject=f"{_PREFIX[tier]} test message",
                    body=(
                        "Alerting is wired. Filters to set up:\n"
                        f"  {_PREFIX[URGENT]}  -> notify immediately\n"
                        f"  {_PREFIX[DAILY]}   -> normal inbox\n"
                        "Sent by execution.alerts.Alerter.send_test()."
                    ),
                )
            )
            return True
        except Exception as error:  # noqa: BLE001
            logger.error("test alert failed: %s", error)
            return False

    # -- plumbing -----------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._drain, name="alert-sender", daemon=True
            )
            self._worker.start()

    def _drain(self) -> None:
        while True:
            message = self._queue.get()
            if message is None:
                return
            try:
                self._send(message)
            except Exception as error:  # noqa: BLE001 - logged, never raised
                logger.error("alert %r failed to send: %s", message.subject, error)

    def _send(self, message: _Outbound) -> None:
        """Try each configured transport in order; the first success wins. Every
        outcome is written to the status file so health can show it. All
        transports failing raises (the drain logs it)."""
        if self._sender is not None:
            self._sender(message.subject, message.body)
            self._record(True, "injected", "")
            return
        errors: list[str] = []
        for transport in self.transports:
            try:
                if transport == "discord":
                    self._send_discord(message)
                elif transport == "ntfy":
                    self._send_ntfy(message)
                elif transport == "sendgrid":
                    self._send_sendgrid(message)
                else:
                    self._send_smtp(message)
            except Exception as error:  # noqa: BLE001 - try the next transport
                errors.append(redact(f"{transport}: {type(error).__name__}: {str(error)[:120]}"))
                continue
            self._record(True, transport, "")
            self._ledger(message, True, transport, "")
            return
        detail = "; ".join(errors) or "no transport configured"
        self._record(False, "none", detail)
        self._ledger(message, False, "none", detail)
        raise RuntimeError(detail)

    def _send_discord(self, message: _Outbound) -> None:
        """Post one EMBED to the tier's channel webhook (ruling 2026-10-12):
        colour = status, title = the verdict, a few plain lines; anything
        longer rides as an attached .txt. A legacy text alert (no card) is
        shown the same way - its first line in the embed, the rest attached -
        so no message is ever a code block. Mentions are disabled."""
        url = self._discord.get(message.tier) or ""
        if not url:
            raise RuntimeError(f"no Discord webhook for the {message.tier} tier")
        post = self._http_post
        if post is None:
            import httpx

            post = httpx.post
        attachment = message.attachment
        if message.card is not None:
            card = message.card
            embed = {"title": card.title, "description": card.description, "color": card.color}
            attachment = card.attachment
        else:
            title = message.subject.split("] ", 1)[-1] if message.subject.startswith("[") else message.subject
            body = " ".join(message.body.replace("`", "'").split())
            short = len(body) <= 300
            embed = {
                "title": title[:256],
                "description": (body if short else body[:280].rsplit(" ", 1)[0] + "... (full text attached)"),
                "color": COLORS[LOOK],
            }
            if not short and attachment is None:
                attachment = ("alert.txt", message.subject + "\n\n" + message.body)
        payload = {"username": "Agentic", "embeds": [embed], "allowed_mentions": {"parse": []}}
        if attachment is None:
            response = post(url, json=payload, timeout=20)
        else:
            name, text = attachment
            response = post(
                url,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (name, text.encode("utf-8"), "text/plain")},
                timeout=30,
            )
        status = getattr(response, "status_code", None)
        if status is None or status >= 300:
            # never the URL: it is the channel's credential
            raise RuntimeError(f"Discord HTTP {status}: {str(getattr(response, 'text', ''))[:120]}")

    def _send_ntfy(self, message: _Outbound) -> None:
        """Publish as JSON to the server root (UTF-8 safe, unlike headers).
        URGENT is ntfy's top priority (a phone alert that breaks through);
        DAILY the default. Bodies are cut under ntfy.sh's 4 KB message limit."""
        post = self._http_post
        if post is None:
            import httpx

            post = httpx.post
        urgent = message.tier == URGENT
        body = message.body
        if len(body.encode("utf-8")) > NTFY_MAX_BYTES:
            body = body.encode("utf-8")[: NTFY_MAX_BYTES - 40].decode("utf-8", "ignore") + "\n[... truncated]"
        response = post(
            self._ntfy_server,
            json={
                "topic": self._ntfy_topic,
                "title": message.subject,
                "message": body,
                "priority": 5 if urgent else 3,
                "tags": ["rotating_light"] if urgent else ["chart_with_upwards_trend"],
            },
            timeout=20,
        )
        status = getattr(response, "status_code", None)
        if status is None or status >= 300:
            raise RuntimeError(f"ntfy HTTP {status}: {str(getattr(response, 'text', ''))[:120]}")

    def _send_sendgrid(self, message: _Outbound) -> None:
        post = self._http_post
        if post is None:
            import httpx

            post = httpx.post
        response = post(
            SENDGRID_URL,
            json={
                "personalizations": [{"to": [{"email": self._to}]}],
                "from": {"email": self._from},
                "subject": message.subject,
                "content": [{"type": "text/plain", "value": message.body}],
            },
            headers={"Authorization": f"Bearer {self._sendgrid_key}"},
            timeout=20,
        )
        status = getattr(response, "status_code", None)
        if status is None or status >= 300:
            raise RuntimeError(f"SendGrid HTTP {status}: {str(getattr(response, 'text', ''))[:120]}")

    def _send_smtp(self, message: _Outbound) -> None:
        email = EmailMessage()
        email["From"] = self._user
        email["To"] = self._to
        email["Subject"] = message.subject
        email.set_content(message.body)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.login(self._user, self._password)
            smtp.send_message(email)

    def _ledger(self, message: _Outbound, delivered: bool, transport: str, reason: str) -> None:
        """Append one attempt to the alert ledger (best-effort, never raises)."""
        if self._ledger_path is None:
            return
        channel = {URGENT: "#urgent", DAILY: "#reports"}.get(message.tier, message.tier)
        if delivered and transport != "discord":
            channel = f"{transport} (fallback for {channel})"
        entry = {
            "at": self._clock().isoformat(timespec="seconds"),
            "channel": channel,
            "tier": message.tier,
            "type": message.key or "legacy-text",
            "title": message.card.title if message.card is not None else message.subject,
            "delivered": delivered,
            "transport": transport,
            "reason": redact(reason)[:300],
        }
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ledger_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except Exception as error:  # noqa: BLE001
            logger.warning("could not write the alert ledger: %s", error)

    def _record(self, ok: bool, transport: str, detail: str) -> None:
        """Persist the last delivery outcome (best-effort, never raises)."""
        if self._status_path is None:
            return
        try:
            existing: dict[str, Any] = {}
            if self._status_path.exists():
                existing = json.loads(self._status_path.read_text(encoding="utf-8")) or {}
            now = self._clock().isoformat(timespec="seconds")
            if ok:
                existing["last_success"] = {"at": now, "transport": transport}
                existing["consecutive_failures"] = 0
            else:
                existing["last_failure"] = {"at": now, "detail": detail}
                existing["consecutive_failures"] = int(existing.get("consecutive_failures", 0)) + 1
            existing["transports"] = list(self.transports)
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            self._status_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception as error:  # noqa: BLE001
            logger.warning("could not write alert status: %s", error)

    @staticmethod
    def read_status(status_path: Path) -> Optional[dict[str, Any]]:
        """The last delivery outcomes, for health. None when never written."""
        try:
            if not status_path.exists():
                return None
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except (OSError, ValueError):
            return None

    def close(self, timeout: float = 10.0) -> None:
        """Let queued alerts drain at shutdown. Best-effort, bounded."""
        if self._worker is None or not self._worker.is_alive():
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._worker.join(timeout=timeout)


def data_sink(account: str, alerter: Optional["Alerter"] = None, *,
              status_path: Optional[Path] = None) -> Callable[[str, str, bool], None]:
    """The ``datahealth`` sink for any process but the main session (which
    routes through its run log): each report becomes a YELLOW card (global
    rule, human ruling 2026-10-15). The alerter is built on the first report,
    AFTER the environment is loaded (2026-10-14: an alerter built before the
    keys was silently disabled), and drained at exit. Rate-limited per
    source and message like every card."""
    import atexit
    from zoneinfo import ZoneInfo

    from execution.cards import data_card
    from execution.environment import load_environment

    holder: dict[str, Any] = {"alerter": alerter}

    def sink(source: str, message: str, urgent: bool) -> None:
        if holder["alerter"] is None:
            load_environment()
            holder["alerter"] = Alerter(status_path=status_path)
            atexit.register(holder["alerter"].close)
        when = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
        holder["alerter"].card(f"DATA:{source}:{message[:60]}",
                               data_card(account, when, source, message, urgent=urgent))

    return sink
