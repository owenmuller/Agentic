"""The Quiver strategy shadows' files and daily step (human ruling 2026-10-29,
item 2). Run by the momentum prep unit (it already runs mornings and
evenings on weekdays); the books' rules are ``orchestrator.quiver_books``.

``data/quiver/``:
  books.json     {name: {"started": day, "schedule": [[day, {symbol: weight}], ...]}}
                 a rebalance is decided on the MORNING of its day from data
                 public before that day, and takes effect at that day's close
                 (``momentum_report.book_path``)
  lobbying.json  the point-in-time lobbying store {symbol: [rows]}: seeded once
                 (from the research pull, else per ticker), then the live feed
                 is merged in every run; rows older than two years are dropped

Software only: no order, no gate, no prompt.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional

from orchestrator import quiver_books as qb

BOOKS = ("congress_buys", "lobbying_growth")
KEEP_DAYS = 730


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def due(books: dict, day: str, sessions: list[str]) -> list[str]:
    """The books that rebalance on ``day`` (a session): Congress Buys on the
    first session of each ISO week, Lobbying Spending Growth on the first of
    each month; a book with no schedule starts on ``day`` (the next session,
    standing rule 2026-10-14); a day already scheduled is never redone."""
    earlier = [s for s in sessions if s < day]
    prior = earlier[-1] if earlier else None
    out = []
    for name in BOOKS:
        schedule = (books.get(name) or {}).get("schedule") or []
        if schedule and schedule[-1][0] >= day:
            continue
        if not schedule:
            out.append(name)
        elif name == "congress_buys" and (prior is None or
                                          date.fromisoformat(prior).isocalendar()[:2]
                                          != date.fromisoformat(day).isocalendar()[:2]):
            out.append(name)
        elif name == "lobbying_growth" and (prior is None or prior[:7] != day[:7]):
            out.append(name)
    return out


def merge_lobbying(store: dict[str, list[dict]], rows: Iterable[dict], today: str) -> int:
    """Add live rows to the store (exact duplicates dropped), forget rows
    older than ``KEEP_DAYS``. Returns how many rows were new."""
    floor = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS)).isoformat()
    added = 0
    for row in rows:
        symbol = str(row.get("Ticker") or "").strip().upper()
        if not symbol or str(row.get("Date"))[:10] < floor:
            continue
        kept = {k: row.get(k) for k in ("Date", "Amount", "Registrant")}
        bucket = store.setdefault(symbol, [])
        before = len(bucket)
        store[symbol] = qb.dedupe(bucket + [kept])
        added += len(store[symbol]) - before
    for symbol in list(store):
        store[symbol] = [r for r in store[symbol] if str(r.get("Date"))[:10] >= floor]
        if not store[symbol]:
            del store[symbol]
    return added


def seed_from_research(path: Path, today: str) -> Optional[dict[str, list[dict]]]:
    """The research pull (``~/research-data/lobbying/rows.jsonl``, one line
    per symbol), only when it is COMPLETE (``complete.json`` beside it): the
    same Quiver per-ticker history. A partial pull never seeds the store."""
    if not path.exists() or not path.with_name("complete.json").exists():
        return None
    store: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        merge_lobbying(store, [dict(r, Ticker=record["symbol"]) for r in record.get("rows") or []], today)
    return store


def step(directory: Path, client, today: str, sessions: list[str], tradeable: Callable[[str], bool],
         log: Callable[[str], None], *, seed_path: Optional[Path] = None,
         seed_symbols: Optional[Callable[[], list[str]]] = None, decide: bool = True) -> list[str]:
    """One run: keep the lobbying store current, then decide every book due
    today. Returns the books rebalanced. Any failure is raised AFTER the other
    book is decided and written (the prep sends a yellow card); an unseeded
    store is never saved, so the next run seeds it. ``decide`` False (an
    evening run) keeps the store current and decides nothing: a rebalance is
    decided before the open of its own day."""
    store_path, books_path = directory / "lobbying.json", directory / "books.json"
    store = load(store_path, None)
    if store is None:
        store = seed_from_research(seed_path, today) if seed_path else None
        if store is None and seed_symbols is not None:
            store = {}
            for symbol in seed_symbols():
                merge_lobbying(store, [dict(r, Ticker=symbol) for r in client.lobbying_history(symbol)], today)
        if store is not None:
            log(f"quiver shadows: lobbying store seeded with {len(store)} symbols")
    failures = []
    if store is None:
        failures.append("the lobbying store is not seeded yet (the research pull is incomplete); retried next run")
    else:
        added = merge_lobbying(store, client.lobbying_live(), today)
        _write(store_path, store)
        log(f"quiver shadows: {added} new lobbying rows merged; {len(store)} symbols in the store")
    books = load(books_path, {})
    rebalanced = []
    for name in (due(books, today, sessions) if decide and today in sessions else []):
        if name == "lobbying_growth" and store is None:
            continue
        try:
            if name == "congress_buys":
                weights = qb.congress_buys_weights(client.congress_live(), today)
            else:
                weights = qb.lobbying_weights(store, today, tradeable)
        except Exception as error:  # noqa: BLE001 - one book failing must not cost the other
            failures.append(f"{name}: {type(error).__name__}: {error}")
            continue
        if not weights:
            failures.append(f"the {name} shadow has no holdings for {today}; its data came back empty")
            continue
        book = books.setdefault(name, {"started": today, "schedule": []})
        book["schedule"].append([today, {s: round(w, 6) for s, w in weights.items()}])
        rebalanced.append(name)
        log(f"quiver shadows: {name} rebalanced for {today}: {len(weights)} names")
    if rebalanced:
        _write(books_path, books)
    if failures:
        raise RuntimeError("; ".join(failures))
    return rebalanced
