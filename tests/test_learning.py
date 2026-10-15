"""The shared learning layer, first pieces (human ruling 2026-10-14): the
fixed vocabulary, the append-only journal, and the weekly pattern miner
(code, no LLM; luck teaches nothing; Bonferroni-corrected flags at n >= 20)."""
from __future__ import annotations

import random

import pytest

from learning import mining, vocabulary
from learning.journal import Entry, Journal

SOURCES = ("momentum_sleeve", "form4_insiders", "ai_trader")


def entry(i, excess, process="right_reason", **tags):
    base = {"sector": "health", "setup": "insider_cluster", "catalyst": "insider_buying", "regime": "calm_uptrend",
            "confidence_band": "50-70", "source": "form4_insiders"}
    base.update(tags)
    return Entry(decision_id=f"d{i}", account="main", strategy="judged", kind="trade", resolved_at="2026-11-01",
                 thesis="t", expected="up", actual="up", excess=excess, process=process, tags=base)


def test_computed_tags_and_the_closed_vocabulary():
    assert vocabulary.confidence_band(49.9) == "<50" and vocabulary.confidence_band(70) == "70-85"
    assert vocabulary.confidence_band(85) == "85+" and vocabulary.confidence_band(None) == "n/a"
    assert vocabulary.regime(18, True) == "calm_uptrend" and vocabulary.regime(25, False) == "volatile_downtrend"
    assert vocabulary.regime(None, True) == "unknown"
    vocabulary.check(entry(1, 0.01).tags, SOURCES)
    with pytest.raises(ValueError):
        vocabulary.check({**entry(1, 0.01).tags, "setup": "vibes"}, SOURCES)
    with pytest.raises(ValueError):
        vocabulary.check({**entry(1, 0.01).tags, "source": "a_blog"}, SOURCES)
    with pytest.raises(ValueError):
        entry(1, 0.01, process="lucky")


def test_the_journal_is_append_only_and_never_journals_twice(tmp_path):
    journal = Journal(tmp_path / "learning" / "journal.jsonl")
    assert journal.append([entry(1, 0.02), entry(2, -0.01)], SOURCES) == 2
    assert journal.append([entry(1, 0.05), entry(3, 0.0)], SOURCES) == 1   # d1 already there
    rows = journal.entries()
    assert [e.decision_id for e in rows] == ["d1", "d2", "d3"] and rows[0].excess == 0.02
    with pytest.raises(ValueError):
        journal.append([entry(9, 0.0, sector="crypto-ish")], SOURCES)


def test_luck_teaches_nothing():
    assert entry(1, 0.1, process="right_luck").teaches is False
    assert entry(1, 0.1, process="wrong_reason").teaches is True


def test_the_miner_flags_only_a_real_pattern_with_enough_cases():
    rng = random.Random(3)
    rows = [entry(i, 0.03 + rng.gauss(0, 0.01), sector="energy") for i in range(30)]          # a real edge
    rows += [entry(100 + i, rng.gauss(0, 0.03), sector="shops") for i in range(30)]           # noise
    rows += [entry(200 + i, 0.05, sector="utilities") for i in range(5)]                      # too few
    rows += [entry(300 + i, 0.5, sector="money", process="right_luck") for i in range(40)]    # luck: excluded
    cells = {c.label: c for c in mining.tally(rows)}
    assert cells["sector=energy"].flagged and cells["sector=energy"].n == 30
    assert not cells["sector=shops"].flagged
    assert not cells["sector=utilities"].flagged and cells["sector=utilities"].n == 5
    assert "sector=money" not in cells
    pair = cells["sector=energy & setup=insider_cluster"]
    assert pair.n == 30 and pair.flagged
    # the correction widens every interval with the number of cells tested
    assert cells["sector=energy"].low < 0.03
