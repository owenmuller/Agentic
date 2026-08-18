"""Environment and credential loading.

CONSTRAINT #4 (CLAUDE.md): ``PAPER_MODE=true`` is the default, and live trading
requires a human to set ``PAPER_MODE=false`` in the environment. Nothing in this
package writes that variable — this module only reads it. There is deliberately no
setter, no ``os.environ[...] =``, and no CLI flag that overrides it, because a flag
the agent can flip is not a human decision.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

#: Values that turn paper mode OFF. Anything else — including an unset variable, an
#: empty string, or a typo — leaves paper mode ON. Constraint #6: an ambiguous value
#: resolves to the safer reading.
_DISABLED = frozenset({"false", "0", "no", "off"})

#: The second key. Disabling PAPER_MODE is not sufficient on its own: a human must
#: also type this phrase, exactly, into LIVE_TRADING_CONFIRMED. One variable can be
#: flipped by a stray line in a shell profile or a copied .env; two, one of which is a
#: sentence stating what you are doing, cannot be done inattentively.
LIVE_CONFIRMATION_VARIABLE = "LIVE_TRADING_CONFIRMED"
LIVE_CONFIRMATION_PHRASE = "I CONFIRM LIVE TRADING WITH REAL MONEY"


class LiveModeMisconfigured(RuntimeError):
    """PAPER_MODE is off but the live confirmation is absent or wrong.

    Deliberately fatal. Falling back to paper would be friendlier and worse: someone
    who believes they are trading live and is not will misread every result that
    follows. Refusing to start is the only outcome that cannot be misinterpreted.
    """


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_environment(dotenv_path: Optional[Path] = None) -> None:
    """Load ``.env`` into the process environment if present.

    ``override=False``: a variable already set in the real environment wins over the
    file. A human who exported ``PAPER_MODE`` in their shell should not have it
    silently replaced by a stale checkout of ``.env``.
    """
    path = dotenv_path or (repo_root() / ".env")
    if path.exists():
        load_dotenv(path, override=False)


def paper_mode() -> bool:
    """True unless a human has explicitly disabled it in the environment."""
    raw = os.environ.get("PAPER_MODE")
    if raw is None:
        return True
    return raw.strip().lower() not in _DISABLED


def live_trading_confirmed() -> bool:
    """True only if the confirmation variable holds the exact phrase.

    Exact: no case folding, no partial match. Surrounding whitespace is stripped
    because that is a copy-paste artefact, not a different intent.
    """
    raw = os.environ.get(LIVE_CONFIRMATION_VARIABLE)
    if raw is None:
        return False
    return raw.strip() == LIVE_CONFIRMATION_PHRASE


def require_paper_or_confirmed_live() -> bool:
    """Resolve the trading mode, or refuse to run. Returns True for paper.

    Called at adapter construction. Never falls back to paper: if PAPER_MODE is off
    and the confirmation is missing or wrong, this raises and the process stops.
    """
    if paper_mode():
        return True
    if not live_trading_confirmed():
        present = os.environ.get(LIVE_CONFIRMATION_VARIABLE)
        detail = (
            "it is not set"
            if present is None
            else f"it is set to {present.strip()!r}, which does not match"
        )
        raise LiveModeMisconfigured(
            f"PAPER_MODE is false, so live trading was requested, but "
            f"{LIVE_CONFIRMATION_VARIABLE} does not hold the required confirmation "
            f"phrase — {detail}.\n"
            f"To trade live, a human must set both:\n"
            f'  PAPER_MODE=false\n'
            f'  {LIVE_CONFIRMATION_VARIABLE}="{LIVE_CONFIRMATION_PHRASE}"\n'
            f"Refusing to start. This process will not silently fall back to paper."
        )
    return False


def require_env(name: str) -> str:
    """Fetch a required credential, failing loudly rather than defaulting."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise KeyError(
            f"{name} is not set. Copy .env.example to .env and fill it in; .env is "
            f"gitignored and must never be committed."
        )
    return value.strip()
