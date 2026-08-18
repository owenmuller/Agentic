"""Operational layer: the run log, session bounds, and the health report.

Three small things unattended operation needs that the trading code deliberately does
not provide:

  - ``RunLog``: a terse append-only line log (STARTED / STOPPED / ERROR / POLL) in
    ``data/run.log``, separate from the audit trail. The audit trail answers "what did
    the system decide"; this answers "did the scheduled run actually fire". One file,
    one line per event, readable with `tail`.
  - ``session_bounds``: today's regular session open/close in UTC, computed from
    America/New_York **at runtime** — never from a registration-time offset, so DST
    transitions and machines in other timezones cannot skew the trading window.
  - ``health_report``: the one-screen daily check. Strictly read-only — it is built
    from a ``Preflight`` (which only reads) plus file tails, spends no research
    budget, places nothing, and writes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from audit.log import AuditLog
from signals.scanners import MARKET_CLOSE, MARKET_OPEN, MARKET_TIMEZONE

from orchestrator.bootstrap import Preflight
from orchestrator.exits import TrackedPosition, unmanaged_exposure

logger = logging.getLogger("orchestrator.ops")


# ================================================================================
# The run log
# ================================================================================


class RunLog:
    """Append-only operational event lines. Not the audit trail; never a substitute.

    Format: ``<UTC ISO timestamp> <EVENT> <detail>`` — grep-friendly, tail-friendly,
    and immune to a crash mid-write corrupting anything but its own last line.
    """

    def __init__(
        self, path: Path, clock: Optional[Callable[[], datetime]] = None
    ) -> None:
        self._path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def path(self) -> Path:
        return self._path

    def note(self, event: str, detail: str = "") -> None:
        """Append one event line. An unwritable log must not kill the run it logs."""
        stamp = self._clock().isoformat(timespec="seconds")
        line = f"{stamp} {event} {detail}".rstrip()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as error:  # pragma: no cover - disk-full territory
            logger.error("could not write run log line %r: %s", line, error)

    def tail(self, count: int = 5) -> list[str]:
        if not self._path.exists():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        return lines[-count:]

    def last(self, event: str) -> Optional[str]:
        """The most recent line for one event type, or None."""
        if not self._path.exists():
            return None
        for line in reversed(self._path.read_text(encoding="utf-8").splitlines()):
            parts = line.split(" ", 2)
            if len(parts) >= 2 and parts[1] == event:
                return line
        return None


# ================================================================================
# Session bounds
# ================================================================================


def session_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Today's regular-session open and close as UTC instants.

    Computed in America/New_York at the moment of asking, which is what makes the
    scheduled task safe to trigger from any machine timezone: the trigger only has to
    be *early enough*, and the run gates itself on these bounds. Reuses the scanner
    module's market constants so there is exactly one definition of the session.
    """
    local = now.astimezone(MARKET_TIMEZONE)
    open_local = local.replace(
        hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0
    )
    close_local = local.replace(
        hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0, microsecond=0
    )
    return (
        open_local.astimezone(timezone.utc),
        close_local.astimezone(timezone.utc),
    )


def is_trading_weekday(now: datetime) -> bool:
    """Weekdays only. No holiday calendar — a holiday run polls quiet feeds and
    rests orders on a closed book, wasting requests and risking nothing (the same
    posture as ``signals.scanners.is_market_hours``)."""
    return now.astimezone(MARKET_TIMEZONE).weekday() < 5


# ================================================================================
# The health report
# ================================================================================


def _fmt_position(position: TrackedPosition, now: datetime) -> str:
    reviewed = (
        position.last_review_at.date().isoformat()
        if position.last_review_at
        else "never"
    )
    flags = ""
    if position.close_verdict:
        flags = "  CLOSE VERDICT PENDING"
    if position.pending_exit:
        flags += f"  exiting ({position.pending_exit})"
    return (
        f"  {position.decision_id}  {position.symbol:<6} {position.quantity:>6} "
        f"@ {position.entry_price}  stop {position.stop_price}  "
        f"leash {position.days_held(now)}/{position.leash_days}d "
        f"({position.time_horizon})  reviewed {reviewed}{flags}"
    )


def health_report(
    checks: Preflight,
    positions: Iterable[TrackedPosition],
    run_log: RunLog,
    now: Optional[datetime] = None,
) -> str:
    """One screen: what is held, what protects it, and whether the runs are firing.

    Read-only by construction — everything here is derived from the preflight (which
    only reads), the replayed positions (in-memory), and file tails.
    """
    moment = now or checks.clock()
    state = checks.gate.state
    tracked = list(positions)

    lines = [
        f"AGENTIC health — {moment.isoformat(timespec='seconds')}",
        "",
        f"mode: {'PAPER' if checks.paper else 'LIVE - REAL MONEY'}"
        f"  |  kill switch: "
        f"{'TRIPPED - opening orders halted' if checks.gate.kill_switch_tripped else 'clear'}",
        f"cash {state.cash}  |  NAV {state.nav}  |  "
        f"drawdown {state.drawdown():.2%} (high-water {state.high_water_mark})",
        f"deployed today: {state.deployed_today}  |  research budget: "
        f"{checks.budget.spent} of {checks.budget.max_per_day} spent for "
        f"{checks.budget.day}",
        "",
        f"open positions: {len(tracked)}",
    ]
    lines.extend(_fmt_position(position, moment) for position in sorted(
        tracked, key=lambda item: item.decision_id
    ))

    unmanaged = unmanaged_exposure(checks.gate, tracked)
    for symbol, quantity in sorted(unmanaged.items()):
        lines.append(
            f"  UNMANAGED  {symbol:<6} {quantity:>6} units held at the broker with "
            f"no audit trail — NO STOPS ARMED; needs a human"
        )

    last_poll = run_log.last("POLL")
    last_start = run_log.last("STARTED")
    last_error = run_log.last("ERROR")
    lines.extend(
        [
            "",
            f"last EDGAR poll:     {last_poll or 'none on record'}",
            f"last run started:    {last_start or 'none on record'}",
            f"last error:          {last_error or 'none on record'}",
            f"last audit record:   {_last_audit_line(checks.audit)}",
        ]
    )
    return "\n".join(lines)


def _last_audit_line(audit: AuditLog) -> str:
    last = None
    for record in audit.records():
        last = record
    if last is None:
        return "none — the log is empty"
    return f"{last.recorded_at.isoformat(timespec='seconds')} ({last.kind})"
