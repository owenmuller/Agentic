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
from research.triage import TriagePass
from research.research_pass import ResearchPass
from risk_gate.gate import RiskGate
from risk_gate.limits import RiskLimits
from signals.config import SignalsConfig
from signals.records import CredibilityLog, SignalQueue
from signals.scanners import Fetcher, build_scanners
from sizing.engine import SizingEngine
from sizing.selection import OptionSelector

from orchestrator.budget import ResearchBudget
from orchestrator.config import OrchestratorConfig
from orchestrator.exits import ExitEngine, unmanaged_exposure
from orchestrator.loop import TradingLoop
from orchestrator.mechanical import MechanicalEngine
from orchestrator.pipeline import PriceSource, SignalPipeline
from orchestrator.prefilter import ResearchPreFilter
from orchestrator.recovery import recover_unsettled_orders
from orchestrator.registry import SignalRegistry
from orchestrator.scalars import SizingScalars
from orchestrator.sweep import CashSweeper
from orchestrator.state import (
    SessionState,
    replay_deployed_today,
    replay_mechanical_deployed_today,
    seed_account_state,
)

logger = logging.getLogger("orchestrator.bootstrap")


def _sleeve_label(weight) -> str:
    """A 0%-weight sleeve is INACTIVE, not a sleeve earning 0% — say so rather
    than letting an operator read dead capital into a deliberate ruling.

    Shared by startup describe() and the daily health report (ops.health_report),
    so the allocation renders identically wherever an operator looks."""
    if weight <= 0:
        return "0% (inactive)"
    return f"{weight:.0%}"


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
        sleeves = self.gate.limits.portfolio.sleeves
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
                f"sleeves:           equity {_sleeve_label(sleeves.equity)}, "
                f"mechanical {_sleeve_label(sleeves.mechanical)}, "
                f"prediction {_sleeve_label(sleeves.prediction)}",
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
    #: The mechanical arm, or None when its sleeve weight is zero.
    mechanical: object = None

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

    # 4a. Settlement recovery (2026-08-27), BEFORE anything reads the log for
    # state: an order that filled while the previous process died is a position
    # the account holds and the log does not know about, and every replay below
    # keys on fills. Asking the broker is the only way to close that window.
    for line in recover_unsettled_orders(audit, adapter):
        logger.warning("SETTLEMENT RECOVERY: %s", line)

    decisions = audit.decisions()
    account = seed_account_state(
        cash=cash,
        positions=positions,
        session=session,
        deployed_today=replay_deployed_today(decisions, today),
        mechanical_deployed_today=replay_mechanical_deployed_today(
            decisions, today
        ),
        # The audit log alone knows which sleeve owns what: split the broker's
        # per-symbol holdings so the mechanical sleeve wakes up holding its own,
        # and the cash-management sleeve its parked ETF (ruling 2026-09-02).
        mechanical_open=audit.mechanical_open_positions(),
        cash_management_open=audit.strategy_open_positions("cash_sweep"),
        today=today,
        account_type=orchestrator_config.account_type,
    )
    gate = RiskGate(limits, account, now_fn)
    budget = ResearchBudget(
        orchestrator_config.max_research_passes_per_day,
        clock=now_fn,
        spent=audit.research_passes_on(today),
        day=today,
        review_reserve_fraction=orchestrator_config.review_budget_reserve_fraction,
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
    cost_warn_sink: Optional[Callable[[str], None]] = None,
    error_sink: Optional[Callable[[str], None]] = None,
    classify_sink: Optional[Callable[[str], None]] = None,
    mechanical_sink: Optional[Callable[[str], None]] = None,
    options_chain=None,
    vix_close: Optional[Callable] = None,
    atr_fraction: Optional[Callable] = None,
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
    # Persisted next to the audit log (ruling 2026-08-26): classification
    # discards are part of the funnel's record, not process-lifetime trivia.
    credibility_log = CredibilityLog(
        path=checks.audit.path.parent / "credibility.jsonl"
    )
    scanners = build_scanners(
        checks.signals_config,
        fetcher,
        queue,
        checks.clock,
        credibility_log,
        classify_sink,
    )
    client = llm_client or AnthropicResearchClient(checks.research_config)
    credibility = CredibilityTracker(credibility_log)
    # Per-source verification tiers (cost architecture 2026-08-25), validated
    # HERE so a typo in signals.yaml is a startup failure, not an upstream_error
    # at 09:31. Two-stage runs whenever research.yaml configures a screen.
    source_tiers: dict[str, str] = {}
    for klass in checks.signals_config.classes.values():
        for source in klass.sources:
            if source.research_tier:
                checks.research_config.tier_for(source.research_tier)  # raises on typo
                source_tiers[source.id] = source.research_tier
    # The convergence registry (ruling 2026-09-01): seeded from the log so a
    # restart remembers last week's cluster, consulted by the loop for dispatch
    # ordering and by the research pass for fenced context. Ordering and context
    # only — it can never touch a cap, a size, or the gate.
    registry = SignalRegistry(
        checks.orchestrator_config.convergence, checks.clock
    )
    registry.seed(checks.audit.records())
    screen_config = checks.research_config.screen
    research = ResearchPass(
        client,
        credibility,
        checks.clock,
        market_context=market_context,
        convergence_context=registry.context_for,
        source_tiers=source_tiers,
        screen_graduation=(
            screen_config.graduation_confidence if screen_config is not None else None
        ),
    )
    # The triage gate: only when configured AND the client can actually run one.
    # Fakes without a triage method simply have no gate — fail-open by absence.
    triage = None
    if checks.research_config.triage is not None and hasattr(client, "triage"):
        triage = TriagePass(client)
    from datetime import time as _time

    from orchestrator.ops import CostMeter

    now = checks.clock()
    day_start = datetime.combine(now.date(), _time.min, tzinfo=timezone.utc)
    cost_meter = CostMeter(
        checks.orchestrator_config.daily_cost_warning_usd,
        warn_sink=cost_warn_sink,
        clock=checks.clock,
        # Seeded from the log: a restart cannot reset the tripwire.
        initial_spent=checks.audit.research_cost_between(day_start),
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
        cost_sink=cost_meter.add,
        option_prices=(options_chain.option_mid if options_chain is not None else None),
        close_before_expiry_days=(
            checks.gate.limits.options_selection.close_before_expiry_days
        ),
        # ATR-stopped positions trigger their adverse review at this fraction
        # of their OWN stop distance (ruling 2026-09-02).
        trigger_down_of_stop=(
            checks.orchestrator_config.atr_sizing.trigger_down_of_stop
        ),
    )
    # Positions opened by earlier runs, rebuilt from the log with stops re-armed. Part
    # of the replay step in spirit, but it needs the wired engine, so it runs here.
    # Marks first, replay second: the ratchet re-arms from the persisted
    # high-water mark during replay, and a position that was riding a trailing
    # stop must not come back on its original one.
    exits.seed_marks(checks.session.position_marks)
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
    # Options expression (2026-08-24): the selector exists only when a chain
    # source does — no chain, no options, equity-only pipeline as before.
    option_selector = (
        OptionSelector(checks.gate.limits.options_selection)
        if options_chain is not None
        else None
    )
    # Post-table risk scalars (rulings 2026-09-01/02): drawdown ladder x regime
    # at ONE composition point. The ladder reads the gate's own drawdown — the
    # kill switch's number, through a read-only accessor; the regime leg runs
    # only when the operator wired a VIX source (production passes CBOE, tests
    # pass nothing and get x1.0 regime with the ladder still armed).
    scalars = (
        SizingScalars(
            checks.orchestrator_config.risk_scalars,
            drawdown=checks.gate.drawdown,
            vix_close=vix_close,
            clock=checks.clock,
        )
        if checks.orchestrator_config.risk_scalars.enabled
        else None
    )
    pipeline = SignalPipeline(
        research=research,
        triage=triage,
        sizing=SizingEngine(checks.limits),
        gate=checks.gate,
        adapter=checks.adapter,
        audit=checks.audit,
        prices=prices,
        id_factory=id_factory,
        fill_sink=exits.track_fill,
        convergence_snapshot=registry.snapshot_for,
        scalars=scalars,
        # ATR sizing (ruling 2026-09-02): production wires AtrSource over the
        # daily bars; a harness that wires nothing runs the fixed-15% regime.
        atr_fraction=atr_fraction,
        atr_config=checks.orchestrator_config.atr_sizing,
        # Execution fidelity (ruling 2026-09-02): the production price source
        # can quote a spread; a harness's stub usually cannot, and None is fine.
        spread_pct=getattr(prices, "spread_pct", None),
        options_chain=options_chain,
        option_selector=option_selector,
        clock=checks.clock,
        probation_sources={
            source.id
            for klass in checks.signals_config.classes.values()
            for source in klass.sources
            if source.probation
        },
    )
    # ONE prefilter instance, shared by the judged loop and the mechanical
    # engine — the funnels are identical by construction (ruling 2026-08-27:
    # the experiment varies only judgment and exits).
    research_prefilter = ResearchPreFilter.from_config(checks.signals_config)
    # The mechanical arm exists only while its sleeve has weight: setting the
    # weight to zero in risk_limits.yaml switches the whole experiment off.
    mechanical = None
    if checks.limits.portfolio.sleeves.mechanical > 0:
        mechanical = MechanicalEngine(
            gate=checks.gate,
            adapter=checks.adapter,
            audit=checks.audit,
            prices=prices,
            limits=checks.limits,
            prefilter=research_prefilter,
            clock=checks.clock,
            note=mechanical_sink,
            id_factory=id_factory,
            virtual_cash=checks.session.mechanical_virtual_cash,
            high_water_mark=checks.session.mechanical_high_water_mark,
            halted=checks.session.mechanical_halted,
        )
        mechanical.replay(checks.audit.mechanical_trails())

    # The idle-cash yield sweeper (ruling 2026-09-02): deterministic, config-
    # switched, replayed from its own trails. Never buying power, never alpha.
    sweeper = None
    if checks.limits.cash_management.enabled:
        import uuid as _uuid

        sweeper = CashSweeper(
            gate=checks.gate,
            adapter=checks.adapter,
            audit=checks.audit,
            prices=prices,
            config=checks.limits.cash_management,
            clock=checks.clock,
            id_factory=id_factory or (lambda: _uuid.uuid4().hex[:16]),
            note=mechanical_sink,
        )
        # Replay only when sweep history exists: trails() assembly is the
        # expensive part of startup, and a log with no sweeps has no lots.
        if any(
            d.sizing.strategy == "cash_sweep" for d in checks.audit.decisions()
        ):
            sweeper.replay(checks.audit.trails())

    loop = TradingLoop(
        scanners=scanners,
        queue=queue,
        pipeline=pipeline,
        exits=exits,
        prefilter=research_prefilter,
        registry=registry,
        mechanical=mechanical,
        sweeper=sweeper,
        cost_meter=cost_meter,
        error_sink=error_sink,
        source_caps={
            source.id: source.daily_research_cap
            for klass in checks.signals_config.classes.values()
            for source in klass.sources
            if source.daily_research_cap is not None
        },
        source_passes=checks.audit.research_passes_by_source_on(now.date()),
        source_pass_day=now.date(),
        previously_capped=checks.audit.capped_external_ids(),
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
        loop=loop,
        queue=queue,
        exits=exits,
        credibility=credibility,
        mechanical=mechanical,
        preflight=checks,
    )
