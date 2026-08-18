"""AlpacaPriceSource tests.

The property that matters most: **an outage never reads as a price.** The exit
engine compares this source's output against max-loss stops, so a failure mode that
surfaced as zero would fire every stop in the book on a data hiccup. Every failure
path must come back as None — and None already has safe handling everywhere (no
order; skip that symbol's stop check this tick).

Unit tests run against a mock transport. The integration test at the bottom hits the
real IEX feed and auto-skips without Alpaca credentials in ``.env``, like the broker
integration tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from execution import DATA_BASE_URL, AlpacaPriceSource
from execution.environment import load_environment, require_env

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=timezone.utc)

#: A quote timestamp well inside the default 300s staleness threshold.
FRESH = (NOW - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")


class Recorder:
    def __init__(self, response: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


def source_for(response: httpx.Response, **kwargs) -> tuple[AlpacaPriceSource, Recorder]:
    recorder = Recorder(response)
    client = httpx.Client(
        base_url=DATA_BASE_URL, transport=httpx.MockTransport(recorder.handler)
    )
    kwargs.setdefault("clock", lambda: NOW)
    return AlpacaPriceSource(client, **kwargs), recorder


def quote_response(**quote) -> httpx.Response:
    return httpx.Response(200, json={"symbol": "AAPL", "quote": quote})


# ================================================================================
# The happy path, and which side gets used
# ================================================================================


def test_a_fresh_two_sided_quote_returns_the_ask():
    source, recorder = source_for(
        quote_response(t=FRESH, ap=227.05, bp=227.03, **{"as": 3, "bs": 5})
    )
    price = source("AAPL")

    assert price == Decimal("227.05")
    assert isinstance(price, Decimal)
    request = recorder.requests[0]
    assert request.url.path == "/v2/stocks/AAPL/quotes/latest"
    assert request.url.params["feed"] == "iex"


def test_a_one_sided_quote_falls_back_to_the_bid():
    """IEX quotes legitimately carry ap=0 when no ask is on record. A bid still
    bounds a buy safely — a limit at the bid cannot fill above the bid."""
    source, _ = source_for(quote_response(t=FRESH, ap=0, bp=226.90))
    assert source("AAPL") == Decimal("226.90")


def test_the_configured_feed_is_requested():
    source, recorder = source_for(
        quote_response(t=FRESH, ap=10.0, bp=9.99), feed="sip"
    )
    source("AAPL")
    assert recorder.requests[0].url.params["feed"] == "sip"


def test_the_symbol_is_url_quoted():
    source, recorder = source_for(quote_response(t=FRESH, ap=10.0, bp=9.99))
    source("BRK.B")
    assert "BRK.B" in str(recorder.requests[0].url)


# ================================================================================
# The zero-price safety property
# ================================================================================


def test_a_quote_with_no_priced_side_is_missing_not_zero():
    """The case the module exists to get right: ap=0, bp=0 must never become
    Decimal("0"), which would sit below every stop in the book."""
    source, _ = source_for(quote_response(t=FRESH, ap=0, bp=0))
    price = source("AAPL")
    assert price is None
    assert price != Decimal("0")


def test_an_http_error_is_a_missing_price_not_a_zero():
    source, _ = source_for(httpx.Response(500, text="upstream exploded"))
    assert source("AAPL") is None


def test_a_not_found_symbol_is_a_missing_price():
    source, _ = source_for(httpx.Response(404, json={"message": "not found"}))
    assert source("ZZZZZT") is None


def test_a_transport_failure_is_a_missing_price_not_an_exception():
    """The loop calls this inside stop checks; a dead feed must not kill the tick."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network is down", request=request)

    client = httpx.Client(
        base_url=DATA_BASE_URL, transport=httpx.MockTransport(explode)
    )
    source = AlpacaPriceSource(client, clock=lambda: NOW)
    assert source("AAPL") is None


def test_a_non_json_body_is_a_missing_price():
    source, _ = source_for(httpx.Response(200, text="<html>maintenance</html>"))
    assert source("AAPL") is None


def test_a_body_without_a_quote_is_a_missing_price():
    source, _ = source_for(httpx.Response(200, json={"symbol": "AAPL"}))
    assert source("AAPL") is None


def test_a_negative_price_is_not_a_price():
    source, _ = source_for(quote_response(t=FRESH, ap=-1.0, bp=-2.0))
    assert source("AAPL") is None


@pytest.mark.parametrize(
    "quote",
    [
        {"t": FRESH, "ap": 0, "bp": 0},
        {"t": FRESH},
        {"ap": 100.0, "bp": 99.0},  # no timestamp
        {"t": "not-a-timestamp", "ap": 100.0, "bp": 99.0},
    ],
)
def test_no_degraded_quote_shape_ever_returns_zero(quote):
    """The property, swept across the failure shapes: None or a positive Decimal."""
    source, _ = source_for(quote_response(**quote))
    price = source("AAPL")
    assert price is None or price > Decimal("0")


# ================================================================================
# Staleness
# ================================================================================


def stamp(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def test_a_quote_older_than_the_threshold_is_missing():
    source, _ = source_for(quote_response(t=stamp(301), ap=227.05, bp=227.03))
    assert source("AAPL") is None


def test_a_quote_exactly_at_the_threshold_still_counts():
    """"Older than" is strict: at the threshold is not past it."""
    source, _ = source_for(quote_response(t=stamp(300), ap=227.05, bp=227.03))
    assert source("AAPL") == Decimal("227.05")


def test_the_threshold_is_configurable():
    source, _ = source_for(
        quote_response(t=stamp(45), ap=227.05, bp=227.03), max_quote_age_seconds=30
    )
    assert source("AAPL") is None


def test_a_missing_timestamp_is_treated_as_stale():
    """A freshness claim that cannot be checked is not a freshness claim."""
    source, _ = source_for(quote_response(ap=227.05, bp=227.03))
    assert source("AAPL") is None


def test_nanosecond_timestamps_parse():
    """Alpaca sends RFC3339 with nanoseconds, which fromisoformat refuses raw."""
    raw = (NOW - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S") + ".123456789Z"
    source, _ = source_for(quote_response(t=raw, ap=227.05, bp=227.03))
    assert source("AAPL") == Decimal("227.05")


def test_a_zero_threshold_is_rejected_at_construction():
    with pytest.raises(ValueError, match="positive"):
        AlpacaPriceSource(httpx.Client(), max_quote_age_seconds=0)


# ================================================================================
# Integration — real IEX quote via paper keys; auto-skipped without them
# ================================================================================


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


@pytest.mark.integration
@needs_keys
def test_a_real_quote_comes_back_positive():
    """The staleness window is a week so this passes outside market hours too —
    what is under test is the wire format and auth, not the market being open."""
    with AlpacaPriceSource(max_quote_age_seconds=7 * 86400) as source:
        price = source("AAPL")
        assert isinstance(price, Decimal)
        assert price > Decimal("0")


@pytest.mark.integration
@needs_keys
def test_a_real_unknown_symbol_is_missing_not_zero():
    with AlpacaPriceSource(max_quote_age_seconds=7 * 86400) as source:
        assert source("ZZZZZT") is None
