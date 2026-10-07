"""Calibration block (human ruling 2026-10-07, item 4): the system's own
measured record, keyed on the voted median confidence, rendered to the research
prompt as data at n >= 20 and byte-silent below it."""
from __future__ import annotations

import ast
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from forward.funnel import FunnelEntry
from forward.returns import ForwardRow, HorizonMark
from orchestrator.calibration import HEADER, CalibrationRecord, band_of, measured_cells, render_measured_record
from signals import SignalClass

T0 = datetime(2026, 9, 1, 14, 0, tzinfo=timezone.utc)


def _entry(i, source="congressional_disclosures", voted=52, horizon="weeks", direction="long"):
    return FunnelEntry(
        decision_id=f"d{i}", source_id=source, credibility_key=source, signal_class=SignalClass.CLASS_2_MOMENTUM,
        observed_at=T0 + timedelta(days=i), tickers=(f"T{i}",), bucket="declined", code="", confidence=voted,
        lag_days=None, time_horizon=horizon, direction=direction, voted_confidence=voted,
    )


def _row(entry, excess5, excess20=None):
    marks = {5: HorizonMark(marked_on=entry.observed_at.date() + timedelta(days=5), close=Decimal("10"), return_pct=Decimal("1"), excess_pct=Decimal(str(excess5)))}
    if excess20 is not None:
        marks[20] = HorizonMark(marked_on=entry.observed_at.date() + timedelta(days=20), close=Decimal("10"), return_pct=Decimal("1"), excess_pct=Decimal(str(excess20)))
    return ForwardRow(symbol=entry.tickers[0], observed=entry.observed_at.date(), base_date=entry.observed_at.date(), base_close=Decimal("10"), marks=marks, computed_at=T0)


def test_bands_follow_the_sizing_table():
    assert band_of(44) == "<45" and band_of(45) == "45-55" and band_of(54) == "45-55"
    assert band_of(55) == "55-70" and band_of(70) == "70-85" and band_of(85) == "85+" and band_of(100) == "85+"


def test_a_cell_renders_only_at_twenty_voted_marks_and_only_for_its_source():
    entries = [_entry(i) for i in range(20)]
    rows = {(e.tickers[0], e.observed_at.date()): _row(e, 1.0 if i % 2 else -0.5, 2.0) for i, e in enumerate(entries)}
    # Nineteen marked: nothing.
    partial = dict(list(rows.items())[:19])
    assert measured_cells(entries, partial) == []
    assert CalibrationRecord(entries, partial).for_source("congressional_disclosures") == ""
    # Twenty marked: one cell, rendered for its source only, fenced, as data.
    cells = measured_cells(entries, rows)
    assert len(cells) == 1
    cell = cells[0]
    assert (cell.source_id, cell.band, cell.horizon, cell.direction, cell.n) == ("congressional_disclosures", "45-55", "weeks", "long", 20)
    assert cell.hit_5d == 0.5 and cell.mean_5d == pytest.approx(0.25) and cell.n_20d == 20 and cell.mean_20d == 2.0
    record = CalibrationRecord(entries, rows)
    text = record.for_source("congressional_disclosures")
    assert text.startswith(HEADER) and "```" in text
    assert "long verdicts, voted confidence 45-55, horizon weeks: n=20, 5d hit 50%, mean 5d excess +0.25%, mean 20d excess +2.00% (n=20)" in text
    assert record.for_source("form4_insiders") == ""

    class _Signal:
        source_id = "congressional_disclosures"

    assert record.for_signal(_Signal()) == text


def test_unvoted_entries_never_count_and_direction_splits_the_cell():
    voted = [_entry(i) for i in range(20)]
    from dataclasses import replace

    unvoted = [replace(_entry(100 + i), voted_confidence=None) for i in range(20)]
    declines = [_entry(200 + i, direction="no_position") for i in range(20)]
    rows = {}
    for e in voted + unvoted + declines:
        rows[(e.tickers[0], e.observed_at.date())] = _row(e, 1.0)
    cells = measured_cells(voted + unvoted + declines, rows)
    assert sorted((c.direction, c.n) for c in cells) == [("long", 20), ("no_position", 20)]


def test_the_prompt_carries_the_block_as_data_and_is_byte_identical_without_it():
    from orchestrator.golden import load_cases
    from research.prompts import build_user_prompt

    case = next(c for c in load_cases() if c.name == "pelosi-uber-priced-in")
    signal = case.signal(T0)
    plain = build_user_prompt(signal)
    assert build_user_prompt(signal, measured_record=None) == plain
    assert build_user_prompt(signal, measured_record="") == plain
    block = render_measured_record(
        measured_cells([_entry(i) for i in range(20)], {(f"T{i}", (T0 + timedelta(days=i)).date()): _row(_entry(i), 0.5) for i in range(20)}),
        "congressional_disclosures",
    )
    with_block = build_user_prompt(signal, measured_record=block)
    assert block in with_block and with_block != plain
    # The block speaks of forward returns only: no target, no P&L, no shortfall.
    for word in ("target", "p&l", "shortfall", "behind"):
        assert word not in block.lower().replace("nothing more", "")


def test_the_research_pass_asks_the_provider_and_survives_its_failure():
    from research.research_pass import ResearchPass

    seen = []

    def provider(signal):
        seen.append(signal.source_id)
        raise RuntimeError("boom")

    pass_ = ResearchPass.__new__(ResearchPass)
    pass_._measured_record = provider

    class _S:
        source_id = "form4_insiders"

    assert pass_._measured_block(_S()) is None and seen == ["form4_insiders"]
    pass_._measured_record = lambda s: ""
    assert pass_._measured_block(_S()) is None
    pass_._measured_record = lambda s: "MEASURED RECORD ..."
    assert pass_._measured_block(_S()) == "MEASURED RECORD ..."


def test_the_calibration_module_is_fenced_from_the_scoreboard():
    """It may read the forward engine (a measured record is allowed evidence);
    it may not read attribution, spend, the audit log or any target module."""
    src = Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "calibration.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    for forbidden in ("audit.attribution", "audit.spend", "audit.log", "orchestrator.target", "orchestrator.weekly_target"):
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in names), forbidden
    assert any(name.startswith("forward") for name in names)
