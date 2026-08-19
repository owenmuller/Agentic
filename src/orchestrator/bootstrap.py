"""Startup, in the order the checks have to happen in.

The sequence is not arbitrary and it is not reorderable:

  1. ``require_paper_or_confirmed_live()``. Before configuration, before the broker,
     before anything is constructed. Constraint #4 says a process that believes it is
     live and is not corrupts every result that follows, and the cheapest moment to
     refuse is the one before any work has been done. Nothing above this line has side
     effects, which is why it is the first statement in the function.
  2. Configuration. All four files, strictly validated, none of them defaulted. A loop
     running on assumed caps is worse than a loop that will not start.
  3. Broker connectivity. Reached before any state is rebuilt, because the broker's
     answer *is* part of that state — cash and positions come from it, not from a
     replay. A broker that cannot be reached is a startup failure.
  4. Replay. The daily deployment total and the research budget from the audit log; the
     high-water mark and the kill switch from session state. This is the step that stops
     a restart forgetting a halt or refilling a spent budget — see ``orchestrator.state``.

Only then is a gate constructed, and only then can an order be approved.

``preflight()`` runs steps 1-4 and stops. ``start()`` runs them and wires a loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from audit.log import AuditLog, default_data_dir
from execution.alpaca import AlpacaAdapter
from execution.base import BrokerAdapter, BrokerError, BrokerPermissions
from execution.environment import load_environment, require_paper_or_confirmed_live
from research.client import AnthropicResearchClient, LLMClient
from research.config import ResearchConfig
from research.credibility import CredibilityTracker
from research.exit_review import ExitReviewPass
from research.research_pass import ResearchPass
from risk_gate.gate import RiskGate
from risk_gate.limits import RiskLimits
from signals.config import SignalsConfig
from signals.records import CredibilityLog, SignalQueue
from signals.scanners import Fetcher, build_scanners
from sizing.engine import SizingEngine

from orchestrator.budget import ResearchBudget
from orchestrator.config import OrchestratorConfig
from orchestrator.exits import ExitEngine, unmanaged_exposure
from orchestrator.loop import TradingLoop
from orchestrator.pipeline import PriceSource, SignalPipeline
from orchestrator.prefilter import ResearchPreFilter
from orchestrator.state import SessionState, replay_deployed_today, seed_account_state

logger = logging.getLogger("orchestrator.bootstrap")


@dataclass(frozen=True, slots=True)
class Preflight:
    """Steps 1-4: the checks and the replay, with no loop built.

    Separated so an operator can run the part that changes nothing — is the mode right,
    do the configs parse, is the broker reachable, what state does the log say we are
    in — without starting something that trades.
    """

    paper: bool
    gate: RiskGate
    audit: AuditLog
    session: SessionState
    budget: ResearchBudget
    adapter: BrokerAdapter
    #: What the broker account is configured to allow, checked every startup.
    permissions: BrokerPermissions
    limits: RiskLimits
    signals_config: SignalsConfig
    research_config: ResearchConfig
    orchestrator_config: OrchestratorConfig
    clock: Callable[[], datetime]

    def describe(self) -> str:
        """Operator-readable summary of the state that was reconstructed."""
        state = self.gate.state
        halt = (
            "TRIPPED - opening orders halted"
            if self.gate.kill_switch_tripped
            else "clear"
        )
        return "\n".join(
            [
                f"mode:              {'PAPER' if self.paper else 'LIVE - REAL MONEY'}",
                f"cash:              {state.cash}",
                f"positions:         {len(state.positions)}",
                f"NAV:               {state.nav}",
                f"high-water mark:   {state.high_water_mark}",
                f"drawdown:          {state.drawdown():.2%}",
                f"kill switch:       {halt}",
                f"deployed today:    {state.deployed_today}",
                f"research budget:   {self.budget.spent} of "
                f"{self.budget.max_per_day} spent for {self.budget.day}",
                f"broker permits:    {self.permissions.describe()}"
                + (
                    "  [EXCEEDS what the code allows - see startup warnings]"
                    if self.permissions.excess_permissions()
                    else "  [matched to the system]"
                ),
            ]
        )


@dataclass(frozen=True, slots=True)
class Startup:
    """A wired loop, plus the checks it was built from."""

    loop: TradingLoop
    queue: SignalQueue
    #: Open-position tracking and both exit layers. Shared with the loop.
    exits: ExitEngine
    #: The one credibility tracker, shared by the entry pass (context), the review
    #: layer's outcomes, and the audit log (hit-rate resolution).
    credibility: CredibilityTracker
    preflight: Preflight

    @property
    def gate(self) -> RiskGate:
        return self.preflight.gate

    @property
    def audit(self) -> AuditLog:
        return self.preflight.audit

    @property
    def session(self) -> SessionState:
        return self.preflight.session

    @property
    def budget(self) -> ResearchBudget:
        return self.preflight.budget

    @property
    def adapter(self) -> BrokerAdapter:
        return self.preflight.adapter

    @property
    def paper(self) -> bool:
        return self.preflight.paper

    def describe(self) -> str:
        return self.preflight.describe()


def preflight(
    *,
    adapter: Optional[BrokerAdapter] = None,
    clock: Optional[Callable[[], datetime]] = None,
    data_dir: Optional[Path] = None,
    limits: Optional[RiskLimits] = None,
    signals_config: Optional[SignalsConfig] = None,
    research_config: Optional[ResearchConfig] = None,
    orchestrator_config: Optional[OrchestratorConfig] = None,
    id_factory: Optional[Callable[[], str]] = None,
) -> Preflight:
    """Run the checks and the replay. Builds a gate; starts nothing."""
    # 1. Constraint #4, before anything else can happen. This raises rather than
    #    falling back to paper, and it raises before a config file has been opened.
    load_environment()
    paper = require_paper_or_confirmed_live()

    # 2. Configuration.
    limits = limits or RiskLimits.load()
    signals_config = signals_config or SignalsConfig.load()
    research_config = research_config or ResearchConfig.load()
    orchestrator_config = orchestrator_config or OrchestratorConfig.load()

    # 3. The broker. Cash and positions are read, never reconstructed.
    adapter = adapter or AlpacaAdapter()
    cash = adapter.get_buying_power()
    positions = adapter.get_positions()
    logger.info("broker reachable: %s cash across %d positions", cash, len(positions))

    # What the ACCOUNT permits, vs what the code can express. The system's real
    # enforcement is structural — the order schema cannot represent a write, a
    # short, or a margin buy — so an over-permissive account is not unsafe, but on a
    # live account the broker-side configuration is a defence-in-depth layer, and a
    # missing layer gets said out loud on day one, not found in an incident.
    permissions = adapter.permissions()
    logger.info("broker account permits: %s", permissions.describe())
    for finding in permissions.excess_permissions():
        logger.warning(
            "ACCOUNT PERMITS MORE THAN THE CODE ALLOWS: %s. The schema's "
            "unrepresentability is the enforcement; on a live account, tighten the "
            "broker-side setting too.",
            finding,
        )
    if not permissions.can_trade_options:
        logger.warning(
            "account options level %d cannot BUY calls or puts; option proposals "
            "will be refused by the broker (the system needs level %d)",
            permissions.options_level,
            BrokerPermissions.SYSTEM_NEEDS_OPTIONS_LEVEL,
        )

    # 4. Replay.
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    today = now_fn().date()
    directory = data_dir or default_data_dir()
    audit = AuditLog(
        path=directory / "audit.jsonl", clock=now_fn, id_factory=id_factory
    )
    session = SessionState.load(directory / "session_state.json")

    account = seed_account_state(
        cash=cash,
        positions=positions,
        session=session,
        deployed_today=replay_deployed_today(audit.decisions(), today),
        today=today,
        account_type=orchestrator_config.account_type,
    )
    gate = RiskGate(limits, account, now_fn)
    budget = ResearchBudget(
        orchestrator_config.max_research_passes_per_day,
        clock=now_fn,
        spent=audit.research_passes_on(today),
        day=today,
    )

    if gate.kill_switch_tripped:
        logger.warning(
            "starting HALTED: the kill switch was tripped and has not been reset. "
            "Opening orders will be refused; risk-reducing closes still pass. "
            "Resuming is a manual human decision."
        )

    return Preflight(
        paper=paper,
        gate=gate,
        audit=audit,
        session=session,
        budget=budget,
        adapter=adapter,
        permissions=permissions,
        limits=limits,
        signals_config=signals_config,
        research_config=research_config,
        orchestrator_config=orchestrator_config,
        clock=now_fn,
    )


def start(
    *,
    fetcher: Fetcher,
    prices: PriceSource,
    llm_client: Optional[LLMClient] = None,
    checks: Optional[Preflight] = None,
    sleeper: Optional[Callable[[float], None]] = None,
    id_factory: Optional[Callable[[], str]] = None,
    market_context: Optional[Callable] = None,
    **preflight_kwargs: object,
) -> Startup:
    """Run the startup sequence and return a loop ready to tick.

    ``fetcher`` and ``prices`` have no defaults on purpose. Neither has a production
    implementation yet — the signal feeds need credentials this machine does not hold,
    and the market-data client is an honest seam rather than a stub (see
    ``orchestrator.pipeline.PriceSource``). Requiring them makes that visible at the
    call site rather than at 09:30.
    """
    checks = checks or preflight(id_factory=id_factory, **preflight_kwargs)  # type: ignore[arg-type]

    # Orphan sweep. An order left working at the broker by a dead process is exposure
    # no gate in THIS process can account for: its reservation lived in an
    # ApprovedOrder that died with the process, unforgeably. Cancel them all before
    # trading starts — anything that already filled is in the positions the gate was
    # just seeded from, and anything still resting must not be allowed to fill
    # unreserved. A clean start sweeps nothing.
    for orphan_id in checks.adapter.open_orders():
        logger.warning(
            "cancelling orphaned order %s left working at the broker by an earlier "
            "process; no live reservation covers it",
            orphan_id,
        )
        try:
            checks.adapter.cancel_order(orphan_id)
        except BrokerError as error:
            logger.error("could not cancel orphaned order %s: %s", orphan_id, error)

    # The queue's dedup is seeded with everything the log says was already
    # researched, so a restart cannot re-buy a pass whichever fetcher re-emits the
    # signal. This is the layer that covers mirrored content, whose external ids are
    # normalised content keys rather than any fetcher's native ids.
    queue = SignalQueue(seen=checks.audit.researched_external_ids())
    credibility_log = CredibilityLog()
    scanners = build_scanners(
        checks.signals_config, fetcher, queue, checks.clock, credibility_log
    )
    client = llm_client or AnthropicResearchClient(checks.research_config)
    credibility = CredibilityTracker(credibility_log)
    research = ResearchPass(
        client, credibility, checks.clock, market_context=market_context
    )
    exits = ExitEngine(
        gate=checks.gate,
        adapter=checks.adapter,
        audit=checks.audit,
        prices=prices,
        review_pass=ExitReviewPass(client, checks.clock),
        budget=checks.budget,
        config=checks.orchestrator_config.exits,
        clock=checks.clock,
        credibility=credibility,
    )
    # Positions opened by earlier runs, rebuilt from the log with stops re-armed. Part
    # of the replay step in spirit, but it needs the wired engine, so it runs here.
    exits.replay(checks.audit.trails())
    for symbol, quantity in sorted(unmanaged_exposure(checks.gate, exits.tracked).items()):
        logger.warning(
            "UNMANAGED POSITION: %d units of %s are held at the broker with no audit "
            "trail behind them — no stops are armed. A crashed process may have "
            "filled without recording, or the account was traded manually. Needs a "
            "human: close it, or accept that it is unprotected.",
            quantity,
            symbol,
        )
    pipeline = SignalPipeline(
        research=research,
        sizing=SizingEngine(checks.limits),
        gate=checks.gate,
        adapter=checks.adapter,
        audit=checks.audit,
        prices=prices,
        id_factory=id_factory,
        fill_sink=exits.track_fill,
    )
    loop = TradingLoop(
        scanners=scanners,
        queue=queue,
        pipeline=pipeline,
        exits=exits,
        prefilter=ResearchPreFilter.from_config(checks.signals_config),
        budget=checks.budget,
        session=checks.session,
        gate=checks.gate,
        adapter=checks.adapter,
        audit=checks.audit,
        tick_interval_seconds=checks.orchestrator_config.tick_interval_seconds,
        clock=checks.clock,
        sleeper=sleeper,
    )
    return Startup(
        loop=loop, queue=queue, exits=exits, credibility=credibility, preflight=checks
    )
