"""Live round trip for the government contract awards source (ruling 2026-10-06).

Runs the golden fixture `award-amtm-sole-source-new-1.8pct` — a REAL DoD digest
award (Amentum, $79M sole-source task order, 1.77% of market cap) read through
the production fetcher — through the PRODUCTION ResearchPass with the current
clock, exactly as the morning session would. Prints the prompt's guidance and
age lines, the verdict and the mandatory priced_in_analysis. Writes no audit
record, places no order; spends real API dollars (one pass).

    cd /home/agentic/Agentic && .venv/bin/python ops/experiments/contract_award_round_trip.py [case-name]
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from execution.environment import load_environment  # noqa: E402
from orchestrator.golden import build_source_tiers, grade, load_cases  # noqa: E402
from research.client import AnthropicResearchClient  # noqa: E402
from research.config import ResearchConfig  # noqa: E402
from research.prompts import build_user_prompt, class1_is_stale  # noqa: E402
from research.reports import ResearchReport  # noqa: E402
from research.research_pass import ResearchPass  # noqa: E402
from signals import SignalsConfig  # noqa: E402

CASE = sys.argv[1] if len(sys.argv) > 1 else "award-amtm-sole-source-new-1.8pct"


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    load_environment()
    case = [c for c in load_cases() if c.name == CASE][0]
    signal = case.signal(datetime.now(timezone.utc))
    prompt = build_user_prompt(signal)
    assert "GOVERNMENT CONTRACT AWARD" in prompt, "the award guidance branch must render"
    for line in prompt.splitlines():
        if line.startswith("- posted at:") or "award / market cap" in line or line.startswith("size tier"):
            print(line)
    print("stale:", class1_is_stale(signal), "| priced_in MANDATORY in prompt:", "priced_in_analysis is MANDATORY" in prompt)
    config = ResearchConfig.load()
    research = ResearchPass(
        AnthropicResearchClient(config), source_tiers=build_source_tiers(SignalsConfig.load())
    )
    print(f"running the production pass (model {config.model}, screen "
          f"{config.screen.model if config.screen else 'off'}) — real API spend")
    outcome = research.run(signal)
    usage = research.last_usage
    cost = usage.cost_usd if usage and usage.cost_usd is not None else None
    if isinstance(outcome, ResearchReport):
        result = grade(case, outcome, usage)
        print(json.dumps({
            "direction": str(outcome.direction), "confidence": outcome.confidence,
            "tickers": list(outcome.tickers), "time_horizon": str(outcome.time_horizon),
            "expected_resolution_date": str(outcome.expected_resolution_date),
            "target_price": str(outcome.target_price),
            "priced_in_analysis": outcome.priced_in_analysis,
            "thesis": outcome.thesis[:700], "est_cost_usd": str(cost) if cost is not None else None,
            "golden_grade": result.status if hasattr(result, "status") else str(result),
        }, indent=1))
        return 0
    print("REJECTED:", outcome)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
