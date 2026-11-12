"""The quote rule (human rulings 2026-11-04) moved to ``execution.quotes`` on
2026-11-12 so the lab's AI trader can read prices by the same rule (human
ruling 2026-11-12, item 1; the lab may not import the orchestrator). The main
book's engines keep importing it from here."""
from __future__ import annotations

from execution.quotes import MAX_SPREAD, QuoteReader, Reading

__all__ = ["MAX_SPREAD", "QuoteReader", "Reading"]
