"""The four defects of the 2026-09-28 integrity check.

1. Judged exits (and mechanical time exits) sell at the BID rounded down when a
   bid is quoted — TPVG's trailing stop rested at the ask three sessions running
   — and a position whose exit is working stays in the review cycle.
2. The unserved-symbols memo blacklists only after repeated client errors on
   distinct days; a successful fetch clears the strikes; legacy entries stand.
3. (The overreaction timer is a systemd unit; nothing to test here.)
4. Alerts have an HTTPS transport (SendGrid) tried before SMTP, and every
   delivery outcome lands in a status file that health renders.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from execution.alerts import Alerter
from execution.environment import LIVE_CONFIRMATION_VARIABLE
from execution.market_data import UNSERVED_STRIKES, AlpacaDailyBars, UnservedSymbols
from orchestrator.ops import RunLog, health_report

from test_exits import MutablePrices, enter_position
from test_orchestrator import FakeBroker, FakeClock, counter
from test_ops import inert_engine, preflight_kwargs
from test_sweep import TwoSidedPrices


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    monkeypatch.delenv(LIVE_CONFIRMATION_VARIABLE, raising=False)


@pytest.fixture(scope="session")
def limits():
    from risk_gate import RiskLimits

    # Pre-redirect weights (judged 55, aggressive 0): this file tests judged
    # mechanics; the shipped 30/25/15/30 split is pinned in test_aggressive.
    from config_overrides import pre_redirect_limits

    return pre_redirect_limits()


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.fixture(scope="session")
def research_config():
    from research.config import ResearchConfig

    return ResearchConfig.load()


# ================================================================================
# 1. Exit pricing and the review cycle
# ================================================================================


def test_a_guardrail_exit_limits_at_the_bid_when_one_is_quoted(
    tmp_path, limits, signals_config, research_config
):
    prices = TwoSidedPrices(NUE="140.00")  # bid a cent under the ask
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config, prices=prices
    )
    broker = started.adapter
    prices.set("NUE", "119.00")  # through the stop
    started.loop.tick()
    sell = broker.payloads[-1]
    assert sell["symbol"] == "NUE"
    assert sell["limit_price"] == Decimal("118.99")  # the bid, not the ask
    started.loop.tick()
    trail = started.audit.trail("dec-1")
    assert trail.outcome is not None
    assert trail.outcome.realised_pnl == Decimal("19") * (Decimal("118.99") - Decimal("140"))


def test_without_a_bid_source_the_mark_rounded_down_is_still_the_limit(
    tmp_path, limits, signals_config, research_config
):
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config
    )
    prices.set("NUE", "119.00")
    started.loop.tick()
    assert started.adapter.payloads[-1]["limit_price"] == Decimal("119.00")


def test_a_position_with_a_working_exit_stays_in_the_review_cycle(
    tmp_path, limits, signals_config, research_config
):
    """TPVG (2026-09-24..28): three unfilled exit attempts, "reviewed never".
    A working exit no longer removes a position from the review queue; the
    review runs, and its verdict cannot place a second order while the first
    works."""
    started, prices, clock = enter_position(
        tmp_path, limits, signals_config, research_config
    )
    broker = started.adapter
    broker.fill = "new"  # the exit will rest
    prices.set("NUE", "119.00")
    report = started.loop.tick()
    assert report.exits_started == 1
    position = started.exits.tracked[0]
    assert position.pending_exit is not None
    orders_before = len(broker.submitted)

    clock.advance(hours=25)  # past the 24h cadence
    report = started.loop.tick()
    assert report.reviews_run == 1
    assert position.last_review_at is not None
    assert len(broker.submitted) == orders_before  # no second exit while one works
    assert position.pending_exit is not None


# ================================================================================
# 2. The unserved memo needs strikes on distinct days
# ================================================================================


def test_one_client_error_does_not_blacklist_and_three_days_do(tmp_path):
    memo = UnservedSymbols(tmp_path / "unserved.json")
    day = datetime(2026, 9, 25, 21, 1, tzinfo=timezone.utc)
    memo.add("INTC", 400, day)
    assert "INTC" not in memo and memo.strikes("INTC") == 1
    memo.add("INTC", 400, day + timedelta(hours=1))  # same day: still one strike
    assert memo.strikes("INTC") == 1
    memo.add("INTC", 400, day + timedelta(days=1))
    assert "INTC" not in memo and memo.strikes("INTC") == 2
    memo.add("INTC", 400, day + timedelta(days=2))
    assert "INTC" in memo and memo.strikes("INTC") == UNSERVED_STRIKES
    assert memo.symbols() == ("INTC",)
    # Persisted, and re-read the same way.
    again = UnservedSymbols(tmp_path / "unserved.json")
    assert "INTC" in again and len(again) == 1


def test_a_successful_fetch_clears_the_strikes_but_never_a_legacy_entry(tmp_path):
    path = tmp_path / "unserved.json"
    path.write_text(json.dumps({"AXIA3": {"first_seen": "2026-09-04T16:29:28+00:00", "status": 400}}))
    memo = UnservedSymbols(path)
    assert "AXIA3" in memo  # written before the ruling: permanent as recorded
    memo.add("BBD", 400, datetime(2026, 9, 25, tzinfo=timezone.utc))
    assert "BBD" not in memo and memo.strikes("BBD") == 1
    memo.served("BBD")
    assert memo.strikes("BBD") == 0
    memo.served("AXIA3")
    assert "AXIA3" in memo
    assert json.loads(path.read_text())["AXIA3"]["status"] == 400


def test_the_bars_client_strikes_on_a_400_and_clears_on_a_200(tmp_path):
    memo = UnservedSymbols(tmp_path / "unserved.json")
    now = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)
    answers = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        answers["n"] += 1
        if answers["n"] == 1:
            return httpx.Response(400, json={"message": "bad request"})
        return httpx.Response(200, json={"bars": [{"t": "2026-09-25T04:00:00Z", "c": 1}]})

    bars = AlpacaDailyBars(
        httpx.Client(base_url="https://data.test", transport=httpx.MockTransport(handler)),
        feed="sip",
        unserved=memo,
        clock=lambda: now,
    )
    assert bars.bars("INTC", now - timedelta(days=10), now) == []
    assert memo.strikes("INTC") == 1 and "INTC" not in memo
    rows = bars.bars("INTC", now - timedelta(days=10), now)  # still requested
    assert rows and memo.strikes("INTC") == 0


# ================================================================================
# 4. HTTPS transport and the delivery status file
# ================================================================================


def _https_alerter(monkeypatch, tmp_path, post, smtp=False):
    monkeypatch.setenv("SENDGRID_API_KEY", "sg-test-key")
    monkeypatch.setenv("ALERT_TO", "ops@example.test")
    monkeypatch.setenv("ALERT_FROM", "agentic@example.test")
    if smtp:
        monkeypatch.setenv("ALERT_SMTP_USER", "agentic@example.test")
        monkeypatch.setenv("ALERT_SMTP_PASSWORD", "app-password")
    else:
        monkeypatch.delenv("ALERT_SMTP_USER", raising=False)
        monkeypatch.delenv("ALERT_SMTP_PASSWORD", raising=False)
    return Alerter(
        clock=FakeClock(),
        http_post=post,
        status_path=tmp_path / "alerts_status.json",
    )


def test_sendgrid_is_tried_first_and_the_status_file_records_the_delivery(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=202, text="")

    alerter = _https_alerter(monkeypatch, tmp_path, post, smtp=True)
    assert alerter.transports == ("sendgrid", "smtp")
    assert alerter.send_test() is True
    url, kwargs = calls[-1]
    assert url == "https://api.sendgrid.com/v3/mail/send"
    assert kwargs["headers"]["Authorization"] == "Bearer sg-test-key"
    assert kwargs["json"]["personalizations"][0]["to"][0]["email"] == "ops@example.test"
    assert kwargs["json"]["from"]["email"] == "agentic@example.test"
    assert "[AGENTIC DAILY] test message" == kwargs["json"]["subject"]
    status = json.loads((tmp_path / "alerts_status.json").read_text())
    assert status["last_success"]["transport"] == "sendgrid"
    assert status["consecutive_failures"] == 0


def test_a_failed_https_send_is_recorded_and_never_raises_into_the_loop(monkeypatch, tmp_path):
    def post(url, **kwargs):
        return SimpleNamespace(status_code=401, text="unauthorized")

    alerter = _https_alerter(monkeypatch, tmp_path, post)
    assert alerter.transports == ("sendgrid",)
    assert alerter.send_test() is False  # logged, not raised
    status = json.loads((tmp_path / "alerts_status.json").read_text())
    assert "SendGrid HTTP 401" in status["last_failure"]["detail"]
    assert status["consecutive_failures"] == 1
    assert "last_success" not in status


def test_health_renders_the_alert_delivery_line(
    tmp_path, limits, signals_config, research_config
):
    checks = __import__("orchestrator").preflight(
        adapter=FakeBroker(),
        id_factory=counter("h"),
        **preflight_kwargs(tmp_path, limits, signals_config, research_config),
    )
    report = health_report(checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log"))
    assert "alerts: no delivery attempted yet" in report
    (tmp_path / "alerts_status.json").write_text(
        json.dumps(
            {
                "last_failure": {"at": "2026-09-24T16:16:27+00:00", "detail": "smtp: OSError: Network is unreachable"},
                "consecutive_failures": 13,
                "transports": ["smtp"],
            }
        )
    )
    report = health_report(checks, inert_engine(checks).tracked, RunLog(tmp_path / "run.log"))
    assert "alerts: NEVER delivered" in report
    assert "13 consecutive failure(s) - needs a human" in report
    assert "Network is unreachable" in report
