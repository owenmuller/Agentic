"""The orchestrator — the loop that connects the other six packages.

Every other package in this system is deliberately unable to reach past its own
neighbours. Scanners can only queue a signal; research can only produce a verdict;
sizing can only propose a number; the gate is the only thing that can approve an order
and the broker adapter is the only thing that can send one. None of them can see the
audit log, because a log the machinery can call into is part of the machinery.

This package is the exception, and it is the only one. It imports everything, wires the
stages together in one direction, and writes each stage's output down. That is why it
is last in the build order and why ``tests/test_topology.py`` names it explicitly: the
privilege of touching more than your neighbours is worth having exactly once, in a
module whose whole job is the seam.

What it does not do
-------------------
It does not decide anything. Confidence comes from research, size comes from the
table, approval comes from the gate, and this package's contribution is to call them
in order and record the answers. If a stage says no, the loop writes that down and
moves to the next signal — there is no path here that reconsiders a rejection.
"""

from orchestrator.bootstrap import Preflight, Startup, preflight, start
from orchestrator.budget import ResearchBudget
from orchestrator.config import OrchestratorConfig, default_orchestrator_path
from orchestrator.loop import TickReport, TradingLoop
from orchestrator.pipeline import (
    PipelineResult,
    PriceSource,
    SignalPipeline,
    WorkingOrder,
)
from orchestrator.state import (
    SessionState,
    position_from_broker,
    replay_deployed_today,
    seed_account_state,
)

__all__ = [
    "OrchestratorConfig",
    "PipelineResult",
    "Preflight",
    "PriceSource",
    "ResearchBudget",
    "SessionState",
    "SignalPipeline",
    "Startup",
    "TickReport",
    "TradingLoop",
    "WorkingOrder",
    "default_orchestrator_path",
    "position_from_broker",
    "preflight",
    "replay_deployed_today",
    "seed_account_state",
    "start",
]
