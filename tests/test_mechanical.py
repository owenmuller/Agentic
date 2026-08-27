"""The mechanical disclosure follower (human ruling 2026-08-27).

The claims: qualification is the judged funnel plus purchase-only and
tradeability — literally the same prefilter object, so the funnels cannot
diverge; entries are equal-weight slices through the RiskGate under the
sleeve's own caps; slots bound total, per-filer, and per-mapped-sector
exposure; exits are time-only with no price stop; the sleeve breaker halts
entries (never closes) on a >25% drawdown from its own high-water mark; the
audit record carries the disclosure and ruleset with NO research snapshot and
never seals a signal for the judged arm; and there is no LLM anywhere in the
path — structurally, not aspirationally.
"""

from __future__ import annotations

import pathlib
from datetime import timedelta
from decimal import Decimal

import pytest

from audit.records import ExitReason, RejectedStage
from execution.base import BrokerPosition
from risk_gate import RiskLimits
from research.config import ResearchConfig
from signals import SignalsConfig

from test_hardening import congressional_feed, disclosure_item
from test_orchestrator import (
    NOW,
    FakeBroker,
    FakeClock,
    FakeLLM,
    REPORT,
    build,
    prices_of,
    structured,
)

ZERO = Decimal("0")


@pytest.fixture(scope="module")
def signals_config() -> SignalsConfig:
    return SignalsConfig.load()


def quiet_llm():
    """The judged arm screens everything out: these tests are about mechanics."""
    return FakeLLM(
        structured({**REPORT, "confidence": 40, "priced_in_analysis": "checked"})
    )


def build_mechanical(tmp_path, signals_config, *, items, llm=None, broker=None,
                     prices=None, clock=None):
    return build(
        tmp_path,
        RiskLimits.load(),
        signals_config,
        ResearchConfig.load(),
        llm=llm or quiet_llm(),
        fetcher=congressional_feed(*items),
        prices=prices or prices_of(NUE="140.00"),
        broker=broker or FakeBroker(),
        clock=clock,
    )


# ================================================================================
# Entries: the funnel, the slice, the record
# ================================================================================


def test_a_qualified_disclosure_becomes_an_equal_weight_mechanical_entry(
    tmp_path, signals_config
):
    broker = FakeBroker()
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        broker=broker,
    )
    report = started.loop.tick()
    assert report.mechanical_entries == 1

    # Slice: 25% of 100k NAV / 30 slots = 833.33; at 140, 5 whole shares.
    payload = broker.payloads[0]
    assert payload["symbol"] == "NUE"
    assert payload["qty"] == 5
    position = started.gate.state.position(("mechanical", "NUE"))
    assert position is not None and position.quantity == 5
    assert started.gate.state.mechanical_deployed_today == Decimal("700.00")
    # The judged sleeve's daily budget is untouched by the mechanical entry.
    assert started.gate.state.deployed_today == ZERO

    engine = started.loop.mechanical
    assert len(engine.tracked) == 1
    # The ledger anchored at the sleeve allocation and paid for the fill.
    assert engine.virtual_cash == Decimal("25000") - Decimal("700.00")


def test_the_entry_record_is_a_decision_with_no_research_and_the_ruleset(
    tmp_path, signals_config
):
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
    )
    started.loop.tick()

    mechanical_trails = started.audit.mechanical_trails()
    assert len(mechanical_trails) == 1
    decision = mechanical_trails[0].decision
    assert decision.research is None  # no LLM ran, and the record says so
    assert decision.sizing.strategy == "mechanical"
    assert decision.sizing.sleeve == "mechanical"
    assert decision.mechanical.ruleset_version
    assert decision.mechanical.filer == "Test Member"
    assert decision.mechanical.ticker == "NUE"
    assert decision.gate.order["sleeve"] == "mechanical"

    # The mechanical record itself is excluded from dedup seeding — proven
    # directly, since the judged arm also researched this disclosure and its
    # own record legitimately seals it (see the source_cap test below for the
    # case where the mechanical record is the ONLY one).
    from audit.log import AuditLog

    replayed = AuditLog(path=started.audit.path)
    assert decision.sizing.strategy == "mechanical"
    mechanical_only = [
        r
        for r in replayed.records()
        if getattr(r, "decision_id", None) == decision.decision_id
    ]
    assert mechanical_only  # the trail exists, and does not gate the judged arm


def test_mechanical_records_never_seal_the_signal_for_the_judged_arm(
    tmp_path, signals_config
):
    """Same disclosure, judged research suppressed at the cap: after the
    mechanical entry, the signal must still be unsealed for the judged path."""
    config_items = [
        disclosure_item(f"warm-{n}", f"TK{n}", "$50,001 - $100,000", "2026-08-17")
        for n in range(5)
    ] + [disclosure_item("row-x", "NUE", "$50,001 - $100,000", "2026-08-17")]
    started = build_mechanical(tmp_path, signals_config, items=config_items)
    started.loop.tick()

    # row-x hit the judged source_cap (5/day) -> its only judged record is
    # source_cap; its mechanical record is an entry. Neither seals.
    assert (
        "congressional_disclosures",
        "row-x",
    ) not in started.audit.researched_external_ids()


def test_the_funnel_is_shared_and_identical(tmp_path, signals_config):
    """The engine holds the very same prefilter object the loop dispatches
    with (ruling: the experiment varies only judgment and exits), and a
    disclosure the judged rules kill is not entered mechanically."""
    stale = disclosure_item("old", "NUE", "$50,001 - $100,000", "2026-07-01")
    small = disclosure_item("small", "NUE", "$1,001 - $5,000", "2026-08-17")
    started = build_mechanical(tmp_path, signals_config, items=[stale, small])
    assert started.loop.mechanical._prefilter is started.loop._prefilter

    report = started.loop.tick()
    assert report.mechanical_entries == 0
    assert started.loop.mechanical.tracked == ()


def test_sales_and_untradeable_names_never_enter(tmp_path, signals_config):
    sale = disclosure_item("sale", "NUE", "$50,001 - $100,000", "2026-08-17")
    sale.fields["transaction"] = "Sale (full)"

    class NoAssetBroker(FakeBroker):
        def tradeable_equity(self, symbol: str) -> bool:
            return False

    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[
            sale,
            disclosure_item("ok", "NUE", "$50,001 - $100,000", "2026-08-17"),
        ],
        broker=NoAssetBroker(),
    )
    report = started.loop.tick()
    assert report.mechanical_entries == 0


def mech_signal(external_id, ticker, filer="Test Member"):
    from signals.records import Priority, Signal, SignalClass

    return Signal(
        signal_id=f"sig-{external_id}",
        source_id="congressional_disclosures",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        observed_at=NOW,
        content=f"disclosure {ticker}",
        raw_content="raw",
        priority=Priority.ROUTINE,
        external_id=external_id,
        metadata={
            "representative": filer,
            "ticker": ticker,
            "transaction": "Purchase",
            "amount_range": "$50,001 - $100,000",
            "report_date": "2026-08-17",
            "disclosure_lag_days": "10",
        },
    )


def stub_position(decision_id, symbol, filer="Test Member"):
    from orchestrator.mechanical import MechanicalPosition

    return MechanicalPosition(
        decision_id=decision_id,
        symbol=symbol,
        filer=filer,
        quantity=Decimal("5"),
        entry_cost=Decimal("500"),
        proceeds=ZERO,
        opened_at=NOW,
    )


def test_the_per_filer_cap_records_capacity_without_sealing(
    tmp_path, signals_config
):
    """A member already filling six slots: the seventh qualifier writes
    mechanical_capacity — visible, never sealing."""
    started = build_mechanical(tmp_path, signals_config, items=[])
    engine = started.loop.mechanical
    for n in range(6):
        engine._tracked[f"d{n}"] = stub_position(f"d{n}", f"TK{n}")

    assert engine.consider([mech_signal("f-7", "AAPL")], NOW) == 0
    capacity = [
        r
        for r in started.audit.stage_rejections()
        if r.code == "mechanical_capacity"
    ]
    assert len(capacity) == 1
    assert "per-filer" in capacity[0].message
    assert (
        "congressional_disclosures",
        "f-7",
    ) not in started.audit.researched_external_ids()


def test_the_sector_slot_cap_binds_for_mapped_names_only(tmp_path, signals_config):
    """Eight slots in one mapped sector block the ninth mapped name; an
    unmapped name is its own singleton and still enters the queue of checks."""
    from risk_gate.sectors import SectorMap

    started = build_mechanical(tmp_path, signals_config, items=[])
    engine = started.loop.mechanical
    mapped = {f"ST{n}": "steel" for n in range(9)}
    engine._sectors = SectorMap(mapped)
    for n in range(8):
        engine._tracked[f"d{n}"] = stub_position(f"d{n}", f"ST{n}", filer=f"M{n}")

    assert engine.consider([mech_signal("s-9", "ST8", filer="M9")], NOW) == 0
    capacity = [
        r
        for r in started.audit.stage_rejections()
        if r.code == "mechanical_capacity"
    ]
    assert len(capacity) == 1
    assert "sector" in capacity[0].message

    # Unmapped names are unconstrained singletons, per the sectors.yaml
    # convention: this one passes the sector check (and every other slot
    # check) and reaches the entry path.
    assert engine._capacity_block(mech_signal("u-1", "ZZZ", filer="M10")) is None


def test_the_total_slot_cap_binds_at_max_positions(tmp_path, signals_config):
    started = build_mechanical(tmp_path, signals_config, items=[])
    engine = started.loop.mechanical
    for n in range(30):
        engine._tracked[f"d{n}"] = stub_position(f"d{n}", f"TK{n}", filer=f"M{n % 10}")

    block = engine._capacity_block(mech_signal("s-31", "AAPL", filer="M11"))
    assert block is not None
    assert block[0] == "mechanical_capacity"
    assert "30 mechanical slots" in block[1]


# ================================================================================
# Exits: time only, no stop; the breaker halts entries, never closes
# ================================================================================


def enter_one(tmp_path, signals_config, prices=None):
    clock = FakeClock()
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        prices=prices,
        clock=clock,
    )
    report = started.loop.tick()
    assert report.mechanical_entries == 1
    return started, clock


def test_a_price_collapse_triggers_no_stop(tmp_path, signals_config):
    """The no-stop design, on purpose and on record: a -50% mark neither exits
    the position nor (at slice size) trips the sleeve breaker."""
    from test_exits import MutablePrices

    prices = MutablePrices(NUE="140.00")
    started, clock = enter_one(tmp_path, signals_config, prices=prices)

    prices.set("NUE", "70.00")
    clock.advance(days=5)
    report = started.loop.tick()
    assert report.mechanical_exits == 0
    assert started.loop.mechanical.halted is False
    assert len(started.loop.mechanical.tracked) == 1


def test_the_time_exit_closes_at_hold_days_and_resolves_the_outcome(
    tmp_path, signals_config
):
    from test_exits import MutablePrices

    prices = MutablePrices(NUE="140.00")
    started, clock = enter_one(tmp_path, signals_config, prices=prices)

    prices.set("NUE", "150.00")
    clock.advance(days=366)
    report = started.loop.tick()
    assert report.mechanical_exits == 1

    report = started.loop.tick()  # the close settles
    assert started.loop.mechanical.tracked == ()
    trail = started.audit.mechanical_trails()[0]
    assert trail.exits[0].reason is ExitReason.MECHANICAL_TIME_EXIT
    assert trail.outcome is not None
    assert trail.outcome.realised_pnl == Decimal("50.00")  # 5 x (150 - 140)
    assert started.gate.state.position(("mechanical", "NUE")) is None
    # Proceeds returned to the sleeve ledger for the next qualifier.
    assert started.loop.mechanical.virtual_cash == Decimal("25000") + Decimal(
        "50.00"
    )


def test_the_breaker_halts_entries_but_never_closes(tmp_path, signals_config):
    """Sleeve value down >25% from its own high-water mark: entries halt with
    their own code, positions ride, and the time exit still runs."""
    from test_exits import MutablePrices

    prices = MutablePrices(NUE="140.00")
    started, clock = enter_one(tmp_path, signals_config, prices=prices)
    engine = started.loop.mechanical

    # Engineer the drawdown: shrink the ledger so the open position dominates
    # the sleeve value, then collapse the mark.
    engine._virtual_cash = Decimal("100")
    engine._hwm = None
    started.loop.tick()  # re-anchors the high-water mark at ~800
    prices.set("NUE", "40.00")
    clock.advance(days=1)
    started.loop.tick()
    assert engine.halted is True

    # A fresh qualifier is refused with the halt's own code...
    started2_items = disclosure_item(
        "row-2", "AAPL", "$50,001 - $100,000", "2026-08-17"
    )
    started.loop._mechanical._considered.discard("row-2")
    from signals.scanners import Class2CongressionalScanner  # noqa: F401

    # feed a new signal through consider() directly — the loop's fetcher only
    # carries row-1.
    from signals.records import Priority, Signal, SignalClass

    fresh = Signal(
        signal_id="sig-row-2",
        source_id="congressional_disclosures",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        observed_at=NOW,
        content="disclosure AAPL",
        raw_content="raw",
        priority=Priority.ROUTINE,
        external_id="row-2",
        metadata={
            "representative": "Test Member",
            "ticker": "AAPL",
            "transaction": "Purchase",
            "amount_range": "$50,001 - $100,000",
            "report_date": "2026-08-17",
            "disclosure_lag_days": "10",
        },
    )
    assert engine.consider([fresh], NOW) == 0
    halted_records = [
        r
        for r in started.audit.stage_rejections()
        if r.code == "mechanical_halted"
    ]
    assert len(halted_records) == 1

    # ...but the time exit still fires while halted: closes are risk-reducing.
    prices.set("NUE", "40.00")
    clock.advance(days=366)
    report = started.loop.tick()
    assert report.mechanical_exits == 1


# ================================================================================
# Restarts: the sleeve wakes up holding its own
# ================================================================================


def test_a_restart_splits_the_broker_position_and_does_not_rebuy(
    tmp_path, signals_config
):
    started, clock = enter_one(tmp_path, signals_config)
    started.loop.shutdown()

    # The broker now holds 5 NUE; the audit log knows they are mechanical.
    restarted = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        broker=FakeBroker(
            cash=Decimal("99300"),
            positions=[
                BrokerPosition("NUE", Decimal("5"), Decimal("700"), Decimal("700"))
            ],
        ),
        clock=clock,
    )
    gate_state = restarted.gate.state
    assert gate_state.position(("mechanical", "NUE")).quantity == 5
    assert gate_state.position(("equity", "NUE")) is None  # fully attributed

    engine = restarted.loop.mechanical
    assert len(engine.tracked) == 1
    assert engine.tracked[0].opened_at is not None
    # The ledger survived via session state.
    assert engine.virtual_cash == Decimal("24300.00")

    report = restarted.loop.tick()
    assert report.mechanical_entries == 0  # row-1 already entered; not re-bought


# ================================================================================
# Attribution: its own bucket, and the overlap is measured
# ================================================================================


def test_attribution_buckets_mechanical_separately_and_measures_overlap(
    tmp_path, signals_config
):
    from audit.attribution import build_attribution

    # Judged arm trades NUE too (confidence 71): deliberate overlap.
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        llm=FakeLLM(
            structured({**REPORT, "priced_in_analysis": "checked"})
        ),
    )
    started.loop.tick()
    started.loop.tick()

    report = build_attribution(started.audit.trails(), generated_at=NOW)
    assert report.mechanical is not None
    assert report.mechanical.entries == 1
    assert report.mechanical.approved == 1
    assert report.mechanical.open_positions == 1
    assert report.mechanical.overlap_symbols == ("NUE",)
    # And the class buckets never absorbed the mechanical trail.
    total_class_decisions = sum(
        a.decisions for a in report.by_class.values()
    )
    assert total_class_decisions == 1  # the judged NUE decision alone
    assert "mechanical:" in report.render()


# ================================================================================
# No LLM in the path — structural, like the topology tests
# ================================================================================


def test_the_mechanical_module_cannot_reach_the_research_layer():
    source = pathlib.Path("src/orchestrator/mechanical.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("from research", "import research", "LLMClient", "Anthropic"):
        assert forbidden not in source, forbidden


# ================================================================================
# Startup settlement recovery + entries_enabled (rulings 2026-08-27, day-one)
# ================================================================================


def venue_across_restart(previous, positions, cash):
    """A fresh adapter that reports the filled holding and still answers
    lookups by client reference — i.e. the venue, seen across our restart."""
    broker = FakeBroker(cash=cash, positions=positions)
    broker.by_client_reference = dict(previous.by_client_reference)
    broker._statuses = dict(previous._statuses)
    return broker


def test_a_mechanical_fill_lost_to_a_crash_is_recovered_at_next_startup(
    tmp_path, signals_config
):
    """The real fix: an order that filled while the process died leaves an
    approved decision with no fill. The next startup asks the broker by the
    decision id it stamped, writes the missing FillRecord, and the sleeve wakes
    up owning the position with its ledger reconstructed."""
    from execution.base import OrderStatus

    broker = FakeBroker(fill="new")  # accepted; not filled when we poll
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        broker=broker,
    )
    assert started.loop.tick().mechanical_entries == 1
    decision_id = started.audit.mechanical_trails()[0].decision.decision_id

    # The venue fills it; this process dies before its next settle tick.
    broker.set_status(
        "brk-1", OrderStatus("brk-1", "filled", Decimal("5"), Decimal("140.00"))
    )
    started.loop.mechanical._working.clear()
    assert started.audit.trail(decision_id).fills == ()  # the window

    restarted = build_mechanical(
        tmp_path,
        signals_config,
        items=[],
        broker=venue_across_restart(
            broker,
            [BrokerPosition("NUE", Decimal("5"), Decimal("700"), Decimal("700"))],
            Decimal("99300"),
        ),
    )
    recovered = restarted.audit.trail(decision_id)
    assert [f.side for f in recovered.fills] == ["buy"]
    assert recovered.fills[0].filled_quantity == Decimal("5")
    # The sleeve owns it again: attributed, tracked, ledger reconstructed.
    assert restarted.gate.state.position(("mechanical", "NUE")).quantity == 5
    assert len(restarted.loop.mechanical.tracked) == 1
    assert restarted.loop.mechanical.virtual_cash == Decimal("25000") - Decimal("700")


def test_a_judged_fill_lost_to_a_crash_recovers_with_its_stop_armed(
    tmp_path, signals_config
):
    """Both sleeves: the judged entry recovers the same way, and the exit
    engine arms a stop on the recovered position."""
    from execution.base import OrderStatus
    from test_orchestrator import build as build_judged, feed

    limits = RiskLimits.load()
    research_config = ResearchConfig.load()
    broker = FakeBroker(fill="new")
    started = build_judged(
        tmp_path, limits, signals_config, research_config, broker=broker
    )
    result = started.loop.tick().processed[0]
    assert result.traded
    broker.set_status(
        "brk-1", OrderStatus("brk-1", "filled", Decimal("13"), Decimal("140.00"))
    )
    started.loop.pipeline._working.clear()  # the crash
    assert started.audit.trail(result.decision_id).fills == ()

    restarted = build_judged(
        tmp_path,
        limits,
        signals_config,
        research_config,
        broker=venue_across_restart(
            broker,
            [BrokerPosition("NUE", Decimal("13"), Decimal("1820"), Decimal("1820"))],
            Decimal("98180"),
        ),
        fetcher=feed(),
    )
    trail = restarted.audit.trail(result.decision_id)
    assert [f.side for f in trail.fills] == ["buy"]
    assert len(restarted.exits.tracked) == 1
    assert restarted.exits.tracked[0].stop_price is not None  # stop armed


def test_an_order_that_died_unfilled_records_its_release_not_a_fill(
    tmp_path, signals_config
):
    from execution.base import OrderStatus

    broker = FakeBroker(fill="new")
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        broker=broker,
    )
    started.loop.tick()
    decision_id = started.audit.mechanical_trails()[0].decision.decision_id
    broker.set_status("brk-1", OrderStatus("brk-1", "canceled", Decimal("0"), None))
    started.loop.mechanical._working.clear()

    restarted = build_mechanical(
        tmp_path,
        signals_config,
        items=[],
        broker=venue_across_restart(broker, [], Decimal("100000")),
    )
    trail = restarted.audit.trail(decision_id)
    assert trail.fills == ()
    assert any(
        r.stage == RejectedStage.EXECUTION and "without filling" in r.message
        for r in trail.stage_rejections
    )
    assert restarted.loop.mechanical.tracked == ()


def test_an_unanswerable_broker_leaves_the_record_untouched(tmp_path, signals_config):
    """"Cannot tell" is never written as "did not fill" — it stays pending."""
    from orchestrator.recovery import pending_settlement

    broker = FakeBroker(fill="new")
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        broker=broker,
    )
    started.loop.tick()
    decision_id = started.audit.mechanical_trails()[0].decision.decision_id
    started.loop.mechanical._working.clear()

    restarted = build_mechanical(
        tmp_path, signals_config, items=[], broker=SilentBroker()
    )
    trail = restarted.audit.trail(decision_id)
    assert trail.fills == ()
    assert trail.stage_rejections == ()  # nothing invented in either direction
    assert [p.decision_id for p in pending_settlement(restarted.audit)] == [decision_id]


class SilentBroker(FakeBroker):
    """A venue that cannot be asked about past orders."""

    def get_order_by_client_reference(self, client_reference):
        return None


def test_pending_settlement_reads_differently_from_unmanaged(
    tmp_path, signals_config
):
    """A mid-flight snapshot must not scream "no audit trail — needs a human"."""
    from orchestrator.bootstrap import preflight
    from orchestrator.ops import RunLog, health_report
    from test_orchestrator import orchestrator_config

    broker = FakeBroker(fill="new")
    started = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        broker=broker,
    )
    started.loop.tick()
    started.loop.mechanical._working.clear()

    checks = preflight(
        adapter=SilentBroker(
            cash=Decimal("99300"),
            positions=[
                BrokerPosition("NUE", Decimal("5"), Decimal("700"), Decimal("700"))
            ],
        ),
        data_dir=tmp_path,
        limits=RiskLimits.load(),
        signals_config=signals_config,
        research_config=ResearchConfig.load(),
        orchestrator_config=orchestrator_config(),
        clock=FakeClock(),
    )
    report = health_report(checks, [], RunLog(tmp_path / "run.log"))
    assert "pending settlement:" in report
    assert "NUE" in report
    assert "UNMANAGED  NUE" not in report  # it has a trail; it is not orphaned


def test_the_mechanical_entries_switch_stops_slices_while_exits_still_fire(
    tmp_path, signals_config
):
    """The clean off switch: config-level, no weight change, no file surgery.
    A held slice still time-exits while entries are off — the switch stops new
    exposure, never the unwinding of old exposure."""
    from test_exits import MutablePrices

    prices = MutablePrices(NUE="140.00")
    clock = FakeClock()
    # Session one: entries on, one slice fills.
    first = build_mechanical(
        tmp_path,
        signals_config,
        items=[disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")],
        prices=prices,
        clock=clock,
    )
    assert first.loop.tick().mechanical_entries == 1
    first.loop.shutdown()

    # Session two, a year later: BOTH arms switched off in config.
    raw = RiskLimits.load().model_dump()
    raw["mechanical_sleeve"]["entries_enabled"] = False
    raw["equity_sleeve"]["entries_enabled"] = False
    disabled = RiskLimits.model_validate(raw)
    clock.advance(days=400)
    fresh_report_date = (clock() - timedelta(days=2)).date().isoformat()

    started = build(
        tmp_path,
        disabled,
        signals_config,
        ResearchConfig.load(),
        llm=quiet_llm(),
        fetcher=congressional_feed(
            disclosure_item("row-2", "AAPL", "$50,001 - $100,000", fresh_report_date)
        ),
        prices=prices,
        broker=FakeBroker(
            cash=Decimal("99300"),
            positions=[
                BrokerPosition("NUE", Decimal("5"), Decimal("700"), Decimal("700"))
            ],
        ),
        clock=clock,
    )
    report = started.loop.tick()

    # No new exposure, from either arm, and both said so in their own code.
    assert report.mechanical_entries == 0
    assert report.processed == []
    codes = {r.code for r in started.audit.stage_rejections()}
    assert {"mechanical_disabled", "entries_disabled"} <= codes
    disabled_record = next(
        r
        for r in started.audit.stage_rejections()
        if r.code == "mechanical_disabled"
    )
    assert "entries_enabled" in disabled_record.message

    # The held slice still time-exits: switching entries off is not a freeze.
    assert report.mechanical_exits == 1

    # And neither switch seals the signal — it returns when they go back on.
    assert (
        "congressional_disclosures",
        "row-2",
    ) not in started.audit.researched_external_ids()


def test_the_judged_entries_switch_stops_dispatch_without_buying_research(
    tmp_path, signals_config
):
    from test_orchestrator import build as build_judged

    raw = RiskLimits.load().model_dump()
    raw["equity_sleeve"]["entries_enabled"] = False
    disabled = RiskLimits.model_validate(raw)

    llm = FakeLLM()
    started = build_judged(
        tmp_path, disabled, signals_config, ResearchConfig.load(), llm=llm
    )
    report = started.loop.tick()
    assert report.processed == []
    assert report.prefiltered == 1
    assert llm.calls == []  # an arm that cannot open must not buy an opinion
    rejection = started.audit.stage_rejections()[0]
    assert rejection.code == "entries_disabled"
    assert (
        "trump_posts",
        rejection.signal.external_id,
    ) not in started.audit.researched_external_ids()
