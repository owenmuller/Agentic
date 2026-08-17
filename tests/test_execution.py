"""Execution layer tests — payload translation, the no-bypass boundary, PAPER_MODE.

These run against a mock transport, so they assert the exact bytes the adapter would
put on the wire without touching a broker. The integration suite in
``test_execution_integration.py`` covers the real round trip.
"""

import ast
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from execution import (
    PAPER_BASE_URL,
    AlpacaAdapter,
    BrokerRejected,
    UnsupportedInstrument,
    paper_mode,
)
from risk_gate import (
    AccountState,
    ApprovedOrder,
    EquityBuyOrder,
    EquitySellToCloseOrder,
    EventContractBuyOrder,
    LimitExecution,
    MarketExecution,
    OptionBuyToOpenOrder,
    RiskGate,
    RiskLimits,
)

JUSTIFICATION = "Tariff headline; the limit would not fill before the move completes."
START_CASH = Decimal("100000")


@pytest.fixture(scope="session")
def limits() -> RiskLimits:
    return RiskLimits.load()


@pytest.fixture
def gate(limits) -> RiskGate:
    return RiskGate(
        limits, AccountState(cash=START_CASH, high_water_mark=START_CASH)
    )


class Recorder:
    """Captures requests and replays canned responses."""

    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response or httpx.Response(200, json={})

    @property
    def last_payload(self) -> dict:
        return json.loads(self.requests[-1].content)


def adapter_with(response: httpx.Response | None = None) -> tuple[AlpacaAdapter, Recorder]:
    recorder = Recorder(response)
    client = httpx.Client(
        base_url=PAPER_BASE_URL, transport=httpx.MockTransport(recorder.handler)
    )
    return AlpacaAdapter(client=client), recorder


ORDER_ACCEPTED = httpx.Response(
    200,
    json={
        "id": "b1e6f0aa-0000-4000-8000-000000000001",
        "client_order_id": "agentic-1",
        "status": "accepted",
        "symbol": "AAPL",
        "qty": "10",
        "limit_price": "100.00",
        "submitted_at": "2026-08-17T14:30:00Z",
    },
)


# ================================================================================
# The no-bypass boundary
# ================================================================================


def test_submit_order_refuses_an_unapproved_order(gate):
    adapter, _ = adapter_with(ORDER_ACCEPTED)
    raw = EquityBuyOrder(
        symbol="AAPL", quantity=1, execution=LimitExecution(limit_price=Decimal("100"))
    )
    with pytest.raises(TypeError, match="ApprovedOrder"):
        adapter.submit_order(raw)


@pytest.mark.parametrize("impostor", [None, {"symbol": "AAPL"}, "AAPL", 42])
def test_submit_order_refuses_anything_that_is_not_an_approved_order(impostor):
    adapter, _ = adapter_with(ORDER_ACCEPTED)
    with pytest.raises(TypeError):
        adapter.submit_order(impostor)


def test_nothing_can_forge_an_approval_for_the_adapter():
    """The only route to a submittable object is RiskGate.submit()."""
    with pytest.raises(PermissionError):
        ApprovedOrder(
            object(),
            EquityBuyOrder(
                symbol="AAPL",
                quantity=1,
                execution=LimitExecution(limit_price=Decimal("100")),
            ),
            Decimal("100"),
            None,
            1,
        )


# ================================================================================
# Payload translation
# ================================================================================


def test_limit_order_is_sent_at_its_limit_price(gate):
    adapter, recorder = adapter_with(ORDER_ACCEPTED)
    approved = gate.submit(
        EquityBuyOrder(
            symbol="AAPL",
            quantity=10,
            execution=LimitExecution(limit_price=Decimal("100.00")),
        )
    )
    adapter.submit_order(approved)
    payload = recorder.last_payload
    assert payload["type"] == "limit"
    assert payload["limit_price"] == "100.00"
    assert payload["side"] == "buy"
    assert payload["qty"] == "10"
    assert payload["symbol"] == "AAPL"


def test_market_execution_is_sent_as_a_limit_at_max_price(gate):
    """A fill above the reserved bound becomes impossible at the venue."""
    adapter, recorder = adapter_with(ORDER_ACCEPTED)
    order = EquityBuyOrder(
        symbol="AAPL",
        quantity=10,
        execution=MarketExecution(
            justification=JUSTIFICATION, max_price=Decimal("105.00")
        ),
    )
    approved = gate.submit(order)
    adapter.submit_order(approved)

    payload = recorder.last_payload
    assert payload["type"] == "limit", "market executions must never leave as market"
    assert Decimal(payload["limit_price"]) == order.execution.price_bound
    # The limit is exactly what the gate cash-secured against.
    assert Decimal(payload["limit_price"]) * 10 == approved.max_loss


def test_sell_to_close_maps_to_a_sell(gate):
    adapter, recorder = adapter_with(ORDER_ACCEPTED)
    opened = gate.submit(
        EquityBuyOrder(
            symbol="AAPL",
            quantity=10,
            execution=LimitExecution(limit_price=Decimal("100.00")),
        )
    )
    gate.record_fill(opened, Decimal("100.00"))
    closed = gate.submit(
        EquitySellToCloseOrder(
            symbol="AAPL",
            quantity=10,
            execution=LimitExecution(limit_price=Decimal("110.00")),
        )
    )
    adapter.submit_order(closed)
    assert recorder.last_payload["side"] == "sell"


def test_option_order_sends_the_occ_symbol_and_contract_count(gate):
    adapter, recorder = adapter_with(ORDER_ACCEPTED)
    approved = gate.submit(
        OptionBuyToOpenOrder(
            symbol="AAPL260117C00250000",
            underlying="AAPL",
            right="call",
            expiration=date(2026, 1, 17),
            strike=Decimal("250.00"),
            contracts=3,
            execution=LimitExecution(limit_price=Decimal("1.50")),
        )
    )
    adapter.submit_order(approved)
    payload = recorder.last_payload
    assert payload["symbol"] == "AAPL260117C00250000"
    assert payload["qty"] == "3"  # contracts, not shares
    assert payload["limit_price"] == "1.50"  # premium per share


def test_event_contracts_are_not_routed_to_alpaca(gate):
    adapter, _ = adapter_with(ORDER_ACCEPTED)
    approved = gate.submit(
        EventContractBuyOrder(
            market_ticker="PRES-2028-D",
            outcome="yes",
            contracts=10,
            strategy="arb",
            execution=LimitExecution(limit_price=Decimal("0.50")),
        )
    )
    with pytest.raises(UnsupportedInstrument, match="Kalshi"):
        adapter.submit_order(approved)


def test_client_order_id_carries_the_gate_sequence(gate):
    adapter, recorder = adapter_with(ORDER_ACCEPTED)
    approved = gate.submit(
        EquityBuyOrder(
            symbol="AAPL",
            quantity=1,
            execution=LimitExecution(limit_price=Decimal("100.00")),
        )
    )
    adapter.submit_order(approved)
    assert recorder.last_payload["client_order_id"] == f"agentic-{approved.sequence}"


# ================================================================================
# Responses
# ================================================================================


def test_receipt_is_built_from_the_broker_response(gate):
    adapter, _ = adapter_with(ORDER_ACCEPTED)
    approved = gate.submit(
        EquityBuyOrder(
            symbol="AAPL",
            quantity=10,
            execution=LimitExecution(limit_price=Decimal("100.00")),
        )
    )
    receipt = adapter.submit_order(approved)
    assert receipt.broker_order_id == "b1e6f0aa-0000-4000-8000-000000000001"
    assert receipt.status == "accepted"
    assert receipt.quantity == Decimal("10")
    assert receipt.submitted_at is not None


def test_broker_error_is_raised_with_the_body_for_the_audit_log(gate):
    adapter, _ = adapter_with(
        httpx.Response(403, text='{"message":"insufficient buying power"}')
    )
    approved = gate.submit(
        EquityBuyOrder(
            symbol="AAPL",
            quantity=1,
            execution=LimitExecution(limit_price=Decimal("100.00")),
        )
    )
    with pytest.raises(BrokerRejected) as caught:
        adapter.submit_order(approved)
    assert caught.value.status_code == 403
    assert "insufficient buying power" in caught.value.body


def test_get_buying_power_reports_cash_not_margin_buying_power():
    """Constraint #1: borrowed buying power must never be reported as spendable."""
    adapter, _ = adapter_with(
        httpx.Response(
            200, json={"cash": "12345.67", "buying_power": "49382.68", "equity": "0"}
        )
    )
    assert adapter.get_buying_power() == Decimal("12345.67")


def test_get_positions_parses_the_broker_view():
    adapter, _ = adapter_with(
        httpx.Response(
            200,
            json=[
                {
                    "symbol": "AAPL",
                    "qty": "10",
                    "market_value": "2500.00",
                    "cost_basis": "2400.00",
                }
            ],
        )
    )
    positions = adapter.get_positions()
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == Decimal("10")


def test_cancel_order_issues_a_delete():
    adapter, recorder = adapter_with(httpx.Response(204))
    adapter.cancel_order("abc-123")
    assert recorder.requests[-1].method == "DELETE"
    assert recorder.requests[-1].url.path == "/v2/orders/abc-123"


# ================================================================================
# PAPER_MODE — Constraint #4
# ================================================================================


def test_paper_mode_defaults_to_true_when_unset(monkeypatch):
    monkeypatch.delenv("PAPER_MODE", raising=False)
    assert paper_mode() is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " false "])
def test_paper_mode_is_disabled_only_by_explicit_false_values(monkeypatch, value):
    monkeypatch.setenv("PAPER_MODE", value)
    assert paper_mode() is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "maybe", "flase"])
def test_anything_ambiguous_leaves_paper_mode_on(monkeypatch, value):
    """Constraint #6: a typo must fail safe, not silently enable live trading."""
    monkeypatch.setenv("PAPER_MODE", value)
    assert paper_mode() is True


def test_adapter_points_at_the_paper_endpoint_by_default(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    adapter = AlpacaAdapter()
    assert adapter.paper is True
    assert adapter.base_url == PAPER_BASE_URL
    adapter.close()


def test_live_mode_warns_loudly(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "false")
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    with pytest.warns(UserWarning, match="LIVE"):
        adapter = AlpacaAdapter()
    adapter.close()


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id == "environ"


class EnvironmentWriteFinder(ast.NodeVisitor):
    """Finds real writes to the process environment, ignoring prose about them."""

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.offenders: list[str] = []

    def _flag(self, node: ast.AST, what: str) -> None:
        self.offenders.append(f"{self.filename}:{node.lineno}: {what}")

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript) and _is_environ(target.value):
                self._flag(node, "assignment to os.environ[...]")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            mutators = {"setdefault", "update", "pop", "__setitem__", "clear"}
            if func.attr in mutators and _is_environ(func.value):
                self._flag(node, f"os.environ.{func.attr}()")
            if func.attr in {"putenv", "unsetenv"}:
                self._flag(node, f"os.{func.attr}()")
        self.generic_visit(node)


def test_no_source_file_ever_writes_to_the_environment():
    """Constraint #4: the agent must never set, or write code that sets, PAPER_MODE.

    Parsed rather than grepped — an earlier string-matching version flagged a
    docstring that merely *described* the rule.
    """
    src = Path(__file__).resolve().parents[1] / "src"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        finder = EnvironmentWriteFinder(path.name)
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        offenders.extend(finder.offenders)
    assert offenders == [], f"source writes to the environment: {offenders}"


def test_the_environment_guard_would_catch_a_real_write(tmp_path):
    """The guard above is only worth having if it fails on the thing it forbids."""
    offender = tmp_path / "bad.py"
    offender.write_text(
        "import os\n"
        "# a docstring mentioning os.environ[...] = must not trip this\n"
        "os.environ['PAPER_MODE'] = 'false'\n",
        encoding="utf-8",
    )
    finder = EnvironmentWriteFinder(offender.name)
    finder.visit(ast.parse(offender.read_text(encoding="utf-8")))
    assert len(finder.offenders) == 1
    assert "os.environ[...]" in finder.offenders[0]
