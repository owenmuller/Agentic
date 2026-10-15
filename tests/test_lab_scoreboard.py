"""PAPER PUSH items 3 and 4: the scoreboard and the fill-realism line."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lab import scoreboard as sb


def snap(day: int, equity: float, rungs=None, hour: int = 20) -> dict:
    return {"event": "snapshot", "at": datetime(2026, 10, day, hour, 10, tzinfo=timezone.utc).isoformat(),
            "equity": str(equity), "rungs": rungs or {}}


def settled(pid, side, qty, price, reason="", mult=1, day=13):
    return {"event": "settled", "at": datetime(2026, 10, day, 15, 0, tzinfo=timezone.utc).isoformat(), "pid": pid,
            "side": side, "filled_qty": str(qty), "avg_price": str(price), "reason": reason, "multiplier": mult}


def opened(pid, symbol):
    return {"event": "position_opened", "at": "2026-10-13T14:00:00+00:00", "pid": pid, "symbol": symbol}


def closed(pid):
    return {"event": "position_closed", "at": "2026-10-13T19:00:00+00:00", "pid": pid}


EXEC = [
    {"day": "2026-10-12", "fills": 4, "pnl_live": 10.0, "pnl_modelled_at_live_sizes": 14.0,
     "slippage_bp_vs_model": {"entry": {"n": 6, "mean": 2.0}, "stop": {"n": 10, "mean": 8.0},
                              "close_out": {"n": 12, "mean": -1.0}}},
    {"day": "2026-10-13", "fills": 2, "pnl_live": -5.0, "pnl_modelled_at_live_sizes": -4.0,
     "slippage_bp_vs_model": {"entry": {"n": 6, "mean": 4.0}}},
]


def test_slippage_pools_by_fill_and_waits_for_ten_fills():
    table = sb.slippage_table(EXEC)
    assert table["entry"] == (12, 3.0)
    assert table["stop"] == (10, 8.0)
    assert sb._rate(table, "entry") == pytest.approx(3.0 / 1e4)
    assert sb._rate(table, "close_out") == 0.0  # favourable: charged zero, never credited
    assert sb._rate(table, "reversal") is None   # unmeasured
    assert sb._rate({"entry": (9, 5.0)}, "entry") is None


def test_round_trips_and_their_slippage_charge():
    table = sb.slippage_table(EXEC)
    events = [opened("a", "AAPL"), settled("a", "buy", 10, 100), settled("a", "sell", 10, 110, "target"), closed("a"),
              opened("b", "SPY"), settled("b", "buy", 2, 1.5, mult=100), settled("b", "sell", 2, 1.0, "stop", 100),
              closed("b"), opened("c", "MSFT"), settled("c", "buy", 1, 400)]  # c still open
    trades = sb.lab_trades(events, table)
    assert [(t.symbol, t.pnl) for t in trades] == [("AAPL", 100.0), ("SPY", -100.0)]
    # AAPL: entry 1000 x 3bp + target exit at the close-out rate (favourable -> 0)
    assert trades[0].cost == pytest.approx(1000 * 3e-4)
    # SPY option: entry 300 x 3bp + stop 200 x 8bp
    assert trades[1].cost == pytest.approx(300 * 3e-4 + 200 * 8e-4)


def test_the_board():
    ladder = [snap(12, 100000, {"spy_2x": 33000}), snap(13, 98000, {"spy_2x": 32000}),
              snap(14, 103000, {"spy_2x": 34000})]
    ai = [snap(13, 100000), snap(14, 101000),
          opened("a", "AAPL"), settled("a", "buy", 10, 100), settled("a", "sell", 10, 110, "target"), closed("a")]
    spy = {date(2026, 10, 12): 600.0, date(2026, 10, 13): 606.0, date(2026, 10, 14): 612.0}
    board = sb.build(ladder_events=ladder, ai_events=ai, exec_lines=EXEC, spy=spy, main_equity={},
                     rung_labels={"spy_2x": "2x SPY (SSO)"})
    rows = {r.name.strip(): r for r in board.rows}
    lad = rows["LEVERAGE LADDER (benchmark)"]
    assert lad.ret == pytest.approx(0.03) and lad.max_dd == pytest.approx(-0.02)
    assert lad.vs_spy == pytest.approx(0.03 - 0.02)
    ai_row = rows["AI TRADER"]
    assert ai_row.ret == pytest.approx(0.01) and ai_row.trades == 1 and ai_row.win_rate == 1.0
    assert ai_row.vs_ladder == pytest.approx(0.01 - (103000 / 98000 - 1))
    assert ai_row.vs_spy == pytest.approx(0.01 - (612 / 606 - 1))
    assert ai_row.ret_adj == pytest.approx(0.01 - 1000 * 3e-4 / 100000)
    assert rows["B EXECUTION TEST (not a strategy)"].sessions == 2
    assert "gap -5.00" in rows["B EXECUTION TEST (not a strategy)"].note
    assert rows["rung 2x SPY (SSO)"].ret == pytest.approx(34000 / 33000 - 1)
    text = sb.render(board, date(2026, 10, 16))
    assert "AI TRADER" in text and "verdict at 60" in text


def test_the_adjusted_line_waits_rather_than_guesses():
    ai = [snap(13, 100000), snap(14, 101000),
          opened("a", "AAPL"), settled("a", "buy", 10, 100), settled("a", "sell", 10, 110, "stop"), closed("a")]
    board = sb.build(ladder_events=[], ai_events=ai, exec_lines=[], spy={}, main_equity={}, rung_labels={})
    row = next(r for r in board.rows if r.name == "AI TRADER")
    assert row.ret_adj is None and "pending" in row.note


def test_the_stop_floor_is_proposed_only_after_ten_measured_stops():
    """Ruling 2026-10-10 (second): 1% until n >= 10 stop fills; then propose
    at least 3x the measured stop slippage - a proposal, never a change."""
    waiting = sb.stop_floor_note(sb.slippage_table(EXEC[1:]), 0.01)
    assert "1.00% holds" in waiting and "n = 0" in waiting
    proposal = sb.stop_floor_note(sb.slippage_table(EXEC), 0.01)
    assert "PROPOSAL" in proposal and "n = 10" in proposal and "0.24%" in proposal  # 3 x 8 bp
    board = sb.build(ladder_events=[], ai_events=[], exec_lines=EXEC, spy={}, main_equity={}, rung_labels={},
                     stop_floor=0.01)
    assert any("PROPOSAL" in note for note in board.notes)


def test_the_momentum_control_and_spmo_sit_beside_the_ai_trader():
    # ruling 2026-10-15, item 4: AI trader vs the mechanical momentum sleeve vs SPMO
    ai = [snap(13, 100000), snap(14, 101000)]
    spy = {date(2026, 10, 12): 600.0, date(2026, 10, 13): 606.0, date(2026, 10, 14): 612.0}
    spmo = {date(2026, 10, 12): 100.0, date(2026, 10, 13): 101.0, date(2026, 10, 14): 103.0}
    momentum = sb.momentum_series([
        {"day": "2026-10-13", "started": "2026-10-13", "since": {"v1.1 live": 0.01, "SPMO": 0.0}},
        {"day": "2026-10-14", "started": "2026-10-13", "since": {"v1.1 live": 0.025, "SPMO": 0.02}},
    ])
    assert momentum == {date(2026, 10, 12): 1.0, date(2026, 10, 13): 1.01, date(2026, 10, 14): 1.025}
    board = sb.build(ladder_events=[], ai_events=ai, exec_lines=[], spy=spy, main_equity={}, rung_labels={},
                     momentum=momentum, spmo=spmo)
    rows = {r.name.strip(): r for r in board.rows}
    names = [r.name for r in board.rows]
    assert names.index("AI TRADER") < names.index("MOMENTUM SLEEVE (main book; control)") < names.index("SPMO")
    mom = rows["MOMENTUM SLEEVE (main book; control)"]
    assert mom.ret == pytest.approx(0.025) and mom.vs_spmo == pytest.approx(0.025 - 0.03)
    assert rows["AI TRADER"].vs_spmo == pytest.approx(0.01 - (103 / 101 - 1))
    assert rows["SPMO"].ret == pytest.approx(103 / 101 - 1)
    assert "vs SPMO" in sb.render(board, date(2026, 10, 16))


def _pass(day, arm, symbols, on, uses=None):
    ideas = [dict({"symbol": s}, **({"brief_use": u} if u else {}))
             for s, u in zip(symbols, uses or [None] * len(symbols))]
    return {"event": "llm_pass", "at": f"2026-10-{day}T14:00:00+00:00", "brief_arm": arm, "ideas": ideas,
            "ideas_on_shortlist": on}


def test_the_brief_ab_reports_by_arm_and_flags():
    # ruling 2026-10-15, second: ideas proposed, taken, share outside the
    # brief, forward results of entries - and the two flags
    events = []
    for i in range(10):
        events.append(_pass(16, "brief", ["A"], [True], ["draws_on"]))
        events.append(_pass(16, "control", ["X", "Y"], [False, True]))
    events += [dict(opened("b1", "A"), brief_arm="brief"), settled("b1", "buy", 10, 100),
               settled("b1", "sell", 10, 110), closed("b1"),
               dict(opened("c1", "X"), brief_arm="control"), settled("c1", "buy", 5, 100),
               settled("c1", "sell", 5, 95), closed("c1"), dict(opened("c2", "Y"), brief_arm="control")]
    lines, flags = sb.brief_ab(events)
    text = "\n".join(lines)
    assert "since 2026-10-16" in text
    assert "brief    passes  10  ideas  10 (1.00/pass)  taken   1  off the shortlist 0%  closed 1 (won 1, mean +10.00%" in text
    assert "control  passes  10  ideas  20 (2.00/pass)  taken   2  off the shortlist 50%  closed 1 (won 0, mean -5.00%" in text
    assert "brief use: draws_on 10" in text
    assert any("notably fewer ideas (1.00 vs 2.00" in f for f in flags)
    assert any("mostly shortlist names (100%)" in f for f in flags)
    assert sb.brief_ab([_pass(16, None, ["A"], None)]) == ([], [])  # no A/B yet: no section


def test_no_flag_below_the_minimum_sample():
    events = [_pass(16, "brief", ["A"], [True]), _pass(16, "control", ["X", "Y"], [False, False])]
    lines, flags = sb.brief_ab(events)
    assert flags == [] and any("no flag" in line for line in lines)
