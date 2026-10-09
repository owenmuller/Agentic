"""``python -m lab`` - the separate paper accounts (PAPER PUSH, 2026-10-09 late).

  tick <account> [--dry-run]   one run of the account's engine. Real runs only
                               inside the account's own unit
                               (agentic-lab-<account>.service); --dry-run reads
                               the account and plans with live quotes, submits
                               nothing and writes only scratch.
  status [<account>]           read-only: pin, kill switch, last snapshot.
  halt <account> --reason R    operator: write the account's halt marker; its
                               next run trips its own kill switch (frozen).
  resume <account> --ack A     operator, A HUMAN ONLY: clear the halt marker and
                               reset the account's kill switch. The agent never
                               runs this.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import datasafety
from lab.account import LabAccount, LabRefused, LabState, Ledger, open_account
from lab.config import LabConfig
from lab.ladder import LadderEngine

OPERATOR_COMMANDS = frozenset({"halt", "resume"})


def _snapshot(account: str) -> Path:
    """A scratch copy of the account's production data, for dry runs."""
    source = datasafety.lab_production_dir(account)
    target = datasafety.scratch_data_dir().parent / "lab-snapshot" / account
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if source.is_dir():
        for entry in source.iterdir():
            if entry.is_file() and entry.name != datasafety.MARKER:
                shutil.copy2(entry, target / entry.name)
    return target


def tick(name: str, dry_run: bool) -> int:
    config = LabConfig.load()
    if not dry_run and datasafety.current_unit() != datasafety.lab_unit(name):
        print(
            f"REFUSED: a real lab run trades the {name!r} paper account and writes its ledger; it runs only "
            f"inside {datasafety.lab_unit(name)}.service. Use --dry-run to plan without trading.",
            file=sys.stderr,
        )
        return 2
    try:
        account = open_account(name, config, data_dir=_snapshot(name) if dry_run else None)
    except LabRefused as refusal:
        print(f"LAB {name} REFUSED TO START: {refusal}", file=sys.stderr)
        return 3
    if account.config.sleeve != "leverage_ladder":
        print(f"LAB {name}: sleeve {account.config.sleeve!r} has no engine yet", file=sys.stderr)
        return 3
    engine = LadderEngine(account, config.ladder, dry_run=dry_run)
    notes = engine.tick(datetime.now(timezone.utc))
    for note in notes:
        print(f"LAB {name}{' DRY RUN' if dry_run else ''}: {note}")
    return 0


def status(names: list[str]) -> int:
    config = LabConfig.load()
    for name in names or list(config.accounts):
        account = config.account(name)
        directory = datasafety.lab_production_dir(name)
        state = LabState.load(directory / "state.json")
        ledger = Ledger(directory / "ledger.jsonl", lambda: datetime.now(timezone.utc))
        snapshots = ledger.events(["snapshot"])
        faults = ledger.events(["fault"])
        print(f"{name}: {account.label} - {'ENABLED' if account.enabled else 'not enabled'}"
              f"{'' if account.enabled else ' (' + (account.status or '') + ')'}")
        print(f"  live eligible: {account.live_eligible}; pinned broker account: {state.account_number or '(not yet)'}")
        print(f"  kill switch: {'TRIPPED ' + (state.trip_reason or '') if state.kill_switch_tripped else 'clear'}; "
              f"halt marker: {'PRESENT' if (directory / 'HALT').exists() else 'none'}; "
              f"high-water mark: {state.high_water_mark}")
        if snapshots:
            last = snapshots[-1]
            print(f"  last snapshot {last['at']}: equity {last.get('equity')}, rungs {json.dumps(last.get('rungs'))}")
        if faults:
            print(f"  faults: {len(faults)}, last: {faults[-1]['at']} {faults[-1]['message']}")
    return 0


def halt(name: str, reason: str) -> int:
    if not reason.strip():
        print("a halt needs --reason", file=sys.stderr)
        return 2
    directory = datasafety.resolve_lab_data_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (directory / "HALT").write_text(f"{reason.strip()} (halted_at={now})\n", encoding="utf-8")
    print(f"LAB {name}: halt marker written; its next run trips its kill switch and freezes the account. "
          f"Resuming is a human's `python -m lab resume {name} --ack ...`.")
    return 0


def resume(name: str, ack: str) -> int:
    """FOR A HUMAN OPERATOR ONLY (CLAUDE.md: resume requires manual human
    reset). Clears the marker and the trip; the high-water mark re-baselines
    to the account's last snapshot equity, as the main gate's reset does."""
    if not ack.strip():
        print("a resume needs --ack naming the human who resets it", file=sys.stderr)
        return 2
    directory = datasafety.resolve_lab_data_dir(name)
    state = LabState.load(directory / "state.json")
    ledger = Ledger(directory / "ledger.jsonl", lambda: datetime.now(timezone.utc))
    snapshots = ledger.events(["snapshot"])
    (directory / "HALT").unlink(missing_ok=True)
    was = state.kill_switch_tripped
    state.kill_switch_tripped = False
    state.tripped_at = state.trip_reason = None
    if snapshots and snapshots[-1].get("equity"):
        state.high_water_mark = str(snapshots[-1]["equity"])
    state.save(directory / "state.json")
    ledger.append("kill_switch_reset", acknowledgement=ack.strip(), was_tripped=was,
                  high_water_mark=state.high_water_mark)
    print(f"LAB {name}: kill switch {'reset' if was else 'was clear'}; halt marker cleared; "
          f"high-water mark {state.high_water_mark}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m lab")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tick")
    p.add_argument("account")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("accounts", nargs="*")
    p = sub.add_parser("halt")
    p.add_argument("account")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("resume")
    p.add_argument("account")
    p.add_argument("--ack", required=True)
    args = parser.parse_args(argv)
    command = args.command
    if command in OPERATOR_COMMANDS:
        # halt / resume write the account's production data by declaration
        # (a human at a shell must be able to stop an account); nothing else may.
        datasafety.declare_operator(command)
    if args.command == "tick":
        return tick(args.account, args.dry_run)
    if args.command == "status":
        return status(args.accounts)
    if args.command == "halt":
        return halt(args.account, args.reason)
    return resume(args.account, args.ack)


if __name__ == "__main__":
    sys.exit(main())
