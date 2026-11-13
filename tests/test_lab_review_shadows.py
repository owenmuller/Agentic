"""The AI trader review's shadows, scored (human rulings 2026-11-12), and the
weekly theme-tag line (readings 2026-11-12, later)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from execution.market_calendar import MarketCalendar
from lab import review_shadows as rs
from lab.scoreboard import theme_share

NY = rs.NY
NOW = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
CAL = MarketCalendar.weekdays(lambda: NOW)


def at(day: int, hhmm: str, month: int = 11) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, month, day, h, m, tzinfo=NY)


def bar(when: datetime, o, h, l, c) -> dict:
    return {"t": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), "o": o, "h": h, "l": l, "c": c}


class Bars:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return list(self.table.get(symbol, []))


def sim_for(table, reports=None):
    return rs.Simulator(Bars(table), CAL, intraday_close="15:50", horizon_sessions={"days": 5, "weeks": 20},
                        now=NOW, report=(lambda s, m: reports.append(m)) if reports is not None else (lambda s, m: None))


# -- the simulator -------------------------------------------------------------------

def test_the_horizon_ends_at_1550_of_the_nth_session_counting_the_entry_day():
    assert rs.horizon_end(at(13, "10:00"), "days", CAL, "15:50", {"days": 5}) == at(19, "15:50")
    assert rs.horizon_end(at(13, "10:00"), "intraday", CAL, "15:50", {}) == at(13, "15:50")


def test_the_entry_minute_is_skipped_a_stop_gap_fills_at_the_open_and_a_target_at_its_level():
    rows = [bar(at(13, "10:00"), 100, 100, 90, 95),      # the entry minute: never read
            bar(at(13, "10:01"), 100, 100.5, 99.5, 100),
            bar(at(13, "10:02"), 97, 97, 96, 96)]         # gaps through the 98 stop
    s = sim_for({"X": rows}).run("X", "stock", "days", at(13, "10:00"), 100.0, 98.0, 104.0)
    assert (s.status, s.reason, s.exit_price) == ("done", "stop", 97.0) and round(s.ret, 4) == -0.03
    rows = [bar(at(13, "10:01"), 100, 105, 99, 104)]      # a gap past a target is never credited
    s = sim_for({"X": rows}).run("X", "stock", "days", at(13, "10:00"), 100.0, 98.0, 103.0)
    assert (s.reason, s.exit_price) == ("target", 103.0)


def test_a_bar_through_both_levels_is_a_stop_and_a_put_gains_on_a_fall():
    rows = [bar(at(13, "10:01"), 100, 105, 97, 100)]
    assert sim_for({"X": rows}).run("X", "call", "days", at(13, "10:00"), 100.0, 98.0, 104.0).reason == "stop"
    rows = [bar(at(13, "10:01"), 100, 100.5, 95, 96)]
    s = sim_for({"X": rows}).run("X", "put", "days", at(13, "10:00"), 100.0, 102.0, 96.0)
    assert (s.reason, round(s.ret, 4)) == ("target", 0.04)


def test_no_level_hit_exits_at_the_last_bar_before_the_horizon_end():
    rows = [bar(at(13, "10:01"), 100, 101, 99, 100.5), bar(at(13, "15:49"), 101, 101, 101, 101.5),
            bar(at(13, "15:50"), 110, 110, 110, 110)]   # at the cut: not held
    s = sim_for({"X": rows}).run("X", "etf", "intraday", at(13, "10:00"), 100.0, 98.0, 104.0)
    assert (s.reason, s.exit_price) == ("intraday_close", 101.5)


def test_an_unfinished_horizon_is_pending_and_missing_bars_are_reported():
    late = rs.Simulator(Bars({}), CAL, intraday_close="15:50", horizon_sessions={"days": 5, "weeks": 20},
                        now=at(17, "12:00"))
    assert late.run("X", "stock", "days", at(13, "10:00"), 100.0, 98.0, 104.0).status == "pending"
    reports = []
    assert sim_for({}, reports).run("X", "stock", "days", at(13, "10:00"), 100.0, 98.0, 104.0).status == "no_bars"
    assert reports and "no minute bars for X" in reports[0]
    assert sim_for({}).run("X", "stock", "days", at(13, "10:00"), None, 98.0, 104.0).status == "no_levels"


# -- the scorers -------------------------------------------------------------------------

def stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def opened(pid, symbol, when, anchor="100", stop="98", target="104", instrument="stock", **extra):
    return dict(event="position_opened", pid=pid, symbol=symbol, instrument=instrument, horizon="days",
                anchor_mid=anchor, stop=stop, target=target, at=stamp(when), **extra)


def round_trip(pid, qty, buy, sell, mult=1):
    return [dict(event="settled", pid=pid, side="buy", filled_qty=str(qty), avg_price=str(buy), multiplier=mult),
            dict(event="settled", pid=pid, side="sell", filled_qty=str(qty), avg_price=str(sell), multiplier=mult),
            dict(event="position_closed", pid=pid)]


def test_a_refused_idea_is_replayed_beside_the_position_that_blocked_it():
    events = [opened("p1", "AAPL", at(13, "09:46")),
              dict(event="idea_rejected", symbol="ABCD", code="concentration", at=stamp(at(13, "09:46")),
                   reason="concentration: theme software_internet already proposed in this pass (AAPL)",
                   idea={"instrument": "stock", "horizon": "days"},
                   levels={"anchor": "50", "stop": "49", "target": "52"})]
    table = {"AAPL": [bar(at(13, "09:47"), 100, 100, 97, 97)],      # stopped: -2%
             "ABCD": [bar(at(13, "09:47"), 50, 52, 50, 52)]}        # target: +4%
    text = "\n".join(rs.concentration(events, sim_for(table)))
    assert "refused (theme in the pass): scored 1 - mean +4.00%" in text
    assert "taken since 2026-11-13 (same simulator): scored 1 - mean -2.00%" in text
    assert "refused minus the position that blocked it: n 1, mean +6.00%, refused ahead 1/1" in text


def test_the_strict_reading_counts_what_its_refusals_earned():
    events = [opened("p2", "ABCD", at(13, "09:46")), *round_trip("p2", 10, 50, 51),
              dict(event="concentration_strict_shadow", symbol="ABCD", pid="p2", placed=True, at=stamp(at(13, "09:46")))]
    text = "\n".join(rs.strict(events, sim_for({"ABCD": [bar(at(13, "09:47"), 50, 52, 50, 52)]})))
    assert "1 closed, $+10 on $500 (+2.0%)" in text and "replayed: scored 1" in text


def test_the_exit_shadow_counts_phantoms_and_prices_a_stocks_old_exit():
    events = [opened("p3", "TRMK", at(13, "09:46"), anchor="50", stop="48", target="52"),
              *round_trip("p3", 10, 50, 53),
              dict(event="exit_shadow", pid="p3", symbol="TRMK", instrument="stock", old_rule="target", new_rule=None,
                   bid="47.88", ask="54.78", last="49.90", trigger="49.90", stop="48", target="52"),
              dict(event="exit_shadow", pid="p4", symbol="LUNR", instrument="stock", old_rule="stop", new_rule=None,
                   bid="9", ask="12", last="9.40", trigger="9.40", stop="9.50", target="12")]
    text = "\n".join(rs.exits(events))
    assert "old rule alone fired 2 (level traded 1, never traded - phantom 1, no last trade 0)" in text
    # TRMK: sold at the 47.88 bid instead of 53 -> 10 x (47.88 - 53) = -51.20
    assert "the old exit would have earned $-51" in text


def test_the_old_windows_contract_is_scored_against_ours():
    events = [opened("p5", "SPY", at(13, "09:46"), instrument="call", occ="SPY261120C00660000"),
              *round_trip("p5", 2, "3.00", "3.50", mult=100),
              dict(event="expiry_counterfactual", phase="entry", pid="p5", status="ok", same_contract=False,
                   occ="SPY261116C00660000", ask="1.20", contracts=4, multiplier=100),
              dict(event="expiry_counterfactual", phase="mark", pid="p5", occ="SPY261116C00660000", bid="0.80")]
    text = "\n".join(rs.expiry(events))
    assert "ours (7+ days): 1 closed, $+100 on $600" in text
    assert "old window (bought at the ask, sold at the marked bid): 1 closed, $-160 on $480" in text


def test_the_move_shadow_splits_skip_from_keep_and_replays_untaken_ideas():
    when = at(13, "09:46")
    events = [dict(event="llm_pass", scan="09:45", at=stamp(when),
                   ideas=[{"symbol": "ABCD", "stop_pct": 2, "target_pct": 4}]),
              opened("p6", "AAPL", when),
              dict(event="move_since_public", scan="09:45", symbol="AAPL", instrument="stock", horizon="days",
                   pid="p6", status="ok", would_skip=True, price="100", at=stamp(when)),
              dict(event="move_since_public", scan="09:45", symbol="ABCD", instrument="stock", horizon="days",
                   pid=None, status="ok", would_skip=False, price="50", at=stamp(when)),
              dict(event="move_since_public", scan="09:45", symbol="SPY", instrument="etf", horizon="days",
                   pid=None, status="no_matching_headline", at=stamp(when))]
    table = {"AAPL": [bar(at(13, "09:47"), 100, 100, 97, 97)], "ABCD": [bar(at(13, "09:47"), 50, 52, 50, 52)]}
    text = "\n".join(rs.move(events, sim_for(table)))
    assert "3 ideas: no_matching_headline 1, ok 2" in text
    assert "would skip (every idea, replayed): scored 1 - mean -2.00%" in text
    assert "would keep (every idea, replayed): scored 1 - mean +4.00%" in text


def test_the_report_renders_every_section_on_an_empty_ledger():
    text = rs.report([], sim_for({}), date(2026, 11, 26))
    for head in ("CONCENTRATION CAP", "STRICT READING", "WOULD EXIT", "MINIMUM EXPIRY", "MOVE SINCE PUBLIC"):
        assert head in text


# -- the weekly theme-tag line -----------------------------------------------------------

def test_the_other_share_is_reported_and_flagged_over_a_fifth_at_twenty_ideas():
    def passes(other, themed):
        ideas = [{"symbol": f"O{i}", "theme": "other"} for i in range(other)]
        ideas += [{"symbol": f"T{i}", "theme": "energy"} for i in range(themed)]
        return [dict(event="llm_pass", at="2026-11-13T15:00:00+00:00", ideas=ideas),
                dict(event="llm_pass", at="2026-11-12T15:00:00+00:00", ideas=[{"symbol": "OLD"}])]

    lines, flags = theme_share(passes(5, 15))
    assert lines[0] == "THEME TAGS (since 2026-11-13): 20 ideas; tagged 'other' 5 (25%)"
    assert flags == ["FLAG: 25% of ideas tagged 'other' (5 of 20): additions to the theme list to be proposed"]
    assert "'other' names: O0, O1" in lines[2]
    lines, flags = theme_share(passes(4, 15))           # under 20 ideas: reported, not flagged
    assert flags == [] and "21%" in lines[0]
    assert theme_share([]) == ([], [])
