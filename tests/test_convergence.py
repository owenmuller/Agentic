"""Signal convergence: the registry, its dispatch bonus, and its prompt block
(human ruling 2026-09-01).

The claims: the registry counts DIVERSITY, never volume — ten posts from one
account are one source, and nothing converges with itself; the cross-filer
cluster counts other members' purchases only; bonuses are capped and join the
dispatch sort as ORDERING ONLY; prior verdicts — declines included — reach the
next research pass as fenced data; and the whole thing is derived from the
system's own records, seeded from the audit log so a restart remembers.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from orchestrator.config import ConvergenceConfig
from orchestrator.registry import SignalRegistry
from research.prompts import build_user_prompt
from research.research_pass import ResearchPass

from test_exits import RoutingLLM, filer_signal
from test_orchestrator import NOW, REPORT, structured
from test_hardening import disclosure_item, congressional_feed


def registry(clock=lambda: NOW, **overrides) -> SignalRegistry:
    return SignalRegistry(ConvergenceConfig(**overrides), clock)


def disclosure(external_id, ticker, filer, transaction="Purchase"):
    signal = filer_signal(external_id, ticker, transaction, filer=filer)
    signal.metadata["tickers"] = ticker
    signal.metadata["credibility_key"] = f"congressional_disclosures/{filer}"
    return signal


def post(external_id, tickers, source="trump_posts"):
    from signals.records import Priority, Signal, SignalClass

    return Signal(
        signal_id=f"sig-{external_id}",
        source_id=source,
        signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=NOW,
        content=f"about {tickers}",
        raw_content="raw",
        priority=Priority.ELEVATED,
        external_id=external_id,
        metadata={"tickers": tickers},
    )


# ================================================================================
# Diversity, never count — and nothing converges with itself
# ================================================================================


def test_ten_posts_from_one_account_are_one_source():
    reg = registry()
    reg.note_signals([post(f"p-{n}", "NUE") for n in range(10)])
    asker = disclosure("d-1", "NUE", "Test Member")
    reg.note_signals([asker])
    # One other identity (trump_posts), however many posts it made.
    assert reg.bonus_for(asker) == Decimal("0.5")


def test_a_signal_never_converges_with_itself():
    reg = registry()
    only = post("p-1", "NUE")
    reg.note_signals([only])
    assert reg.bonus_for(only) == Decimal("0")
    context = reg.context_for(only)
    assert context is None


def test_the_cross_filer_cluster_counts_other_members_purchases_only():
    reg = registry()
    mine = disclosure("d-1", "NUE", "Test Member")
    other_buy = disclosure("d-2", "NUE", "Other Member")
    other_sale = disclosure("d-3", "NUE", "Third Member", transaction="Sale (Full)")
    reg.note_signals([mine, other_buy, other_sale])

    # Cluster: only Other Member's purchase counts (1.0). Diversity: two other
    # identities are active on NUE (0.5 each, cap 2.0) -> 1.0. Total 2.0.
    assert reg.bonus_for(mine) == Decimal("2.0")
    context = reg.context_for(mine)
    assert "1 other congressional filer(s) disclosed purchases" in context
    assert "Other Member" in context
    assert "Third Member" not in context.split("cluster")[1].split("\n")[0]


def test_bonuses_are_capped():
    reg = registry(cluster_bonus_cap="2.0", diversity_bonus_cap="1.0")
    mine = disclosure("d-0", "NUE", "Me")
    others = [disclosure(f"d-{n}", "NUE", f"Member {n}") for n in range(1, 8)]
    reg.note_signals([mine, *others])
    # Uncapped would be 7 x 1.0 + 7 x 0.5; the caps bound it at 2.0 + 1.0.
    assert reg.bonus_for(mine) == Decimal("3.0")


def test_signals_age_out_of_the_window():
    moments = {"now": NOW}
    reg = registry(clock=lambda: moments["now"], window_days=14)
    reg.note_signals([post("p-1", "NUE")])
    asker = disclosure("d-1", "NUE", "Test Member")
    reg.note_signals([asker])
    assert reg.bonus_for(asker) > 0

    moments["now"] = NOW + timedelta(days=15)
    assert reg.bonus_for(asker) == Decimal("0")


# ================================================================================
# Prior verdicts reach the next pass — declines included
# ================================================================================


def test_a_prior_decline_is_shown_to_the_next_source():
    reg = registry()
    first = post("p-1", "NUE", source="nolimitgains")
    reg.note_signals([first])
    reg.note_outcome(first, "declined", 42, code="confidence_below_floor")

    second = disclosure("d-1", "NUE", "Test Member")
    reg.note_signals([second])
    context = reg.context_for(second)
    assert "prior research verdicts on NUE" in context
    assert "declined (confidence_below_floor), confidence 42" in context


def test_verdicts_are_capped_at_the_most_recent():
    reg = registry(max_prior_verdicts=2)
    for n in range(5):
        signal = post(f"p-{n}", "NUE")
        reg.note_signals([signal])
        reg.note_outcome(signal, "declined", 40 + n, code="confidence_below_floor")
    context = reg.context_for(disclosure("d-1", "NUE", "Test Member"))
    assert "confidence 44" in context and "confidence 43" in context
    assert "confidence 40" not in context


# ================================================================================
# Seeding from the audit log
# ================================================================================


def test_the_registry_seeds_from_the_log(tmp_path):
    """A restart remembers last week's cluster: records inside the window seed
    active entries AND their verdicts; mechanical records do not."""
    from test_forward import decision, rejection, snapshot
    from audit.records import RejectedStage
    from signals import SignalClass

    congressional = snapshot(
        source="congressional_disclosures",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        content=(
            "ticker: NUE\ntransaction: Purchase\n"
            "disclosure lag: 10 days between the trade and its disclosure"
        ),
        credibility_key="congressional_disclosures/Old Member",
        filer="Old Member",
        tickers=("NUE",),
    )
    reg = registry()
    seeded = reg.seed(
        [
            decision("d-1", approved=True, snap=congressional),
            decision("d-2", approved=True, strategy="mechanical", snap=congressional),
            rejection("d-3", RejectedStage.SIZING, "confidence_below_floor"),
        ]
    )
    assert seeded > 0

    fresh = disclosure("d-9", "NUE", "New Member")
    reg.note_signals([fresh])
    context = reg.context_for(fresh)
    assert "Old Member" in context  # the cluster survived the restart
    assert "traded" in context  # and so did the verdict
    assert "declined" in context  # the sizing decline too (same NUE ticker)


# ================================================================================
# The prompt block: fenced data, guidance outside the fence
# ================================================================================


def test_the_convergence_block_is_fenced_with_guidance_outside():
    signal = disclosure("d-1", "NUE", "Test Member")
    prompt = build_user_prompt(
        signal,
        convergence_context="NUE:\n- independent sources active on NUE: 2",
    )
    assert "SIGNAL CONVERGENCE" in prompt
    assert "never corroboration by itself" in prompt
    assert "a prior decline is not a reason to decline now" in prompt
    # The registry text itself sits inside the fence.
    fence_start = prompt.index("SIGNAL CONVERGENCE")
    fenced = prompt[fence_start:]
    assert "BEGIN" in fenced and "independent sources active" in fenced


def test_no_convergence_means_no_block():
    prompt = build_user_prompt(disclosure("d-1", "NUE", "Test Member"))
    assert "SIGNAL CONVERGENCE" not in prompt


def test_the_research_pass_carries_the_registry_context_to_the_model():
    llm = RoutingLLM()
    ran = ResearchPass(
        llm,
        convergence_context=lambda signal: "NUE:\n- cross-filer purchase cluster: 2",
    )
    ran.run(post("p-1", "NUE"))
    assert "SIGNAL CONVERGENCE" in llm.calls[-1]["user"]
    assert "cross-filer purchase cluster: 2" in llm.calls[-1]["user"]


def test_a_broken_context_callable_never_blocks_the_pass():
    def boom(signal):
        raise RuntimeError("registry exploded")

    llm = RoutingLLM()
    outcome = ResearchPass(llm, convergence_context=boom).run(post("p-1", "NUE"))
    assert llm.calls  # the pass still ran
    assert "SIGNAL CONVERGENCE" not in llm.calls[-1]["user"]


# ================================================================================
# Source families and the decision stamp (ruling 2026-09-02)
# ================================================================================


def test_the_four_families_map_deterministically():
    from orchestrator.registry import family_of
    from signals.records import SignalClass

    assert family_of("congressional_disclosures", SignalClass.CLASS_2_MOMENTUM) == (
        "congressional_filings"
    )
    assert family_of("form13f_situational", SignalClass.CLASS_3_THESIS) == (
        "13f_filings"
    )
    assert family_of("trump_posts", SignalClass.CLASS_1_REALTIME) == "trump_posts"
    # ALL X accounts are ONE family — amplification is not independence.
    for account in ("nolimitgains", "citrini", "optionshawk", "unusual_whales"):
        assert family_of(account, SignalClass.CLASS_1_REALTIME) == "x_callers"


def test_the_snapshot_counts_families_including_the_signals_own():
    reg = registry()
    reg.note_signals([post("p-1", "NUE", source="trump_posts")])
    reg.note_signals([post("p-2", "NUE", source="nolimitgains")])
    subject = disclosure("d-1", "NUE", "Test Member")
    reg.note_signals([subject])

    snapshot = reg.snapshot_for(subject)
    assert snapshot.symbol == "NUE"
    assert snapshot.families == (
        "congressional_filings",
        "trump_posts",
        "x_callers",
    )
    assert snapshot.family_count == 3
    assert snapshot.has_filing_family  # the future band-up rule's second gate
    assert snapshot.independent_identities == 2

    # A signal alone on its name still stamps its own family — count 1.
    solo = post("p-9", "ZZZQ", source="nolimitgains")
    reg.note_signals([solo])
    assert reg.snapshot_for(solo).families == ("x_callers",)


def test_the_mechanical_record_is_never_stamped(tmp_path):
    """No judgment-layer artefacts in the control arm — the mechanical entry's
    DecisionRecord carries no convergence stamp."""
    from risk_gate import RiskLimits
    from research.config import ResearchConfig
    from signals import SignalsConfig
    from test_orchestrator import FakeBroker, FakeClock, build, prices_of

    started = build(
        tmp_path,
        RiskLimits.load(),
        SignalsConfig.load(),
        ResearchConfig.load(),
        llm=RoutingLLM(
            **{
                "submit_research": structured(
                    {**REPORT, "priced_in_analysis": "measured", "confidence": 40}
                )
            }
        ),
        fetcher=congressional_feed(
            disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")
        ),
        prices=prices_of(NUE="140.00", SGOV="100.40"),
        broker=FakeBroker(),
        clock=FakeClock(),
    )
    started.loop.tick()
    # Confidence 40 -> the judged path declines at sizing (a stage rejection,
    # no stamp needed); the MECHANICAL entry's DecisionRecord must NOT be
    # stamped — no judgment-layer artefacts in the control arm.
    mechanical = [
        d
        for d in started.audit.decisions()
        if d.sizing.strategy == "mechanical"
    ]
    assert mechanical and mechanical[0].convergence is None


def test_a_traded_decision_is_stamped(tmp_path):
    from risk_gate import RiskLimits
    from research.config import ResearchConfig
    from signals import SignalsConfig
    from test_orchestrator import FakeBroker, FakeClock, build, prices_of

    started = build(
        tmp_path,
        RiskLimits.load(),
        SignalsConfig.load(),
        ResearchConfig.load(),
        llm=RoutingLLM(
            **{
                "submit_research": structured(
                    {**REPORT, "priced_in_analysis": "measured"}
                )
            }
        ),
        fetcher=congressional_feed(
            disclosure_item("row-1", "NUE", "$50,001 - $100,000", "2026-08-17")
        ),
        prices=prices_of(NUE="140.00", SGOV="100.40"),
        broker=FakeBroker(),
        clock=FakeClock(),
    )
    started.loop.tick()
    traded = [
        d
        for d in started.audit.decisions()
        if d.sizing.strategy not in ("mechanical", "cash_sweep")
    ]
    assert traded
    stamp = traded[0].convergence
    assert stamp is not None
    assert stamp.symbol == "NUE"
    assert "congressional_filings" in stamp.families
    # And it survives the file round trip.
    from audit.log import AuditLog

    replayed = AuditLog(path=started.audit.path).decisions()
    judged_replayed = [
        d for d in replayed if d.sizing.strategy not in ("mechanical", "cash_sweep")
    ]
    assert judged_replayed[0].convergence == stamp


def test_the_loop_writes_the_iv_watch_file(tmp_path):
    import json

    from risk_gate import RiskLimits
    from research.config import ResearchConfig
    from signals import SignalsConfig
    from test_exits import enter_position

    started, _, _ = enter_position(
        tmp_path, RiskLimits.load(), SignalsConfig.load(), ResearchConfig.load()
    )
    payload = json.loads((tmp_path / "iv_watch.json").read_text(encoding="utf-8"))
    assert "NUE" in payload["symbols"]  # held first, funnel names behind
    assert len(payload["symbols"]) <= 60


# ================================================================================
# Dispatch ordering, end to end: the bonus moves the queue, never a cap
# ================================================================================


def test_clustered_disclosures_outrank_a_solo_one_for_the_last_slot(
    tmp_path,
):
    """Three same-weight disclosures, budget for one entry pass: the clustered
    pair spends it, the solo name (which arrived FIRST) waits. Ordering only —
    nothing about caps or sizes moves."""
    from risk_gate import RiskLimits
    from research.config import ResearchConfig
    from signals import SignalsConfig
    from test_orchestrator import FakeBroker, FakeClock, build, orchestrator_config, prices_of

    solo = disclosure_item("solo-1", "AAA", "$50,001 - $100,000", "2026-08-17")
    solo.fields["representative"] = "Solo Member"
    solo.fields["credibility_key"] = "congressional_disclosures/Solo Member"
    pair_one = disclosure_item("pair-1", "BBB", "$50,001 - $100,000", "2026-08-17")
    pair_one.fields["representative"] = "Member One"
    pair_one.fields["credibility_key"] = "congressional_disclosures/Member One"
    pair_two = disclosure_item("pair-2", "BBB", "$50,001 - $100,000", "2026-08-17")
    pair_two.fields["representative"] = "Member Two"
    pair_two.fields["credibility_key"] = "congressional_disclosures/Member Two"

    started = build(
        tmp_path,
        RiskLimits.load(),
        SignalsConfig.load(),
        ResearchConfig.load(),
        llm=RoutingLLM(
            **{
                "submit_research": structured(
                    {**REPORT, "priced_in_analysis": "measured", "confidence": 40}
                )
            }
        ),
        fetcher=congressional_feed(solo, pair_one, pair_two),
        prices=prices_of(AAA="100.00", BBB="100.00"),
        broker=FakeBroker(),
        clock=FakeClock(),
        # One pass today: exactly one signal gets researched, order decides which.
        config=orchestrator_config(max_research_passes_per_day=1),
    )
    report = started.loop.tick()

    assert len(report.processed) == 1
    record = report.processed[0].decision or report.processed[0].rejection
    assert record.signal.external_id in {"pair-1", "pair-2"}  # not the solo
    assert report.deferred == 2
