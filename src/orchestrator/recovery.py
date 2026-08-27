"""Startup settlement recovery (human ruling 2026-08-27).

The hole this closes
--------------------
Between the broker filling an order and this system's next settle tick there is
a window — seconds, usually — in which the fill is real at the venue and absent
from the audit log. A process that dies inside that window leaves a position
nobody owns:

  - the log has an approved decision with no fill, so every replay that keys on
    fills skips it: the exit engine arms no stop, the mechanical engine tracks
    no time exit, the sleeve ledger under-counts,
  - the broker reports the shares, so the account holds them,
  - and nothing ever reconciles the two. The window is small; its consequence
    is permanent.

So the next startup asks. Entry orders are stamped with their decision id
(``client_reference``), which is in the log, so the broker can be asked what
happened to them by name. Exit orders already record their broker order id at
submission, so those are asked about directly. Three outcomes, and each is
written down rather than assumed:

  terminal, filled      write the missing ``FillRecord`` (and the partial-fill
                        note, when it filled short). The GATE needs no telling:
                        it is seeded from broker cash and positions, which
                        already include the fill — recording it twice would
                        double-count.
  terminal, unfilled    write the execution rejection the dead process would
                        have written. Nothing filled, so nothing is held.
  still working         left alone: the existing orphan sweep cancels working
                        orders at startup, and cancelling is its job, not this
                        module's.
  unknown / no answer   left alone, loudly. "Cannot tell" must never be
                        recorded as "did not fill"; the next startup asks
                        again, and until then it shows as pending settlement.

Runs before the account state is seeded, so the sleeve attribution and every
audit-derived replay see a complete log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from audit.log import AuditLog
from audit.records import AuditTrail, RejectedStage
from execution.base import BrokerAdapter, BrokerError
from risk_gate.schema import parse_order
from risk_gate.state import unit_multiplier, units_of

ZERO = Decimal("0")

logger = logging.getLogger("orchestrator.recovery")


@dataclass(frozen=True, slots=True)
class PendingSettlement:
    """An order that was approved (and, for exits, submitted) whose outcome the
    log does not yet know. Transient in the ordinary case; a line that persists
    across startups is the actionable one."""

    decision_id: str
    symbol: str
    side: str
    quantity: Decimal
    sleeve: str

    def describe(self) -> str:
        return (
            f"{self.decision_id} {self.side} {self.quantity} {self.symbol} "
            f"({self.sleeve} sleeve)"
        )


def _entry_needs_settlement(trail: AuditTrail) -> bool:
    decision = trail.decision
    if not decision.was_approved:
        return False
    order = decision.gate.order or {}
    if not order:
        return False
    if any(fill.side == "buy" for fill in trail.fills):
        return False
    # An execution-stage rejection means the dead process (or an earlier
    # startup) already accounted for the order's end.
    return not any(
        rejection.stage is RejectedStage.EXECUTION
        for rejection in trail.stage_rejections
    )


def _unsettled_exits(trail: AuditTrail) -> list:
    """Exits submitted to the broker with no sell fill recorded against them."""
    settled = {fill.broker_order_id for fill in trail.fills if fill.side == "sell"}
    return [
        exit_record
        for exit_record in trail.exits
        if exit_record.submitted
        and exit_record.broker_order_id
        and exit_record.broker_order_id not in settled
    ]


def pending_settlement(audit: AuditLog) -> list[PendingSettlement]:
    """Orders whose fate the log does not know — after recovery has run, this
    is the honest residue: still-working orders, and anything the broker could
    not be asked about. Distinct from unmanaged exposure, which is a position
    with no audit trail at all."""
    pending: list[PendingSettlement] = []
    for trail in audit.trails():
        decision = trail.decision
        order_payload = decision.gate.order or {}
        symbol = str(order_payload.get("symbol", "")) or "?"
        sleeve = decision.sizing.sleeve
        if trail.outcome is None and _entry_needs_settlement(trail):
            pending.append(
                PendingSettlement(
                    decision_id=decision.decision_id,
                    symbol=symbol,
                    side="buy",
                    quantity=Decimal(str(order_payload.get("quantity", "0") or 0)),
                    sleeve=sleeve,
                )
            )
        for exit_record in _unsettled_exits(trail):
            pending.append(
                PendingSettlement(
                    decision_id=decision.decision_id,
                    symbol=symbol,
                    side="sell",
                    quantity=ZERO,
                    sleeve=sleeve,
                )
            )
    return pending


def recover_unsettled_orders(
    audit: AuditLog, adapter: BrokerAdapter
) -> list[str]:
    """Ask the broker about every order the log left unfinished, and write down
    the answer. Returns operator-readable lines describing what was recovered.

    Never raises: a broker that cannot answer leaves the record untouched, and
    a startup that cannot recover is still a startup that can trade.
    """
    recovered: list[str] = []
    for trail in audit.trails():
        decision = trail.decision
        try:
            if _entry_needs_settlement(trail):
                line = _recover_entry(audit, adapter, trail)
                if line:
                    recovered.append(line)
            for exit_record in _unsettled_exits(trail):
                line = _recover_exit(audit, adapter, trail, exit_record)
                if line:
                    recovered.append(line)
        except Exception as error:  # noqa: BLE001 - recovery must not block startup
            logger.exception(
                "settlement recovery failed for %s: %s", decision.decision_id, error
            )
    return recovered


def _status_for(adapter: BrokerAdapter, reference: str, by_id: bool):
    try:
        if by_id:
            return adapter.get_order(reference)
        return adapter.get_order_by_client_reference(reference)
    except BrokerError as error:
        logger.warning("could not ask the broker about %s: %s", reference, error)
        return None
    except Exception as error:  # noqa: BLE001 - unknown is not "unfilled"
        logger.warning("order lookup for %s failed: %s", reference, error)
        return None


def _recover_entry(
    audit: AuditLog, adapter: BrokerAdapter, trail: AuditTrail
) -> Optional[str]:
    decision = trail.decision
    status = _status_for(adapter, decision.decision_id, by_id=False)
    if status is None:
        logger.info(
            "no broker answer for entry %s; left pending", decision.decision_id
        )
        return None
    if not status.is_terminal:
        return None  # still working: the orphan sweep owns it

    order = parse_order(decision.gate.order)
    symbol = getattr(order, "symbol", "?")
    if status.filled_quantity <= 0 or status.filled_avg_price is None:
        audit.record_stage_rejection(
            decision.decision_id,
            RejectedStage.EXECUTION,
            status.status,
            f"recovered at startup: order terminated {status.status} without "
            f"filling; nothing was held and no reservation survived the process "
            f"that submitted it",
            signal_snapshot=decision.signal,
        )
        return (
            f"{decision.decision_id} {symbol}: terminated {status.status} "
            f"unfilled — release recorded"
        )

    filled = status.filled_quantity
    multiplier = unit_multiplier(order)
    audit.record_fill(
        decision.decision_id,
        status.broker_order_id or "recovered",
        filled,
        status.filled_avg_price,
        filled_value=filled * status.filled_avg_price * multiplier,
        side="buy",
    )
    ordered = units_of(order)
    if filled < ordered:
        audit.record_stage_rejection(
            decision.decision_id,
            RejectedStage.EXECUTION,
            status.status,
            f"recovered at startup: filled {filled} of {ordered} before "
            f"terminating {status.status}; the balance was released",
            signal_snapshot=decision.signal,
        )
    return (
        f"{decision.decision_id} {symbol}: recovered a fill of {filled} at "
        f"{status.filled_avg_price} that the previous process never recorded"
    )


def _recover_exit(
    audit: AuditLog, adapter: BrokerAdapter, trail: AuditTrail, exit_record
) -> Optional[str]:
    decision = trail.decision
    status = _status_for(adapter, exit_record.broker_order_id, by_id=True)
    if status is None or not status.is_terminal:
        return None
    if status.filled_quantity <= 0 or status.filled_avg_price is None:
        return None  # nothing sold; the position is still held and still tracked

    order = parse_order(decision.gate.order)
    multiplier = unit_multiplier(order)
    audit.record_fill(
        decision.decision_id,
        exit_record.broker_order_id,
        status.filled_quantity,
        status.filled_avg_price,
        filled_value=status.filled_quantity * status.filled_avg_price * multiplier,
        side="sell",
    )
    return (
        f"{decision.decision_id} {getattr(order, 'symbol', '?')}: recovered an "
        f"exit fill of {status.filled_quantity} at {status.filled_avg_price}"
    )
