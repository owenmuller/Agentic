"""Integration tests against a real Alpaca **paper** account.

Auto-skipped unless credentials are present, so the default suite stays offline and
hermetic. To run them, put these in ``.env`` (gitignored) at the repo root:

    PAPER_MODE=true
    ALPACA_API_KEY=<paper key id>
    ALPACA_API_SECRET=<paper secret>

Generate the pair at https://app.alpaca.markets under Paper Trading → API Keys. The
key id starts with ``PK`` for paper accounts; a live key (``AK``) will make these
tests refuse to run.

These tests place a real order on the paper account. It is priced far below market so
it rests unfilled, and it is cancelled in teardown — but it is a real order on a real
(simulated) book, not a mock.
"""

from decimal import Decimal

import pytest

from execution import AlpacaAdapter, PAPER_BASE_URL, load_environment, paper_mode
from execution.environment import require_env
from risk_gate import (
    AccountState,
    EquityBuyOrder,
    LimitExecution,
    RiskGate,
    RiskLimits,
)

pytestmark = pytest.mark.integration

load_environment()


def _credentials_present() -> bool:
    try:
        require_env("ALPACA_API_KEY")
        require_env("ALPACA_API_SECRET")
    except KeyError:
        return False
    return True


needs_keys = pytest.mark.skipif(
    not _credentials_present(),
    reason="no Alpaca paper credentials in .env — see this module's docstring",
)


@pytest.fixture(scope="module")
def adapter():
    assert paper_mode(), "refusing to run integration tests with PAPER_MODE disabled"
    broker = AlpacaAdapter()
    assert broker.base_url == PAPER_BASE_URL, "integration tests are paper-only"
    yield broker
    broker.close()


@needs_keys
def test_paper_key_is_not_a_live_key():
    """Live keys start with AK. Refuse before anything is sent."""
    key = require_env("ALPACA_API_KEY")
    assert key.startswith("PK"), (
        "ALPACA_API_KEY does not look like a paper key (expected a PK... id)"
    )


@needs_keys
def test_account_is_reachable_and_reports_cash(adapter):
    cash = adapter.get_buying_power()
    assert isinstance(cash, Decimal)
    assert cash >= 0


@needs_keys
def test_positions_are_retrievable(adapter):
    positions = adapter.get_positions()
    assert isinstance(positions, list)
    for position in positions:
        assert position.symbol


@needs_keys
def test_resting_order_round_trip(adapter):
    """Submit a gate-approved order that will not fill, then cancel it.

    Priced at $1 on a liquid name so it rests at the bottom of the book. If this ever
    fills, something is very wrong with the market, not with this test.
    """
    cash = adapter.get_buying_power()
    if cash < Decimal("100"):
        pytest.skip(f"paper account has only {cash} cash; fund it to run this test")

    gate = RiskGate(
        RiskLimits.load(), AccountState(cash=cash, high_water_mark=cash)
    )
    decision = gate.submit(
        EquityBuyOrder(
            symbol="AAPL",
            quantity=1,
            execution=LimitExecution(limit_price=Decimal("1.00")),
        )
    )
    assert decision.is_approved, f"gate rejected the probe order: {decision}"

    receipt = adapter.submit_order(decision)
    try:
        assert receipt.broker_order_id
        assert receipt.status in {"accepted", "new", "pending_new"}
        assert receipt.limit_price == Decimal("1.00")
    finally:
        adapter.cancel_order(receipt.broker_order_id)


@needs_keys
def test_account_permissions_are_visible_and_match_the_known_paper_config(adapter):
    """The real paper account, as verified 2026-08-18: options fixed at level 3 by
    Alpaca, shorting disabled, margin 1x. Level 3 exceeds the system's level-2 need,
    which is exactly what the preflight warning exists to say out loud."""
    permissions = adapter.permissions()

    assert permissions.options_level >= 2, "account cannot buy calls/puts"
    assert permissions.shorting_enabled is False
    assert permissions.margin_multiplier == Decimal("1")
    # The one expected excess on this account: level 3 permits spreads.
    findings = permissions.excess_permissions()
    assert all("shorting" not in finding for finding in findings)
    assert all("margin" not in finding.lower() for finding in findings)


@needs_keys
def test_the_account_can_actually_see_the_options_market(adapter):
    """Visibility as a fact, not an inference from the approval level: the contracts
    endpoint returns real OCC symbols for a liquid underlying."""
    contracts = adapter.option_contracts("AAPL", limit=5)

    assert contracts, "no option contracts visible for AAPL"
    for symbol in contracts:
        assert symbol.startswith("AAPL")
        assert len(symbol) >= 16  # OCC symbology: root + yymmdd + C/P + strike
