"""The production-data write guard (human ruling 2026-10-09, item 4).

Origin: a dry run of uncommitted code, pointed at the production data directory
through a symlink, persisted market-data strikes against two held symbols into
``data/unserved_symbols.json`` on 2026-10-08. These tests fail if any
non-production entry point can open a production data file for writing.

Each guard check runs in a FRESH interpreter (subprocess): the guard is an
interpreter audit hook installed at import, and the point is what an entry
point gets from a cold start, not what this test process happens to hold.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import datasafety

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PACKAGES = sorted(
    p.name for p in SRC.iterdir()
    if (p / "__init__.py").is_file() and p.name != "datasafety"
)


def _run(code: str, tmp_path: Path, extra_env=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["AGENTIC_SCRATCH_DATA_DIR"] = str(tmp_path / "scratch")
    env["AGENTIC_PRODUCTION_DATA_DIR"] = str(tmp_path / "prod")
    env.pop("DATA_DIR", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.fixture
def trees(tmp_path):
    prod = tmp_path / "prod"
    prod.mkdir()
    (prod / "audit.jsonl").write_text('{"existing": true}\n')
    (prod / "sub").mkdir()
    # the marker LAST: once it exists this (non-production) process may not
    # write the tree, which is the point
    (prod / datasafety.MARKER).write_text("test production tree\n")
    free = tmp_path / "free"
    free.mkdir()
    return tmp_path


# ------------------------------------------------------------ production test

@pytest.mark.parametrize(
    "cgroup, expected",
    [
        ("0::/system.slice/agentic-paper.service\n", True),
        ("0::/system.slice/agentic-weekly.service\n", True),
        ("0::/system.slice/ssh.service\n", False),
        ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/research-attn.service\n", False),
        ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/agentic-paper.service\n", False),
        ("0::/user.slice/user-1000.slice/session-10757.scope\n", False),
        ("0::/system.slice/agentic-paper.service/extra\n", False),
        ("", False),
    ],
)
def test_only_a_human_installed_agentic_system_unit_is_production(cgroup, expected):
    assert datasafety.production_process(cgroup) is expected


def test_this_test_process_is_not_production_and_is_guarded():
    assert not datasafety.production_process()
    assert datasafety.guarded()


# ------------------------------------------------- every write form, refused

WRITE_ATTEMPTS = r'''
import os, shutil, sys
from pathlib import Path
import {package}
import datasafety
assert datasafety.guarded(), "guard not installed by importing {package}"
prod, free = Path("prod"), Path("free")
attempts = {{
    "open-w": lambda: open(prod / "audit.jsonl", "w"),
    "open-a": lambda: open(prod / "audit.jsonl", "a"),
    "open-r+": lambda: open(prod / "audit.jsonl", "r+"),
    "open-x": lambda: open(prod / "new.json", "x"),
    "write_text": lambda: (prod / "state.json").write_text("{{}}"),
    "write_bytes-sub": lambda: (prod / "sub" / "x.bin").write_bytes(b"x"),
    "os.open": lambda: os.open(prod / "fd.json", os.O_WRONLY | os.O_CREAT),
    "os.replace-into": lambda: ((free / "t.tmp").write_text("x"), os.replace(free / "t.tmp", prod / "unserved_symbols.json")),
    "os.rename-out": lambda: os.rename(prod / "audit.jsonl", free / "stolen.jsonl"),
    "os.remove": lambda: os.remove(prod / "audit.jsonl"),
    "mkdir": lambda: (prod / "newdir").mkdir(),
    "copyfile-into": lambda: ((free / "c.txt").write_text("x"), shutil.copyfile(free / "c.txt", prod / "c.txt")),
    "via-symlink": lambda: (os.symlink(prod.resolve(), free / "link") or True) and (free / "link" / "unserved_symbols.json").write_text("{{}}"),
}}
for name, attempt in attempts.items():
    try:
        handle = attempt()
    except datasafety.ProductionDataWriteRefused:
        continue
    print("NOT REFUSED", name)
    sys.exit(1)
assert (prod / "audit.jsonl").read_text() == '{{"existing": true}}\n', "production file changed"
assert sorted(p.name for p in prod.iterdir()) == [".agentic-production-data", "audit.jsonl", "sub"], sorted(p.name for p in prod.iterdir())
# reads stay allowed; writes outside the marked tree stay allowed
assert open(prod / "audit.jsonl").read()
(free / "ok.json").write_text("{{}}")
print("ALL REFUSED")
'''


@pytest.mark.parametrize("package", PACKAGES)
def test_importing_any_project_package_guards_every_write_form(trees, package):
    result = _run(WRITE_ATTEMPTS.format(package=package), trees)
    assert result.returncode == 0 and "ALL REFUSED" in result.stdout, result.stdout + result.stderr


def test_project_writers_cannot_reach_production_either(trees):
    result = _run(
        r'''
        from datetime import datetime, timezone
        from pathlib import Path
        from audit.log import AuditLog, default_data_dir
        from execution.market_data import UnservedSymbols
        import datasafety
        prod = Path("prod")
        # the exact 2026-10-08 failure: a strike persisted into production
        memo = UnservedSymbols(prod / "unserved_symbols.json")
        memo.add("RWT", 400, datetime(2026, 10, 8, 20, 16, tzinfo=timezone.utc))
        assert not (prod / "unserved_symbols.json").exists(), "strike reached production"
        # the default data dir of a non-production process is scratch
        assert default_data_dir() == datasafety.scratch_data_dir()
        assert not datasafety.in_production_tree(default_data_dir())
        print("OK")
        ''',
        trees,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_a_data_dir_pointing_into_production_is_refused_not_honoured(trees):
    result = _run(
        r'''
        import datasafety
        from audit.log import default_data_dir
        try:
            default_data_dir()
        except datasafety.ProductionDataWriteRefused:
            print("REFUSED")
        ''',
        trees,
        extra_env={"DATA_DIR": str(trees / "prod" / "sub")},
    )
    assert "REFUSED" in result.stdout, result.stdout + result.stderr


def test_the_session_command_is_refused_outside_a_production_unit(trees):
    env = dict(os.environ, PYTHONPATH=str(SRC), AGENTIC_SCRATCH_DATA_DIR=str(trees / "scratch"),
               AGENTIC_PRODUCTION_DATA_DIR=str(trees / "prod"))
    env.pop("DATA_DIR", None)
    result = subprocess.run(
        [sys.executable, "-m", "orchestrator", "run"],
        cwd=str(trees), env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 2 and "REFUSED" in result.stderr, result.stdout + result.stderr


def test_read_only_commands_read_a_snapshot_in_scratch(trees):
    result = _run(
        r'''
        from pathlib import Path
        import datasafety
        snap = datasafety.refresh_snapshot()
        assert snap == datasafety.scratch_data_dir(), snap
        assert (snap / "audit.jsonl").read_text() == '{"existing": true}\n'
        assert not (snap / datasafety.MARKER).exists(), "the snapshot must not be production"
        (snap / "unserved_symbols.json").write_text("{}")
        print("OK")
        ''',
        trees,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_only_the_operator_commands_may_declare_production_writes(trees):
    from orchestrator.__main__ import OPERATOR_COMMANDS

    assert OPERATOR_COMMANDS == {"halt", "resume"}
    result = _run(
        r'''
        from pathlib import Path
        import datasafety
        datasafety.declare_operator("halt")
        (Path("prod") / "halt.marker").write_text("operator")
        print("OK")
        ''',
        trees,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
    calls = [(path.relative_to(SRC).as_posix(), line) for path in SRC.rglob("*.py")
             for line in path.read_text(encoding="utf-8").splitlines()
             if "declare_operator(" in line and "def declare_operator" not in line]
    # one declaration per operator CLI: the main book's and the paper lab's
    # (PAPER PUSH, 2026-10-09), each behind its own OPERATOR_COMMANDS check
    assert sorted(path for path, _ in calls) == ["lab/__main__.py", "orchestrator/__main__.py"], calls
    assert all("declare_operator(command)" in line for _, line in calls), calls
    for cli in ("orchestrator", "lab"):
        assert "if command in OPERATOR_COMMANDS:" in (SRC / cli / "__main__.py").read_text(encoding="utf-8")
    from lab.__main__ import OPERATOR_COMMANDS as LAB_OPERATOR_COMMANDS

    assert LAB_OPERATOR_COMMANDS == {"halt", "resume"}


# ------------------------------------------- every entry point imports it

def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


ENTRY_POINTS = sorted(
    [p for p in (ROOT / "ops").rglob("*.py")]
    + [p for p in SRC.rglob("__main__.py")]
)


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_entry_point_imports_the_guard(path):
    """A script that imports no project package could write anywhere; every
    entry point must import the guard or a package that installs it."""
    guarded_by = _imports(path) & (set(PACKAGES) | {"datasafety"})
    assert guarded_by, f"{path.relative_to(ROOT)} imports no project package: the guard is never installed"
