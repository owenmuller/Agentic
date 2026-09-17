"""Live round trip for the Class 1 staleness ruling (2026-09-16).

Runs the golden fixture `trump-energy-relay-read-10h-later` — a REAL
@TrumpTruthOnX policy relay observed ten hours after it was posted — through
the PRODUCTION ResearchPass with the frozen observation moment, exactly as the
golden replay does. Prints the prompt's age line, the verdict, and the
priced_in_analysis the ruling makes mandatory. Writes no audit records, places
no order; spends real API dollars (one pass).

    cd /home/agentic/Agentic && .venv/bin/python ops/experiments/class1_staleness_round_trip.py
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

CASE = "trump-energy-relay-read-10h-later"


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    load_environment()
    case = [c for c in load_cases() if c.name == CASE][0]
    signal = case.signal(datetime.now(timezone.utc))
    assert class1_is_stale(signal), "fixture must be stale"
    prompt = build_user_prompt(signal)
    age_line = [line for line in prompt.splitlines() if line.startswith("- posted at:")][0]
    print(age_line)
    assert "STALE" in age_line and "priced_in_analysis is MANDATORY" in prompt
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
            "priced_in_analysis": outcome.priced_in_analysis,
            "thesis": outcome.thesis[:500], "est_cost_usd": str(cost) if cost is not None else None,
            "golden_grade": "PASS" if result.passed else f"DRIFT {list(result.problems)}",
        }, indent=2, ensure_ascii=False))
        print("ROUND TRIP OK: production request shape accepted")
        return 0
    print(json.dumps({"rejection": str(getattr(outcome, "code", "?")),
                      "message": str(getattr(outcome, "message", outcome))[:500],
                      "est_cost_usd": str(cost) if cost is not None else None}, indent=2))
    code = str(getattr(outcome, "code", ""))
    print("ROUND TRIP", "OK (typed rejection)" if "error" not in code else "FAILED")
    return 0 if "error" not in code else 1


if __name__ == "__main__":
    sys.exit(main())
