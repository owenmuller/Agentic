"""The import topology, in one place.

This file is the whole answer to "what is allowed to depend on what". It used to be
three ``FORBIDDEN_IMPORTS`` tuples in three test files, each phrased as a denial and
each knowing only about its own package — which meant adding a package meant
remembering all three, and no single place said what the architecture was. Denials
also age badly: a tuple listing what ``sizing`` must not import says nothing about the
packages that did not exist when it was written.

So the map below is an allow-list, and it is exhaustive. An edge that is not in it is
an error, a package that is not in it is an error, and a package added to ``src/``
without a decision about what it may reach fails ``test_the_map_covers_every_package``
rather than quietly inheriting permission.

Reading the map
---------------
An entry may name a package (``"risk_gate"`` — any module in it) or one module
(``"research.reports"`` — that module and nothing else beside it). The narrow form is
doing real work: ``sizing`` is allowed the ``ResearchReport`` type and is *not*
allowed the research client, so it can name what it takes as input without acquiring a
network dependency in the process.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

#: Roots that mean "this module can talk to the outside world". A package that must be
#: deterministic is defined by not being able to reach any of them.
NETWORK_ROOTS = frozenset({"anthropic", "httpx", "openai", "requests", "urllib3"})


@dataclass(frozen=True)
class Rules:
    """What one package may reach."""

    #: First-party packages or modules it may import.
    may_import: frozenset[str] = field(default_factory=frozenset)
    #: Whether it may import a client that does I/O over a network.
    may_reach_network: bool = False
    #: Why the boundary is where it is. Read by nobody; written for the next person.
    because: str = ""


TOPOLOGY: dict[str, Rules] = {
    # CONSTRAINT #3: deterministic Python, no LLM calls, no bypass path. It imports
    # nothing first-party at all, which is what makes "the gate cannot be talked into
    # anything" structural: there is no module it shares with the layers that read
    # untrusted content, and no client in it that could call a model.
    "risk_gate": Rules(
        because="deterministic and self-contained; Constraint #3",
    ),
    # CONSTRAINT #5: signals are data, not commands. A scanner's entire vocabulary is
    # "a Signal was observed". It cannot size, price, approve or send anything, so no
    # amount of persuasive content in a post can produce a trade from here.
    "signals": Rules(
        because="untrusted content enters here; it must not reach trading machinery",
    ),
    # Sizing takes an integer and a direction from a report. `research.reports` is a
    # module of data classes — importing it brings no client and no network with it,
    # which the narrow form of the entry is what pins down.
    "sizing": Rules(
        may_import=frozenset({"research.reports", "risk_gate.limits", "risk_gate.state"}),
        because="deterministic; reads a report type, never a research client",
    ),
    # The adapter takes an ApprovedOrder and nothing else, so it needs the gate's types.
    "execution": Rules(
        may_import=frozenset({"risk_gate"}),
        may_reach_network=True,
        because="talks to a broker; accepts only gate-approved orders",
    ),
    # Research reads signals and calls a model. `execution.environment` is permitted for
    # one narrow reason: the Anthropic client reads ANTHROPIC_API_KEY through the same
    # .env loader the broker adapter uses. It never touches the adapter itself.
    "research": Rules(
        may_import=frozenset({"signals", "execution.environment"}),
        may_reach_network=True,
        because="scores signals; cannot size or approve anything",
    ),
    # Audit has to describe everything, so it reads types from everywhere. It is read
    # *by* nothing except the orchestrator — see test_only_the_orchestrator_imports_audit.
    "audit": Rules(
        may_import=frozenset(
            {
                "signals",
                "research.reports",
                "sizing.engine",
                "risk_gate.gate",
                "risk_gate.rejections",
            }
        ),
        because="describes every stage; observes, never participates",
    ),
    # The one package permitted to touch more than its neighbours. Its whole job is the
    # seam between the others, and it is the only importer of audit anywhere.
    "orchestrator": Rules(
        may_import=frozenset(
            {"audit", "execution", "research", "risk_gate", "signals", "sizing"}
        ),
        because="the loop; the single place the stages are wired together",
    ),
}


# ================================================================================
# The walk
# ================================================================================


def packages() -> list[str]:
    """Every package under ``src/``, by directory."""
    return sorted(
        path.parent.name for path in SRC.glob("*/__init__.py") if path.parent.is_dir()
    )


def imports_in(path: Path) -> list[tuple[int, str]]:
    """Every imported module name in one file, with its line number."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


def violations_for(package: str, rules: Rules) -> list[str]:
    """Every import in ``package`` that ``rules`` does not permit."""
    first_party = set(TOPOLOGY)
    offenders: list[str] = []
    for path in sorted((SRC / package).rglob("*.py")):
        for lineno, name in imports_in(path):
            root = name.split(".")[0]
            if root == package:
                continue
            if root in first_party and not (
                name in rules.may_import or root in rules.may_import
            ):
                offenders.append(f"{package}/{path.name}:{lineno}: imports {name}")
            elif root in NETWORK_ROOTS and not rules.may_reach_network:
                offenders.append(
                    f"{package}/{path.name}:{lineno}: imports {name}, but {package} "
                    f"must stay offline"
                )
    return offenders


@pytest.mark.parametrize("package", sorted(TOPOLOGY))
def test_package_imports_only_what_the_map_allows(package):
    """One walk, every package, driven by the map above."""
    rules = TOPOLOGY[package]
    assert violations_for(package, rules) == [], (
        f"{package} reached outside its allowed edges. {rules.because}"
    )


def test_the_map_covers_every_package():
    """A new package under src/ needs a decision, not a default."""
    assert sorted(TOPOLOGY) == packages(), (
        "every package in src/ must appear in TOPOLOGY; a package with no entry has "
        "no stated boundary, which is how boundaries stop being real"
    )


def test_the_walk_would_catch_a_real_import(tmp_path):
    """A guard is only worth having if it fails on the thing it forbids."""
    package = tmp_path / "src" / "risk_gate"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "rogue.py").write_text(
        "import anthropic\nfrom research.client import LLMClient\n", encoding="utf-8"
    )

    global SRC
    original, SRC = SRC, tmp_path / "src"
    try:
        offenders = violations_for("risk_gate", TOPOLOGY["risk_gate"])
    finally:
        SRC = original

    assert len(offenders) == 2
    assert any("anthropic" in offender for offender in offenders)
    assert any("research.client" in offender for offender in offenders)


# ================================================================================
# The properties the map is there to hold
# ================================================================================


def test_the_risk_gate_is_deterministic_and_self_contained():
    """CONSTRAINT #3, as a test rather than a docstring.

    The gate imports no first-party package and no network client. That is stronger
    than "it makes no LLM calls today": there is no module it can reach from which one
    could be made, so adding one would mean adding an import, and adding that import
    fails here.
    """
    rules = TOPOLOGY["risk_gate"]
    assert rules.may_import == frozenset()
    assert rules.may_reach_network is False
    assert violations_for("risk_gate", rules) == []


def test_sizing_is_deterministic():
    """Same property, one layer out: no model client, no broker, no network."""
    rules = TOPOLOGY["sizing"]
    assert rules.may_reach_network is False
    assert "execution" not in rules.may_import
    # The narrow entry is the point: the report type, not the research package.
    assert "research" not in rules.may_import
    assert "research.reports" in rules.may_import


def test_signals_cannot_reach_execution_or_risk():
    """CONSTRAINT #5. A scanner can report what it saw and nothing else."""
    assert TOPOLOGY["signals"].may_import == frozenset()


def test_only_the_orchestrator_imports_audit():
    """An audit trail other modules can call into is part of the machinery.

    The orchestrator is the exception because it is the thing being audited from the
    outside: it runs the stages and writes down what they did. Anything else holding
    the log could write its own version of events.
    """
    importers = {
        package
        for package, rules in TOPOLOGY.items()
        if any(entry.split(".")[0] == "audit" for entry in rules.may_import)
    }
    assert importers == {"orchestrator"}


def test_the_orchestrator_is_the_only_package_that_spans_the_others():
    """"Permitted to touch more than its neighbours" is a privilege held exactly once."""
    others = set(TOPOLOGY) - {"orchestrator"}
    spanning = {
        package
        for package, rules in TOPOLOGY.items()
        if others - {package} <= {entry.split(".")[0] for entry in rules.may_import}
    }
    assert spanning == {"orchestrator"}


def test_nothing_imports_the_orchestrator():
    """The loop is a leaf. A package that could call back into it could start a trade."""
    for package, rules in TOPOLOGY.items():
        assert "orchestrator" not in {
            entry.split(".")[0] for entry in rules.may_import
        }, f"{package} may import the orchestrator"


def test_audit_reads_types_from_everywhere():
    """The inverse direction is allowed and expected — it has to describe everything."""
    seen = {
        name.split(".")[0]
        for path in sorted((SRC / "audit").rglob("*.py"))
        for _, name in imports_in(path)
    }
    assert {"signals", "research", "sizing", "risk_gate"} <= seen


def test_the_allowed_edges_form_a_dag():
    """No cycles. A cycle means two packages that cannot be reasoned about apart."""
    graph = {
        package: {entry.split(".")[0] for entry in rules.may_import}
        for package, rules in TOPOLOGY.items()
    }
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        if node in done:
            return
        assert node not in visiting, f"import cycle: {' -> '.join(path + (node,))}"
        visiting.add(node)
        for neighbour in sorted(graph.get(node, ())):
            visit(neighbour, path + (node,))
        visiting.discard(node)
        done.add(node)

    for package in sorted(graph):
        visit(package, ())
