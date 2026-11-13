"""Learning acceleration (human ruling 2026-11-13): every AI-trader idea graded
at its own horizon, the ideas not traded journaled as "refusal" cases (item
2), and the intelligence scorecard on the Friday card (item 3)."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

import pytest

from execution import cards
from learning import scorecard, vocabulary, writer
from learning.journal import Entry
from orchestrator import learning_feed as feed

D = date


def weekdays(start: date, end: date, close: float = 100.0, **overrides) -> dict:
    out, day = {}, start
    while day <= end:
        if day.weekday() < 5:
            out[day] = overrides.get(day.isoformat(), (close, close))
        day += timedelta(days=1)
    return out


BARS = {
    "SPY": weekdays(D(2026, 10, 12), D(2026, 10, 23), **{"2026-10-19": (100.0, 101.0)}),
    "AAA": weekdays(D(2026, 10, 12), D(2026, 10, 23), 9.8, **{"2026-10-19": (11.0, 11.0)}),
    "BBB": weekdays(D(2026, 10, 12), D(2026, 10, 23), 50.0, **{"2026-10-19": (45.0, 45.0)}),
    "CCC": weekdays(D(2026, 10, 12), D(2026, 10, 23), 30.0, **{"2026-10-19": (27.0, 27.0)}),
    "DDD": weekdays(D(2026, 10, 12), D(2026, 10, 23), 20.0, **{"2026-10-13": (20.0, 21.0)}),
    "EEE": weekdays(D(2026, 10, 12), D(2026, 10, 23), 10.0),
}


def look(spy_at=lambda when: 100.0):
    return feed.Lookups(bars=lambda s: BARS.get(s, {}), sector=lambda s: "manufacturing",
                        regime=lambda day: "calm_uptrend", fund=lambda s: s == "SPY", leverage=lambda s: 1.0,
                        spy_at=spy_at)


AT = "2026-10-13T14:00:00+00:00"          # 10:00 ET
LATER = "2026-10-13T14:46:00+00:00"


def idea(symbol, instrument="stock", horizon="days", confidence=60):
    return {"symbol": symbol, "instrument": instrument, "horizon": horizon, "confidence": confidence,
            "stop_pct": 3, "target_pct": 6, "thesis": f"{symbol} thesis", "catalyst": f"{symbol} news"}


EVENTS = [
    {"event": "llm_pass", "at": AT, "scan": "09:45", "brief_arm": "control",
     "ideas": [idea("AAA"), idea("BBB", "call", confidence=72), idea("CCC", "put", confidence=55),
               idea("DDD", horizon="intraday", confidence=80), idea("EEE")]},
    {"event": "anchor_check", "at": AT, "scan": "09:45", "symbol": "BBB", "live_mid": "50.0"},
    {"event": "anchor_check", "at": AT, "scan": "09:45", "symbol": "DDD", "live_mid": "20.0"},
    {"event": "anchor_check", "at": AT, "scan": "09:45", "symbol": "EEE", "live_mid": "10.0"},
    {"event": "position_opened", "at": AT, "pid": "a", "scan": "09:45", "symbol": "AAA", "instrument": "stock",
     "anchor_mid": "10.0"},
    {"event": "idea_rejected", "at": AT, "scan": "09:45", "symbol": "BBB", "reason": "no contract: no chain data"},
    {"event": "position_opened", "at": AT, "pid": "c", "scan": "09:45", "symbol": "CCC", "instrument": "put",
     "anchor_mid": "30.0"},
    {"event": "position_closed", "at": AT, "pid": "c", "reason": "entry_unfilled"},
    {"event": "idea_rejected", "at": AT, "scan": "09:45", "symbol": "EEE", "reason": "confidence 40 below 50"},
    # DDD: no verdict recorded (unplaced). Later the same day: BBB again (one case),
    # EEE taken (no case: the trade was made)
    {"event": "llm_pass", "at": LATER, "scan": "10:45", "ideas": [idea("BBB", "call"), idea("EEE")]},
    {"event": "idea_rejected", "at": LATER, "scan": "10:45", "symbol": "BBB", "reason": "no contract: no chain data"},
    {"event": "position_opened", "at": LATER, "pid": "e", "scan": "10:45", "symbol": "EEE", "instrument": "stock",
     "anchor_mid": "10.0"},
]


def test_every_idea_is_graded_at_its_own_horizon_whatever_its_fate():
    grades = {(g.scan, g.symbol): g for g in feed.ai_trader_idea_grades(EVENTS, look())}
    a = grades[("09:45", "AAA")]
    assert (a.status, a.resolved, a.sessions) == ("taken", "2026-10-19", 5)       # Oct 13 is the 1st of 5 sessions
    assert a.return_pct == pytest.approx(10.0) and a.excess == pytest.approx(9.0)  # SPY +1%
    assert a.moved_since_close == pytest.approx((10.0 / 9.8 - 1) * 100)
    b = grades[("09:45", "BBB")]
    assert (b.status, b.reason) == ("refused", "no contract: no chain data") and b.excess == pytest.approx(-11.0)
    c = grades[("09:45", "CCC")]
    assert c.status == "unfilled" and c.direction == "short"
    assert c.return_pct == pytest.approx(10.0) and c.excess == pytest.approx(11.0)  # a put gains on the fall
    d = grades[("09:45", "DDD")]
    assert (d.status, d.resolved) == ("unplaced", "2026-10-13") and d.excess == pytest.approx(5.0)
    assert grades[("10:45", "EEE")].status == "taken" and grades[("09:45", "EEE")].status == "refused"


def test_without_spys_minute_price_an_intraday_idea_is_not_graded_and_a_days_idea_starts_at_the_close():
    grades = {(g.scan, g.symbol): g for g in feed.ai_trader_idea_grades(EVENTS, look(spy_at=lambda when: None))}
    assert grades[("09:45", "DDD")].excess is None and grades[("09:45", "DDD")].resolved is None
    a = grades[("09:45", "AAA")]
    assert a.return_pct == pytest.approx((11.0 / 9.8 - 1) * 100)                  # from Oct 13's close


def test_a_horizon_still_open_is_pending():
    short = dict(BARS, SPY=weekdays(D(2026, 10, 12), D(2026, 10, 16)))
    pending = feed.Lookups(bars=lambda s: short.get(s, {}), sector=lambda s: "x", regime=lambda d: "unknown",
                           fund=lambda s: False, leverage=lambda s: 1.0, spy_at=lambda w: 100.0)
    a = next(g for g in feed.ai_trader_idea_grades(EVENTS, pending) if g.symbol == "AAA")
    assert a.resolved is None and a.excess is None


def test_ideas_not_traded_become_refusal_cases_once_a_name_and_day():
    grades = feed.ai_trader_idea_grades(EVENTS, look())
    cases = {c.symbol: c for c in feed.ai_trader_refusal_cases(grades, look())}
    assert set(cases) == {"BBB", "CCC", "DDD"}          # AAA and EEE were taken that day
    assert all(c.kind == "refusal" and c.account == "ai-trader" for c in cases.values())
    assert cases["BBB"].decision_id == "idea-2026-10-13-09:45-BBB"   # the first of its two refusals
    assert writer.side(cases["BBB"]) == "right" and writer.side(cases["CCC"]) == "wrong"
    assert cases["CCC"].own_move == pytest.approx(11.0) and cases["CCC"].market_move == pytest.approx(-1.0)
    assert "refused by the rules engine (no contract: no chain data)" in cases["BBB"].expected
    assert "placed and never filled" in cases["CCC"].expected
    for case in cases.values():
        vocabulary.check({**case.tags, "setup": "ai_idea", "catalyst": "other"}, ("ai_trader",))
    text = writer.user_prompt(cases["BBB"])
    assert "an idea its rules REFUSED" in text and "why it was not traded" in text
    assert "or a refusal" in writer.SYSTEM


def test_a_refusal_entry_is_in_the_vocabulary():
    assert "refusal" in vocabulary.KINDS
    Entry(decision_id="x", account="ai-trader", strategy="ai_trader", kind="refusal", resolved_at="2026-10-19",
          thesis="t", expected="e", actual="a", excess=-1.0, process="right_reason")


# -- the scorecard ------------------------------------------------------------------------

def grade(day, symbol, status, excess, confidence=60, moved=None, direction="long"):
    return {"day": day, "symbol": symbol, "status": status, "excess": excess, "confidence": confidence,
            "moved_since_close": moved, "direction": direction}


def entry(resolved, process, kind="trade", account="ai-trader"):
    return Entry(decision_id=f"{resolved}{process}{kind}", account=account, strategy="ai_trader", kind=kind,
                 resolved_at=resolved, thesis="", expected="", actual="", excess=0.0, process=process)


TODAY = D(2026, 11, 13)


def test_the_scorecard_reads_plainly_with_a_trend_by_month():
    grades = [grade("2026-10-13", "A", "taken", 2.0, 60, 3.0), grade("2026-10-14", "B", "taken", -4.0, 75, 1.0),
              grade("2026-10-14", "C", "refused", 1.0, 60), grade("2026-11-02", "D", "taken", -1.0, 60, 2.0),
              grade("2026-11-03", "E", "unfilled", 3.0, 80), grade("2026-11-03", "F", "refused", None),
              # the same name, day and direction twice: counted once, under the fate that was taken
              grade("2026-11-02", "D", "refused", 9.0, 60)]
    entries = [entry("2026-10-20", "right_reason"), entry("2026-10-21", "wrong_reason"),
               entry("2026-11-05", "right_reason", kind="refusal"), entry("2026-11-05", "unclear"),
               entry("2026-11-05", "right_reason", account="main")]
    moves = [{"pid": "p", "status": "ok", "move": "0.031"}, {"pid": None, "status": "ok", "move": "0.5"}]
    lines, detail = scorecard.lines(grades, entries, moves, TODAY)
    calib, process, selection, timing = lines
    assert calib.startswith("Calibration (ideas graded at their own horizon): 50-70 beat SPY 67% (n 3)")
    assert "(Oct 100% (n 2) -> Nov 0% (n 1))" in calib and "70-85 beat SPY 50% (n 2)" in calib
    assert "85+ none yet" in calib
    assert process == ("Process: right for the right reason in Oct 1 of 2 (50%) -> Nov 1 of 2 (50%) of the AI "
                       "trader's post-mortems.")
    assert selection.startswith("Selection: ideas taken -1.0 points a case against SPY at their horizon (n 3), "
                                "ideas not taken +2.0 (n 2)")
    assert "not yet clear of zero" in selection
    assert timing.startswith("Timing: the median trade was entered after a +2.0% move in its favour since the "
                             "prior close (n 3; Oct +2.0% (n 2) -> Nov +2.0% (n 1))")
    assert "since its catalyst went public, +3.1% (n 1" in timing
    assert any(line.startswith("  refused: n 1") for line in detail)


def test_an_empty_record_says_so():
    lines, _ = scorecard.lines([], [], [], TODAY)
    assert lines[0] == "Calibration: no idea has reached the end of its horizon yet."
    assert lines[1] == "Process: no AI-trader post-mortem yet."
    assert lines[2].startswith("Selection: not enough graded ideas")
    assert lines[3] == "Timing: no entry measured yet."


def test_the_scorecard_lines_open_the_learning_card():
    card = cards.learning_card(3, 300, 0.2, [], [], [], 0, 0, [], [], [], "d",
                               scorecard=["Calibration: x.", "Process: y."])
    assert card.lines[:3] == ("Calibration: x.", "Process: y.", "Journal: 3 new post-mortems this week, 300 in all.")


def test_grades_serialize_for_the_friday_card():
    g = feed.ai_trader_idea_grades(EVENTS, look())[0]
    row = asdict(g)
    assert {"day", "symbol", "status", "excess", "confidence", "moved_since_close", "direction"} <= set(row)
