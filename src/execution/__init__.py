"""Broker adapters — one interface, swappable backends.

Alpaca paper trading first; the paper/live flip is one environment variable, which
only a human may set (CLAUDE.md Constraint #4).

Robinhood is not an execution backend: no official public equities API exists, and
community wrappers are reverse-engineered and violate RH ToS. Kalshi's official API
serves the prediction-market sleeve.

Build step 2. Not yet implemented.
"""
