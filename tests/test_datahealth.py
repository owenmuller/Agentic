"""NO SILENT DATA GAPS (global rule, human ruling 2026-10-15): an expected
fetch that comes back empty, or fails, raises a yellow card. The mechanism,
its wiring into every production entry point, and the fixes the audit made."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import datahealth
from execution import cards
from execution.alerts import data_sink

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean():
    datahealth.reset()
    datahealth.set_sink(None)
    yield
    datahealth.reset()
    datahealth.set_sink(None)


def test_reports_before_a_sink_are_held_and_delivered_when_one_is_installed():
    datahealth.report("feed", "nothing came back")
    assert datahealth.held() == [("feed", "nothing came back", False)]
    got = []
    datahealth.set_sink(lambda source, message, urgent: got.append((source, message, urgent)))
    assert got == [("feed", "nothing came back", False)] and datahealth.held() == []


def test_an_outage_cards_at_most_three_per_source_then_counts_the_rest(monkeypatch):
    got = []
    datahealth.set_sink(lambda source, message, urgent: got.append(message))
    clock = [1000.0]
    monkeypatch.setattr(datahealth.time, "monotonic", lambda: clock[0])
    for i in range(5):
        datahealth.report("quotes", f"symbol {i}")
    datahealth.report("bars", "other source still cards")
    assert got == ["symbol 0", "symbol 1", "symbol 2", "other source still cards"]
    clock[0] += datahealth.WINDOW_SECONDS + 1
    datahealth.report("quotes", "symbol 5")
    assert got[-1].startswith("symbol 5 (+2 more from quotes")


def test_a_sink_failure_never_breaks_the_fetch_that_found_the_gap():
    def broken(source, message, urgent):
        raise RuntimeError("discord down")

    datahealth.set_sink(broken)
    datahealth.report("feed", "x")  # no exception


def test_the_data_card_is_yellow_and_says_what_is_missing():
    card = cards.data_card("Main book", "2026-10-15 16:00 ET", "Alpaca daily bars", "no SPY bars")
    assert card.status == cards.LOOK and card.channel == cards.DAILY
    assert "Alpaca daily bars" in card.title and card.look_at
    assert cards.data_card("Main book", "t", "s", "w", urgent=True).channel == cards.URGENT


def test_the_direct_sink_sends_a_yellow_card():
    sent = []

    class FakeAlerter:
        def card(self, key, card):
            sent.append((key, card))

    sink = data_sink("Paper lab", FakeAlerter())
    sink("Alpaca quotes", "no usable quote for SPY for 15 minutes", False)
    key, card = sent[0]
    assert key.startswith("DATA:Alpaca quotes:") and card.status == cards.LOOK and "Paper lab" in card.title


def test_every_production_entry_point_installs_a_sink():
    """The units run ``python -m orchestrator|lab|earnings``: each CLI installs
    its sink before any command, and the session routes through its run log."""
    for module in ("orchestrator", "lab", "earnings"):
        text = (ROOT / "src" / module / "__main__.py").read_text(encoding="utf-8")
        assert "datahealth.set_sink(" in text, module
    session = (ROOT / "src" / "orchestrator" / "__main__.py").read_text(encoding="utf-8")
    assert "datahealth.set_sink(session_data_sink(run_log))" in session and 'event == "DATA"' in session
    units = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "ops" / "vps").glob("*.service"))
    for line in units.splitlines():
        if line.startswith("ExecStart=") and " -m " in line:
            assert line.split(" -m ")[1].split()[0] in ("orchestrator", "lab", "earnings"), line


# -- the fixes -------------------------------------------------------------------


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


def _alpaca(trading=None, data=None):
    from momentum import feeds

    client = feeds.Alpaca.__new__(feeds.Alpaca)
    client.cache = None
    client._trading, client._data, client.last_failed = trading, data, {}
    return client


def test_momentum_calendar_and_assets_never_take_empty_for_an_answer(tmp_path):
    from momentum import feeds

    class Trading:
        def get(self, path, params):
            return _Resp(200, [])

    client = _alpaca(trading=Trading())
    client.cache = feeds.Cache(tmp_path)
    with pytest.raises(RuntimeError, match="no sessions"):
        client.sessions("2026-10-01", "2026-10-14")
    assert client.sessions("2026-10-17", "2026-10-18") == []  # a weekend is legitimately empty
    with pytest.raises(RuntimeError, match="no active assets"):
        client.assets()
    assert feeds.Cache(tmp_path).get("alpaca_assets") is None  # never cached


def test_momentum_bars_name_the_symbols_that_failed():
    class Data:
        def get(self, path, params):
            symbols = params["symbols"].split(",")
            if "BAD" in symbols:
                return _Resp(503 if symbols == ["BAD"] or len(symbols) > 1 else 200, {})
            if "GONE" in symbols:
                return _Resp(422, {})
            return _Resp(200, {"bars": {s: [{"t": "2026-10-01T04:00:00Z", "c": 1.0}] for s in symbols}})

    client = _alpaca(data=Data())
    out = client.bars(["AAA", "BAD", "GONE"], "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "all")
    assert set(out) == {"AAA"}
    assert client.last_failed == {"BAD": "HTTP 503", "GONE": "HTTP 422"}
    assert client.failed() == {"BAD": "HTTP 503"}  # GONE is a symbol the feed does not serve


def test_vix_retries_after_a_blip_and_reports_it():
    from execution.vix import CboeVixSource

    now = [datetime(2026, 10, 15, 13, 31, tzinfo=timezone.utc)]
    responses = [httpx.Response(503), httpx.Response(200, text="DATE,OPEN,HIGH,LOW,CLOSE\n10/14/2026,1,2,0.5,18.4\n")]
    source = CboeVixSource(get=lambda url: responses.pop(0), clock=lambda: now[0])
    assert source() is None
    assert any(s == "VIX (CBOE)" and "unavailable" in m for s, m, _ in datahealth.held())
    now[0] += timedelta(minutes=10)
    assert source() is None and len(responses) == 1  # inside the retry window: no request
    now[0] += timedelta(minutes=25)
    assert source() == (date(2026, 10, 14), Decimal("18.4"))  # retried the same day


def test_a_missing_atr_is_never_cached_for_the_day():
    from execution.atr import AtrSource

    calls = []

    class Bars:
        def bars(self, symbol, start, end):
            calls.append(symbol)
            return []

    atr = AtrSource(Bars(), clock=lambda: datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc))
    assert atr("XYZ") is None and atr("XYZ") is None
    assert calls == ["XYZ", "XYZ"]  # asked again: an outage must not last the day


def test_the_session_path_turns_a_report_into_a_yellow_card(tmp_path):
    # datahealth -> session_data_sink -> run-log DATA line -> session_observer -> card
    from orchestrator.__main__ import session_data_sink, session_observer
    from orchestrator.ops import RunLog

    sent = []

    class FakeAlerter:
        def card(self, key, card):
            sent.append((key, card))

    run_log = RunLog(tmp_path / "run.log", observer=session_observer(FakeAlerter()))
    datahealth.set_sink(session_data_sink(run_log))
    datahealth.report("Alpaca daily bars", "no SPY bars for 2026-10-01..2026-10-14")
    datahealth.report("Alpaca quotes", "held position unpriced", urgent=True)
    assert [c.status for _, c in sent] == [cards.LOOK, cards.LOOK]
    assert sent[0][1].channel == cards.DAILY and sent[1][1].channel == cards.URGENT
    assert "Alpaca daily bars" in sent[0][1].title and "DATA" in (tmp_path / "run.log").read_text()
