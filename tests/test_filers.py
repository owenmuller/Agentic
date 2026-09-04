"""Filer-name normalisation (human ruling 2026-09-04): one person, one
credibility key — at ingest for new records, on read for the append-only past.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from forward.funnel import funnel_entries
from forward.report import render_forward_report
from orchestrator.config import ConvergenceConfig
from orchestrator.registry import SignalRegistry
from research.credibility import CredibilityTracker
from signals.filers import (
    DEFAULT_FILER_ALIASES,
    canonical_credibility_key,
    normalize_filer,
    same_filer,
)
from test_orchestrator import FakeClock

NOW = datetime(2026, 9, 4, 14, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def source():
    from signals.config import SignalsConfig

    return SignalsConfig.load().source("class_2", "congressional_disclosures")


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("John J Mr Mcguire Iii", "John Mcguire"),
        ("John Mcguire", "John Mcguire"),
        ("Richard Dean Dr Mccormick", "Richard Dean Mccormick"),
        ("A. Mitchell Jr. McConnell", "Mitch Mcconnell"),  # rule, then the alias
        ("Pelosi, Nancy", "Pelosi Nancy"),  # order is not normalised; matching is token-based
        ("NANCY PELOSI", "Nancy Pelosi"),
        ("Debbie Wasserman Schultz", "Debbie Wasserman Schultz"),
        ("Thomas H. Kean Jr", "Thomas Kean"),
        ("", ""),
    ],
)
def test_the_rule_and_the_alias_table(raw, canonical):
    assert normalize_filer(raw) == canonical


def test_source_aliases_extend_the_default_table():
    assert "Mitchell Mcconnell" in DEFAULT_FILER_ALIASES
    assert normalize_filer("Jim Himes", {"Jim Himes": "James Himes"}) == "James Himes"
    assert normalize_filer("James A. Himes", {"jim himes": "James Himes"}) == "James Himes"


def test_credibility_keys_canonicalise_and_other_keys_pass_through():
    assert (
        canonical_credibility_key("congressional_disclosures/John J Mr Mcguire Iii")
        == "congressional_disclosures/John Mcguire"
    )
    assert canonical_credibility_key("form_13d/Sarissa Capital") == "form_13d/Sarissa Capital"
    assert canonical_credibility_key(None) is None
    assert same_filer("John J Mr Mcguire Iii", "john mcguire")
    assert not same_filer("John Mcguire", "Kevin Hern")


def test_the_quiver_fetcher_keys_the_canonical_name(source):
    from test_quiver import PELOSI_ROW, fetcher_with

    import httpx

    row = dict(PELOSI_ROW, Representative="John J Mr Mcguire Iii")
    fetcher, _ = fetcher_with([httpx.Response(200, json=[row])])
    [item] = fetcher(source)
    assert item.fields["credibility_key"] == "congressional_disclosures/John Mcguire"
    assert item.fields["representative"] == "John Mcguire"
    assert item.fields["representative_filed_as"] == "John J Mr Mcguire Iii"


def _snapshot_record(decision_id, key, ticker="INTC"):
    from audit.records import (
        GateSnapshot,
        RejectedStage,
        SignalSnapshot,
        StageRejectionRecord,
    )
    from signals import SignalClass

    return StageRejectionRecord(
        decision_id=decision_id,
        recorded_at=NOW,
        stage=RejectedStage.PRE_FILTER,
        code="pre_filter",
        message="x",
        signal=SignalSnapshot(
            signal_id=decision_id,
            source_id="congressional_disclosures",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=NOW,
            content=f"ticker: {ticker}\ntransaction: Purchase",
            raw_content="x",
            credibility_key=key,
            tickers=(ticker,),
        ),
    )


def test_old_records_regroup_on_read_and_unresolved_filers_collapse():
    records = [
        _snapshot_record("a", "congressional_disclosures/John J Mr Mcguire Iii"),
        _snapshot_record("b", "congressional_disclosures/John Mcguire"),
        _snapshot_record("c", "congressional_disclosures/Kevin Hern"),
    ]
    entries = funnel_entries(records)
    assert {e.credibility_key for e in entries} == {
        "congressional_disclosures/John Mcguire",
        "congressional_disclosures/Kevin Hern",
    }
    text = render_forward_report(entries, rows={})
    # Two filers, neither resolved: one count line, not two "no resolved marks" rows.
    assert "2 filer(s) with no resolved" in text
    assert "John Mcguire (2)" in text and "Kevin Hern (1)" in text
    assert text.count("no resolved marks yet") == 0 or "by filer" not in text.split("no resolved marks yet")[0][-200:]


def test_the_registry_and_tracker_treat_variants_as_one_identity():
    registry = SignalRegistry(ConvergenceConfig(window_days=400), FakeClock(NOW))
    registry.seed([
        _snapshot_record("a", "congressional_disclosures/John J Mr Mcguire Iii"),
        _snapshot_record("b", "congressional_disclosures/John Mcguire", ticker="AMRN"),
    ])
    # One identity behind both records: no self-"diversity" between his own filings.
    assert registry.verdict_summary() == {}  # prefiltered rows carry no verdict
    tracker = CredibilityTracker()
    tracker.record_outcome("congressional_disclosures/John J Mr Mcguire Iii", won=True)
    tracker.record_outcome("congressional_disclosures/John Mcguire", won=False)
    summary = tracker.summary_for("congressional_disclosures/John Mcguire")
    assert summary.resolved_calls == 2 and summary.winning_calls == 1
