"""Non-US symbols never enter the funnel, and the data API is never asked
twice about a symbol it does not serve (ruling 2026-09-04: AXIA3, a B3 symbol
from a Brazilian issuer's Form 4, entered as a no_cluster row and 400ed the bars
API on every weekly report).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from execution.market_data import DATA_BASE_URL, AlpacaDailyBars, UnservedSymbols
from orchestrator.overreaction import build_universe
from signals.classification import is_us_listed_symbol
from signals.form13d import tickers_from_display
from test_form4 import fetcher_with, form4_xml


@pytest.fixture(scope="session")
def signals_config():
    from signals.config import SignalsConfig

    return SignalsConfig.load()


@pytest.mark.parametrize(
    "symbol,ok",
    [
        ("AAPL", True), ("F", True), ("GOOGL", True), ("BRK.B", True), ("BRK-B", True),
        ("RDS.A", True),
        ("AXIA3", False), ("PETR4", False), ("0700", False), ("ABCDEF", False),
        ("aapl", False), ("", False), ("AA PL", False), ("BRK.BB.C", False),
    ],
)
def test_the_us_listed_shape_rule(symbol, ok):
    assert is_us_listed_symbol(symbol) is ok


def test_a_foreign_issuers_form4_is_tallied_and_never_emitted(signals_config):
    filing = (
        "0001213900-26-096631", "0009999999", "2026-09-03",
        form4_xml("0009999999", "Batista de Lima Filho Pedro", shares=93_600,
                  price=10.27, issuer_symbol="AXIA3"),
    )
    source = signals_config.source("class_2", "form4_insiders")
    fetcher, _ = fetcher_with([filing])
    items = fetcher(source)
    assert items == []
    # And the same filing with a US symbol IS a (single) row — so the shape
    # rule, not something else, is what stopped it.
    fetcher_us, _ = fetcher_with([(filing[0], filing[1], filing[2],
                                   form4_xml("0009999999", "Batista de Lima Filho Pedro",
                                             shares=93_600, price=10.27))])
    assert len(fetcher_us(source)) == 1


def test_13d_display_tickers_drop_foreign_shapes():
    assert tickers_from_display("AXIA Energia S.A. (AXIA3)") == ()
    assert tickers_from_display("Amarin Corp plc (AMRN)") == ("AMRN",)
    assert tickers_from_display("Two Class Co (TCC, TCC.B, PETR4)") == ("TCC", "TCC.B")


def test_the_screen_universe_never_admits_a_foreign_symbol():
    universe = build_universe(held=["INTC"], researched=["AXIA3"], active_purchase=["PETR4", "AMRN"])
    assert set(universe) == {"INTC", "AMRN"}


def test_an_unserved_symbol_is_asked_once_and_persisted(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if "AXIA3" in request.url.path:
            return httpx.Response(400, json={"message": "invalid symbol"})
        if "RATE" in request.url.path:
            return httpx.Response(429, json={"message": "too many requests"})
        if "PLAN" in request.url.path:
            return httpx.Response(403, json={"message": "subscription does not permit"})
        return httpx.Response(200, json={"bars": [{"t": "2026-09-01T04:00:00Z", "c": "100", "v": "1"}]})

    memo_path = tmp_path / "unserved_symbols.json"
    client = httpx.Client(base_url=DATA_BASE_URL, transport=httpx.MockTransport(handler))
    bars = AlpacaDailyBars(client, unserved=UnservedSymbols(memo_path))
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)
    assert bars.bars("AXIA3", start, now) == []
    assert bars.bars("AXIA3", start, now) == []  # memoised: no second request
    assert sum(1 for p in requests if "AXIA3" in p) == 1
    # Transient statuses are NOT memoised: the next run may succeed.
    assert bars.bars("RATE", start, now) == [] and bars.bars("RATE", start, now) == []
    assert sum(1 for p in requests if "RATE" in p) == 2
    assert bars.bars("PLAN", start, now) == [] and bars.bars("PLAN", start, now) == []
    assert sum(1 for p in requests if "PLAN" in p) == 2
    assert len(bars.bars("AAPL", start, now)) == 1
    # Persisted for the next process.
    saved = json.loads(memo_path.read_text(encoding="utf-8"))
    assert set(saved) == {"AXIA3"} and saved["AXIA3"]["status"] == 400
    fresh = AlpacaDailyBars(client, unserved=UnservedSymbols(memo_path))
    before = len(requests)
    assert fresh.bars("AXIA3", start, now) == []
    assert len(requests) == before  # no request at all
