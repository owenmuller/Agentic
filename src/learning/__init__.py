"""The shared learning layer (GLOBAL RULE, human ruling 2026-10-14; CLAUDE.md
§ Learning Loop): every sleeve and lab account learns from its own record
through one structured, evidence-gated loop.

  vocabulary  the FIXED tag vocabulary and the process verdicts; the tags
              code can compute (sector, regime, confidence band, source) are
              computed, never asked of a model
  journal     one structured post-mortem per resolved decision, append-only
  mining      the weekly pattern tally by tag and tag pair (code, no LLM),
              with n, confidence intervals and a multiple-testing correction

Still to come, each behind its own golden replay where a prompt is involved:
the post-mortem writer (the cheapest reliable model), hypotheses as shadows,
the evidence-gated playbook with monthly re-checks, and the weekly card.

This package reads nothing on its own: the orchestrator (the only importer of
the audit log) hands it resolved cases. By the scoreboard rule it never sees
recent P&L, account value or any target - only per-decision outcomes, which
are aggregated before anything is learned from them.
"""
import datasafety  # noqa: F401 - production-data write guard, installed on import (ruling 2026-10-09)
