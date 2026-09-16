"""Live round trip for the ADD DECISION research pass (human ruling 2026-09-16).

Runs ONE add decision through the PRODUCTION ResearchPass — the exact request
shape production sends (screen -> verification, source tiers, the revised
SYSTEM_PROMPT and tool schema with add_verdict/add_fraction) — with the held
position stated exactly as the pipeline states it. The position is CELH as it
stands after the 2026-09-16 merge: the two real lots read from the audit log.

Default fixture: a SYNTHETIC congressional purchase disclosure of CELH (an
independent filing family converging). ``--same-family`` instead replays the
real second Form 4 cluster signal (1eb9c63f8e6141c4) against the single-lot
position it originally opened a second position on.

Prints the verdict and what the deterministic sizing layer WOULD do with it
(combined cap band, family bump, headroom). Writes NO audit records, places no
order; spends real API dollars (one full pass, about $0.35-0.60).

    cd /home/agentic/Agentic && .venv/bin/python ops/experiments/add_decision_round_trip.py [--same-family]
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from audit.log import AuditLog, default_data_dir  # noqa: E402
from execution.environment import load_environment  # noqa: E402
from orchestrator.golden import build_source_tiers  # noqa: E402
from research.add_decision import HeldPositionContext, LotContext  # noqa: E402
from research.client import AnthropicResearchClient  # noqa: E402
from research.config import ResearchConfig  # noqa: E402
from research.prompts import SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from research.reports import ResearchReport  # noqa: E402
from research.research_pass import ResearchPass  # noqa: E402
from risk_gate import RiskLimits  # noqa: E402
from signals import SignalsConfig  # noqa: E402
from signals.records import Priority, Signal, SignalClass  # noqa: E402

ORIGIN = "8ba9299d416f4ce0"
SECOND = "1eb9c63f8e6141c4"
CENTS = Decimal("0.01")

#: SYNTHETIC. Written for this round trip; not a real filing by anyone.
SYNTHETIC_DISCLOSURE = (
    "Congressional trading disclosure (STOCK Act filing)\n"
    "representative: Synthetic Member (Representatives) — SYNTHETIC ROUND-TRIP FIXTURE, not a real filing\n"
    "ticker: CELH\n"
    "transaction: Purchase\n"
    "amount range: $250,001 - $500,000\n"
    "transaction date: 2026-09-12 (when the trade was executed)\n"
    "report date: 2026-09-16 (when it became public)\n"
    "disclosure lag: 4 days between the trade and its disclosure"
)


def load_lots(audit: AuditLog) -> dict:
    trails = {d: audit.trail(d) for d in (ORIGIN, SECOND)}
    lots = []
    for decision_id in (ORIGIN, SECOND):
        trail = trails[decision_id]
        buys = [f for f in trail.fills if f.side == "buy"]
        quantity = sum((f.filled_quantity for f in buys), Decimal("0"))
        cost = sum((f.filled_value for f in buys), Decimal("0"))
        research = trail.decision.research
        lots.append(
            {
                "trail": trail,
                "lot": LotContext(
                    decision_id=decision_id,
                    opened_at=buys[0].recorded_at,
                    quantity=quantity,
                    entry_price=(cost / quantity).quantize(Decimal("0.0001")),
                    entry_cost=cost.quantize(CENTS),
                    source_id=trail.decision.signal.source_id,
                    family="insider_filings",
                    confidence=research.confidence,
                    thesis=research.thesis,
                ),
                "research": research,
                "stop_fraction": trail.decision.sizing.stop_fraction,
                "sleeve_nav": trail.decision.sizing.sleeve_nav,
            }
        )
    return {"lots": lots, "trails": trails}


def build_context(loaded: dict, same_family: bool, now: datetime) -> HeldPositionContext:
    lots = loaded["lots"]
    used = lots[:1] if same_family else lots
    quantity = sum((item["lot"].quantity for item in used), Decimal("0"))
    cost = sum((item["lot"].entry_cost for item in used), Decimal("0"))
    blended = (cost / quantity).quantize(Decimal("0.0001"))
    stop_fraction = used[0]["stop_fraction"] or Decimal("0.15")
    origin = used[0]["research"]
    resolution = max(item["research"].expected_resolution_date for item in used if item["research"].expected_resolution_date)
    opened = used[0]["lot"].opened_at
    leash = (resolution - opened.date()).days
    return HeldPositionContext(
        symbol="CELH",
        position_decision_id=ORIGIN,
        instrument_kind="equity",
        opened_at=opened,
        days_held=(now.date() - opened.date()).days,
        quantity=quantity,
        entry_price=blended,
        entry_cost=cost,
        current_price=None,  # the pass may search for one; stated as unavailable
        market_value=None,
        sleeve_nav=Decimal(str(used[0]["sleeve_nav"])).quantize(CENTS),
        originating_family="insider_filings",
        signal_family="insider_filings" if same_family else "congressional_filings",
        source_id="form4_insiders",
        thesis=origin.thesis,
        invalidation_condition=origin.invalidation_condition,
        time_horizon=origin.time_horizon,
        confidence_at_entry=origin.confidence,
        resolution_date=resolution,
        leash_days=leash,
        stop_price=(blended * (1 - stop_fraction)).quantize(CENTS),
        lots=tuple(item["lot"] for item in used),
    )


def deterministic_sizing(report: ResearchReport, context: HeldPositionContext, limits: RiskLimits) -> dict:
    sizing = limits.sizing
    band = sizing.size_for(report.confidence)
    bumped = False
    if band > 0 and context.independent_family:
        wider = sorted({b.size for b in sizing.bands if b.size > band})
        if wider:
            band, bumped = wider[0], True
    combined = min(band, sizing.hard_cap)
    cap_capital = (context.sleeve_nav * combined).quantize(CENTS, rounding=ROUND_DOWN)
    headroom = cap_capital - context.held_value
    fraction = report.add_fraction or Decimal("0")
    capital = (headroom * fraction).quantize(CENTS, rounding=ROUND_DOWN) if headroom > 0 else Decimal("0")
    return {
        "combined_cap_fraction": str(combined),
        "family_bump": bumped,
        "combined_cap_capital": str(cap_capital),
        "held_value": str(context.held_value),
        "headroom": str(headroom),
        "add_capital_before_atr_and_scalars": str(capital),
    }


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    same_family = "--same-family" in sys.argv[1:]
    load_environment()
    signals_config = SignalsConfig.load()
    config = ResearchConfig.load()
    limits = RiskLimits.load()
    now = datetime.now(timezone.utc)
    loaded = load_lots(AuditLog(default_data_dir()))
    context = build_context(loaded, same_family, now)

    if same_family:
        source_signal = loaded["trails"][SECOND].decision.signal
        signal = Signal(
            signal_id="add-round-trip-same-family-2026-09-16",
            source_id="form4_insiders",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=now,
            content=source_signal.content,
            raw_content=source_signal.raw_content,
            priority=Priority.for_class(SignalClass.CLASS_2_MOMENTUM),
            external_id="add-round-trip-same-family-2026-09-16",
            metadata={"tickers": "CELH", "form": "4", "cluster": "true", "filer": source_signal.filer or ""},
        )
    else:
        signal = Signal(
            signal_id="add-round-trip-cross-family-2026-09-16",
            source_id="congressional_disclosures",
            signal_class=SignalClass.CLASS_2_MOMENTUM,
            observed_at=now,
            content=SYNTHETIC_DISCLOSURE,
            raw_content=SYNTHETIC_DISCLOSURE,
            priority=Priority.for_class(SignalClass.CLASS_2_MOMENTUM),
            external_id="add-round-trip-cross-family-2026-09-16",
            metadata={
                "ticker": "CELH", "tickers": "CELH", "transaction": "Purchase",
                "amount_range": "$250,001 - $500,000", "report_date": "2026-09-16",
                "transaction_date": "2026-09-12", "representative": "Synthetic Member",
                "disclosure_lag_days": "4", "priced_in_analysis_required": "true",
                "credibility_key": "congressional_disclosures/Synthetic Member",
            },
        )
    prompt = build_user_prompt(signal, add_context=context)
    assert "ADD DECISION" in prompt and "POSITION HELD" in prompt
    assert "ADD DECISIONS" in SYSTEM_PROMPT
    print(f"context: {len(context.lots)} lot(s), {context.quantity} CELH at blended {context.entry_price}, "
          f"cost {context.entry_cost}, family {context.originating_family} <- signal family {context.signal_family}")

    client = AnthropicResearchClient(config)
    research = ResearchPass(client, source_tiers=build_source_tiers(signals_config))
    print(f"running the production pass (model {config.model}, screen "
          f"{config.screen.model if config.screen else 'off'}) — real API spend")
    outcome = research.run(signal, add_context=context)
    usage = research.last_usage
    cost = usage.cost_usd if usage and usage.cost_usd is not None else None
    if isinstance(outcome, ResearchReport):
        summary = {
            "add_verdict": str(outcome.add_verdict) if outcome.add_verdict else None,
            "add_fraction": str(outcome.add_fraction) if outcome.add_fraction is not None else None,
            "direction": str(outcome.direction),
            "confidence": outcome.confidence,
            "tickers": list(outcome.tickers),
            "time_horizon": str(outcome.time_horizon),
            "expected_resolution_date": (
                outcome.expected_resolution_date.isoformat() if outcome.expected_resolution_date else None
            ),
            "target_price": str(outcome.target_price) if outcome.target_price else None,
            "is_add": outcome.is_add,
            "thesis": outcome.thesis[:600],
            "priced_in_analysis": (outcome.priced_in_analysis or "")[:300],
            "est_cost_usd": str(cost) if cost is not None else None,
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if outcome.is_add:
            print("deterministic sizing would do:")
            print(json.dumps(deterministic_sizing(outcome, context, limits), indent=2))
        print("ROUND TRIP OK: production request shape accepted; add decision",
              "ADD" if outcome.is_add else "HOLD (no add)")
        return 0
    print(json.dumps({
        "rejection": str(getattr(outcome, "code", "?")),
        "message": str(getattr(outcome, "message", outcome))[:500],
        "excerpt": (getattr(outcome, "raw_excerpt", "") or "")[:500],
        "est_cost_usd": str(cost) if cost is not None else None,
    }, indent=2))
    code = str(getattr(outcome, "code", ""))
    print("ROUND TRIP", "OK (typed verdict)" if "error" not in code else "FAILED")
    return 0 if "error" not in code else 1


if __name__ == "__main__":
    sys.exit(main())
