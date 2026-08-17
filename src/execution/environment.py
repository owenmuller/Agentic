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


def require_env(name: str) -> str:
    """Fetch a required credential, failing loudly rather than defaulting."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise KeyError(
            f"{name} is not set. Copy .env.example to .env and fill it in; .env is "
            f"gitignored and must never be committed."
        )
    return value.strip()
