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
_UNIT = re.compile(r"^/system\.slice/agentic-[A-Za-z0-9_-]+\.service$")
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
_marker_cache: dict[str, bool] = {}


class ProductionDataWriteRefused(PermissionError):
    """A non-production process tried to write production data."""


def _cgroup_text() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""


def production_process(cgroup_text: Optional[str] = None) -> bool:
    """True only inside a human-installed ``agentic-*`` system unit."""
    text = _cgroup_text() if cgroup_text is None else cgroup_text
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and _UNIT.match(parts[2].strip()):
            return True
    return False


def _marked(directory: Path) -> bool:
    key = str(directory)
    if key not in _marker_cache:
        try:
            _marker_cache[key] = (directory / MARKER).is_file()
        except OSError:
            _marker_cache[key] = False
    return _marker_cache[key]


def in_production_tree(path) -> bool:
    """True when ``path`` (resolved, symlinks followed) lies in a marked tree."""
    if isinstance(path, int):
        return False
    try:
        raw = os.fsdecode(path)
    except TypeError:
        return False
    resolved = Path(os.path.realpath(raw))
    for directory in (resolved, *resolved.parents):
        if _marked(directory):
            return True
    return False


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
            if in_production_tree(target):
                raise ProductionDataWriteRefused(
                    f"refused: this process is not a production unit, and {event} "
                    f"would write production data ({os.fsdecode(target)}). Dry runs and "
                    f"backtests write to the scratch data directory ({scratch_data_dir()}); "
                    f"see datasafety (ruling 2026-10-09)."
                )
    finally:
        _reentry.busy = False


def install(is_production: Optional[Callable[[], bool]] = None) -> bool:
    """Install the guard in a non-production process (idempotent). Returns
    True when the process is production (no guard), False when guarded."""
    global _installed
    production = (is_production or production_process)()
    if production:
        return True
    if not _installed:
        sys.addaudithook(_hook)
        _installed = True
    return False


def guarded() -> bool:
    return _installed


def resolve_data_dir(repo_data: Path) -> Path:
    """THE data-directory rule (ruling 2026-10-09, item 4): production data
    (``$DATA_DIR`` or the checkout's ``data/``) for a production unit or a
    declared operator command; for every other process the scratch directory,
    and a ``$DATA_DIR`` that points into production data is refused rather
    than honoured. The production unit marks its directory on first use."""
    configured = os.environ.get("DATA_DIR")
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


def ensure_marker(directory: Path) -> None:
    """Mark ``directory`` as production data. Only a production process may
    (a non-production process cannot write into the tree it would mark once
    it is marked, and marking a tree it does not own is not its decision)."""
    if not production_process():
        return
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / MARKER
    if not marker.exists():
        marker.write_text(
            "This directory holds PRODUCTION data. Only the agentic-* system units may "
            "write here; every other process is refused (datasafety, ruling 2026-10-09).\n",
            encoding="utf-8",
        )


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
