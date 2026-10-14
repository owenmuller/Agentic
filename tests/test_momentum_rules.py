"""The momentum book's frozen v1 rules (human ruling 2026-10-13)."""
from __future__ import annotations

import math
import random
from dataclasses import FrozenInstanceError, replace

import pytest

from momentum import rules
from momentum.rules import V1, Candidate


def test_v1_is_frozen_and_the_shadows_are_declared():
    with pytest.raises(FrozenInstanceError):
        V1.holdings = 20  # type: ignore[misc]
    assert (V1.universe, V1.holdings, V1.sell_rank, V1.sector_cap, V1.position_cap) == (1000, 15, 30, 0.30, 0.10)
    assert V1.weighting == "equal" and V1.market_filter and V1.vol_scaling and V1.ai_veto
    assert set(rules.SHADOWS) == {"v1_1_no_veto", "v1_top_1000", "inverse_vol", "market_filter_only",
                                  "vol_scaling_only", "plain_12_1"}
    assert not any(s.ai_veto for s in rules.SHADOWS.values())
    # v1.1 (human ruling 2026-10-14): v1 on the top 500, the live spec; v1 itself is a shadow
    assert rules.LIVE is rules.V1_1 and rules.V1_1.universe == 500 and rules.V1_1.ai_veto
    assert replace(rules.V1_1, universe=1000, name="v1") == V1
    assert rules.SHADOWS["v1_top_1000"].universe == 1000 and rules.SHADOWS["v1_1_no_veto"].universe == 500
    assert rules.SHADOWS["market_filter_only"].market_filter and not rules.SHADOWS["market_filter_only"].vol_scaling


@pytest.mark.parametrize("sic, group", [(3571, 6), (7372, 6), (2834, 10), (6021, 11), (1311, 4), (4911, 8),
                                        (5331, 9), (3711, 2), (2080, 1), (4813, 7), (2821, 5), (3550, 3),
                                        (None, 12), (8711, 12)])
def test_fama_french_12_sectors(sic, group):
    assert rules.ff12(sic) == group


def test_residuals_recover_a_known_regression():
    rng = random.Random(1)
    x1 = [rng.gauss(0, 0.04) for _ in range(36)]
    x2 = [rng.gauss(0, 0.05) for _ in range(36)]
    noise = [rng.gauss(0, 0.01) for _ in range(36)]
    y = [0.002 + 1.2 * a + 0.5 * b + e for a, b, e in zip(x1, x2, noise)]
    resid = rules.ols_residuals(y, x1, x2)
    assert abs(sum(resid)) < 1e-9 and rules.stdev(resid) < 0.012


def test_residual_momentum_rewards_idiosyncratic_strength():
    rng = random.Random(2)
    market = [rng.gauss(0.01, 0.04) for _ in range(36)]
    industry = [m + rng.gauss(0, 0.02) for m in market]
    strong = [1.0 * m + 0.02 + rng.gauss(0, 0.01) if i >= 24 else 1.0 * m + rng.gauss(0, 0.01)
              for i, m in enumerate(market)]
    weak = [1.0 * m - 0.02 + rng.gauss(0, 0.01) if i >= 24 else 1.0 * m + rng.gauss(0, 0.01)
            for i, m in enumerate(market)]
    assert rules.residual_momentum(strong, market, industry) > 0 > rules.residual_momentum(weak, market, industry)
    assert rules.residual_momentum(strong[:11], market[:11], industry[:11]) is None  # under 12 months


def test_plain_momentum_skips_the_last_month():
    closes = [100.0] * 12 + [150.0]
    assert rules.plain_momentum(closes) == 0.0  # the jump is in month t-1, which is skipped
    assert rules.plain_momentum([100.0] + [110.0] * 12) == pytest.approx(0.10)


def test_earnings_momentum_sums_complete_windows_inside_the_year():
    stock = [0.0] * 300
    market = [0.0] * 300
    stock[99], stock[100], stock[101] = 0.01, 0.05, 0.01   # event at 100: +7%
    stock[200], market[200] = -0.03, 0.01                  # event at 200: -4%
    assert rules.earnings_momentum([100, 200], stock, market, today=260) == pytest.approx(0.03)
    assert rules.earnings_momentum([259], stock, market, today=260) is None  # window not complete
    assert rules.earnings_momentum([5], stock, market, today=300) is None    # older than 12 months


def test_exclusions_gate_buys():
    closes = [10.0 + i * 0.01 for i in range(120)]
    rets = [0.001] * 120
    assert rules.excluded(rets, closes, False) is None
    assert rules.excluded(rets, closes, True) == "pending acquisition target"
    gap = rets[:-10] + [0.16] + rets[-9:]
    assert "single-day move" in rules.excluded(gap, closes, False)
    falling = [20.0 - i * 0.05 for i in range(120)]
    assert rules.excluded(rets, falling, False) == "below its 100-day average"


def _cands(n, sector=lambda i: 1 + i % 12, exclusion=lambda i: None):
    return [Candidate(f"S{i:03d}", score=float(n - i), sector=sector(i), vol=0.3, exclusion=exclusion(i))
            for i in range(n)]


def test_buffer_buys_the_top_15_and_holds_to_rank_30():
    cands = _cands(100)
    first = rules.select(cands, [], V1, buys_allowed=True)
    assert first.holdings == tuple(f"S{i:03d}" for i in range(15))
    held = ["S020", "S029", "S031"]  # ranks 21, 30, 32
    second = rules.select(cands, held, V1, buys_allowed=True)
    assert "S020" in second.holdings and "S029" in second.holdings and "S031" not in second.holdings
    assert ("S031", "fell to rank 32, past 30") in second.sold
    assert len(second.holdings) == 15


def test_an_excluded_or_vetoed_name_hands_its_slot_to_the_next_rank():
    cands = _cands(100, exclusion=lambda i: "below its 100-day average" if i == 3 else None)
    sel = rules.select(cands, [], V1, buys_allowed=True, vetoed={"S005": "pending acquisition"})
    assert "S003" not in sel.holdings and "S005" not in sel.holdings
    assert {"S015", "S016"} <= set(sel.holdings) and len(sel.holdings) == 15
    assert ("S005", "vetoed: pending acquisition") in sel.skipped


def test_the_sector_cap_holds_four_names_per_sector():
    cands = _cands(60, sector=lambda i: 6 if i < 10 else 1 + i % 5)
    sel = rules.select(cands, [], V1, buys_allowed=True)
    assert sum(1 for k in sel.holdings if int(k[1:]) < 10) == 4  # floor(0.30 x 15) = 4


def test_the_market_filter_stops_buys_but_holdings_run():
    cands = _cands(100)
    sel = rules.select(cands, ["S002", "S050"], V1, buys_allowed=False)
    assert sel.holdings == ("S002",) and sel.bought == ()
    assert rules.market_filter_blocks([100.0] * 199 + [90.0]) and not rules.market_filter_blocks([100.0] * 200)


def test_weights_slots_cap_and_inverse_vol():
    eq = rules.weights([f"S{i}" for i in range(10)], {}, V1)
    assert all(w == pytest.approx(1 / 15) for w in eq.values()) and sum(eq.values()) == pytest.approx(10 / 15)
    inv = rules.weights(["A", "B", "C"], {"A": 0.1, "B": 0.4, "C": 0.4}, replace(V1, weighting="inverse_vol"))
    assert max(inv.values()) <= 0.10 + 1e-12 and sum(inv.values()) == pytest.approx(3 / 15)


def test_vol_scaling_never_levers():
    assert rules.exposure(0.20, 0.40) == 0.5 and rules.exposure(0.30, 0.20) == 1.0
    assert rules.exposure(None, 0.2) == 1.0 and rules.exposure(0.2, None) == 1.0


def test_zscores_clip_and_neutralize_missing():
    z = rules.zscores({"a": 1.0, "b": 2.0, "c": 3.0, "d": None, "e": 100.0})
    assert z["d"] == 0.0 and max(z.values()) <= 3.0 and math.isfinite(z["a"])


def test_each_declared_shadow_is_one_rule_away_from_the_live_spec():
    # Human ruling 2026-10-14 (second): the declared variants sit on v1.1's
    # top-500 universe, so each differs from it in exactly one rule; the
    # original v1 (top 1,000) is the universe comparison.
    from dataclasses import fields

    base = replace(rules.LIVE, ai_veto=False)

    def differences(spec):
        return {f.name for f in fields(spec) if f.name != "name" and getattr(spec, f.name) != getattr(base, f.name)}

    assert differences(rules.SHADOWS["v1_1_no_veto"]) == set()
    assert differences(rules.SHADOWS["v1_top_1000"]) == {"universe"}
    assert differences(rules.SHADOWS["inverse_vol"]) == {"weighting"}
    assert differences(rules.SHADOWS["market_filter_only"]) == {"vol_scaling"}
    assert differences(rules.SHADOWS["vol_scaling_only"]) == {"market_filter"}
    assert differences(rules.SHADOWS["plain_12_1"]) == {"score"}
