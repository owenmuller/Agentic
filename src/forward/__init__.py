"""Universal forward-return tracking (human ruling 2026-09-01).

Every signal that entered the funnel — pre-filtered, triaged out, declined,
gate-rejected, or traded — gets forward returns at 1/5/20/60/120 calendar days
from observation, computed lazily from daily bars at report time and cached
append-only. The point is counterfactual accountability: the funnel's kills and
the research layer's declines finally have a scoreboard.

Nothing here feeds forward into a decision. No threshold reads these numbers, no
weight consumes them, no LLM sees them. They exist so a HUMAN can tune the
prefilter, the lag rules, and the sizing table as dated rulings — data argues,
humans rule.
"""

from forward.funnel import FunnelEntry, funnel_entries
from forward.returns import HORIZONS, ForwardReturns, ForwardRow, HorizonMark
from forward.report import render_forward_report, wanted_pairs

__all__ = [
    "FunnelEntry",
    "ForwardReturns",
    "ForwardRow",
    "HORIZONS",
    "HorizonMark",
    "funnel_entries",
    "render_forward_report",
    "wanted_pairs",
]
