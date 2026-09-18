"""Same-name-same-day de-duplication (human ruling 2026-09-18).

Claims: the first tradeable candidate for a symbol dispatches and every later
same-day candidate on that symbol writes stage_rejection same_name_today naming
the first decision and spends nothing; on a held name the repeat is noted as
convergence on the position and owes a review; the ledger is seeded from the
log so a restart cannot buy a second pass; a new day starts clean; signals
that name nothing are never de-duplicated.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from signals.scanners import RawItem
from test_adds import Feeds
from test_orchestrator import FakeBroker, FakeClock, FakeLLM, build, feed, prices_of


@pytest.fixture(scope="session")
def signals_config():
    from signals import SignalsConfig

    return SignalsConfig.load()


def _build(tmp_path, signals_config, posts=None, clock=None, fetcher=None):
    from research.config import ResearchConfig
    from risk_gate import RiskLimits

    return build(
        tmp_path, RiskLimits.load(), signals_config, ResearchConfig.load(),
        llm=FakeLLM(), broker=FakeBroker(), clock=clock or FakeClock(),
        fetcher=fetcher or feed(nolimitgains=posts or []), prices=prices_of(NUE="140.00"),
    )


def test_the_second_same_day_candidate_on_a_name_attaches_instead_of_spending(tmp_path, signals_config):
    posts = [
        "Loading $NUE here. Setup is live, entry: 140.",
        "Adding $NUE calls here, entry: 141.",
        "Buying $STLD here, entry: 90.",
    ]
    started = _build(tmp_path, signals_config, posts)
    report = started.loop.tick()
    processed = [(r.decision or r.rejection).signal.content for r in report.processed]
    assert processed == [posts[0], posts[2]]
    repeats = [r for r in started.audit.stage_rejections() if r.code == "same_name_today"]
    assert len(repeats) == 1
    first_id = report.processed[0].decision_id
    assert f"NUE already researched today (decision {first_id})" in repeats[0].message
    assert repeats[0].signal.content == posts[1]
    # Two passes bought, not three.
    assert started.audit.research_passes_on(started.loop._clock().date()) == 2


def test_a_repeat_on_a_held_name_is_convergence_and_owes_a_review(tmp_path, signals_config):
    """Tick 1 opens NUE on a Trump post; a caller names NUE an hour later, same
    day: no second pass, a convergence note on the position, a review owed."""
    clock = FakeClock()
    feeds = Feeds()
    # The opening post must NAME the symbol: the ledger keys on the candidate's
    # own instrument, not on what research later returned.
    feeds.items["trump_posts"] = [RawItem("trump_posts-0", "Buying $NUE here. Entry: 140, stop: 130. Setup is live.", clock.now)]
    started = _build(tmp_path, signals_config, clock=clock, fetcher=feeds)
    started.loop.tick()
    position = started.exits.tracked[0]
    assert position.symbol == "NUE" and position.convergence == []
    clock.advance(minutes=61)
    feeds.items["nolimitgains"] = [RawItem("nl-1", "Adding $NUE here, entry: 141.", clock.now)]
    report = started.loop.tick()
    assert report.processed == []
    repeats = [r for r in started.audit.stage_rejections() if r.code == "same_name_today"]
    assert len(repeats) == 1 and position.decision_id in repeats[0].message
    assert [note.verdict for note in position.convergence] == ["same_name_today"]
    assert note_source(position) == "nolimitgains"
    # The owed review runs in the same tick (as a held add verdict's does) and
    # clears the flag; the trail shows what triggered it.
    assert report.reviews_run == 1 and position.last_review_at is not None
    review = started.audit.trail(position.decision_id).reviews[-1]
    assert "same_name_today" in review.trigger_reason


def note_source(position):
    return position.convergence[0].source_id


def test_the_ledger_is_seeded_from_the_log_and_rolls_at_the_day(tmp_path, signals_config):
    from datetime import timedelta

    clock = FakeClock()
    started = _build(tmp_path, signals_config, ["Loading $NUE here. Setup is live, entry: 140."], clock)
    started.loop.tick()
    today = clock.now.date()
    assert started.audit.research_symbols_on(today) == {"NUE": started.audit.decisions()[0].decision_id}
    assert started.audit.research_symbols_on(today + timedelta(days=1)) == {}
    # A rebuilt loop the same day: the ledger comes back from the log. (The
    # filler post shifts the fixture's external id so the queue's own dedup
    # does not swallow the repeat before the rule can see it.)
    rebuilt = _build(tmp_path, signals_config, ["Good morning traders.", "Buying $NUE again, entry: 142."], clock)
    report = rebuilt.loop.tick()
    assert report.processed == []
    assert any(r.code == "same_name_today" for r in rebuilt.audit.stage_rejections())
    # Tomorrow the name is fair game again.
    clock.advance(days=1)
    fresh = _build(tmp_path, signals_config, ["Morning.", "Also.", "Buying $NUE tomorrow, entry: 143."], clock)
    report = fresh.loop.tick()
    assert len(report.processed) == 1


def test_signals_naming_nothing_are_not_deduplicated():
    from orchestrator.loop import _primary_symbol
    from signals.records import Priority, Signal, SignalClass

    def sig(content, tickers=""):
        return Signal(
            signal_id="s", source_id="trump_posts", signal_class=SignalClass.CLASS_1_REALTIME,
            observed_at=datetime(2026, 9, 18, tzinfo=timezone.utc), content=content, raw_content=content,
            priority=Priority.ELEVATED, external_id="e", metadata={"tickers": tickers},
        )

    assert _primary_symbol(sig("Tariffs are working!")) == ""
    assert _primary_symbol(sig("x", "nue,stld")) == "NUE"
    assert _primary_symbol(sig("Buying $AMD calls")) == "AMD"
