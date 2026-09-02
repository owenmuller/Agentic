"""Operational layer: the run log, session bounds, and the health report.

Four small things unattended operation needs that the trading code deliberately does
not provide:

  - ``InstanceLock``: one running ``orchestrator run`` per data directory. Two
    processes against one audit file and one broker account would interleave the
    log, double-spend a budget each had replayed as unspent, and trade twice. The
    lock is held by the OS for the life of the process, so a crash releases it
    automatically — staleness is solved by the kernel, not by a heuristic a wedged
    PID file could defeat.

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
import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, Optional

try:  # Windows
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None
    import fcntl

from audit.log import AuditLog
from signals.scanners import MARKET_CLOSE, MARKET_OPEN, MARKET_TIMEZONE

from orchestrator.bootstrap import Preflight, _sleeve_label
from orchestrator.exits import TrackedPosition, unmanaged_exposure

logger = logging.getLogger("orchestrator.ops")


# ================================================================================
# Single-instance protection
# ================================================================================

#: The locked byte sits far past anything written to the file, so the pid/started
#: info at offset 0 stays readable by the refused process even under Windows'
#: mandatory byte-range locking.
_LOCK_BYTE_OFFSET = 1_000_000


class InstanceLock:
    """An exclusive, OS-held lock on the data directory.

    The guarantee comes from the operating system, not from the file's contents: the
    byte-range lock (Windows) or flock (POSIX) is released automatically when the
    holding process exits, **however** it exits. A lock file left behind by a crash
    is therefore just a note about a dead process — the next ``acquire`` succeeds
    without any staleness guesswork, and a PID-recycling race cannot brick a run.

    The file's text (pid, started-at) exists only for the human and the refused
    process's log line; nothing decides anything by reading it.
    """

    def __init__(self, path: Path, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._handle = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """Take the lock. False means another live process holds it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+", encoding="utf-8")
        try:
            if msvcrt is not None:
                handle.seek(_LOCK_BYTE_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - POSIX path, exercised on non-Windows machines
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        # Held. Record who, for the human and for the refused process's log line.
        handle.seek(0)
        handle.truncate()
        started = self._clock().isoformat(timespec="seconds")
        handle.write(
            f"pid={os.getpid()} started={started}\n"
            "held by a live orchestrator run; released automatically when it exits\n"
        )
        handle.flush()
        self._handle = handle
        return True

    def holder(self) -> str:
        """Whatever the lock file says about its holder. Informational only."""
        try:
            text = self._path.read_text(encoding="utf-8").splitlines()
            return text[0] if text else "unknown"
        except OSError:
            return "unknown"

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if msvcrt is not None:
                self._handle.seek(_LOCK_BYTE_OFFSET)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - POSIX path
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - the OS will release at exit anyway
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise RuntimeError(f"another instance holds {self._path} ({self.holder()})")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


# ================================================================================
# The run log
# ================================================================================


def first_poll_lookback_seconds(
    last_alive: Optional[datetime],
    now: datetime,
    floor_seconds: int = 900,
    ceiling_seconds: int = 86400,
) -> int:
    """Session-gap-sized X first-poll lookback (ruling 2026-08-26).

    The fixed 15-minute window silently lost every post made between sessions
    (~17.5h overnight). The gap since the system was last alive (the newest
    audit record) is the honest window, floored at 15 minutes so a mid-session
    bounce re-reads almost nothing, and capped at 24h because X bills per post
    returned — the cap is the ruled bound on what a long-idle restart may buy.
    A fresh data directory (no records) gets the ceiling: there is no earlier
    session to be continuous with.
    """
    if last_alive is None:
        return ceiling_seconds
    gap = (now - last_alive).total_seconds()
    return int(min(max(gap, floor_seconds), ceiling_seconds))


class RunLog:
    """Append-only operational event lines. Not the audit trail; never a substitute.

    Format: ``<UTC ISO timestamp> <EVENT> <detail>`` — grep-friendly, tail-friendly,
    and immune to a crash mid-write corrupting anything but its own last line.
    """

    def __init__(
        self,
        path: Path,
        clock: Optional[Callable[[], datetime]] = None,
        observer: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._path = path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        #: Sees every (event, detail) after the write (alerting, 2026-09-02).
        #: Failures are swallowed — an observer must never kill the run it
        #: observes, same rule as the log write itself.
        self._observer = observer

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
        if self._observer is not None:
            try:
                self._observer(event, detail)
            except Exception:  # noqa: BLE001
                logger.exception("run log observer failed on %s", event)

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
# Mirror health
# ================================================================================


def _trading_days_between(start, end) -> int:
    """Weekdays strictly after ``start`` up to and including ``end``."""
    if end <= start:
        return 0
    count = 0
    cursor = start
    while cursor < end:
        cursor = cursor + timedelta(days=1)
        if cursor.weekday() < 5:
            count += 1
    return count


def mirror_silence(
    audit: AuditLog,
    signals_config,
    now: datetime,
    default_threshold_trading_days: int = 2,
) -> list[str]:
    """Mirror sources that have delivered nothing for too many trading days.

    Silence is ambiguous by nature — the principal may be quiet, or the bot may be
    dead — so this produces a warning for a human to disambiguate, never an action.
    The baseline for a mirror that has never delivered is the first record in the
    log: a fresh system is not "silent", it is new.
    """
    last_delivery: dict[str, datetime] = {}
    first_record: Optional[datetime] = None
    for record in audit.records():
        if first_record is None or record.recorded_at < first_record:
            first_record = record.recorded_at
        signal = getattr(record, "signal", None)
        delivered_by = getattr(signal, "delivered_by", None) if signal else None
        if delivered_by:
            seen = last_delivery.get(delivered_by)
            if seen is None or record.recorded_at > seen:
                last_delivery[delivered_by] = record.recorded_at
    if first_record is None:
        return []  # nothing has ever run; there is no silence to measure

    messages: list[str] = []
    for klass in signals_config.classes.values():
        for source in klass.sources:
            if not source.mirror_of:
                continue
            threshold = (
                source.silence_warning_trading_days
                or default_threshold_trading_days
            )
            last = last_delivery.get(source.id)
            baseline = last or first_record
            gap = _trading_days_between(baseline.date(), now.date())
            if gap < threshold:
                continue
            if last is None:
                messages.append(
                    f"mirror {source.id} ({source.handle}) has NEVER delivered a "
                    f"post in {gap} trading days of records — the bot may be dead, "
                    f"misconfigured, or {source.mirror_of} may be quiet; a human "
                    f"should check which"
                )
            else:
                messages.append(
                    f"mirror {source.id} ({source.handle}) has been silent for "
                    f"{gap} trading days (last delivery "
                    f"{last.date().isoformat()}) — {source.mirror_of} may be "
                    f"quiet, or the bot may be dead; a human should check which"
                )
    return messages


# ================================================================================
# The health report
# ================================================================================


def _cost_line(checks: Preflight, moment: datetime) -> str:
    """Today / yesterday / month-to-date estimated research spend, from the log.

    Rejected passes are included — they were paid for. Estimates only; the
    weekly console reconciliation is the truth.
    """
    day_start = datetime.combine(moment.date(), time.min, tzinfo=timezone.utc)
    month_start = day_start.replace(day=1)
    today = checks.audit.research_cost_between(day_start)
    yesterday = checks.audit.research_cost_between(
        day_start - timedelta(days=1), day_start
    )
    month = checks.audit.research_cost_between(month_start)
    return (
        f"est. research cost: today ${today:.2f}  |  yesterday ${yesterday:.2f}"
        f"  |  month-to-date ${month:.2f}  (estimates; console bill is truth)"
    )


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


class CostMeter:
    """Daily research-spend tripwire, same pattern as the X reads counter.

    Accumulates estimated dollars per UTC day; the first time a day's total
    crosses the threshold, exactly one warning goes to ``warn_sink``. Seeded at
    startup with what the audit log says today already cost, so a restart cannot
    reset the tripwire. Estimates only — the console bill is the truth this
    warns ahead of.
    """

    def __init__(
        self,
        threshold_usd: Decimal,
        warn_sink: Optional[Callable[[str], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        initial_spent: Decimal = Decimal("0"),
    ) -> None:
        self._threshold = threshold_usd
        self._sink = warn_sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._day = self._clock().date()
        self._spent = initial_spent
        # Not pre-warned even when seeded above the threshold: after a mid-day
        # restart the first new pass re-warns once. Twice across a crash beats
        # never.
        self._warned = False

    @property
    def today_spent(self) -> Decimal:
        self._roll()
        return self._spent

    def _roll(self) -> None:
        today = self._clock().date()
        if today != self._day:
            self._day = today
            self._spent = Decimal("0")
            self._warned = False

    def add(self, cost: Optional[Decimal]) -> None:
        """Fold one pass's estimated cost in. None (unpriced, or no pass) is zero."""
        self._roll()
        if cost is None:
            return
        self._spent += cost
        if (
            not self._warned
            and self._threshold > Decimal("0")
            and self._spent > self._threshold
        ):
            self._warned = True
            if self._sink is not None:
                self._sink(
                    f"estimated research spend today ${self._spent:.2f} crossed "
                    f"the ${self._threshold:.2f}/day warning threshold "
                    f"(orchestrator.yaml daily_cost_warning_usd); estimates from "
                    f"research.yaml pricing — reconcile against the console bill"
                )


def _pending_settlement_items(checks):
    from orchestrator.recovery import pending_settlement

    return pending_settlement(checks.audit)


def _pending_lines(checks) -> list[str]:
    """Pending settlement is not unmanaged exposure (2026-08-27). An order
    approved (or submitted) whose fill is not yet recorded is a transient — a
    snapshot taken mid-flight, or a broker that could not be asked at startup.
    A line that KEEPS appearing is the actionable one: recovery could not
    resolve it, and a human should look at the venue."""
    pending = _pending_settlement_items(checks)
    if not pending:
        return []
    lines = [
        f"pending settlement: {len(pending)} order(s) approved with no fill "
        f"recorded — transient mid-flight; persistent means recovery could not "
        f"reach the venue",
    ]
    lines.extend(f"  - {item.describe()}" for item in pending[:10])
    return lines


def _mechanical_line(checks, state) -> str:
    """The mechanical sleeve's daily line (ruling 2026-08-27): open slots,
    exposure, and — the part that must reach a human — the circuit breaker."""
    session = checks.session
    positions = [
        p for key, p in state.positions.items() if key[0] == "mechanical"
    ]
    exposure = sum((p.exposure for p in positions), Decimal("0"))
    if session.mechanical_halted:
        breaker = (
            "BREAKER TRIPPED - new entries halted, positions ride; human "
            "review required (reset mechanical_halted in session_state.json)"
        )
    else:
        breaker = "breaker clear"
    value = session.mechanical_virtual_cash
    ledger = (
        f"ledger {value} + open {exposure}"
        if value is not None
        else "ledger unseeded (no entries yet)"
    )
    return (
        f"mechanical: {len(positions)} positions, {ledger}, deployed today "
        f"{state.mechanical_deployed_today}  |  {breaker}"
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
        f"sleeves: equity {_sleeve_label(checks.gate.limits.portfolio.sleeves.equity)}, "
        f"mechanical {_sleeve_label(checks.gate.limits.portfolio.sleeves.mechanical)}, "
        f"prediction {_sleeve_label(checks.gate.limits.portfolio.sleeves.prediction)}",
        _mechanical_line(checks, state),
        f"deployed today: {state.deployed_today}  |  research budget: "
        f"{checks.budget.spent} of {checks.budget.max_per_day} spent for "
        f"{checks.budget.day}",
        _cost_line(checks, moment),
        f"broker permits: {checks.permissions.describe()}"
        + (
            "  [EXCEEDS the code - schema is the enforcement]"
            if checks.permissions.excess_permissions()
            else "  [matched]"
        ),
        "",
        f"open positions: {len(tracked)}",
    ]
    lines.extend(_fmt_position(position, moment) for position in sorted(
        tracked, key=lambda item: item.decision_id
    ))

    # Pending settlement is computed first and EXCLUDED from unmanaged: a
    # position whose fill simply has not been recorded yet has an audit trail,
    # and calling it "no audit trail — needs a human" is a false alarm
    # (2026-08-27). What is left in unmanaged is the real thing: exposure
    # nothing in the log accounts for at all.
    pending = _pending_lines(checks)
    pending_symbols = {
        item.symbol for item in _pending_settlement_items(checks)
    }
    unmanaged = unmanaged_exposure(
        checks.gate, tracked, pending_symbols=pending_symbols
    )
    for symbol, quantity in sorted(unmanaged.items()):
        lines.append(
            f"  UNMANAGED  {symbol:<6} {quantity:>6} units held at the broker with "
            f"no audit trail — NO STOPS ARMED; needs a human"
        )
    lines.extend(pending)

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
