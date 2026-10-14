"""The MOMENTUM book (human rulings 2026-10-13): one rules package shared by
the sanity backtest, the live sleeve in the main book and every shadow
variant, so a result in one is a result of the same rules in the others.

  rules     the frozen v1 specification and its declared variants, as pure
            functions over prices, events and shares (no I/O, no network)
  engine    the monthly book: rebalance, drift, costs, delistings (pure)
  feeds     the data the rules read: Alpaca daily bars with ticker-reuse
            segmentation, and SEC filings (ticker->CIK, SIC, 8-K 2.02
            timestamps, merger filings, shares outstanding) - read-only,
            rate-limited, cached
"""
import datasafety  # noqa: F401,E402 - production-data write guard, installed on import (ruling 2026-10-09)
