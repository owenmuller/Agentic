"""Secrets never reach a log line (human ruling 2026-10-14, audit item 13).

httpx logs every request at INFO with its full URL, and a Discord webhook URL
IS its secret: until this module, every card wrote the webhook token into the
system journal. Exception text from our own loggers can carry the same URL.

``install()`` (run on import of the ``execution`` package, which every entry
point loads) wraps the logging record factory, so EVERY record from EVERY
logger is redacted once its message is formatted:
  - Discord webhook tokens:  /api/webhooks/<id>/<token>  -> /api/webhooks/<id>/<redacted>
  - ntfy topics in a URL:    ntfy.sh/<topic>             -> ntfy.sh/<redacted>
  - token-like query values: ?token=... &apikey=... &api_key=... &key=... &secret=...
Request logs stay (method, host, path, status); only the secret part goes.
"""
from __future__ import annotations

import logging
import re

_PATTERNS = (
    (re.compile(r"(/api/webhooks/\d+/)[A-Za-z0-9_\-.]+"), r"\1<redacted>"),
    (re.compile(r"(ntfy\.sh/)[A-Za-z0-9_\-]+"), r"\1<redacted>"),
    (re.compile(r"([?&](?:token|apikey|api_key|key|secret|access_token|client_secret)=)[^&\s\"'<>]+", re.I),
     r"\1<redacted>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_INSTALLED = False


def install() -> None:
    """Wrap the record factory once; idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record is logging's problem, not ours
            return record
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return record

    logging.setLogRecordFactory(factory)
    _INSTALLED = True
