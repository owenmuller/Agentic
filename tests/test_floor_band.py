"""Congressional floor band tagging (human ruling 2026-09-18, final at $15,001).

Claims: the shipped floor is 15,001; the weekly attribution renders judged
congressional decisions by floor band with the 15-50K band marked UNDER REVIEW;
the forward report renders every congressional purchase in the funnel by band
at 5d and 20d, researched or not.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from audit.attribution import CONGRESSIONAL_FLOOR_BANDS, congressional_floor_band
from audit.records import RejectedStage
from forward import funnel_entries, render_forward_report
from signals.records import SignalClass
from test_forward import rejection, snapshot

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def test_the_shipped_floor_is_15001_and_the_bands_are_the_ruled_three():
    from signals import SignalsConfig

    congressional = [
        s
        for k in SignalsConfig.load().classes.values()
        for s in k.sources
        if s.id == "congressional_disclosures"
    ][0]
    assert congressional.prefilter.min_amount_max == 15001
    assert [label for _, label in CONGRESSIONAL_FLOOR_BANDS] == [
        "<=15K (prefiltered since 2026-09-18)", "15-50K (UNDER REVIEW)", ">50K",
    ]
    assert congressional_floor_band("$1,001 - $15,000") == "<=15K (prefiltered since 2026-09-18)"
    assert congressional_floor_band("$15,001 - $50,000") == "15-50K (UNDER REVIEW)"
    assert congressional_floor_band("$50,001 - $100,000") == ">50K"
    assert congressional_floor_band("undisclosed") is None


def _congressional(amount, transaction="Purchase", ticker="NUE"):
    return snapshot(
        source="congressional_disclosures",
        signal_class=SignalClass.CLASS_2_MOMENTUM,
        content=(
            f"ticker: {ticker}\ntransaction: {transaction}\namount range: {amount}\n"
            "disclosure lag: 10 days between the trade and its disclosure"
        ),
        credibility_key="congressional_disclosures/Test Member",
        filer="Test Member",
    )


def test_the_forward_report_renders_every_purchase_by_floor_band():
    from types import SimpleNamespace

    entries = funnel_entries(
        [
            rejection("d-1", RejectedStage.PRE_FILTER, "pre_filter", snap=_congressional("$1,001 - $15,000", ticker="AAA")),
            rejection("d-2", RejectedStage.PRE_FILTER, "source_cap", snap=_congressional("$15,001 - $50,000", ticker="BBB")),
            rejection("d-3", RejectedStage.SIZING, "no_position", snap=_congressional("$250,001 - $500,000", ticker="CCC")),
            rejection("d-4", RejectedStage.PRE_FILTER, "pre_filter", snap=_congressional("$15,001 - $50,000", "Sale (Full)", ticker="DDD")),
        ]
    )
    day = entries[0].observed_at.date()

    def row(excess):
        return SimpleNamespace(
            has_base=True,
            marks={5: SimpleNamespace(excess_pct=Decimal(excess)), 20: SimpleNamespace(excess_pct=Decimal(excess))},
        )

    rows = {("AAA", day): row("-1.0"), ("BBB", day): row("0.5"), ("CCC", day): row("2.0")}
    report = render_forward_report(entries, rows)
    assert "Congressional floor band (ruling 2026-09-18" in report
    assert "<=15K, 5d" in report and "15-50K, 5d" in report and ">50K, 20d" in report
    # The 2026-10-15 verdict package: ticker-weighted beside row means, with
    # distinct-ticker and observation-day counts, and the top contributors.
    band_line = next(l for l in report.splitlines() if l.strip().startswith("<=15K, 5d"))
    assert "rows mean -1.00%" in band_line
    assert "ticker-weighted mean -1.00%" in band_line
    assert "1 tickers, 1 observation day" in band_line
    assert "top-2 tickers 100% of rows: AAA x1 (-1.0%)" in band_line
    assert ">50K, 60d: no resolved marks yet" in report
    assert "all purchases, 20d" in report and "live flow only" in report
    # The sale is not a purchase and is not in the slice.
    section = report[report.index("Congressional floor band"):]
    section = section[: section.index("Form 4") if "Form 4" in section else len(section)]
    assert "DDD" not in section
