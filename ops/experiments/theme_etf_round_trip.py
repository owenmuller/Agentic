"""Live round trip for the theme->ETF expression and the revised research prompt
(human ruling 2026-09-15, step 2 of the recalibration bundle).

Runs ONE synthetic no-ticker policy post through the PRODUCTION ResearchPass —
the exact request shape production sends (screen -> verification, source tiers,
the revised SYSTEM_PROMPT with the options doors) — with the theme proposal
stamped exactly as the pipeline stamps it. Prints the verdict and whether the
model took or declined the ETF mapping. Writes NO audit records and places no
order; spends real API dollars (one full pass, about $0.35 at the shipped tiers).

    cd /home/agentic/Agentic && .venv/bin/python ops/experiments/theme_etf_round_trip.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from execution.environment import load_environment  # noqa: E402
from orchestrator.golden import build_source_tiers  # noqa: E402
from research.client import AnthropicResearchClient  # noqa: E402
from research.config import ResearchConfig  # noqa: E402
from research.prompts import SYSTEM_PROMPT, build_user_prompt  # noqa: E402
from research.reports import ResearchReport  # noqa: E402
from research.research_pass import ResearchPass  # noqa: E402
from signals import SignalsConfig  # noqa: E402
from signals.records import Priority, Signal, SignalClass  # noqa: E402
from signals.themes import ThemeEtfMap, shortlist_of  # noqa: E402

#: SYNTHETIC. Written for this round trip; not a real post by anyone.
POST = (
    "Effective Monday, a 25% tariff on ALL imported steel and aluminum. Foreign "
    "dumping has hollowed out American mills for decades and it ends now. Our "
    "workers and our industry are going to WIN again like never before!"
)
#: SYNTHETIC dual-theme fixture (ruling 2026-09-16): tariffs AND china_trade
#: match, so the proposal is the UNION of both shortlists.
POST_CHINA = (
    "China has cheated on trade for decades. Starting Monday a 60% tariff on ALL "
    "Chinese imports, no exceptions, until Beijing comes to the table. The trade "
    "talks are over. America will not be ripped off any longer!"
)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    china = "--china" in sys.argv[1:]
    post = POST_CHINA if china else POST
    expect = ("XLI", "FXI") if china else ("XLI",)
    load_environment()
    signals_config = SignalsConfig.load()
    config = ResearchConfig.load()
    themes = ThemeEtfMap.from_config(signals_config)

    signal = Signal(
        signal_id="theme-etf-round-trip-2026-09-15" + ("-china" if china else ""),
        source_id="trump_posts",
        signal_class=SignalClass.CLASS_1_REALTIME,
        observed_at=datetime.now(timezone.utc),
        content=post,
        raw_content=post,
        priority=Priority.for_class(SignalClass.CLASS_1_REALTIME),
        external_id="theme-etf-round-trip-2026-09-15" + ("-china" if china else ""),
        metadata={"tickers": ""},
    )
    stamped = themes.apply(signal)
    shortlist = shortlist_of(stamped)
    print(f"theme proposal stamped: {stamped.metadata.get('theme')!r} -> {shortlist}")
    if any(etf not in shortlist for etf in expect):
        print(f"FAIL: expected {expect} in the proposal, got {shortlist}", file=sys.stderr)
        return 1
    prompt = build_user_prompt(stamped)
    assert "theme -> ETF proposal" in prompt, "prompt lacks the proposal line"
    assert "Conviction door" in SYSTEM_PROMPT and "hard 10% cap" in SYSTEM_PROMPT

    # Capture the FULL payload and pydantic errors of any schema failure: the
    # pass keeps only a 500-char excerpt, which is not enough to diagnose one.
    import research.research_pass as research_pass_module
    from pydantic import ValidationError

    captured: list = []

    class CapturingReport(ResearchReport):
        @classmethod
        def model_validate(cls, payload, *args, **kwargs):  # type: ignore[override]
            try:
                return ResearchReport.model_validate(payload, *args, **kwargs)
            except ValidationError as error:
                captured.append((payload, error))
                raise

    research_pass_module.ResearchReport = CapturingReport

    client = AnthropicResearchClient(config)
    research = ResearchPass(client, source_tiers=build_source_tiers(signals_config))
    print(
        f"running the production pass (model {config.model}, screen "
        f"{config.screen.model if config.screen else 'off'}) — real API spend"
    )
    outcome = research.run(stamped)
    usage = research.last_usage
    cost = usage.cost_usd if usage and usage.cost_usd is not None else None
    if isinstance(outcome, ResearchReport):
        # "Taken" means expressed THROUGH the ETF: a no_position verdict that
        # names XLI only to explain its decline is a decline.
        took = (
            len(outcome.tickers) == 1
            and outcome.tickers[0].upper() in shortlist
            and str(outcome.direction) != "no_position"
        )
        summary = {
            "direction": str(outcome.direction),
            "confidence": outcome.confidence,
            "tickers": list(outcome.tickers),
            "time_horizon": str(outcome.time_horizon),
            "catalyst_present": outcome.has_catalyst,
            "target_price": str(outcome.target_price) if outcome.target_price else None,
            "took_etf_mapping": took,
            "thesis": outcome.thesis[:400],
            "priced_in_analysis": (outcome.priced_in_analysis or "")[:300],
            "est_cost_usd": str(cost) if cost is not None else None,
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print("ROUND TRIP OK: production request shape accepted; mapping",
              "TAKEN" if took else "DECLINED (allowed by design)")
        return 0
    print(
        json.dumps(
            {
                "rejection": str(getattr(outcome, "code", "?")),
                "message": str(getattr(outcome, "message", outcome))[:500],
                "est_cost_usd": str(cost) if cost is not None else None,
            },
            indent=2,
        )
    )
    code = str(getattr(outcome, "code", ""))
    for payload, error in captured:
        print("--- FULL rejected payload ---")
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str)[:6000])
        print("--- schema errors ---")
        for item in error.errors():
            print(f"  {'.'.join(str(x) for x in item['loc'])}: {item['msg']} (got {str(item.get('input'))[:200]!r})")
    excerpt = getattr(outcome, "raw_excerpt", "") or ""
    if excerpt:
        print(f"--- model payload ({len(excerpt)} chars, truncated by the pass) ---")
        print(excerpt)
        try:
            import ast

            from pydantic import ValidationError

            payload = ast.literal_eval(excerpt)
            try:
                ResearchReport.model_validate(payload)
            except ValidationError as error:
                print("--- schema errors ---")
                for item in error.errors():
                    print(f"  {'.'.join(str(x) for x in item['loc'])}: {item['msg']} (got {item.get('input')!r})")
        except (ValueError, SyntaxError):
            print("(payload truncated; cannot re-validate)")
    # A typed research-layer decline (e.g. triage no) still proves the request
    # path; an upstream error does not.
    print("ROUND TRIP", "OK (typed verdict)" if "error" not in code else "FAILED")
    return 0 if "error" not in code else 1


if __name__ == "__main__":
    sys.exit(main())
