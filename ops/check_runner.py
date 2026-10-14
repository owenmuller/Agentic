"""One-shot checks that fire ON THE DROPLET, on time (human ruling 2026-10-10).

Scheduled checks used to live in the laptop's Claude session and fired only
while it was open and idle (two fired late on 2026-10-12). They now run as
installed, persistent systemd USER timers on the droplet (``ops/vps/schedule_check.sh``),
which fire on their own clock regardless of any laptop.

  python ops/check_runner.py NAME -- COMMAND [ARGS...]

runs COMMAND from the checkout, writes its whole output to
``~/checks/NAME-<UTC stamp>.txt`` and emails it: the DAILY tier when the
command exits 0, the URGENT tier when it fails or cannot start. A check is a
non-production process (a user unit), so the data guard confines it to
reads and scratch - exactly what a check should be able to do.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from execution import load_environment  # noqa: E402
from execution import cards  # noqa: E402
from execution.alerts import Alerter  # noqa: E402 - installs the data guard

ROOT = Path(__file__).resolve().parents[1]
NOISE = ("HTTP Request:",)


def describe(name: str, command: Sequence[str]) -> tuple[str, str]:
    """(account, what it checked) in plain words, for the card."""
    words = list(command)
    account = ("Leverage ladder" if name.startswith("ladder") else
               "AI trader" if name.startswith("ai-trader") else "Server")
    if "lab" in words and "status" in words:
        target = words[-1] if words[-1] != "status" else "every lab"
        purpose = f"the {DISPLAY.get(target, target)} account's state, trades and problems so far"
    elif "lab" in words and "scoreboard" in words:
        purpose = "the paper lab scoreboard"
    else:
        purpose = "the command " + " ".join(w.rsplit("/", 1)[-1] for w in words[-3:])
    return account, purpose


DISPLAY = {"ladder": "leverage ladder", "ai-trader": "AI trader"}


def run_check(name: str, command: Sequence[str], *, out_dir: Path,
              alert: Callable[[cards.Card], None], timeout: int = 1800,
              clock: Optional[Callable[[], datetime]] = None) -> int:
    started = (clock or (lambda: datetime.now(timezone.utc)))()
    try:
        done = subprocess.run(list(command), cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as error:
        code, output = 127, f"could not run {list(command)}: {type(error).__name__}: {error}"
    kept = "\n".join(line for line in output.splitlines() if not any(n in line for n in NOISE))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}-{started:%Y%m%dT%H%M%SZ}.txt"
    header = f"CHECK {name} at {started.isoformat(timespec='seconds')}: exit {code}\n$ {' '.join(command)}\n\n"
    path.write_text(header + kept + "\n", encoding="utf-8")
    ok = code == 0
    account, purpose = describe(name, command)
    local = started.astimezone(ZoneInfo("America/New_York"))
    alert(cards.check_card(name, account, purpose, ok, f"{local:%b} {local.day}, {local:%H:%M} ET", code,
                           header + kept[-60000:], f"~/checks/{path.name} on the server"))
    return code


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: check_runner.py NAME -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    load_environment()  # the alert routes (Discord, ntfy, ...) live in .env
    alerter = Alerter()

    def alert(card: cards.Card) -> None:
        # a pass is a green card in #reports; a failure a red one in #urgent
        alerter.card(f"check-{argv[0]}", card)

    try:
        return run_check(argv[0], argv[2:], out_dir=Path.home() / "checks", alert=alert)
    finally:
        alerter.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
