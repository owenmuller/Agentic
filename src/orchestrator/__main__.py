"""``python -m orchestrator`` — the startup checks, run and reported.

This runs steps 1-4 of ``orchestrator.bootstrap``: the Constraint #4 mode check, the
config load, the broker connectivity check, and the replay of state from the audit log
and session file. It then prints what it reconstructed and exits.

It deliberately does not start the loop. Two seams have no production implementation
yet — the signal ``Fetcher`` (the feeds need credentials this machine does not hold)
and the ``PriceSource`` (there is no market-data client) — so a runnable entry point
here would be one that polls nothing and prices nothing while looking like it worked.
Wiring those in is the next build step; until then this is the honest command: it
verifies that everything which *can* be checked is right, and says so.

Exits non-zero if any check fails, so it is usable as a preflight in a startup script.
"""

from __future__ import annotations

import logging
import sys

from orchestrator.bootstrap import preflight


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        checks = preflight()
    except Exception as error:  # noqa: BLE001 - the point is the operator sees why
        print(f"STARTUP CHECK FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(checks.describe())
    print()
    print(
        "Checks passed. The loop is not started: no production signal fetcher or "
        "price source is wired yet, so orchestrator.start() must be called with both."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
