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

import logging
import os
import queue
import smtplib
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Optional

logger = logging.getLogger("execution.alerts")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL

URGENT = "URGENT"
DAILY = "DAILY"

_PREFIX = {URGENT: "[AGENTIC URGENT]", DAILY: "[AGENTIC DAILY]"}


@dataclass(frozen=True, slots=True)
class _Outbound:
    subject: str
    body: str


class Alerter:
    """Queue-and-worker email sender. Safe to construct unconfigured."""

    def __init__(
        self,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        resend_window_minutes: int = 240,
        sender: Optional[Callable[[str, str], None]] = None,
        max_queued: int = 50,
    ) -> None:
        self._user = (os.environ.get("ALERT_SMTP_USER") or "").strip()
        self._password = (os.environ.get("ALERT_SMTP_PASSWORD") or "").strip()
        self._to = (os.environ.get("ALERT_TO") or "").strip()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._window = timedelta(minutes=resend_window_minutes)
        #: Injectable transport for tests; None = real SMTP.
        self._sender = sender
        self._last_sent: dict[tuple[str, str], datetime] = {}
        self._queue: queue.Queue[Optional[_Outbound]] = queue.Queue(maxsize=max_queued)
        self._worker: Optional[threading.Thread] = None
        if not self.enabled:
            logger.info(
                "alerting disabled: ALERT_SMTP_USER / ALERT_SMTP_PASSWORD / "
                "ALERT_TO not all set; the run log remains the operator"
            )

    @property
    def enabled(self) -> bool:
        return bool(
            (self._user and self._password and self._to) or self._sender is not None
        )

    # -- the two tiers ----------------------------------------------------------------

    def urgent(self, key: str, subject: str, body: str = "") -> bool:
        return self._alert(URGENT, key, subject, body)

    def daily(self, key: str, subject: str, body: str = "") -> bool:
        return self._alert(DAILY, key, subject, body)

    def _alert(self, tier: str, key: str, subject: str, body: str) -> bool:
        """Queue one alert. Returns whether it was queued (False = disabled,
        rate-limited, or the queue is full). Never blocks, never raises."""
        if not self.enabled:
            return False
        now = self._clock()
        identity = (tier, key)
        last = self._last_sent.get(identity)
        if last is not None and now - last < self._window:
            return False
        self._last_sent[identity] = now
        message = _Outbound(
            subject=f"{_PREFIX[tier]} {subject}",
            body=body or subject,
        )
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            logger.error("alert queue full; dropping %r", message.subject)
            return False
        self._ensure_worker()
        return True

    def send_test(self) -> bool:
        """One synchronous test message, for delivery verification and mail
        filters. Bypasses the rate limiter; used at build/setup time only."""
        if not self.enabled:
            logger.error("cannot send test message: alerting is not configured")
            return False
        try:
            self._send(
                _Outbound(
                    subject=f"{_PREFIX[DAILY]} test message",
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
        if self._sender is not None:
            self._sender(message.subject, message.body)
            return
        email = EmailMessage()
        email["From"] = self._user
        email["To"] = self._to
        email["Subject"] = message.subject
        email.set_content(message.body)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.login(self._user, self._password)
            smtp.send_message(email)

    def close(self, timeout: float = 10.0) -> None:
        """Let queued alerts drain at shutdown. Best-effort, bounded."""
        if self._worker is None or not self._worker.is_alive():
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._worker.join(timeout=timeout)
