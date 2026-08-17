"""Confidence-weighted sizing engine.

Implements the deterministic confidence -> size table in ``config/risk_limits.yaml``.
Sizing is applied after risk-gate caps and never exceeds them: confidence 100 is
still 5% of sleeve NAV. Option sizes use the same table against premium at risk,
then halved.

Build step 5. Not yet implemented.
"""
