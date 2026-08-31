"""Earnings shadow logger — observation only, no trading path.

Human ruling 2026-08-31: build the logger, not the strategy. Pre-earnings long
premium is a well-documented retail graveyard, so the claim under test —
*does the realised post-earnings move systematically exceed the options market's
implied move, on the names a screen would select?* — gets answered with recorded
data over two earnings seasons before any capital is committed. If it does not,
the strategy dies having cost nothing.

What this package deliberately cannot do
----------------------------------------
There is no order type, no risk gate, no broker adapter and no sizing engine
anywhere in it, and the import topology forbids reaching them: ``earnings`` may
import ``execution.options_data``, ``execution.market_data`` and
``execution.environment`` — market data and the .env loader — and nothing else
first-party. "Places nothing" is therefore structural rather than a promise, the
same way the mechanical sleeve's "no LLM in its path" is.

It also spends no LLM budget. Every number here is arithmetic over quotes and
bars.

The three things it records
---------------------------
1. **armed** — a few sessions before a print: spot, the first expiry after the
   print, the ATM strike, both legs' mids, and the implied move the straddle is
   pricing. This is the market's estimate, captured before the event.
2. **iv** — a daily at-the-money implied-volatility snapshot for every tracked
   name. This is the series the system does not have and cannot buy cheaply:
   ``options_selection.max_iv_percentile`` ranks a contract inside its own chain,
   which by construction cannot see a whole surface lifted together. An IV rank
   against history needs stored history, and the only way to get it is to start
   storing it. (The realised-move series below is a realised-volatility
   reference, which is a different and weaker thing — worth being precise about.)
3. **resolved** — the session after the print: where the underlying actually
   went, and what those exact two contracts are now worth. The straddle P&L is
   therefore a real mark, not a model of one.

Degradation
-----------
Every input can be absent — no calendar key, no chain, no quote, a name with no
options — and absence is recorded as absence. Nothing here estimates a missing
number, because the entire point of the exercise is to find out what was true.
"""

from earnings.calendar import (
    EarningsCalendar,
    EarningsCalendarError,
    EarningsEvent,
    FinnhubEarningsCalendar,
)
from earnings.config import EarningsConfig
from earnings.implied import ImpliedMove, atm_straddle
from earnings.realised import realised_move_pct
from earnings.shadow import ShadowLog, ShadowObserver

__all__ = [
    "EarningsCalendar",
    "EarningsCalendarError",
    "EarningsConfig",
    "EarningsEvent",
    "FinnhubEarningsCalendar",
    "ImpliedMove",
    "ShadowLog",
    "ShadowObserver",
    "atm_straddle",
    "realised_move_pct",
]
