"""Production-data write guard (human ruling 2026-10-09, item 4).

Origin: on 2026-10-08 a dry run of uncommitted code, pointed at the production
data directory through a symlink, persisted market-data "strikes" against two
held symbols into ``data/unserved_symbols.json``. A process defect, not a
one-off: nothing stopped a non-production process from writing production
state. This module makes that impossible by construction.

* **Which process is production** is decided by the kernel, not by anything a
  script can set: the process's cgroup. The human-installed production units
  are SYSTEM units named ``agentic-*.service`` (paper session, earnings
  shadow, overreaction screen, weekly report); only root can create a system
  unit, so ``0::/system.slice/agentic-<name>.service`` cannot be faked from
  the agentic account. Research jobs run as USER units
  (``user.slice/.../research-*.service``) and interactive shells as session
  scopes: neither is production.
* **What is production data** is marked on disk: a directory holding the
  ``.agentic-production-data`` marker file, and everything under it. Marking
  by file (not by path string) survives symlinks, clones and moved checkouts -
  every path is resolved before it is checked.
* **The guard**: in every non-production process, an interpreter audit hook
  (``sys.addaudithook``; it cannot be removed once added) refuses any write
  into a marked tree - ``open`` in a write mode, ``os.open`` with write flags,
  rename/replace onto it, remove, mkdir, rmdir, symlink, truncate. Reads are
  allowed. The hook is installed when any project package is imported (each
  package's ``__init__`` imports this module), so every entry point that uses
  this codebase is guarded before it can resolve a data path.
* **Where non-production processes write instead**: ``scratch_data_dir()``,
  ``$AGENTIC_SCRATCH_DATA_DIR`` or ``~/agentic-scratch/data``.
  ``audit.log.default_data_dir`` returns it for every non-production process.
  ``refresh_snapshot()`` copies production data into it (a read of
  production, a write of scratch) for read-only commands such as health.
* **Separate paper accounts** (PAPER PUSH, human ruling 2026-10-09 late): each
  lab account's data lives in its own marked tree whose marker names its OWNER
  unit (``owner=agentic-lab-<name>``). In production processes too the hook
  now enforces ownership: an owned tree is writable only by its owner unit; an
  unowned tree (the main book's ``data/``) by every production unit EXCEPT the
  lab units. So no lab account can write the main book, the main book cannot
  write a lab account, and lab accounts cannot write each other.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable, Optional

MARKER = ".agentic-production-data"
_UNIT = re.compile(r"^/system\.slice/(agentic-[A-Za-z0-9_-]+)\.service$")
#: Units of the separate paper accounts (PAPER PUSH, 2026-10-09 late).
LAB_UNIT_PREFIX = "agentic-lab-"
_OWNER = re.compile(r"^owner=(agentic-[A-Za-z0-9_-]+)\s*$", re.MULTILINE)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_PATH_EVENTS = {
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.replace": (0, 1),
    "os.symlink": (1,),
    "os.link": (1,),
    "os.truncate": (0,),
    "shutil.rmtree": (0,),
    "shutil.move": (0, 1),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
}

_installed = False
_operator: Optional[str] = None
_reentry = threading.local()
#: directory -> None (no marker) or the marker's owner ("" = unowned).
_marker_cache: dict[str, Optional[str]] = {}
#: This process's production unit, resolved once at install (None = not one).
_unit_name: Optional[str] = None


class ProductionDataWriteRefused(PermissionError):
    """A non-production process tried to write production data."""


def _cgroup_text() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""


def current_unit(cgroup_text: Optional[str] = None) -> Optional[str]:
    """The human-installed ``agentic-*`` system unit this process runs in
    (``agentic-paper``, ``agentic-lab-ladder``, ...), or None."""
    text = _cgroup_text() if cgroup_text is None else cgroup_text
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            match = _UNIT.match(parts[2].strip())
            if match:
                return match.group(1)
    return None


def production_process(cgroup_text: Optional[str] = None) -> bool:
    """True only inside a human-installed ``agentic-*`` system unit."""
    return current_unit(cgroup_text) is not None


def lab_unit(account: str) -> str:
    """The production unit that owns lab account ``account``'s data."""
    return f"{LAB_UNIT_PREFIX}{account}"


def _marker_owner(directory: Path) -> Optional[str]:
    """None when ``directory`` holds no marker; its owner unit when the
    marker names one; "" for an unowned marker (the main book)."""
    key = str(directory)
    if key in _marker_cache:
        return _marker_cache[key]
    owner: Optional[str] = None
    try:
        marker = directory / MARKER
        if marker.is_file():
            match = _OWNER.search(marker.read_text(encoding="utf-8", errors="replace"))
            owner = match.group(1) if match else ""
    except OSError:
        owner = None
    # Only a FOUND marker is cached: a directory marked after this process
    # first looked at it (a lab unit marking its tree on first run) must be
    # seen as marked from then on. Writes are rare; the stat is cheap.
    if owner is not None:
        _marker_cache[key] = owner
    return owner


def _nearest_owner(path) -> Optional[str]:
    """The owner of the NEAREST marked tree holding ``path`` (resolved,
    symlinks followed): None when unmarked, "" when the main book's."""
    if isinstance(path, int):
        return None
    try:
        raw = os.fsdecode(path)
    except TypeError:
        return None
    resolved = Path(os.path.realpath(raw))
    for directory in (resolved, *resolved.parents):
        owner = _marker_owner(directory)
        if owner is not None:
            return owner
    return None


def in_production_tree(path) -> bool:
    """True when ``path`` (resolved, symlinks followed) lies in a marked tree."""
    return _nearest_owner(path) is not None


def write_permitted(path, unit: Optional[str]) -> bool:
    """May a process running in ``unit`` (None = not production) write
    ``path``? Unmarked: yes. Marked: only a production unit, and only the
    owner of an owned tree; an unowned tree (the main book) refuses the lab
    units (PAPER PUSH, 2026-10-09 late)."""
    owner = _nearest_owner(path)
    if owner is None:
        return True
    if unit is None:
        return False
    if owner == "":
        return not unit.startswith(LAB_UNIT_PREFIX)
    return unit == owner


def _write_intent(mode, flags) -> bool:
    if isinstance(mode, str):
        return any(c in mode for c in "wax+")
    if isinstance(flags, int):
        return bool(flags & _WRITE_FLAGS)
    return False


def declare_operator(command: str) -> None:
    """The emergency operator commands (``orchestrator halt`` / ``resume``,
    ops/EMERGENCY.md) write production by explicit declaration: a human at a
    shell must be able to stop and restart production. Only the CLI's
    operator dispatch calls this."""
    global _operator
    _operator = command


def writes_production() -> bool:
    """True in a production unit or a declared operator command."""
    return _operator is not None or production_process()


def _hook(event: str, args) -> None:
    if event != "open" and event not in _PATH_EVENTS:
        return
    if _operator is not None:
        return
    if getattr(_reentry, "busy", False):
        return
    _reentry.busy = True
    try:
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or not _write_intent(mode, flags):
                return
            targets: Iterable = (path,)
        else:
            targets = [args[i] for i in _PATH_EVENTS[event] if i < len(args) and args[i] is not None]
        for target in targets:
            try:
                permitted = write_permitted(target, _unit_name)
            except Exception:  # noqa: BLE001 - a bug in the guard, not a refusal
                # Fail CLOSED outside production (nothing there needs to write a
                # marked tree); inside a production unit fail OPEN, as before
                # the ownership rule existed, rather than kill a live session
                # over a guard defect.
                if _unit_name is None:
                    raise
                continue
            if permitted:
                continue
            if _unit_name is None:
                raise ProductionDataWriteRefused(
                    f"refused: this process is not a production unit, and {event} "
                    f"would write production data ({os.fsdecode(target)}). Dry runs and "
                    f"backtests write to the scratch data directory ({scratch_data_dir()}); "
                    f"see datasafety (ruling 2026-10-09)."
                )
            raise ProductionDataWriteRefused(
                f"refused: production unit {_unit_name} may not write "
                f"{os.fsdecode(target)}, which belongs to another account "
                f"(owner {_nearest_owner(target) or 'the main book'}); each paper account "
                f"writes only its own data (datasafety, PAPER PUSH 2026-10-09)."
            )
    finally:
        _reentry.busy = False


def install(is_production: Optional[Callable[[], bool]] = None) -> bool:
    """Install the guard (idempotent). Returns True when the process is
    production, False otherwise. Every process is hooked: a non-production
    process may write no marked tree; a production unit only the trees it
    owns (see ``write_permitted``)."""
    global _installed, _unit_name
    if is_production is None:
        _unit_name = current_unit()
        production = _unit_name is not None
    else:
        production = is_production()
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    return production


def guarded() -> bool:
    return _installed


def resolve_data_dir(repo_data: Path) -> Path:
    """THE data-directory rule (ruling 2026-10-09, item 4): production data
    (``$DATA_DIR`` or the checkout's ``data/``) for a production unit or a
    declared operator command; for every other process the scratch directory,
    and a ``$DATA_DIR`` that points into production data is refused rather
    than honoured. The production unit marks its directory on first use."""
    configured = os.environ.get("DATA_DIR")
    if _operator is None and _unit_name is not None and _unit_name.startswith(LAB_UNIT_PREFIX):
        raise ProductionDataWriteRefused(
            f"{_unit_name} is a lab account's unit: it has no main-book data directory "
            f"(its own is resolve_lab_data_dir; PAPER PUSH 2026-10-09)"
        )
    if writes_production():
        directory = Path(configured) if configured else repo_data
        ensure_marker(directory)
        return directory
    if configured:
        if in_production_tree(configured):
            raise ProductionDataWriteRefused(
                f"DATA_DIR={configured} is production data and this process is not a "
                f"production unit; use the scratch data directory ({scratch_data_dir()}) "
                f"or a snapshot of it (ruling 2026-10-09)"
            )
        return Path(configured)
    return scratch_data_dir()


def scratch_data_dir() -> Path:
    configured = os.environ.get("AGENTIC_SCRATCH_DATA_DIR")
    return Path(configured) if configured else Path.home() / "agentic-scratch" / "data"


def production_data_dir(candidates: Optional[Iterable[Path]] = None) -> Optional[Path]:
    """The marked production data directory, if one is reachable."""
    repo_data = Path(__file__).resolve().parents[2] / "data"
    configured = os.environ.get("AGENTIC_PRODUCTION_DATA_DIR")
    defaults = (Path(configured),) if configured else (repo_data, Path.home() / "Agentic" / "data")
    for candidate in candidates or defaults:
        try:
            if (candidate / MARKER).is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def ensure_marker(directory: Path, owner: Optional[str] = None) -> None:
    """Mark ``directory`` as production data. Only a production process may
    (a non-production process cannot write into the tree it would mark once
    it is marked, and marking a tree it does not own is not its decision).
    ``owner`` names the one unit that may write it (a lab account's)."""
    if not production_process():
        return
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / MARKER
    if not marker.exists():
        text = (
            "This directory holds PRODUCTION data. Only the agentic-* system units may "
            "write here; every other process is refused (datasafety, ruling 2026-10-09).\n"
        )
        if owner:
            text += f"owner={owner}\n"
        marker.write_text(text, encoding="utf-8")
        _marker_cache.pop(str(directory), None)


def lab_data_root() -> Path:
    """Where the separate paper accounts keep their data: one marked tree
    per account under ``$AGENTIC_LAB_DATA_ROOT`` or the checkout's
    ``data-lab/`` (beside ``data/``, never inside it: a snapshot of the main
    book must never carry a lab account's marker)."""
    configured = os.environ.get("AGENTIC_LAB_DATA_ROOT")
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "data-lab"


def lab_production_dir(account: str) -> Path:
    """Lab account ``account``'s production data directory, for READING
    (status, the scoreboard). Writing it is its owner unit's alone."""
    return lab_data_root() / account


def resolve_lab_data_dir(account: str) -> Path:
    """The data directory a process WRITES for lab account ``account``: its
    production tree in the account's own unit (marked, owned) or a declared
    operator command (halt / resume); everywhere else a scratch directory."""
    if _unit_name == lab_unit(account):
        directory = lab_production_dir(account)
        ensure_marker(directory, owner=_unit_name)
        return directory
    if _operator is not None:
        return lab_production_dir(account)
    return scratch_data_dir().parent / "lab" / account


_SNAPSHOT_SKIP = (".tmp",)


def refresh_snapshot(source: Optional[Path] = None, target: Optional[Path] = None) -> Optional[Path]:
    """Copy production data (top-level files and subdirectories) into the
    scratch data directory, so read-only commands see the real record while
    anything they write lands in scratch. Returns the scratch dir, or None
    when no production data is reachable."""
    source = source or production_data_dir()
    if source is None:
        return None
    target = target or scratch_data_dir()
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.name == MARKER or entry.name.endswith(_SNAPSHOT_SKIP):
            continue
        destination = target / entry.name
        if entry.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(entry, destination)
        elif entry.is_file():
            shutil.copy2(entry, destination)
    return target


install()
