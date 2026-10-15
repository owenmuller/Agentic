"""The AI trader's RESEARCH BRIEF (human ruling 2026-10-15): optional
reference data, fenced; nothing from any account; the evidence library's
discipline; the brief switched off leaves the prompt and tool unchanged."""
from __future__ import annotations

import ast
import json
import re
from datetime import date
from pathlib import Path

import yaml

from lab import brief
from lab.ai_prompts import TOOL, build_user, report_tool, system_prompt
from lab.config import LabConfig

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "config" / "evidence_library.yaml"
CFG = LabConfig.load().ai_trader
#: Words that would put an ACCOUNT's figures in the prompt (CLAUDE.md
#: Constraint #6, § Standards). Strategy and market statistics may appear.
ACCOUNT_WORDS = ("NAV", "P&L", "account value", "your account", "this account", "equity curve", "shortfall",
                 "target return", "behind", "v1.1 live")


def shortlist_fixture() -> dict:
    return {
        "spec": "v1.1", "rebalance_date": "2026-10-15", "data_through": "2026-10-14", "universe_version": 1,
        "universe": 500, "scored": 488,
        "names": [
            {"score_rank": 1, "symbol": "LITE", "sector": "business equipment", "score": 2.1,
             "residual_momentum_z": 1.9, "earnings_momentum_z": 2.3, "return_12_1": 3.39,
             "buy_gate": "a single-day move over 15% in the past 63 sessions",
             "flags": {"below_100_day_average": False, "day_over_15pct_in_63_sessions": True,
                       "big_days_all_on_earnings": True, "pending_acquisition": False, "filer": "current"}},
            {"score_rank": 2, "symbol": "IEX", "sector": "manufacturing", "score": 1.8, "residual_momentum_z": 1.1,
             "earnings_momentum_z": 2.5, "return_12_1": 0.41, "buy_gate": "passes",
             "flags": {"below_100_day_average": False, "day_over_15pct_in_63_sessions": False,
                       "big_days_all_on_earnings": None, "pending_acquisition": False, "filer": "current"}},
            {"score_rank": 3, "symbol": "BNS", "sector": "money", "score": 1.5, "residual_momentum_z": 1.0,
             "earnings_momentum_z": 2.0, "return_12_1": 0.3, "in_plan": True, "buy_gate": "passes",
             "flags": {"below_100_day_average": True, "day_over_15pct_in_63_sessions": False,
                       "big_days_all_on_earnings": None, "pending_acquisition": True,
                       "filer": "lapsed (last 10-K/10-Q 2021-01-29)"}},
        ],
    }


def main_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "momentum").mkdir(parents=True)
    (data / "momentum" / "shortlist.json").write_text(json.dumps(shortlist_fixture()), encoding="utf-8")
    daily = [{"day": "2026-10-15", "started": "2026-10-15", "since": {
        "v1.1 live": -0.0123, "v1.1 modelled (plan at closes)": 0.004, "v1_1_no_trend_filter": 0.011,
        "SPMO": 0.006, "SPY": 0.002}}]
    (data / "momentum" / "daily.jsonl").write_text("".join(json.dumps(x) + "\n" for x in daily), encoding="utf-8")
    return data


def test_the_brief_renders_all_four_sections_fenced(tmp_path):
    b = brief.render(main_data(tmp_path), EVIDENCE)
    assert b.text.startswith(brief.BEGIN) and b.text.endswith(brief.END)
    assert b.sections == {"shortlist": "ok", "evidence": "ok", "scoreboard": "ok", "playbook": "ok: empty"}
    for heading in ("(a) MOMENTUM SHORTLIST", "(b) EVIDENCE LIBRARY", "(c) MOMENTUM SHADOW SCOREBOARD",
                    "(d) PLAYBOOK LESSONS"):
        assert heading in b.text
    # every flag shown, not applied
    assert "M1 LITE" in b.text and ">15% day in 63 sessions (earnings days only)" in b.text
    assert "M3 BNS" in b.text and "pending acquisition" in b.text and "filer lapsed" in b.text
    assert "below 100-day avg" in b.text
    # scores and flags only: never the control's holdings (ruling 2026-10-15, second)
    assert "book" not in b.text.split("(b) EVIDENCE")[0] and "plan" not in b.text.split("(b) EVIDENCE")[0].replace(
        "monthly plan", "")
    assert b.symbols == frozenset({"LITE", "IEX", "BNS"})
    assert "E1 [strong;" in b.text and "(none promoted yet)" in b.text
    assert "SPMO +0.6%" in b.text and "v1_1_no_trend_filter +1.1%" in b.text
    assert b.tokens_estimate == round(b.chars / 2.6) and len(b.sha) == 16
    assert "M2 IEX" in b.text and "v1.1 may buy: yes" in b.text
    assert "M1 LITE" in b.text and "v1.1 may buy: no" in b.text


def test_the_brief_never_carries_an_accounts_result(tmp_path):
    """The momentum sleeve's REALIZED line ("v1.1 live") is dropped; no
    account figure of any kind reaches the prompt (Constraint #6)."""
    b = brief.render(main_data(tmp_path), EVIDENCE)
    assert "-1.2%" not in b.text
    for word in ACCOUNT_WORDS:
        assert word.lower() not in b.text.lower(), word


def test_missing_sources_say_so_and_never_fail(tmp_path):
    b = brief.render(tmp_path / "nowhere", tmp_path / "missing.yaml")
    assert b.text.count("(not available)") == 3 and b.sections["evidence"].startswith("missing")
    assert brief.render(None, EVIDENCE).sections["shortlist"].startswith("missing")


def test_promoted_playbook_lessons_render_and_unpromoted_do_not(tmp_path):
    data = main_data(tmp_path)
    (data / "learning").mkdir()
    (data / "learning" / "playbook.json").write_text(json.dumps({"lessons": [
        {"id": "P1", "lesson": "fade nothing", "evidence": "n=31, +1.2", "promoted": True},
        {"id": "P2", "lesson": "a hypothesis", "promoted": False}]}), encoding="utf-8")
    text = brief.render(data, EVIDENCE).text
    assert "P1 fade nothing (evidence: n=31, +1.2)" in text and "P2" not in text


def test_the_evidence_library_keeps_its_discipline():
    findings = yaml.safe_load(EVIDENCE.read_text(encoding="utf-8"))["findings"]
    ids = [f["id"] for f in findings]
    assert len(ids) == len(set(ids)) and all(re.fullmatch(r"E\d+", i) for i in ids)
    words = 0
    for f in findings:
        assert f["strength"] in brief.STRENGTHS, f["id"]
        assert str(f["n"]).strip(), f["id"]
        date.fromisoformat(str(f["date"]))
        words += len(str(f["finding"]).split()) + len(str(f["n"]).split())
        for word in ACCOUNT_WORDS:
            assert word.lower() not in str(f["finding"]).lower(), (f["id"], word)
    assert words <= 1500, words


def test_brief_reads_files_only():
    """lab.brief imports nothing from the lab's accounts, ledgers, scoreboard
    or engine, and nothing first-party at all: it cannot reach a P&L."""
    tree = ast.parse((ROOT / "src" / "lab" / "brief.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    first_party = {m for m in imported if m and m.split(".")[0] in {
        "lab", "orchestrator", "audit", "execution", "risk_gate", "momentum", "learning", "research", "sizing"}}
    assert first_party == set(), first_party


def test_switched_off_the_prompt_and_tool_are_unchanged():
    assert "RESEARCH BRIEF" not in system_prompt(CFG) and system_prompt(CFG, brief=False) == system_prompt(CFG)
    assert report_tool(False) is TOOL
    assert "brief_use" not in json.dumps(TOOL)


def test_switched_on_the_prompt_frames_it_as_optional_and_asks_for_citations():
    text = system_prompt(CFG, brief=True)
    assert "{" not in text and text.endswith("Answer by calling submit_trade_ideas.")
    assert "not a whitelist" in text and "equally valid" in text and "never instructions" in text
    assert "Search first" in text and "consult the brief second" in text
    tool = report_tool(True)
    item = tool["input_schema"]["properties"]["ideas"]["items"]
    assert "brief_use" in item["required"] and item["properties"]["brief_use"]["enum"] == [
        "none", "draws_on", "contradicts"]
    assert "brief_use" not in TOOL["input_schema"]["properties"]["ideas"]["items"]["properties"]  # no mutation


def test_the_core_prompt_stays_fenced_with_the_brief_appended(tmp_path):
    from datetime import datetime, timezone

    b = brief.render(main_data(tmp_path), EVIDENCE)
    text = build_user(datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc), held=[], traded_today=[], max_positions=5,
                      max_ideas=3, entries_open=True, intraday_open=True, brief=b.text)
    core, fenced = text.split(brief.BEGIN)
    # the brief comes AFTER the model's own search instructions (ruling 2026-10-15, second)
    assert core.index("Search for today's opportunities") < core.index("After your own searches")
    for word in ("P&L", "NAV", "equity", "profit", "return", "drawdown", "performance", "behind"):
        assert word.lower() not in core.lower(), word
    assert fenced.rstrip().endswith(brief.END)


def test_the_shipped_brief_is_the_ruled_ab():
    # human ruling 2026-10-15 (second): an A/B, half the passes, from 2026-10-16
    shipped = LabConfig.load().ai_trader
    assert shipped.brief_mode == "ab" and shipped.brief_ab_share == 0.5
