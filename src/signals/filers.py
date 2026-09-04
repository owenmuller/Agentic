"""Congressional filer-name normalization (human ruling 2026-09-04).

The disclosure feed spells one person several ways — "John J Mr Mcguire Iii"
and "John Mcguire" arrived as two filers, two credibility keys, and a split
track record. A filer is a person, not a string, so names are canonicalised at
ingest and every reader of a congressional credibility key canonicalises on the
way in (the audit log is append-only; old records are re-keyed on read, never
rewritten).

The rule, deterministic and offline: drop honorifics (Mr, Mrs, Ms, Dr, Hon,
Sen, Rep …) and generational suffixes (Jr, Sr, II, III, IV) wherever they sit,
drop single-letter initials, title-case what remains, then apply the alias
table for the variants the rule cannot see (a nickname, a reordered surname).
Under-merging is the safe failure: two keys for one person split a record;
one key for two people corrupts two.
"""

from __future__ import annotations

import re
from typing import Mapping, Optional

_HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "miss", "dr", "hon", "sen", "senator", "rep",
     "representative", "congressman", "congresswoman", "gov", "prof"}
)
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "esq", "md", "phd"})

#: Variants the rule cannot see, keyed by the RULE-NORMALISED form. Small and
#: human-edited; config/signals.yaml may add to it per source.
DEFAULT_FILER_ALIASES: dict[str, str] = {
    # Quiver renders Addison Mitchell McConnell Jr. as "A. Mitchell Jr. McConnell".
    "Mitchell Mcconnell": "Mitch Mcconnell",
}

CONGRESSIONAL_PREFIX = "congressional_disclosures/"


def _clean(token: str) -> str:
    return token.strip(" ,.;()").lower()


def normalize_filer(name: str, aliases: Optional[Mapping[str, str]] = None) -> str:
    """Canonical form of a filer's name. Empty in, empty out."""
    tokens = []
    for raw in name.replace(",", " ").split():
        token = _clean(raw)
        if not token or token in _HONORIFICS or token in _SUFFIXES:
            continue
        if len(token) == 1 and token.isalpha():  # an initial, with or without its period
            continue
        tokens.append(token.capitalize())
    canonical = " ".join(tokens)
    table = dict(DEFAULT_FILER_ALIASES)
    if aliases:
        table.update({normalize_filer(k): v for k, v in aliases.items()})
    return table.get(canonical, canonical)


def canonical_credibility_key(key: Optional[str], aliases: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Re-key a congressional credibility key on read; other keys pass through."""
    if not key or not key.startswith(CONGRESSIONAL_PREFIX):
        return key
    name = normalize_filer(key[len(CONGRESSIONAL_PREFIX):], aliases)
    return CONGRESSIONAL_PREFIX + name if name else key


_MULTI_SPACE = re.compile(r"\s+")


def same_filer(a: str, b: str, aliases: Optional[Mapping[str, str]] = None) -> bool:
    """Whether two rendered names are one person under the rule."""
    return bool(a and b) and normalize_filer(a, aliases) == normalize_filer(b, aliases)
