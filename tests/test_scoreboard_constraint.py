"""The scoreboard constraint (CLAUDE.md, human rulings 2026-10-06), tested the
way the regime scalar is tested: STRUCTURALLY. No threshold, prompt or sizing
input may be a function of realized P&L, distance from any return target, or
elapsed time without a trade — so the modules that compute the hurdle, the
sizing scalars (where the deployment multiplier composes) and the research /
review prompts must import nothing from attribution, spend, P&L accounting or
any target module, and the prompt text must never carry the words that would
tell a model it is behind.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

#: Modules whose inputs the constraint fences.
FENCED = (
    "orchestrator/hurdle.py",
    "orchestrator/scalars.py",
    "orchestrator/deployment.py",  # the deployment multiplier (step 3), when it exists
    "orchestrator/vote.py",  # the self-consistency vote (ruling 2026-10-07, requirement d)
    "research/prompts.py",
    "research/exit_review.py",
    "research/triage.py",
)

#: Where the scoreboard lives. Importing any of these from a fenced module is
#: the violation this test exists to catch.
SCOREBOARD = (
    "audit.attribution",
    "audit.spend",
    "audit.log",
    "orchestrator.target",
    "orchestrator.weekly_target",
    "forward",
)

#: Words a prompt may never carry: they would tell the model how the book is doing.
FORBIDDEN_PROMPT_WORDS = (
    "weekly target",
    "behind target",
    "shortfall",
    "deployment multiplier",
    "realized p&l",
    "realised p&l",
    "our p&l",
    "4-week return",
    "beta-adjusted return",
    "weeks without a trade",
)


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("relative", FENCED)
def test_a_fenced_module_imports_nothing_from_the_scoreboard(relative):
    path = SRC / relative
    if not path.exists():
        pytest.skip(f"{relative} not built yet")
    offending = {
        name for name in _imports_of(path)
        if any(name == root or name.startswith(root + ".") for root in SCOREBOARD)
    }
    assert offending == set(), f"{relative} reads the scoreboard: {sorted(offending)}"


def test_the_hurdle_reads_only_the_opportunity_set():
    from orchestrator.hurdle import Opportunity

    fields = set(Opportunity.__dataclass_fields__)
    assert fields == {"deployed_fraction", "positive_candidates", "open_positions", "target_positions"}


@pytest.mark.parametrize("relative", ("research/prompts.py", "research/exit_review.py", "research/triage.py", "research/add_decision.py"))
def test_prompt_builders_never_name_the_target_or_the_scoreboard(relative):
    path = SRC / relative
    if not path.exists():
        pytest.skip(f"{relative} absent")
    text = path.read_text(encoding="utf-8").lower()
    hits = [word for word in FORBIDDEN_PROMPT_WORDS if word in text]
    assert hits == [], f"{relative} carries scoreboard language: {hits}"


def test_the_claude_md_constraint_is_stated():
    text = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Never With the Scoreboard" in text
    assert "never told about the weekly target" in text
