"""The paper lab (PAPER PUSH, human ruling 2026-10-09 late): separate paper
accounts, the data guard's per-account ownership, and 2a - the leverage
ladder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest

import datasafety
from execution.base import BrokerPosition, OrderReceipt, OrderStatus
from execution.environment import LIVE_CONFIRMATION_PHRASE, LIVE_CONFIRMATION_VARIABLE
from lab.account import LabRefused, key_variables, open_account
from lab.config import LabConfig, account_limits
from lab.ladder import Book, LadderEngine, plan, replay_books

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
D = Decimal
NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)  # 10:00 ET, a Tuesday

QUOTES = {"SPY": (D("500.00"), D("500.10")), "SSO": (D("90.00"), D("90.04")), "QLD": (D("110.00"), D("110.06"))}


# ----------------------------------------------------------------- the guard

@pytest.fixture
def owned(tmp_path):
    # every directory first, the markers last (a marked tree is unwritable here)
    main = tmp_path / "data"
    nested = main / "lab-inside"
    nested.mkdir(parents=True)
    lab = tmp_path / "data-lab" / "ladder"
    lab.mkdir(parents=True)
    other = tmp_path / "data-lab" / "ai-trader"
    other.mkdir(parents=True)
    (lab / datasafety.MARKER).write_text("lab\nowner=agentic-lab-ladder\n")
    (other / datasafety.MARKER).write_text("lab\nowner=agentic-lab-ai-trader\n")
    (nested / datasafety.MARKER).write_text("owner=agentic-lab-ladder\n")
    (main / datasafety.MARKER).write_text("main\n")
    return tmp_path


@pytest.mark.parametrize(
    "target, unit, allowed",
    [
        ("data/audit.jsonl", None, False),
        ("data/audit.jsonl", "agentic-paper", True),
        ("data/audit.jsonl", "agentic-weekly", True),
        ("data/audit.jsonl", "agentic-lab-ladder", False),
        ("data-lab/ladder/ledger.jsonl", "agentic-lab-ladder", True),
        ("data-lab/ladder/ledger.jsonl", "agentic-paper", False),
        ("data-lab/ladder/ledger.jsonl", "agentic-lab-ai-trader", False),
        ("data-lab/ladder/ledger.jsonl", None, False),
        ("data-lab/ai-trader/ledger.jsonl", "agentic-lab-ladder", False),
        # the nearest marker decides: an owned tree inside the main book
        ("data/lab-inside/x.json", "agentic-lab-ladder", True),
        ("data/lab-inside/x.json", "agentic-paper", False),
        ("elsewhere/x.json", "agentic-lab-ladder", True),
        ("elsewhere/x.json", None, True),
    ],
)
def test_each_account_writes_only_its_own_data(owned, target, unit, allowed):
    assert datasafety.write_permitted(owned / target, unit) is allowed


@pytest.mark.parametrize(
    "cgroup, unit",
    [
        ("0::/system.slice/agentic-lab-ladder.service\n", "agentic-lab-ladder"),
        ("0::/system.slice/agentic-paper.service\n", "agentic-paper"),
        ("0::/user.slice/user-1000.slice/session-3.scope\n", None),
    ],
)
def test_the_unit_is_read_from_the_cgroup(cgroup, unit):
    assert datasafety.current_unit(cgroup) == unit


def _run(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC), AGENTIC_SCRATCH_DATA_DIR=str(tmp_path / "scratch"),
               AGENTIC_PRODUCTION_DATA_DIR=str(tmp_path / "data"), AGENTIC_LAB_DATA_ROOT=str(tmp_path / "data-lab"))
    env.pop("DATA_DIR", None)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)


def test_the_hook_holds_a_lab_unit_to_its_own_tree(owned):
    """In a cold interpreter posing as the ladder's unit: its own ledger is
    writable; the main book and another lab account are refused, every form."""
    result = _run(
        r'''
        import os, shutil
        from pathlib import Path
        import lab, datasafety
        datasafety._unit_name = "agentic-lab-ladder"
        (Path("data-lab/ladder") / "ledger.jsonl").write_text("ok\n")
        attempts = {
            "main-open": lambda: open("data/audit.jsonl", "a"),
            "main-replace": lambda: (Path("free.tmp").write_text("x"), os.replace("free.tmp", "data/x.json")),
            "main-remove": lambda: os.remove("data/audit.jsonl"),
            "other-lab": lambda: Path("data-lab/ai-trader/ledger.jsonl").write_text("x"),
            "main-dir": lambda: __import__("datasafety").resolve_data_dir(Path("data")),
        }
        for name, attempt in attempts.items():
            try:
                attempt()
            except datasafety.ProductionDataWriteRefused:
                continue
            print("NOT REFUSED", name)
            raise SystemExit(1)
        print("OK")
        ''',
        owned,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_the_main_unit_cannot_write_a_lab_account(owned):
    result = _run(
        r'''
        from pathlib import Path
        import orchestrator, datasafety
        datasafety._unit_name = "agentic-paper"
        (Path("data") / "session_state.json").write_text("{}")
        try:
            Path("data-lab/ladder/state.json").write_text("{}")
        except datasafety.ProductionDataWriteRefused:
            print("OK")
        ''',
        owned,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_a_guard_error_fails_open_loudly_in_production(owned):
    """Ruling 2026-10-10, item 4: a guard ERROR inside a production unit
    allows the write, logs at ERROR and alerts exactly as a refusal does."""
    result = _run(
        r'''
        import logging
        from pathlib import Path
        import lab, datasafety
        records = []
        class Grab(logging.Handler):
            def emit(self, record):
                records.append((record.levelname, record.getMessage()))
        logging.getLogger("datasafety").addHandler(Grab())
        alerts = []
        datasafety.set_alert_sink(lambda kind, message: alerts.append(kind))
        datasafety._unit_name = "agentic-lab-ladder"
        try:
            Path("data/audit.jsonl").write_text("x")
        except datasafety.ProductionDataWriteRefused:
            pass
        assert alerts == ["WRITE REFUSED"], alerts
        def broken(target, unit):
            raise RuntimeError("guard defect")
        datasafety.write_permitted = broken
        Path("data/audit.jsonl").write_text("allowed")
        assert Path("data/audit.jsonl").read_text() == "allowed"
        assert alerts == ["WRITE REFUSED", "GUARD ERROR, WRITE ALLOWED"], alerts
        assert [lvl for lvl, _ in records] == ["ERROR", "ERROR"], records
        assert "guard defect" in records[1][1]
        # outside production the same defect fails CLOSED
        datasafety._unit_name = None
        try:
            Path("data/audit.jsonl").write_text("y")
        except RuntimeError:
            print("OK")
        ''',
        owned,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr


def test_outside_its_unit_a_lab_account_resolves_to_scratch(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_SCRATCH_DATA_DIR", str(tmp_path / "scratch" / "data"))
    assert datasafety.resolve_lab_data_dir("ladder") == tmp_path / "scratch" / "lab" / "ladder"


def test_a_real_lab_run_is_refused_outside_its_unit(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(SRC), AGENTIC_SCRATCH_DATA_DIR=str(tmp_path / "s"),
               AGENTIC_LAB_DATA_ROOT=str(tmp_path / "lab"))
    result = subprocess.run([sys.executable, "-m", "lab", "tick", "ladder"], cwd=str(tmp_path), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 2 and "REFUSED" in result.stderr, result.stdout + result.stderr


# ------------------------------------------------------------------ config

def test_the_lab_config_loads_and_only_the_ladder_is_enabled():
    config = LabConfig.load()
    assert [n for n, a in config.accounts.items() if a.enabled] == ["ladder"]
    assert "unconstrained" not in config.accounts  # sleeve c dropped, ruling 2026-10-10
    assert config.ladder.symbols == ("QLD", "SPY", "SSO")
    assert key_variables("ai-trader") == ("ALPACA_LAB_AI_TRADER_API_KEY", "ALPACA_LAB_AI_TRADER_API_SECRET")


def test_the_ladder_cap_table_is_the_main_one_with_baseline_at_one():
    limits = account_limits(LabConfig.load().account("ladder"))
    assert limits.portfolio.sleeves.baseline == 1 and limits.portfolio.sleeves.equity == 0
    assert limits.account.margin_enabled is False and limits.account.short_selling == "forbidden"
    assert limits.kill_switch.drawdown_from_high_water_mark == 1  # no kill switch (2026-10-10)


def test_an_override_cannot_switch_margin_or_shorting_on():
    from lab.config import AccountConfig

    for override in ({"account": {"margin_enabled": True}}, {"account": {"short_selling": "allowed"}}):
        with pytest.raises(Exception):
            account_limits(AccountConfig(enabled=True, label="x", sleeve="x", limits_overrides=override))


# ----------------------------------------------------------------- planning

def test_books_replay_funding_fills_and_adjustments():
    rungs = LabConfig.load().ladder.rungs
    events = [
        {"event": "rung_funded", "rung": "spy_2x", "amount": "1000"},
        {"event": "settled", "rung": "spy_2x", "symbol": "SSO", "side": "buy", "filled_qty": "10", "avg_price": "90"},
        {"event": "settled", "rung": "spy_2x", "symbol": "SSO", "side": "sell", "filled_qty": "2", "avg_price": "95"},
        {"event": "settled", "rung": "spy_2x", "symbol": "SSO", "side": "buy", "filled_qty": "0", "avg_price": "0"},
        {"event": "cash_adjustment", "rung": "spy_2x", "amount": "1.50"},
    ]
    book = replay_books(events, rungs)["spy_2x"]
    assert book.qty == {"SSO": D("8")} and book.cash == D("1000") - 900 + 190 + D("1.50")


def test_the_initial_build_buys_each_rung_to_its_weights_from_its_own_cash():
    config = LabConfig.load().ladder
    books = {r.name: Book(cash=D("33333.33")) for r in config.rungs}
    trades = plan(books, config.rungs, QUOTES, config, "buy")
    by = {(t.rung, t.symbol): t for t in trades}
    assert set(by) == {("spy_1_5x", "SPY"), ("spy_1_5x", "SSO"), ("spy_2x", "SSO"), ("qqq_2x", "QLD")}
    for rung in config.rungs:
        spent = sum(t.notional for t in trades if t.rung == rung.name)
        assert spent <= D("33333.33")
        assert spent >= D("33333.33") * (1 - config.cash_reserve_fraction) * D("0.997")
    half = by[("spy_1_5x", "SPY")].quantity * D("500.05")
    assert abs(half / (D("33333.33") * (1 - config.cash_reserve_fraction)) - D("0.5")) < D("0.002")
    assert plan(books, config.rungs, QUOTES, config, "sell") == []


def test_the_monthly_rebalance_restores_the_half_and_half():
    config = LabConfig.load().ladder
    rungs = tuple(r for r in config.rungs if r.name == "spy_1_5x")
    # SSO doubled against SPY: the rung is now overweight SSO
    books = {"spy_1_5x": Book(cash=D("100"), qty={"SPY": D("33"), "SSO": D("366")})}
    sells = plan(books, rungs, QUOTES, config, "sell")
    assert [(t.symbol, t.side) for t in sells] == [("SSO", "sell")]
    assert plan(books, rungs, QUOTES, config, "buy")[0].symbol == "SPY"


def test_small_drift_is_left_alone():
    config = LabConfig.load().ladder
    rungs = tuple(r for r in config.rungs if r.name == "spy_2x")
    nav = D("33000")
    qty = (nav * (1 - config.cash_reserve_fraction) / D("90.02")).quantize(D("0.000001"))
    books = {"spy_2x": Book(cash=nav - qty * D("90.02"), qty={"SSO": qty})}
    assert plan(books, rungs, QUOTES, config, "buy") == [] and plan(books, rungs, QUOTES, config, "sell") == []


# --------------------------------------------------------------- the engine

class FakeBroker:
    """A paper account that fills marketable limits at the limit."""

    equity_quantity_step = D("0.000000001")

    def __init__(self, cash=D("100000"), number="PA-LAB-1", is_open=True, fill=True):
        self.cash = cash
        self.qty: dict[str, D] = {}
        self.number = number
        self.is_open = is_open
        self.fill = fill
        self.orders: dict[str, dict] = {}
        self.cancelled: list[str] = []

    def account_snapshot(self):
        equity = self.cash + sum(q * QUOTES[s][0] for s, q in self.qty.items())
        return {"account_number": self.number, "cash": str(self.cash), "equity": str(equity)}

    def market_clock(self):
        return {"is_open": self.is_open}

    def get_positions(self):
        return [BrokerPosition(symbol=s, quantity=q, market_value=q * QUOTES[s][0], cost_basis=q * QUOTES[s][0])
                for s, q in self.qty.items() if q]

    def get_buying_power(self):
        return self.cash

    def submit_order(self, approved, client_reference=None):
        order = approved.order
        oid = f"o{len(self.orders) + 1}"
        price = order.execution.limit_price
        self.orders[oid] = {"symbol": order.symbol, "qty": order.quantity, "price": price,
                            "buy": order.kind == "equity_buy", "status": "new"}
        if self.fill:
            self._fill(oid)
        return OrderReceipt(broker_order_id=oid, status="new", symbol=order.symbol, quantity=order.quantity,
                            limit_price=price)

    def _fill(self, oid):
        o = self.orders[oid]
        sign = 1 if o["buy"] else -1
        self.qty[o["symbol"]] = self.qty.get(o["symbol"], D(0)) + sign * o["qty"]
        self.cash -= sign * o["qty"] * o["price"]
        o["status"] = "filled"

    def get_order(self, oid):
        o = self.orders[oid]
        filled = o["qty"] if o["status"] == "filled" else D(0)
        return OrderStatus(broker_order_id=oid, status=o["status"], filled_quantity=filled,
                           filled_avg_price=o["price"] if filled else None)

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        if self.orders[oid]["status"] != "filled":
            self.orders[oid]["status"] = "canceled"


@pytest.fixture
def lab_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.setenv("ALPACA_LAB_LADDER_API_KEY", "lab-key")
    monkeypatch.setenv("ALPACA_LAB_LADDER_API_SECRET", "lab-secret")
    monkeypatch.setenv("ALPACA_API_KEY", "main-key")
    monkeypatch.setenv("AGENTIC_LAB_DATA_ROOT", str(tmp_path / "data-lab"))
    return tmp_path


def _open(broker, tmp_path, main="PA-MAIN"):
    return open_account("ladder", LabConfig.load(), adapter=broker, main_account_number=lambda: main,
                        data_dir=tmp_path / "ladder", clock=lambda: NOW)


def _engine(account, **kwargs):
    return LadderEngine(account, LabConfig.load().ladder, quotes=lambda s: QUOTES[s], sleep=lambda s: None,
                        **kwargs)


def test_the_first_run_funds_three_rungs_and_builds_them(lab_env):
    broker = FakeBroker()
    account = _open(broker, lab_env)
    notes = _engine(account).tick(NOW)
    assert any("rebalance complete" in n for n in notes), notes
    events = account.ledger.events()
    assert [e["event"] for e in events][:4] == ["account_pinned", "rung_funded", "rung_funded", "rung_funded"]
    assert {s for s, q in broker.qty.items() if q} == {"SPY", "SSO", "QLD"}
    assert broker.cash >= 0 and broker.cash < D("100000") * D("0.01")
    assert account.state.engine["rebalanced_month"] == "2026-10"
    # a second run in the same month trades nothing
    before = len(broker.orders)
    notes = _engine(_open(broker, lab_env)).tick(NOW)
    assert len(broker.orders) == before and any("already done" in n for n in notes)
    # the rung books reconcile to the broker exactly
    books = replay_books(account.ledger.events(), LabConfig.load().ladder.rungs)
    for symbol, held in broker.qty.items():
        assert sum(b.qty.get(symbol, D(0)) for b in books.values()) == held


def test_the_next_month_rebalances_again(lab_env):
    broker = FakeBroker()
    _engine(_open(broker, lab_env)).tick(NOW)
    broker.cash += D("150")  # a dividend
    later = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    account = _open(broker, lab_env)
    notes = _engine(account).tick(later)
    assert any("cash adjustment" in n for n in notes) and account.state.engine["rebalanced_month"] == "2026-11"


def test_outside_the_session_the_ladder_waits(lab_env):
    broker = FakeBroker(is_open=False)
    account = _open(broker, lab_env)
    notes = _engine(account).tick(NOW)
    assert not broker.orders and any("outside the trading window" in n for n in notes)


def test_a_halt_freezes_the_ladder_both_ways(lab_env):
    broker = FakeBroker()
    _engine(_open(broker, lab_env)).tick(NOW)
    (lab_env / "ladder" / "HALT").write_text("test halt")
    account = _open(broker, lab_env)
    notes = _engine(account).tick(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
    assert any("FROZEN" in n for n in notes) and account.state.kill_switch_tripped
    assert [e for e in account.ledger.events(["kill_switch_tripped"])]
    orders = len(broker.orders)
    _engine(_open(broker, lab_env)).tick(datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc))
    assert len(broker.orders) == orders


def test_the_ladder_has_no_kill_switch_only_a_drawdown_alert(lab_env):
    """Ruling 2026-10-10, item 3a: through a 23% and then a 30% drawdown the
    ladder keeps rebalancing; at 25% it alerts once per peak."""
    broker = FakeBroker()
    _engine(_open(broker, lab_env)).tick(NOW)
    account = _open(broker, lab_env)
    account.state.high_water_mark = "130000"  # NAV ~100K: a 23% drawdown
    alerts = []
    notes = _engine(account, alert=lambda s, b: alerts.append(s)).tick(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
    assert not account.state.kill_switch_tripped and not any("FROZEN" in n for n in notes)
    assert account.state.engine["rebalanced_month"] == "2026-11" and alerts == []
    account = _open(broker, lab_env)
    account.state.high_water_mark = "145000"  # a 31% drawdown
    for day in (3, 4):
        account = _open(broker, lab_env)
        account.state.high_water_mark = "145000"
        notes = _engine(account, alert=lambda s, b: alerts.append(s)).tick(datetime(2026, 11, day, 15, 0, tzinfo=timezone.utc))
        assert not account.state.kill_switch_tripped
    assert len(alerts) == 1 and "drawdown" in alerts[0]
    assert len(account.ledger.events(["drawdown_alert"])) == 1


def test_every_other_lab_account_keeps_the_twelve_percent_switch():
    from lab.config import AccountConfig

    config = LabConfig.load()
    assert account_limits(config.account("ladder")).kill_switch.drawdown_from_high_water_mark == 1
    assert account_limits(config.account("ai-trader")).kill_switch.drawdown_from_high_water_mark == D("0.12")
    raw = config.model_dump(mode="json")
    raw["accounts"]["ai-trader"]["kill_switch"] = False
    with pytest.raises(Exception, match="only the leverage ladder"):
        LabConfig.model_validate(raw)

def test_an_unfilled_order_is_recovered_by_the_next_run(lab_env):
    broker = FakeBroker(fill=False)
    account = _open(broker, lab_env)
    clock = iter(range(0, 100000, 30))
    engine = LadderEngine(account, LabConfig.load().ladder, quotes=lambda s: QUOTES[s], sleep=lambda s: None,
                          monotonic=lambda: next(clock))
    notes = engine.tick(NOW)
    assert any("unfilled" in n for n in notes) and "rebalanced_month" not in account.state.engine
    assert all(o["status"] == "canceled" for o in broker.orders.values())
    settled = account.ledger.events(["settled"])
    assert len(settled) == len(account.ledger.events(["order"]))


def test_a_share_count_that_does_not_reconcile_freezes_trading(lab_env):
    broker = FakeBroker()
    _engine(_open(broker, lab_env)).tick(NOW)
    broker.qty["SSO"] += D("3")
    account = _open(broker, lab_env)
    notes = _engine(account).tick(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
    assert any("reconciles" in n for n in notes) and any("FROZEN" in n for n in notes)


def test_a_dry_run_submits_nothing_and_writes_nothing(lab_env):
    broker = FakeBroker()
    account = _open(broker, lab_env)
    before = account.ledger.events()
    notes = _engine(account, dry_run=True).tick(NOW)
    assert not broker.orders and any("DRY RUN would buy" in n for n in notes)
    assert account.ledger.events() == before


# -------------------------------------------------------------- refusals

def test_preflight_refuses_what_it_cannot_verify(lab_env, monkeypatch):
    with pytest.raises(LabRefused, match="MAIN book's account"):
        _open(FakeBroker(number="PA-MAIN"), lab_env)
    _open(FakeBroker(number="PA-LAB-1"), lab_env)
    with pytest.raises(LabRefused, match="pinned"):
        _open(FakeBroker(number="PA-OTHER"), lab_env)
    monkeypatch.setenv("ALPACA_LAB_LADDER_API_KEY", "main-key")
    with pytest.raises(LabRefused, match="MAIN book's API key"):
        _open(FakeBroker(), lab_env)
    monkeypatch.delenv("ALPACA_LAB_LADDER_API_KEY")
    with pytest.raises(LabRefused, match="no keys"):
        _open(FakeBroker(), lab_env)
    with pytest.raises(LabRefused, match="not enabled"):
        open_account("ai-trader", LabConfig.load(), adapter=FakeBroker())


def test_the_lab_refuses_to_run_live(lab_env, monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "false")
    monkeypatch.setenv(LIVE_CONFIRMATION_VARIABLE, LIVE_CONFIRMATION_PHRASE)  # scoped to this test
    with pytest.raises(LabRefused, match="paper only"):
        _open(FakeBroker(), lab_env)
