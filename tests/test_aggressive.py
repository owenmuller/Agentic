"""The aggressive sleeve, increment A (risk-on redirect, human ruling
2026-10-08; amendment 4: 25% of NAV, 2% risk budget per trade).

Claims under test: the shipped weights and caps are as ruled; the gate bounds
the sleeve with its own single-position, position-count, daily and sector caps
and refuses to spend today's unsettled sale proceeds on its buys (T+1) while
the older sleeves are unchanged; the attention-momentum screen confirms events
exactly on the pre-registered thresholds, once a morning, first confirmation
only; an aggressive-source verdict sizes by the fixed risk budget into its own
sleeve, never as an add, with confidence gating but not scaling; its position
lives in its own exit engine with a stop that trails from entry, no LLM
reviews, sells tagged with its sleeve; and a restart wakes it up in its own
sleeve.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from audit.log import AuditLog
from audit.records import RejectedStage
from execution.base import BrokerPosition
from orchestrator import start
from orchestrator.attention import AttentionMomentumFetcher
from orchestrator.config import AttentionMomentumConfig, OrchestratorConfig
from risk_gate import RiskGate, RiskLimits
from risk_gate.rejections import RejectionCode
from risk_gate.schema import EquityBuyOrder, EquitySellToCloseOrder, LimitExecution
from risk_gate.state import AccountState, Sleeve
from signals import SignalsConfig
from signals.records import Priority, Signal, SignalClass
from signals.scanners import RawItem
from test_orchestrator import (
    NOW,
    QUOTE,
    REPORT,
    FakeBroker,
    FakeClock,
    counter,
    orchestrator_config,
    structured,
)
from test_exits import MutablePrices, RoutingLLM
from research.reports import REPORT_TOOL_NAME


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")


@pytest.fixture(scope="session")
def shipped_limits():
    return RiskLimits.load()  # the SHIPPED config: this file pins the rulings


@pytest.fixture(scope="session")
def limits(shipped_limits):
    """The shipped caps with the sleeve's entries switched back ON: these tests
    exercise the sleeve's machinery. The shipped switch - OFF by human ruling
    2026-10-09 - is pinned in its own tests below."""
    caps = shipped_limits.aggressive_sleeve.model_copy(update={"entries_enabled": True})
    return shipped_limits.model_copy(update={"aggressive_sleeve": caps})


@pytest.fixture(scope="session")
def signals_config():
    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    from research.config import ResearchConfig

    return ResearchConfig.load()


# ================================================================================
# The ruling, as shipped
# ================================================================================


def test_the_shipped_weights_and_caps_are_as_ruled(shipped_limits, signals_config):
    sleeves = shipped_limits.portfolio.sleeves
    assert (sleeves.equity, sleeves.aggressive, sleeves.mechanical, sleeves.baseline, sleeves.prediction) == (
        Decimal("0.30"), Decimal("0.25"), Decimal("0.15"), Decimal("0.30"), Decimal("0.00"),
    )
    caps = shipped_limits.aggressive_sleeve
    # Entries OFF by human ruling 2026-10-09 (item 1): the attention screen
    # measured -2.42% vs SPY, CI [-3.38, -1.38]. Exits keep running.
    assert caps is not None and not caps.entries_enabled
    assert (caps.max_single_position, caps.max_positions, caps.max_daily_deployment, caps.max_sector_exposure) == (
        Decimal("0.25"), 5, Decimal("1.00"), Decimal("0.50"),
    )
    config = OrchestratorConfig.load().aggressive_sleeve
    assert config.enabled and config.sources == ("attention_momentum",)
    assert config.risk_budget_fraction == Decimal("0.02")
    assert (config.stop_atr_k, config.stop_floor, config.stop_ceiling, config.leash_days) == (
        Decimal("2.5"), Decimal("0.04"), Decimal("0.20"), 84,
    )
    attention = config.attention
    assert (attention.confirm_sessions, attention.pre_high_sessions, attention.volume_avg_sessions, attention.volume_multiple) == (
        3, 5, 20, Decimal("2.0"),
    )
    source = signals_config.source("class_2", "attention_momentum")
    assert source.daily_research_cap == 4


# ================================================================================
# The gate
# ================================================================================


def _gate(limits, cash="100000"):
    state = AccountState(cash=Decimal(cash), high_water_mark=Decimal(cash))
    return RiskGate(limits, state, clock=FakeClock())


def _buy(symbol, qty, price="100.00", sleeve="aggressive"):
    return EquityBuyOrder(symbol=symbol, quantity=qty, execution=LimitExecution(limit_price=Decimal(price)), sleeve=sleeve)


def test_the_gate_holds_the_sleeve_to_its_own_caps(limits):
    gate = _gate(limits)
    assert gate.sleeve_nav(Sleeve.AGGRESSIVE) == Decimal("25000.00")
    # Single position: 25% of the sleeve = 6,250.
    rejected = gate.submit(_buy("AAA", 63))
    assert rejected.code is RejectionCode.MAX_SINGLE_POSITION_EXCEEDED
    assert gate.submit(_buy("AAA", 62)).is_approved
    # Five names at once, pending opens included.
    for symbol in ("BBB", "CCC", "DDD", "EEE"):
        assert gate.submit(_buy(symbol, 10)).is_approved
    sixth = gate.submit(_buy("FFF", 10))
    assert sixth.code is RejectionCode.MAX_POSITIONS_EXCEEDED
    # The judged sleeve is untouched by the aggressive counters.
    assert gate.submit(_buy("FFF", 10, sleeve="equity")).is_approved
    assert gate.state.aggressive_deployed_today == Decimal("10200.00")


def test_aggressive_buys_may_not_spend_unsettled_proceeds(limits):
    """Amendment 3: a cash account settles T+1; buying with today's sale
    proceeds and selling before settlement is a good-faith violation."""
    gate = _gate(limits, cash="100000")
    approved = gate.submit(_buy("JDG", 20, sleeve="equity"))  # judged: 2,000
    gate.record_fill(approved, Decimal("100.00"))
    sell = gate.submit(EquitySellToCloseOrder(symbol="JDG", quantity=20, execution=LimitExecution(limit_price=Decimal("100.00")), sleeve="equity"))
    gate.record_fill(sell, Decimal("100.00"))
    assert gate.state.cash == Decimal("100000.00") and gate.state.unsettled_proceeds == Decimal("2000.00")
    # Park 93,000 (a sweep buy reserves it): 7,000 spendable, 5,000 of it settled.
    assert gate.submit(_buy("SGOV", 930, sleeve="cash_management")).is_approved
    assert gate.state.buying_power == Decimal("7000.00") and gate.state.settled_buying_power == Decimal("5000.00")
    # 6,000 of aggressive buying needs more than the 5,000 settled.
    refused = gate.submit(_buy("AGG", 60))
    assert refused.code is RejectionCode.UNSETTLED_FUNDS
    assert gate.submit(_buy("AGG", 50)).is_approved  # 5,000: exactly the settled cash
    # The judged sleeve keeps its pre-redirect behaviour (paper settles at once):
    # the last 2,000 - unsettled proceeds - still funds a judged buy.
    assert gate.submit(_buy("JD2", 20, sleeve="equity")).is_approved
    # The next session the proceeds have settled.
    gate._clock.advance(days=1)  # noqa: SLF001
    gate.submit(_buy("ZZZ", 1, price="1.00", sleeve="equity"))  # any submit rolls the day
    assert gate.state.unsettled_proceeds == Decimal("0")


# ================================================================================
# The attention-momentum screen
# ================================================================================


def _sessions(end: date, n: int) -> list[date]:
    days, day = [], end
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def _bar(day, close, volume=1_000_000, spread=1.0):
    return {"t": f"{day.isoformat()}T04:00:00Z", "o": close, "h": close + spread, "l": close - spread, "c": close, "v": volume}


def _record_event(audit: AuditLog, ticker: str, observed: datetime, source="form_8k", code="source_cap", transaction=""):
    signal = Signal(
        signal_id=f"s-{ticker}-{observed.date()}",
        source_id=source,
        signal_class=SignalClass.CLASS_1_REALTIME if source == "form_8k" else SignalClass.CLASS_2_MOMENTUM,
        observed_at=observed,
        content=f"Form 8-K current report (SEC EDGAR)\nissuer: Test Co ({ticker})\nitems: 1.01, 9.01",
        raw_content="x",
        priority=Priority.for_class(SignalClass.CLASS_2_MOMENTUM),
        external_id=f"e-{ticker}-{observed.date()}",
        metadata={"tickers": ticker, **({"transaction": transaction} if transaction else {})},
    )
    audit.record_stage_rejection(f"d-{ticker}-{observed.date()}", RejectedStage.PRE_FILTER, code, "test", signal)


def _screen(tmp_path, closes_by_ticker, volumes_by_ticker, now, events):
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    for event in events:
        _record_event(audit, *event)
    sessions = _sessions((now - timedelta(days=1)).date() if now.astimezone().hour >= 0 else now.date(), 40)
    calls = []

    def bars_many(symbols, start, end):
        calls.append(list(symbols))
        out = {"SPY": [_bar(d, 100.0) for d in sessions]}
        for t in symbols:
            if t in closes_by_ticker:
                out[t] = [_bar(d, c, v) for d, c, v in zip(sessions, closes_by_ticker[t], volumes_by_ticker[t])]
        return out

    fetcher = AttentionMomentumFetcher(audit.records, bars_many, AttentionMomentumConfig(), clock=lambda: now)
    return fetcher, sessions, calls


NOW_SCREEN = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)  # Thu 10:00 ET


def test_the_screen_confirms_on_the_registered_thresholds(tmp_path):
    sessions = _sessions(date(2026, 10, 7), 40)
    t0 = sessions[-2]  # the event's session; the latest completed session is t0+1
    closes = [50.0] * 38 + [51.0, 56.0]  # pre-event high 50; confirmation close 56
    volumes = [1_000_000] * 39 + [2_500_000]  # 2.5x the 20-session average
    observed = datetime(t0.year, t0.month, t0.day, 15, 0, tzinfo=timezone.utc)  # 11:00 ET on t0
    fetcher, _, calls = _screen(tmp_path, {"VST": closes}, {"VST": volumes}, NOW_SCREEN, [("VST", observed)])
    items = fetcher(None)
    assert [i.external_id for i in items] == [f"attn:VST:{t0.isoformat()}"]
    item = items[0]
    assert item.fields["ticker"] == "VST" and item.fields["event_source"] == "form_8k"
    assert item.fields["confirm_offset"] == "1" and float(item.fields["volume_ratio"]) == pytest.approx(2.5)
    assert float(item.fields["atr_fraction"]) > 0
    assert "excess vs SPY since the close before t0: +12.00%" in item.content  # 56 vs the 50 close before t0; SPY flat
    # Once per session day: a second poll the same morning reads nothing.
    assert fetcher(None) == [] and len(calls) == 1


def test_the_screen_rejects_what_the_rule_rejects(tmp_path):
    sessions = _sessions(date(2026, 10, 7), 40)
    t0 = sessions[-2]
    observed = datetime(t0.year, t0.month, t0.day, 15, 0, tzinfo=timezone.utc)
    base = [50.0] * 38
    cases = {
        "LOWV": (base + [51.0, 56.0], [1_000_000] * 39 + [1_900_000]),  # volume 1.9x: no
        "LOWP": (base + [49.0, 49.9], [1_000_000] * 39 + [3_000_000]),  # under the pre-event high: no
    }
    events = [(t, observed) for t in cases] + [("SALE", observed, "congressional_disclosures", "pre_filter", "Sale (Full)")]
    fetcher, _, _ = _screen(
        tmp_path,
        {t: c[0] for t, c in cases.items()} | {"SALE": base + [51.0, 56.0]},
        {t: c[1] for t, c in cases.items()} | {"SALE": [1_000_000] * 39 + [3_000_000]},
        NOW_SCREEN,
        events,
    )
    assert fetcher(None) == []  # a congressional SALE is never an event
    assert fetcher.last_tally["confirmed"] == 0


def test_the_screen_enters_only_after_the_first_confirmation(tmp_path):
    """The registered rule enters the open after the FIRST confirming session;
    a later confirmation of an event that already confirmed is not a new entry."""
    sessions = _sessions(date(2026, 10, 7), 40)
    t0 = sessions[-3]
    observed = datetime(t0.year, t0.month, t0.day, 15, 0, tzinfo=timezone.utc)
    closes = [50.0] * 37 + [51.0, 56.0, 58.0]
    volumes = [1_000_000] * 38 + [2_500_000, 2_600_000]
    fetcher, _, _ = _screen(tmp_path, {"EARLY": closes}, {"EARLY": volumes}, NOW_SCREEN, [("EARLY", observed)])
    assert fetcher(None) == []
    assert fetcher.last_tally["earlier_confirmation"] == 1


def test_the_screen_waits_for_the_morning_and_skips_weekends(tmp_path):
    early = datetime(2026, 10, 8, 13, 20, tzinfo=timezone.utc)  # 09:20 ET
    fetcher, _, calls = _screen(tmp_path, {}, {}, early, [])
    assert fetcher(None) == [] and calls == []
    saturday = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
    fetcher, _, calls = _screen(tmp_path, {}, {}, saturday, [])
    assert fetcher(None) == [] and calls == []


# ================================================================================
# The pipeline and the exit engine, end to end
# ================================================================================


def _attention_items(atr="0.04"):
    def fetcher(source):
        if source.id != "attention_momentum":
            return []
        return [
            RawItem(
                external_id="attn:NUE:2026-08-14",
                content="Attention-momentum confirmation (test)\nticker: NUE",
                published_at=NOW,
                fields={"ticker": "NUE", "event_source": "form_8k", "t0": "2026-08-13", "confirm_day": "2026-08-14", "confirm_offset": "1", "volume_ratio": "2.40", "excess_since_event": "0.05", "atr_fraction": atr, "report_date": "2026-08-14", "measurement_only": "false"},
            )
        ]

    return fetcher


def _session(tmp_path, limits, signals_config, research_config, *, broker=None, prices=None, clock=None, llm=None, prefix="dec", fetcher=None):
    return start(
        fetcher=fetcher or _attention_items(),
        prices=prices or MutablePrices(NUE=str(QUOTE)),
        llm_client=llm or RoutingLLM(),
        adapter=broker or FakeBroker(),
        clock=clock or FakeClock(),
        data_dir=tmp_path,
        limits=limits,
        signals_config=signals_config,
        research_config=research_config,
        orchestrator_config=orchestrator_config(aggressive_sleeve={"enabled": True}),
        id_factory=counter(prefix),
    )


def test_a_confirmed_event_sizes_by_the_risk_budget_into_its_own_sleeve(tmp_path, limits, signals_config, research_config):
    started = _session(tmp_path, limits, signals_config, research_config)
    report = started.loop.tick()
    assert report.processed and report.processed[0].traded
    decision = started.audit.trail("dec-1").decision
    assert decision.sizing.sleeve == "aggressive" and decision.sizing.strategy == "aggressive"
    # 25,000 sleeve x 2% = 500 at risk over a 2.5 x 4% = 10% stop -> 5,000.
    assert decision.sizing.capital == Decimal("5000.00")
    assert decision.sizing.stop_fraction == Decimal("0.100")
    assert decision.gate.order["sleeve"] == "aggressive"
    assert started.gate.state.position(("aggressive", "NUE")) is not None
    assert started.gate.state.position(("equity", "NUE")) is None
    tracked = started.exits.tracked
    assert len(tracked) == 1 and tracked[0].sleeve == "aggressive" and tracked[0].key == ("aggressive", "NUE")
    assert tracked[0].stop_price == Decimal("126.0000")  # 140 x (1 - 10%)
    assert tracked[0].leash_days == 84
    assert started.exits.judged.tracked == ()


def test_confidence_gates_entry_but_never_scales_size(tmp_path, limits, signals_config, research_config):
    llm = RoutingLLM(**{REPORT_TOOL_NAME: structured({**REPORT, "confidence": 95})})
    started = _session(tmp_path, limits, signals_config, research_config, llm=llm)
    started.loop.tick()
    assert started.audit.trail("dec-1").decision.sizing.capital == Decimal("5000.00")  # same as at 71
    low = RoutingLLM(**{REPORT_TOOL_NAME: structured({**REPORT, "confidence": 40})})
    started = _session(tmp_path / "b", limits, signals_config, research_config, llm=low)
    result = started.loop.tick().processed[0]
    assert not result.traded and result.rejection.code == "below_floor"


def test_the_stop_trails_from_entry_and_the_exit_carries_the_sleeve(tmp_path, limits, signals_config, research_config):
    prices = MutablePrices(NUE=str(QUOTE))
    broker = FakeBroker()
    llm = RoutingLLM()
    started = _session(tmp_path, limits, signals_config, research_config, prices=prices, broker=broker, llm=llm)
    started.loop.tick()
    position = started.exits.tracked[0]
    prices.set("NUE", "160.00")  # no 12% arm threshold: the stop follows at once
    started.loop.tick()
    assert position.stop_price == Decimal("144.000")  # 160 x 0.90
    prices.set("NUE", "150.00")
    started.loop.tick()
    assert position.stop_price == Decimal("144.000")  # never falls
    prices.set("NUE", "143.00")
    assert started.loop.tick().exits_started == 1
    trail = started.audit.trail("dec-1")
    assert trail.exits and trail.exits[0].reason.value == "trailing_stop"
    # The sell was keyed to the aggressive sleeve: under the judged key the
    # gate would have refused it as a position not held.
    started.loop.tick()
    assert started.gate.state.position(("aggressive", "NUE")) is None
    assert started.gate.state.position(("equity", "NUE")) is None
    assert started.exits.tracked == ()
    # No LLM review ever ran on the aggressive position.
    assert not any(call["tool"] != REPORT_TOOL_NAME for call in llm.calls)


def test_a_restart_wakes_the_position_up_in_its_own_sleeve(tmp_path, limits, signals_config, research_config):
    clock = FakeClock()
    first = _session(tmp_path, limits, signals_config, research_config, clock=clock)
    first.loop.tick()
    held = first.gate.state.position(("aggressive", "NUE"))
    quantity, cost = held.quantity, held.cost_basis
    first.loop.shutdown()
    venue = FakeBroker(
        cash=first.gate.state.cash,
        positions=[BrokerPosition("NUE", quantity, cost, cost)],
    )
    restarted = _session(tmp_path, limits, signals_config, research_config, broker=venue, clock=clock, prefix="b", fetcher=lambda s: [])
    assert restarted.gate.state.position(("aggressive", "NUE")).quantity == quantity
    assert restarted.gate.state.position(("equity", "NUE")) is None
    assert [p.key for p in restarted.exits.tracked] == [("aggressive", "NUE")]
    assert restarted.exits.judged.tracked == ()


def test_the_weekly_reports_the_sleeve_against_spy_in_its_own_bucket(tmp_path, limits, signals_config, research_config):
    """Increment C: the sleeve's own lines - since inception and trailing four
    weeks, per-trade return against SPY over each trade's own window - and not
    a cent of it in a judged class or the judged total."""
    from audit.attribution import build_attribution
    from datetime import timedelta

    prices = MutablePrices(NUE=str(QUOTE))
    started = _session(tmp_path, limits, signals_config, research_config, prices=prices, broker=FakeBroker(), llm=RoutingLLM())
    started.loop.tick()
    prices.set("NUE", "160.00")
    started.loop.tick()
    prices.set("NUE", "143.00")
    started.loop.tick()
    started.loop.tick()
    trail = started.audit.trail("dec-1")
    cost = sum(f.filled_value for f in trail.fills if f.side == "buy")
    proceeds = sum(f.filled_value for f in trail.fills if f.side != "buy")
    assert cost > 0 and proceeds > 0
    generated = max(f.recorded_at for f in trail.fills) + timedelta(days=2)
    report = build_attribution(
        started.audit.trails(), generated_at=generated,
        price_on=lambda s, d: Decimal("500") if s == "SPY" else None,  # SPY flat: excess == return
    )
    assert report.by_class == {}
    assert report.total_pnl == Decimal("0")
    assert report.aggressive is not None
    (row,) = report.aggressive.trades
    assert row.symbol == "NUE" and row.closed is not None
    assert row.pnl == (proceeds - cost).quantize(Decimal("0.01"))
    assert row.spy_return_pct == Decimal("0.00")
    assert row.excess_pct == row.return_pct
    rendered = report.render()
    assert "aggressive (redirect 2026-10-08, fixed 2% risk): since inception 1 trades (0 open)" in rendered
    assert "vs SPY over each trade's own window" in rendered
    assert "aggressive, trailing 4 weeks" in rendered
    assert "EXCLUDED from every alpha line" in rendered



# ================================================================================
# Rulings 2026-10-09: entries off (item 1); no target, no trade (item 3)
# ================================================================================


def test_switched_off_the_sleeve_records_candidates_and_pays_for_no_research(tmp_path, shipped_limits, signals_config, research_config):
    llm = RoutingLLM()
    started = _session(tmp_path, shipped_limits, signals_config, research_config, llm=llm)
    report = started.loop.tick()
    assert report.processed == []
    (record,) = [r for r in started.audit.stage_rejections() if r.code == "entries_disabled"]
    assert "aggressive_sleeve.entries_enabled" in record.message
    assert llm.calls == []  # not a cent of research on a sleeve that cannot buy
    assert started.gate.state.position(("aggressive", "NUE")) is None


def test_switched_off_the_sleeve_parks_no_cash_for_itself(shipped_limits, limits):
    from orchestrator.sweep import liquidity_buffer

    on = liquidity_buffer(_gate(limits, cash="100000"), limits.cash_management)
    off = liquidity_buffer(_gate(shipped_limits, cash="100000"), shipped_limits.cash_management)
    assert on - off == Decimal("100000") * Decimal("0.25")  # the sleeve's full daily cap, no longer parked


def test_no_target_no_trade(tmp_path, limits, signals_config, research_config):
    untargeted = {k: v for k, v in REPORT.items() if k != "target_price"}
    llm = RoutingLLM(**{REPORT_TOOL_NAME: structured(untargeted)})
    started = _session(tmp_path, limits, signals_config, research_config, llm=llm)
    result = started.loop.tick().processed[0]
    assert not result.traded
    assert result.rejection.code == "insufficient_reward_risk"
    assert "requires a stated target" in result.rejection.message
    assert started.gate.state.position(("aggressive", "NUE")) is None


def test_a_stated_target_below_the_floor_is_refused_and_above_it_trades(tmp_path, limits, signals_config, research_config):
    # QUOTE 140, the screen's stop 10%: a 141 target is reward:risk 0.07, far below the floor.
    low = RoutingLLM(**{REPORT_TOOL_NAME: structured({**REPORT, "target_price": "141"})})
    started = _session(tmp_path / "a", limits, signals_config, research_config, llm=low)
    assert started.loop.tick().processed[0].rejection.code == "insufficient_reward_risk"
    started = _session(tmp_path / "b", limits, signals_config, research_config)
    assert started.loop.tick().processed[0].traded
