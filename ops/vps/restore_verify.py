"""Restore-drill verifier (human ruling 2026-09-02): parse a restored data/
with the production audit parser and compare it with the live data/.

    python ops/vps/restore_verify.py <restored data dir> <live data dir>

Read-only on both. Exit 0 = PASS (restored log parses completely and is a
prefix of the live log; session flags reported), 1 = FAIL with the first
divergence, 2 = usage.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "src"))

from audit.log import AuditLog  # noqa: E402


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _summary(data_dir: Path) -> dict:
    audit = AuditLog(path=data_dir / "audit.jsonl")
    kinds: Counter[str] = Counter()
    last = None
    for record in audit.records():  # raises on a corrupt line — that IS the test
        kinds[str(record.kind)] += 1
        last = record.recorded_at
    open_by_strategy = {
        strategy: sorted(audit.strategy_open_positions(strategy))
        for strategy in ("judged", "mechanical", "cash_sweep")
    }
    session_path = data_dir / "session_state.json"
    session = json.loads(session_path.read_text(encoding="utf-8")) if session_path.exists() else {}
    return {
        "records": sum(kinds.values()),
        "kinds": dict(sorted(kinds.items())),
        "last_recorded_at": last.isoformat(timespec="seconds") if last else None,
        "open_positions": open_by_strategy,
        "kill_switch_tripped": session.get("kill_switch_tripped"),
        "mechanical_halted": session.get("mechanical_halted"),
        "halt_marker": (data_dir / "HALT").exists(),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    restored, live = Path(argv[1]), Path(argv[2])
    try:
        restored_summary = _summary(restored)
    except Exception as error:  # noqa: BLE001
        print(f"FAIL: restored audit log did not parse: {type(error).__name__}: {error}")
        return 1
    live_summary = _summary(live)

    restored_lines = _lines(restored / "audit.jsonl")
    live_lines = _lines(live / "audit.jsonl")
    for index, (a, b) in enumerate(zip(restored_lines, live_lines)):
        if a != b:
            print(f"FAIL: restored and live logs diverge at line {index + 1}")
            print(f"  restored: {a[:160]}")
            print(f"  live:     {b[:160]}")
            return 1
    if len(restored_lines) > len(live_lines):
        print("FAIL: restored log is LONGER than live — the live log lost records")
        return 1

    behind = len(live_lines) - len(restored_lines)
    print("RESTORE DRILL")
    print(f"  restored: {restored_summary['records']} records, last "
          f"{restored_summary['last_recorded_at']}")
    print(f"  live:     {live_summary['records']} records, last "
          f"{live_summary['last_recorded_at']}")
    print(f"  restored is a prefix of live, {behind} record(s) behind")
    print(f"  kinds (restored): {restored_summary['kinds']}")
    for strategy in ("judged", "mechanical", "cash_sweep"):
        r = restored_summary["open_positions"][strategy]
        l = live_summary["open_positions"][strategy]
        print(f"  open {strategy:<11} restored={r} live={l}"
              + ("" if r == l else "  (differs: positions changed since the backup)"))
    print(f"  session flags restored: kill_switch={restored_summary['kill_switch_tripped']} "
          f"mechanical_halted={restored_summary['mechanical_halted']} "
          f"halt_marker={restored_summary['halt_marker']}")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
