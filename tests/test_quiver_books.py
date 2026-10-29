"""The Quiver strategy shadows (human ruling 2026-10-29): Congress Buys and
Lobbying Spending Growth on Quiver's published rules, point in time."""
from __future__ import annotations

import json
from datetime import date

import pytest

from orchestrator import quiver_books as qb
from orchestrator import quiver_shadows as qs
from orchestrator.momentum_report import quiver_rows


def buy(ticker, disclosed, rng="$15,001 - $50,000", kind="Purchase", ttype="ST"):
    return {"Ticker": ticker, "ReportDate": disclosed, "Transaction": kind, "TickerType": ttype, "Range": rng,
            "Amount": "15001.0"}


def test_reported_size_is_the_range_midpoint():
    assert qb.reported_size("$15,001 - $50,000") == pytest.approx(32_500.5)
    assert qb.reported_size("Over $50,000,000") == 50_000_000
    assert qb.reported_size("", "1001.0") == 1001.0
    assert qb.reported_size(None, None) is None


def test_congress_buys_weighs_purchases_disclosed_in_the_window_only():
    rows = [buy("AAA", "2026-10-28"), buy("AAA", "2026-09-01", "$1,001 - $15,000"),
            buy("BBB", "2026-10-29"),                      # disclosed ON the rebalance day: not yet public
            buy("CCC", "2026-04-01"),                      # outside the 182-day window
            buy("DDD", "2026-10-20", kind="Sale"), buy("EEE", "2026-10-20", ttype="OP")]
    w = qb.congress_buys_weights(rows, "2026-10-29")
    assert set(w) == {"AAA"} and w["AAA"] == pytest.approx(1.0)
    w = qb.congress_buys_weights(rows + [buy("FFF", "2026-10-27", "$50,001 - $100,000")], "2026-10-29")
    aaa, fff = 32_500.5 + 8_000.5, 75_000.5
    assert w["FFF"] == pytest.approx(fff / (aaa + fff))


def test_lobbying_quarters_and_deadlines():
    assert qb.report_quarter(date(2026, 10, 20)) == (2026, 3)   # filed on the deadline: Q3
    assert qb.report_quarter(date(2026, 11, 10)) == (2026, 3)   # mildly late: still Q3
    assert qb.report_quarter(date(2027, 1, 20)) == (2026, 4)
    assert qb.quarter_deadline(2026, 4) == date(2027, 1, 20)
    assert qb.latest_reported_quarter(date(2026, 10, 20)) == (2026, 2)  # the deadline must have PASSED
    assert qb.latest_reported_quarter(date(2026, 10, 21)) == (2026, 3)


def lob(day, amount, registrant="R"):
    return {"Date": day, "Amount": str(amount), "Registrant": registrant}


def test_lobbying_growth_is_point_in_time_with_a_minimum_base():
    store = {
        "UPP": [lob("2026-07-20", 100_000), lob("2026-10-20", 300_000)],            # +200%
        "MID": [lob("2026-07-20", 100_000), lob("2026-10-20", 150_000)],            # +50%
        "TINY": [lob("2026-07-20", 5_000), lob("2026-10-20", 500_000)],             # base under $10K
        "LATE": [lob("2026-07-20", 100_000), lob("2026-11-02", 900_000)],           # filed after the day
        "DUP": qb.dedupe([lob("2026-07-20", 100_000), lob("2026-07-20", 100_000), lob("2026-10-20", 120_000)]),
    }
    ranked = qb.lobbying_growth(store, "2026-11-02")
    assert [s for s, _ in ranked] == ["UPP", "MID", "DUP"]
    assert dict(ranked)["DUP"] == pytest.approx(0.2)  # the duplicate row was dropped
    w = qb.lobbying_weights(store, "2026-11-02", tradeable=lambda s: s != "UPP", top=10)
    assert w == {"MID": 0.5, "DUP": 0.5}
    assert [s for s, _ in qb.lobbying_growth(store, "2026-11-02", metric="dollars")][0] == "UPP"


def test_the_books_rebalance_weekly_and_monthly_and_start_at_once():
    sessions = ["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
    assert qs.due({}, "2026-10-30", sessions) == ["congress_buys", "lobbying_growth"]
    books = {n: {"started": "2026-10-30", "schedule": [["2026-10-30", {"X": 1.0}]]} for n in qs.BOOKS}
    assert qs.due(books, "2026-10-30", sessions) == []             # never redone
    assert qs.due(books, "2026-11-02", sessions) == ["congress_buys", "lobbying_growth"]  # Monday and the 1st
    books = {n: {"started": "2026-11-02", "schedule": [["2026-11-02", {"X": 1.0}]]} for n in qs.BOOKS}
    assert qs.due(books, "2026-11-03", sessions) == []


def test_the_lobbying_store_merges_and_forgets_old_rows():
    store = {}
    added = qs.merge_lobbying(store, [dict(lob("2026-10-20", 5), Ticker="aapl"), dict(lob("2026-10-20", 5), Ticker="AAPL"),
                                      dict(lob("2023-01-20", 5), Ticker="OLD")], "2026-10-29")
    assert added == 1 and list(store) == ["AAPL"]


class FakeClient:
    def __init__(self, congress=None, live=None):
        self.congress = congress if congress is not None else [buy("AAA", "2026-10-28")]
        self.live = live if live is not None else [dict(lob("2026-10-20", 300_000), Ticker="UPP")]

    def congress_live(self):
        return self.congress

    def lobbying_live(self):
        return self.live

    def lobbying_history(self, symbol):
        return [lob("2026-07-20", 100_000)] if symbol == "UPP" else []


def test_one_step_seeds_the_store_and_starts_both_books(tmp_path):
    notes = []
    done = qs.step(tmp_path, FakeClient(), "2026-10-30", ["2026-10-29", "2026-10-30"], lambda s: True, notes.append,
                   seed_symbols=lambda: ["UPP", "NONE"])
    assert done == ["congress_buys", "lobbying_growth"]
    books = json.loads((tmp_path / "books.json").read_text())
    assert books["congress_buys"]["schedule"] == [["2026-10-30", {"AAA": 1.0}]]
    assert books["lobbying_growth"]["schedule"] == [["2026-10-30", {"UPP": 1.0}]]
    assert qs.step(tmp_path, FakeClient(), "2026-10-30", ["2026-10-30"], lambda s: True, notes.append) == []


def test_an_empty_book_is_a_failure_not_a_quiet_cash_position(tmp_path):
    with pytest.raises(RuntimeError, match="no holdings"):
        qs.step(tmp_path, FakeClient(congress=[buy("AAA", "2026-01-01")]), "2026-10-30", ["2026-10-30"],
                lambda s: True, lambda m: None, seed_symbols=lambda: [])


def test_the_weekly_table_shows_each_book_against_spy_and_qqq_over_its_own_days(tmp_path):
    (tmp_path / "momentum").mkdir()
    (tmp_path / "quiver").mkdir()
    (tmp_path / "quiver" / "books.json").write_text(json.dumps(
        {"congress_buys": {"started": "2026-10-30", "schedule": [["2026-10-30", {"AAA": 1.0}]]}}))
    days = ["2026-10-29", "2026-10-30", "2026-11-02"]
    prices = {"AAA": dict(zip(days, [10.0, 10.0, 11.0])), "SPY": dict(zip(days, [100.0, 100.0, 101.0])),
              "QQQ": dict(zip(days, [50.0, 50.0, 49.0]))}
    rows = quiver_rows(tmp_path / "momentum", prices, days, "2026-11-02", "2026-11-02")
    name, started, week, since, spy, qqq = rows[0]
    assert (name, started) == ("congress_buys", "2026-10-30")
    assert since == pytest.approx(11 / 10 * (1 - 0.001) - 1, rel=1e-6)   # 10 bp entry cost
    assert spy == pytest.approx(0.01) and qqq == pytest.approx(-0.02)


def test_a_partial_research_pull_never_seeds_the_store_and_congress_still_starts(tmp_path):
    pull = tmp_path / "pull" / "rows.jsonl"
    pull.parent.mkdir()
    pull.write_text(json.dumps({"symbol": "UPP", "rows": [lob("2026-07-20", 100_000)]}) + "\n")
    books_dir = tmp_path / "quiver"
    with pytest.raises(RuntimeError, match="not seeded yet"):
        qs.step(books_dir, FakeClient(), "2026-10-30", ["2026-10-30"], lambda s: True, lambda m: None,
                seed_path=pull)
    assert not (books_dir / "lobbying.json").exists()       # retried next run
    assert "congress_buys" in json.loads((books_dir / "books.json").read_text())
    (pull.parent / "complete.json").write_text("{}")         # the pull finished
    assert qs.step(books_dir, FakeClient(), "2026-10-30", ["2026-10-30"], lambda s: True, lambda m: None,
                   seed_path=pull) == ["lobbying_growth"]


def test_an_evening_run_keeps_the_store_but_decides_nothing(tmp_path):
    done = qs.step(tmp_path, FakeClient(), "2026-10-29", ["2026-10-29"], lambda s: True, lambda m: None,
                   seed_symbols=lambda: ["UPP"], decide=False)
    assert done == [] and (tmp_path / "lobbying.json").exists() and not (tmp_path / "books.json").exists()
