"""The panic button (human ruling 2026-09-02): ``orchestrator halt`` and
``orchestrator resume`` — the phone-SSH emergency path (see ops/EMERGENCY.md).

Why a marker file
-----------------
The trading session is its own process with its own in-memory gate, and it
persists ``session_state.json`` from that gate every tick — so a second process
flipping ``kill_switch_tripped`` in the file would simply be overwritten on the
next tick. The durable, race-free channel is a marker file the LIVE loop reads
at the top of every tick: present means "trip the kill switch now, cancel every
working order, say so". ``halt`` writes the marker, cancels every open order at
the broker directly (immediate — it does not wait for the loop), and only edits
the session file itself when no session is running (the lock says). The kill
switch is sticky in the session file thereafter; the marker stays until a human
resumes, so a session started tomorrow trips again on its first tick.

Why resume demands a phrase
---------------------------
CLAUDE.md: "Resume of opening orders requires manual human reset." The gate's
``reset_kill_switch`` is documented FOR A HUMAN OPERATOR ONLY, and this module
is that operator's tool: ``resume`` refuses to run while a session is live
(stop the service first — a reset the running gate cannot see is not a reset),
refuses an acknowledgement without the operator's name and the exact phrase
``I CONFIRM MANUAL RESET``, and records the acknowledgement in the audit trail so
every reset has a name on it. Nothing in the automated path imports this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HALT_MARKER_NAME = "HALT"
RESUME_PHRASE = "I CONFIRM MANUAL RESET"


def halt_marker_path(data_dir: Path) -> Path:
    return data_dir / HALT_MARKER_NAME


def read_halt(path: Path) -> Optional[str]:
    """The marker's text when a halt is on record, else None."""
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        return "(unreadable halt marker)"


def write_halt(path: Path, reason: str, operator: str, now: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"halted_at={now.isoformat(timespec='seconds')}\n"
        f"operator={operator}\nreason={reason}\n"
        "Opening orders are halted until a human runs `orchestrator resume` "
        "with the manual-reset acknowledgement.\n",
        encoding="utf-8",
    )


def clear_halt(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False


def acknowledgement_is_valid(acknowledgement: str) -> bool:
    """A name AND the exact phrase: ``"<name>: I CONFIRM MANUAL RESET"``.

    Case-sensitive on the phrase, like the live-trading confirmation. The name
    is whatever is left once the phrase is removed — at least three
    non-whitespace characters, so the phrase alone does not pass.
    """
    if RESUME_PHRASE not in acknowledgement:
        return False
    remainder = acknowledgement.replace(RESUME_PHRASE, "").replace(":", " ")
    return len("".join(remainder.split())) >= 3


@dataclass
class HaltReport:
    marker_path: Path
    marker_written: bool = False
    live_session: bool = False
    session_tripped_here: bool = False
    orders_cancelled: list[str] = field(default_factory=list)
    cancel_errors: list[str] = field(default_factory=list)
    alert_sent: Optional[bool] = None
    audit_recorded: bool = False
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ["OPERATOR HALT"]
        lines.append(
            f"  marker:   {'written' if self.marker_written else 'NOT WRITTEN'} "
            f"({self.marker_path})"
        )
        if self.live_session:
            lines.append(
                "  session:  a trading session is LIVE — it trips its own kill "
                "switch and cancels its working orders on its next tick (within "
                "one tick interval) and persists the halt itself"
            )
        else:
            lines.append(
                "  session:  no live session; kill switch "
                + ("tripped in session_state.json" if self.session_tripped_here
                   else "NOT persisted (see errors)")
            )
        lines.append(
            f"  broker:   {len(self.orders_cancelled)} open order(s) cancelled"
            + (f"; {len(self.cancel_errors)} cancel error(s)" if self.cancel_errors else "")
        )
        lines.append(
            "  alert:    "
            + {True: "urgent email queued", False: "NOT sent (rate-limited or refused)",
               None: "alerting not configured"}[self.alert_sent]
        )
        lines.append(f"  audit:    {'operator_action recorded' if self.audit_recorded else 'NOT recorded'}")
        for error in self.errors + self.cancel_errors:
            lines.append(f"  error:    {error}")
        return "\n".join(lines)


def perform_halt(
    *,
    marker_path: Path,
    session_path: Path,
    live_session: bool,
    reason: str,
    operator: str,
    adapter=None,
    alert: Optional[Callable[[str, str, str], bool]] = None,
    audit=None,
    now: Optional[datetime] = None,
) -> HaltReport:
    """Every step is attempted; no step's failure stops the next. The marker
    goes first because it is the one thing the live loop reads."""
    moment = now or datetime.now(timezone.utc)
    report = HaltReport(marker_path=marker_path, live_session=live_session)

    try:
        write_halt(marker_path, reason, operator, moment)
        report.marker_written = True
    except OSError as error:
        report.errors.append(f"marker: {error}")

    # Cancel at the broker directly — immediate, independent of the loop. The
    # live loop (if any) reconciles the cancels as terminal, releasing its
    # reservations the same way a shutdown does.
    if adapter is not None:
        try:
            open_ids = list(adapter.open_orders())
        except Exception as error:  # noqa: BLE001
            open_ids = []
            report.cancel_errors.append(f"could not list open orders: {error}")
        for order_id in open_ids:
            try:
                adapter.cancel_order(order_id)
                report.orders_cancelled.append(order_id)
            except Exception as error:  # noqa: BLE001
                report.cancel_errors.append(f"{order_id}: {error}")

    # Only the process that owns the session file may write it. With no live
    # session, that is us; with one, the marker makes IT do the writing.
    if not live_session:
        try:
            from orchestrator.state import SessionState

            session = SessionState.load(session_path)
            session.kill_switch_tripped = True
            session.kill_switch_tripped_at = moment
            session.save()
            report.session_tripped_here = True
        except Exception as error:  # noqa: BLE001
            report.errors.append(f"session state: {error}")

    if audit is not None:
        try:
            audit.record_operator_action(
                action="halt",
                operator=operator,
                detail=reason,
            )
            report.audit_recorded = True
        except Exception as error:  # noqa: BLE001
            report.errors.append(f"audit: {error}")

    if alert is not None:
        try:
            report.alert_sent = bool(
                alert(
                    "operator_halt",
                    "OPERATOR HALT — kill switch tripped by hand",
                    f"{operator} halted trading at {moment.isoformat(timespec='seconds')}: "
                    f"{reason}\n{len(report.orders_cancelled)} broker order(s) "
                    f"cancelled. Resume requires `orchestrator resume` with the "
                    f"manual-reset acknowledgement.",
                )
            )
        except Exception as error:  # noqa: BLE001
            report.errors.append(f"alert: {error}")
    return report


@dataclass
class ResumeReport:
    was_tripped: bool
    marker_cleared: bool
    high_water_mark: object
    nav: object
    acknowledgement: str

    def render(self) -> str:
        return "\n".join(
            [
                "MANUAL RESET",
                f"  kill switch: {'was tripped, now clear' if self.was_tripped else 'was already clear'}",
                f"  halt marker: {'removed' if self.marker_cleared else 'none present'}",
                f"  high-water mark re-based to NAV {self.nav} (drawdown clock restarts here)",
                f"  acknowledged: {self.acknowledgement}",
            ]
        )


def perform_resume(
    *,
    gate,
    session,
    audit,
    marker_path: Path,
    acknowledgement: str,
    operator: str,
    live_session: bool,
    now: Optional[datetime] = None,
) -> ResumeReport:
    """The human's reset. Raises rather than half-resuming."""
    if live_session:
        raise RuntimeError(
            "a trading session is running; a reset its gate cannot see is not a "
            "reset. Stop it first (sudo systemctl stop agentic-paper.service), "
            "then resume."
        )
    if not acknowledgement_is_valid(acknowledgement):
        raise ValueError(
            f'the acknowledgement must carry your name and the exact phrase '
            f'"{RESUME_PHRASE}", e.g. "Owen: {RESUME_PHRASE}"'
        )
    moment = now or datetime.now(timezone.utc)
    was_tripped = gate.kill_switch_tripped
    if was_tripped:
        # The gate's own documented operator path: re-bases the high-water
        # mark to NAV so the reset is not inert. Called only here, by a human.
        gate.reset_kill_switch(acknowledgement)
    session.capture(gate, moment)
    session.save()
    cleared = clear_halt(marker_path)
    audit.record_operator_action(
        action="resume",
        operator=operator,
        acknowledgement=acknowledgement,
        detail=(
            f"kill switch {'reset' if was_tripped else 'already clear'}; "
            f"halt marker {'removed' if cleared else 'absent'}; high-water mark "
            f"re-based to {gate.state.nav}"
        ),
    )
    return ResumeReport(
        was_tripped=was_tripped,
        marker_cleared=cleared,
        high_water_mark=gate.state.high_water_mark,
        nav=gate.state.nav,
        acknowledgement=acknowledgement,
    )
