"""Durable state across restarts, and the startup replay that rebuilds it.

What comes from where
---------------------
Restarting must not hand the account a clean slate it has not earned. Three different
sources hold three different parts of the truth, and each is used for what it alone
knows:

  the broker      cash and positions. Authoritative — it is the account. Replaying
                  fills from the audit log to derive holdings would be reconstructing
                  a number the broker will simply tell you, and any drift between the
                  two would silently favour the reconstruction.
  the audit log   how much was deployed today, and how much research has been bought
                  today. Neither is visible to the broker, and both are per-day caps
                  that a restart must not reset.
  session state   the high-water mark and the kill switch. Neither is derivable from
                  anywhere else, and getting the kill switch wrong is the expensive
                  one — see below.

Why the kill switch cannot be inferred
--------------------------------------
The obvious approach is to recompute drawdown at startup and let the gate re-trip
itself. That works right up until it matters. The halt is sticky by design: once
tripped it stays tripped through a recovery, because the point is to make a human
look. So an account that fell 13%, halted, and then rallied back to a 4% drawdown
would restart *un*-halted — the recomputation says 4% and the 12% threshold is not
met. The system would resume opening positions on its own, which is the one thing the
kill switch exists to prevent.

So the flag is persisted, and it is persisted faithfully in both directions. The only
code that can clear it is ``RiskGate.reset_kill_switch``, which is documented for a
human operator and is called from nowhere in this package. What gets written here is
whatever the gate currently reports; the file is a mirror, not a second opinion.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Optional

from audit.records import DecisionRecord
from execution.base import BrokerPosition
from risk_gate.gate import RiskGate
from risk_gate.schema import OPTION_CONTRACT_MULTIPLIER, parse_order
from risk_gate.state import AccountState, AccountType, Position, Sleeve, sleeve_of

ZERO = Decimal("0")

#: Explanation written into the state file itself. A human who has halted trading and
#: wants to resume will find this file before they find this module.
_FILE_NOTE = (
    "Written by the trading loop. kill_switch_tripped survives restarts on purpose: a "
    "halt that a recovery in NAV could clear would not be a halt. Resuming opening "
    "orders is a manual human decision (CLAUDE.md: reset is manual_human_only) — set "
    "kill_switch_tripped to false here, or delete this file, only if you are the human "
    "making it."
)


@dataclass(slots=True)
class SessionState:
    """The two figures no other system holds."""

    path: Path
    high_water_mark: Optional[Decimal] = None
    kill_switch_tripped: bool = False
    kill_switch_tripped_at: Optional[datetime] = None

    @classmethod
    def load(cls, path: Path) -> "SessionState":
        """Read persisted state, or return an empty one on the first run.

        A corrupt or unreadable file is not treated as "no halt". It raises, because
        the failure modes are not symmetric: starting halted when you were not costs a
        morning, and starting unhalted when you were costs whatever the halt was there
        to stop.
        """
        if not path.exists():
            return cls(path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        mark = raw.get("high_water_mark")
        tripped_at = raw.get("kill_switch_tripped_at")
        return cls(
            path=path,
            high_water_mark=Decimal(str(mark)) if mark is not None else None,
            kill_switch_tripped=bool(raw.get("kill_switch_tripped", False)),
            kill_switch_tripped_at=(
                datetime.fromisoformat(tripped_at) if tripped_at else None
            ),
        )

    def capture(self, gate: RiskGate, now: Optional[datetime] = None) -> None:
        """Take the gate's current view. Does not write."""
        newly_tripped = gate.kill_switch_tripped and not self.kill_switch_tripped
        self.high_water_mark = gate.state.high_water_mark
        self.kill_switch_tripped = gate.kill_switch_tripped
        if newly_tripped:
            self.kill_switch_tripped_at = now or datetime.now(timezone.utc)
        elif not gate.kill_switch_tripped:
            self.kill_switch_tripped_at = None

    def save(self) -> None:
        """Write via a temporary file and replace, so a crash mid-write cannot truncate."""
        payload = {
            "_note": _FILE_NOTE,
            "high_water_mark": (
                str(self.high_water_mark) if self.high_water_mark is not None else None
            ),
            "kill_switch_tripped": self.kill_switch_tripped,
            "kill_switch_tripped_at": (
                self.kill_switch_tripped_at.isoformat()
                if self.kill_switch_tripped_at
                else None
            ),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def persist(self, gate: RiskGate, now: Optional[datetime] = None) -> None:
        self.capture(gate, now)
        self.save()


def position_from_broker(
    holding: BrokerPosition, today: Optional[date] = None
) -> Position:
    """Turn a broker holding into gate state.

    ``last_open_date`` is set to today rather than left unknown. It drives day-trade
    detection, and the gate only reads it when a close is submitted: an unknown date
    means a close on the day of a restart is not counted as a day trade, and a
    restart is exactly when the system has least idea what it did earlier. Counting
    marginally too many day trades is the same direction ``business_days_before``
    already errs in, and it costs at most a deferred close in a sub-$25K margin
    account.
    """
    option = holding.is_option
    quantity = int(holding.quantity)
    return Position(
        key=("option", holding.symbol) if option else ("equity", holding.symbol),
        sleeve=Sleeve.EQUITY,
        quantity=quantity,
        cost_basis=holding.cost_basis,
        market_value=holding.market_value,
        unit_multiplier=OPTION_CONTRACT_MULTIPLIER if option else 1,
        is_option=option,
        last_open_date=today,
    )


def replay_deployed_today(
    decisions: Iterable[DecisionRecord], today: date
) -> Decimal:
    """Equity-sleeve capital committed today, from approved decisions in the log.

    Counted from approvals rather than from fills, matching the gate: the daily cap
    limits what the system committed, and an approval that has not printed yet is
    still cash it promised.
    """
    total = ZERO
    for record in decisions:
        gate_snapshot = record.gate
        if not gate_snapshot.approved or gate_snapshot.approved_at is None:
            continue
        if gate_snapshot.approved_at.date() != today:
            continue
        order = parse_order(gate_snapshot.order)
        if order.is_opening and sleeve_of(order) is Sleeve.EQUITY:
            total += gate_snapshot.max_loss or ZERO
    return total


def seed_account_state(
    *,
    cash: Decimal,
    positions: Iterable[BrokerPosition],
    session: SessionState,
    deployed_today: Decimal,
    today: date,
    account_type: AccountType = AccountType.CASH,
) -> AccountState:
    """Assemble the state a restarted gate should wake up holding.

    ``reserved_cash`` starts at zero deliberately. Reservations belong to
    ``ApprovedOrder`` instances, which cannot outlive the process that created them —
    that unforgeability is the whole point of the type. The loop's shutdown cancels
    working orders precisely so there is nothing left for a reservation to have been
    protecting, and anything that slipped through shows up in the broker's cash and
    positions, which is where this function is reading from anyway.
    """
    held = {}
    for holding in positions:
        if holding.quantity == 0:
            continue
        position = position_from_broker(holding, today)
        held[position.key] = position

    state = AccountState(
        cash=cash,
        high_water_mark=ZERO,
        account_type=account_type,
        positions=held,
        deployed_today=deployed_today,
        deployment_date=today,
    )
    state.high_water_mark = (
        session.high_water_mark if session.high_water_mark is not None else state.nav
    )
    # Set before the gate is constructed: RiskGate.__init__ evaluates the kill switch,
    # and a switch that is already tripped must not be given the chance to un-trip.
    state.kill_switch_tripped = session.kill_switch_tripped
    return state
