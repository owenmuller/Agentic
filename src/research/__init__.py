"""LLM research and confidence layer.

Produces the structured output defined in CLAUDE.md § Research & Confidence Layer:
thesis, tickers, direction, time_horizon, priced_in_analysis (mandatory for Class 2
and 3), confidence 0-100, invalidation_condition.

Safe to iterate on: the risk gate sits downstream and does not care how confident
the model feels.

Build step 4. Not yet implemented.
"""
