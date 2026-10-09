"""The paper lab: separate Alpaca paper accounts, one per experimental sleeve
(PAPER PUSH, human ruling 2026-10-09 late evening).

"Risk costs nothing in paper, so run aggressive strategies live in parallel and
let results sort them. The main paper book and its rules are untouched."

What is shared with the main book, and what is not
--------------------------------------------------
* SHARED: the deterministic ``RiskGate`` (Constraint #3 - every lab order passes
  it; there is no other path to a broker), the order schema (so Constraints #1
  and #2 hold here exactly as in the main book), the broker adapter, the
  paper-mode check (Constraint #4; a lab process refuses to start unless it
  resolves to paper, and it never sets or suggests either live variable).
* SEPARATE, per account: the keys (``ALPACA_LAB_<NAME>_API_KEY``/``_SECRET``),
  the pinned broker account number, the ledger and state (``data-lab/<name>``,
  writable only by the account's own system unit - datasafety owner markers),
  the cap table (``risk_limits.yaml`` + the account's overrides in
  ``config/lab.yaml``), the high-water mark and kill switch, the halt marker.
* NOT TOUCHED: the orchestrator imports nothing from here, and nothing here
  reads or writes the main book's data.
"""
import datasafety  # noqa: F401,E402 - production-data write guard, installed on import (ruling 2026-10-09)
