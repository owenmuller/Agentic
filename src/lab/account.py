"""One separate paper account: preflight, ledger, state, kill switch, gate
(PAPER PUSH, human ruling 2026-10-09 late).

Preflight refuses to start - it never falls back - when:
  * the process does not resolve to PAPER (Constraint #4: the lab is paper by
    ruling; this module reads the mode, it never sets either variable);
  * the account's own keys are missing, or equal the main book's keys;
  * the keys resolve to the main book's broker account;
  * the keys resolve to a different broker account than the one pinned at
    this account's first run (keys swapped between books).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import datasafety
from execution import AlpacaAdapter, BrokerPosition, load_environment, require_paper_or_confirmed_live
from lab.config import AccountConfig, LabConfig, account_limits
from risk_gate.gate import RiskGate
from risk_gate.limits import RiskLimits
from risk_gate.state import AccountState, AccountType, Sleeve

ZERO = Decimal("0")


class LabRefused(RuntimeError):
    """The lab account will not start (or will not act); the message says why."""


def key_variables(account: str) -> tuple[str, str]:
    stem = "ALPACA_LAB_" + re.sub(r"[^A-Za-z0-9]", "_", account).upper()
    return f"{stem}_API_KEY", f"{stem}_API_SECRET"


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not serializable: {type(value).__name__}")


class Ledger:
    """Append-only JSONL: every order, fill, funding, adjustment, snapshot and
    kill-switch event of one account. The account's books are a replay of it."""

    def __init__(self, path: Path, clock: Callable[[], datetime]) -> None:
        self.path = path
        self._clock = clock

    def append(self, event: str, **fields: Any) -> dict:
        record = {"event": event, "at": self._clock().isoformat(), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=_json_default, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def events(self, kinds: Optional[Iterable[str]] = None) -> list[dict]:
        if not self.path.exists():
            return []
        wanted = set(kinds) if kinds is not None else None
        out = []
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if wanted is None or record.get("event") in wanted:
                    out.append(record)
        return out


@dataclass
class LabState:
    """Persisted per account: identity pin, high-water mark, kill switch,
    the engine's cadence keys."""

    account_number: Optional[str] = None
    high_water_mark: Optional[str] = None
    kill_switch_tripped: bool = False
    tripped_at: Optional[str] = None
    trip_reason: Optional[str] = None
    engine: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "LabState":
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.__dict__, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


@dataclass
class LabAccount:
    name: str
    config: AccountConfig
    data_dir: Path
    adapter: AlpacaAdapter
    limits: RiskLimits
    ledger: Ledger
    state: LabState
    state_path: Path
    account_number: str
    clock: Callable[[], datetime]
    api_key: str
    api_secret: str

    @property
    def halt_path(self) -> Path:
        return self.data_dir / "HALT"

    def halted(self) -> Optional[str]:
        try:
            return self.halt_path.read_text(encoding="utf-8").strip() or "(halt marker present)"
        except FileNotFoundError:
            return None

    def build_gate(self, cash: Decimal, positions: list[BrokerPosition], sleeve: Sleeve) -> RiskGate:
        """The account's own RiskGate, seeded from the broker (cash and
        positions are read, never reconstructed) and this account's own
        high-water mark and kill switch."""
        account = AccountState(
            cash=cash,
            high_water_mark=ZERO,
            account_type=AccountType.CASH,
            kill_switch_tripped=self.state.kill_switch_tripped,
        )
        for holding in positions:
            if holding.is_option:
                raise LabRefused(f"{self.name}: holds an option ({holding.symbol}); this account's engine trades none")
            position = account.ensure_position((sleeve.value, holding.symbol), sleeve, 1, False)
            position.quantity = holding.quantity
            position.cost_basis = holding.cost_basis
            position.market_value = holding.market_value
        recorded = Decimal(self.state.high_water_mark) if self.state.high_water_mark else ZERO
        account.high_water_mark = max(recorded, account.nav)
        return RiskGate(self.limits, account, self.clock)

    def persist_gate(self, gate: RiskGate, reason: Optional[str] = None) -> None:
        """Carry the gate's high-water mark and kill switch into the state
        file; a NEW trip is written to the ledger once."""
        state = gate.state
        self.state.high_water_mark = str(state.high_water_mark)
        if state.kill_switch_tripped and not self.state.kill_switch_tripped:
            self.state.kill_switch_tripped = True
            self.state.tripped_at = self.clock().isoformat()
            self.state.trip_reason = reason or (
                f"drawdown {state.drawdown():.4f} from high-water mark {state.high_water_mark:.2f} "
                f"reached the {self.limits.kill_switch.drawdown_from_high_water_mark} trip"
            )
            self.ledger.append(
                "kill_switch_tripped",
                reason=self.state.trip_reason,
                nav=state.nav,
                high_water_mark=state.high_water_mark,
            )
        self.state.save(self.state_path)


def open_account(
    name: str,
    lab_config: LabConfig,
    *,
    adapter: Optional[AlpacaAdapter] = None,
    main_account_number: Optional[Callable[[], Optional[str]]] = None,
    data_dir: Optional[Path] = None,
    clock: Optional[Callable[[], datetime]] = None,
    limits_path: Optional[Path] = None,
) -> LabAccount:
    """Preflight one lab account. Raises ``LabRefused`` rather than start
    on anything it cannot verify."""
    config = lab_config.account(name)
    if not config.enabled:
        raise LabRefused(f"lab account {name!r} is not enabled: {config.status or 'see config/lab.yaml'}")
    load_environment()
    if not require_paper_or_confirmed_live():
        raise LabRefused(
            "the paper lab runs in paper only (PAPER PUSH ruling); this process resolved to live "
            "and the lab will not start"
        )
    key_var, secret_var = key_variables(name)
    api_key, api_secret = os.environ.get(key_var, ""), os.environ.get(secret_var, "")
    if not api_key or not api_secret:
        raise LabRefused(
            f"lab account {name!r} has no keys: a human creates the Alpaca paper account and adds "
            f"{key_var} and {secret_var} to .env"
        )
    if api_key == os.environ.get("ALPACA_API_KEY"):
        raise LabRefused(f"lab account {name!r} is configured with the MAIN book's API key; refusing")
    clock = clock or (lambda: datetime.now(timezone.utc))
    adapter = adapter or AlpacaAdapter(api_key=api_key, api_secret=api_secret)
    number = str(adapter.account_snapshot().get("account_number") or "")
    if not number:
        raise LabRefused(f"lab account {name!r}: the broker returned no account number")
    main_number = (main_account_number or _main_account_number)()
    if main_number is not None and number == main_number:
        raise LabRefused(f"lab account {name!r}: its keys resolve to the MAIN book's account {number}; refusing")
    directory = data_dir or datasafety.resolve_lab_data_dir(name)
    state_path = directory / "state.json"
    state = LabState.load(state_path)
    ledger = Ledger(directory / "ledger.jsonl", clock)
    if state.account_number is None:
        state.account_number = number
        ledger.append("account_pinned", account_number=number, label=config.label)
        state.save(state_path)
    elif state.account_number != number:
        raise LabRefused(
            f"lab account {name!r} is pinned to broker account {state.account_number} but its keys now "
            f"resolve to {number}; keys swapped between books? refusing"
        )
    for other, other_config in lab_config.accounts.items():
        if other == name:
            continue
        other_state = datasafety.lab_production_dir(other) / "state.json"
        try:
            pinned = json.loads(other_state.read_text(encoding="utf-8")).get("account_number")
        except (OSError, ValueError):
            pinned = None
        if pinned == number:
            raise LabRefused(f"lab account {name!r}: broker account {number} is already lab account {other!r}'s")
    return LabAccount(
        name=name,
        config=config,
        data_dir=directory,
        adapter=adapter,
        limits=account_limits(config, limits_path),
        ledger=ledger,
        state=state,
        state_path=state_path,
        account_number=number,
        clock=clock,
        api_key=api_key,
        api_secret=api_secret,
    )


def _main_account_number() -> Optional[str]:
    """The main book's broker account number (a read with the main keys), or
    None when the main keys are absent."""
    if not os.environ.get("ALPACA_API_KEY") or not os.environ.get("ALPACA_API_SECRET"):
        return None
    with AlpacaAdapter() as main:
        return str(main.account_snapshot().get("account_number") or "") or None
