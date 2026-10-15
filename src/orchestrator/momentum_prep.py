"""The momentum book's monthly prep (human ruling 2026-10-13).

``python -m orchestrator momentum-prep`` runs in its own unit. When a month
turns (the evening of its last session, or a morning catch-up on the first
session of the next one) it:

  1. builds the data: Alpaca daily bars (active and delisted names) and SEC
     facts, ``history_years`` back, into a fresh dated cache folder;
  2. computes v1 on the morning of the rebalance day (``engine.live_view``:
     data through the prior close, no read of the day itself possible), with
     the live sleeve's holdings as v1's held list and the vol-scaling target
     from v1's own simulated book over the trailing three years;
  3. runs the AI leg: a veto review of every NEW entrant (a vetoed slot goes
     to the next rank by v1's rule, and the replacement is reviewed in turn)
     and a six-month sentiment score for every name, logged only;
  4. computes every declared shadow with its own held list and its own book
     history, the X-thread pick (each January) and SPMO;
  5. writes ``data/momentum/plan-YYYY-MM.json`` (what the session executes),
     ``detail-YYYY-MM.json`` (ranks, scores, exclusions, reviews, problems),
     ``shadows-YYYY-MM.json`` and the rolling ``shadows.json`` state.

v1 is frozen: nothing here, and nothing the model says, changes its rules.
A failed or malformed review never vetoes. Outside a production unit the
data guard sends every write to scratch (a dry run).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from momentum import engine, rules
from momentum.rules import LIVE, SECTOR_NAMES, SHADOWS, Spec

from orchestrator.momentum_sleeve import PLAN_VERSION, plan_path
from orchestrator.momentum_veto import Review

NY = ZoneInfo("America/New_York")
SIX = Decimal("0.000001")
logger = logging.getLogger("orchestrator.momentum_prep")


# -- when -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Due:
    rebalance_day: str
    data_through: str


def _hhmm(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def due(now: datetime, sessions: list[str], *, evening_after: str, morning_before: str) -> Optional[Due]:
    """The month to prepare, if any. ``sessions``: trading days around now.
    Evening of a month's last session -> the next month; morning of a
    month's first session (before ``morning_before``) -> that month."""
    local = now.astimezone(NY)
    today = local.date().isoformat()
    if today not in sessions:
        return None
    later = [s for s in sessions if s > today]
    earlier = [s for s in sessions if s < today]
    if later and later[0][:7] != today[:7] and local.time() >= _hhmm(evening_after):
        return Due(rebalance_day=later[0], data_through=today)
    if earlier and earlier[-1][:7] != today[:7] and local.time() < _hhmm(morning_before):
        return Due(rebalance_day=today, data_through=earlier[-1])
    return None


# -- the computation (pure over a Store) ----------------------------------------------


@dataclass
class Prep:
    plan: dict
    detail: dict
    shadows: dict
    problems: list = field(default_factory=list)
    reviews: list = field(default_factory=list)


def live_keys(store, d: int) -> dict[str, str]:
    """symbol -> the segment trading under it at session ``d``."""
    return {seg.symbol: seg.key for seg in store.segments if seg.first <= d <= seg.last}


def _weight(value: float, cap: float) -> Decimal:
    """Rounded DOWN to six places and never above the cap (Constraint #6)."""
    return min(Decimal(repr(value)).quantize(SIX, rounding=ROUND_DOWN), Decimal(repr(cap)))


def compute(
    store,
    rebalance_day: str,
    held: list[str],
    *,
    review: Optional[Callable[[dict], Review]] = None,
    shadow_held: Optional[dict[str, list[str]]] = None,
    x_pick: Optional[str] = None,
    names: Optional[dict[str, str]] = None,
    max_reviews: int = 30,
    spec: Spec = LIVE,
) -> Prep:
    view = engine.live_view(store, rebalance_day)
    t = len(view.calendar) - 1
    d = t - 1
    starts = engine.month_starts(view.calendar)
    month = rebalance_day[:7]
    problems: list[str] = []
    keys = live_keys(view, d)
    sims: dict[Spec, list] = {}

    def to_keys(symbols: list[str], who: str, report: bool) -> list[str]:
        out = []
        for symbol in symbols:
            key = keys.get(symbol)
            if key is None:
                if report:
                    problems.append(
                        f"{who}: {symbol} is not in the data on {view.calendar[d]} - it leaves the book by rule"
                    )
            else:
                out.append(key)
        return out

    def book(s: Spec) -> list:
        """The unscaled book's daily returns over the trailing target window -
        independent of vol scaling and the veto, so shared where specs agree."""
        if not s.vol_scaling:
            return []
        signature = replace(s, name="", vol_scaling=True, ai_veto=False)
        if signature not in sims:
            start = max(1, d - s.target_vol_days - 30)
            sims[signature] = engine.run(view, s, start, d).book_returns
        return sims[signature]

    def symbol(key: str) -> str:
        return view.by_key[key].symbol

    def context(dec, key: str, new: bool) -> dict[str, Any]:
        seg = view.by_key[key]
        adj, raw = view.adj[key], view.raw[key]
        closes = engine.month_end_closes(adj, starts, t, 13)
        r12 = rules.plain_momentum(closes) if len(closes) == 13 and all(engine.ok(c) for c in closes) else None
        returns = engine.Returns(adj)
        moves = [returns[i] for i in range(max(1, d - spec.gap_lookback + 1), d + 1)]
        moves = [abs(m) for m in moves if m is not None]
        return dict(
            symbol=seg.symbol,
            name=(names or {}).get(seg.symbol),
            sector=SECTOR_NAMES.get(seg.sector, ""),
            rank=dec.selection.ranks.get(key),
            new_entrant=new,
            data_through=view.calendar[d],
            last_close=raw[d] if engine.ok(raw[d]) else None,
            return_12_1=r12,
            max_daily_move_3m=max(moves) if moves else None,
            residual_z=dec.details.get("residual_z", {}).get(key),
            earnings_z=dec.details.get("earnings_z", {}).get(key),
        )

    # v1, with the veto of new entrants
    held_keys = to_keys(held, "v1", True)
    v1_book = book(spec)
    reviews: dict[str, Review] = {}
    unreviewed: list[str] = []
    vetoed: dict[str, str] = {}
    dec = None
    for _ in range(8):
        dec = engine.decide(view, t, spec, held_keys, v1_book, starts, vetoed=vetoed, explain=True)
        new = [k for k in dec.selection.bought if k not in reviews and k not in unreviewed]
        newly = False
        for key in new:
            if review is None or len(reviews) >= max_reviews:
                unreviewed.append(key)
                continue
            result = review(context(dec, key, True))
            reviews[key] = result
            if result.fault:
                problems.append(f"review of {symbol(key)}: {result.fault} - no veto, v1 stands")
            if result.applies:
                vetoed[key] = f"{result.category}: {result.evidence}"
                newly = True
        if not newly:
            break
    else:
        dec = engine.decide(view, t, spec, held_keys, v1_book, starts, vetoed=vetoed, explain=True)
        unreviewed += [k for k in dec.selection.bought if k not in reviews and k not in unreviewed]
        problems.append("the veto loop did not settle in 8 rounds; the last replacements were not reviewed")
    assert dec is not None
    bought_unreviewed = [k for k in dec.selection.bought if k in unreviewed]
    if bought_unreviewed:
        why = "the AI leg is off" if review is None else f"the {max_reviews}-review cap was reached"
        problems.append(
            f"bought without a veto review ({why}): {', '.join(symbol(k) for k in bought_unreviewed)}"
        )
    # sentiment for the names kept (logged only)
    if review is not None:
        for key in dec.selection.holdings:
            if key not in reviews and len(reviews) < max_reviews:
                reviews[key] = review(context(dec, key, False))

    ranks = dec.selection.ranks
    targets = []
    for key, value in sorted(dec.holdings.items(), key=lambda kv: (ranks.get(kv[0], 10**6), kv[0])):
        weight = _weight(value, spec.position_cap)
        if weight > 0:
            targets.append({"symbol": symbol(key), "weight": str(weight), "rank": ranks.get(key),
                            "sector": SECTOR_NAMES.get(view.by_key[key].sector, "")})
    sold = {symbol(k): why for k, why in dec.selection.sold}
    for name in held:
        if name not in keys:
            sold[name] = f"not in the data on {view.calendar[d]}"
    computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plan = {
        "version": PLAN_VERSION,
        "spec": spec.name,
        "month": month,
        "rebalance_date": rebalance_day,
        "data_through": view.calendar[d],
        "computed_at": computed_at,
        "buys_allowed": dec.buys_allowed,
        "exposure": str(Decimal(repr(dec.exposure)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)),
        "targets": targets,
        "sold": sold,
        "vetoed": {symbol(k): why for k, why in vetoed.items()},
    }

    details = dec.details
    ranking = []
    shown = set(sorted(ranks, key=lambda k: ranks[k])[:40]) | set(dec.holdings) | set(held_keys)
    for key in sorted((k for k in shown if k in ranks), key=lambda k: ranks[k]):
        ranking.append({
            "rank": ranks[key], "symbol": symbol(key), "score": details.get("score", {}).get(key),
            "residual_z": details.get("residual_z", {}).get(key), "earnings_z": details.get("earnings_z", {}).get(key),
            "excluded": details["excluded"].get(key), "vetoed": key in vetoed, "held": key in held_keys,
            "in_book": key in dec.holdings,
        })
    detail = {
        "month": month,
        "rebalance_date": rebalance_day,
        "data_through": view.calendar[d],
        "computed_at": computed_at,
        "universe": details["universe"],
        "scored": details["scored"],
        "exposure": dec.exposure,
        "current_vol": details["current_vol"],
        "target_vol": details["target_vol"],
        "book_history_sessions": len(v1_book),
        "buys_allowed": dec.buys_allowed,
        "selection": {
            "kept": [symbol(k) for k in dec.selection.holdings if k not in dec.selection.bought],
            "bought": [symbol(k) for k in dec.selection.bought],
            "sold": [[symbol(k), why] for k, why in dec.selection.sold],
            "skipped": [[symbol(k), why] for k, why in dec.selection.skipped],
        },
        "ranking": ranking,
        "reviews": [r.as_record() for r in reviews.values()],
        "unreviewed": [symbol(k) for k in unreviewed],
        "problems": problems,
        # The simulated book behind the vol target is v1 WITHOUT the veto -
        # flagged: the live book's own history is too short until 2029.
        "notes": ["vol target from v1's simulated book (no veto), trailing three years"],
    }

    # the shadows, software only
    books: dict[str, dict] = {}
    previous = shadow_held or {}
    for name, shadow in SHADOWS.items():
        sdec = engine.decide(view, t, shadow, to_keys(previous.get(name, []), name, False), book(shadow), starts)
        books[name] = {
            "weights": {symbol(k): float(_weight(w, shadow.position_cap)) for k, w in sdec.holdings.items()},
            "exposure": sdec.exposure,
            "buys_allowed": sdec.buys_allowed,
        }
    pick, pick_return = x_pick, None
    if month[5:] == "01":
        pick, pick_return = _x_thread_pick(view, t)
    books["x_thread"] = {"weights": {pick: 1.0} if pick else {}, "pick_return": pick_return}
    books["spmo"] = {"weights": {"SPMO": 1.0}}
    shadows = {"month": month, "rebalance_date": rebalance_day, "data_through": view.calendar[d], "books": books}
    return Prep(plan, detail, shadows, problems, list(reviews.values()))


def _x_thread_pick(view, t: int) -> tuple[Optional[str], Optional[float]]:
    """Each January: the single top gainer of the calendar year just ended
    among the top 500 by market cap (an S&P 500 proxy - flagged)."""
    d = t - 1
    year = int(view.calendar[t][:4])
    prior = [i for i, day in enumerate(view.calendar[:t]) if day[:4] == str(year - 2)]
    if not prior:
        return None, None
    base = prior[-1]
    best = None
    for seg in engine.universe(view, t, 500, 5.0):
        a, b = view.adj[seg.key][base], view.adj[seg.key][d]
        if engine.ok(a) and engine.ok(b) and (best is None or b / a > best[0]):
            best = (b / a, seg.symbol)
    return (best[1], best[0] - 1) if best else (None, None)


# -- files ----------------------------------------------------------------------------


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write(directory: Path, prep: Prep) -> Path:
    """Detail and shadows first, the plan last: the session acts on the plan
    file alone, so it appears only once everything behind it is on disk."""
    month = prep.plan["month"]
    write_json(directory / f"detail-{month}.json", prep.detail)
    write_json(directory / f"shadows-{month}.json", prep.shadows)
    state = {
        "month": month,
        "held": {name: sorted(book.get("weights", {})) for name, book in prep.shadows["books"].items()},
        "x_thread_pick": next(iter(prep.shadows["books"].get("x_thread", {}).get("weights", {})), None),
    }
    write_json(directory / "shadows.json", state)
    path = plan_path(directory, month)
    write_json(path, prep.plan)
    return path


def read_shadow_state(directory: Path) -> tuple[dict[str, list[str]], Optional[str]]:
    try:
        raw = json.loads((directory / "shadows.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    return dict(raw.get("held") or {}), raw.get("x_thread_pick")


def missing_shadows(directory: Path, month: str) -> list[str]:
    """Declared shadows the month's shadow file does not carry yet (a shadow
    declared after the month's plan was made, 2026-10-14)."""
    try:
        books = json.loads((directory / f"shadows-{month}.json").read_text(encoding="utf-8")).get("books", {})
    except (OSError, ValueError):
        return []
    return [name for name in SHADOWS if name not in books]


def backfill_shadows(store, start_day: str, names: list[str], directory: Path, month: str) -> list[str]:
    """Compute the missing shadows as of ``start_day`` (the session they start
    on, from the prior close; held list empty - a new shadow starts flat) and
    add them to ``month``'s shadow file, dated, and to the rolling state.
    The plan itself is never touched (standing rule 2026-10-14: a new shadow
    is tracked from the next session, not the next month)."""
    view = engine.live_view(store, start_day)
    t = len(view.calendar) - 1
    d = t - 1
    starts = engine.month_starts(view.calendar)
    path = directory / f"shadows-{month}.json"
    shadows = json.loads(path.read_text(encoding="utf-8"))
    state_path = directory / "shadows.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"month": month, "held": {}}
    added = []
    for name in names:
        spec = SHADOWS[name]
        book = engine.run(view, spec, max(1, d - spec.target_vol_days - 30), d).book_returns if spec.vol_scaling else []
        dec = engine.decide(view, t, spec, [], book, starts)
        shadows["books"][name] = {
            "weights": {view.by_key[k].symbol: float(_weight(w, spec.position_cap)) for k, w in dec.holdings.items()},
            "exposure": dec.exposure,
            "buys_allowed": dec.buys_allowed,
            "started": start_day,
        }
        state.setdefault("held", {})[name] = sorted(shadows["books"][name]["weights"])
        added.append(name)
    write_json(path, shadows)
    write_json(state_path, state)
    return added


#: The AI trader's brief (human ruling 2026-10-15): the top names by v1.1's
#: score, every flag SHOWN rather than excluded.
SHORTLIST_SIZE = 50
SHORTLIST_FILE = "shortlist.json"


def shortlist(store, rebalance_day: str, *, spec: Spec = LIVE, size: int = SHORTLIST_SIZE,
              names: Optional[dict[str, str]] = None) -> dict:
    """The top ``size`` of v1.1's universe by score on ``rebalance_day``
    (data through the prior close), with the score's two legs and every
    buy-gate flag computed on its own - not only the first that excludes:
    below the 100-day average; a >15% day in 63 sessions and whether every
    such day sat within [-1,+1] of an 8-K 2.02 filing; a pending acquisition;
    the 18-month filer status. Scores and flags ONLY: never which names the
    sleeve holds or plans to (ruling 2026-10-15, second). Reference data for
    the AI trader: it changes nothing in v1.1."""
    view = engine.live_view(store, rebalance_day)
    t = len(view.calendar) - 1
    d = t - 1
    starts = engine.month_starts(view.calendar)
    dec = engine.decide(view, t, spec, [], [], starts, explain=True)
    score = dec.details["score"]
    excluded = dec.details["excluded"]
    rows = []
    for position, key in enumerate(sorted(score, key=lambda k: -score[k])[:size], start=1):
        seg = view.by_key[key]
        adj, raw = view.adj[key], view.raw[key]
        returns = engine.Returns(adj)
        days = range(max(1, d - spec.gap_lookback + 1), d + 1)
        big = [i for i in days if returns[i] is not None and abs(returns[i]) > spec.gap_limit]
        window = {e + k for e in seg.events for k in (-1, 0, 1)}
        closes = [adj[i] for i in range(max(0, d - spec.trend_days + 1), d + 1) if engine.ok(adj[i])]
        month_closes = engine.month_end_closes(adj, starts, t, 13)
        r12 = (rules.plain_momentum(month_closes)
               if len(month_closes) == 13 and all(engine.ok(c) for c in month_closes) else None)
        periodic = seg.periodic or []
        rows.append(dict(
            score_rank=position,
            symbol=seg.symbol,
            name=(names or {}).get(seg.symbol),
            sector=SECTOR_NAMES.get(seg.sector, ""),
            score=round(score[key], 3),
            residual_momentum_z=_round(dec.details["residual_z"].get(key)),
            earnings_momentum_z=_round(dec.details["earnings_z"].get(key)),
            return_12_1=_round(r12),
            buy_gate=excluded.get(key) or "passes",
            flags=dict(
                below_100_day_average=(len(closes) >= spec.trend_days
                                       and closes[-1] < sum(closes[-spec.trend_days:]) / spec.trend_days),
                day_over_15pct_in_63_sessions=bool(big),
                big_days_all_on_earnings=(all(i in window for i in big) if big else None),
                pending_acquisition=any(a <= d <= b for a, b in seg.pending),
                filer=("current" if engine.current_filer(seg, view.calendar[d])
                       else f"lapsed (last 10-K/10-Q {periodic[-1]})" if periodic else "no 10-K/10-Q on record"),
            ),
        ))
    return dict(spec=spec.name, rebalance_date=rebalance_day, data_through=view.calendar[d],
                universe_version=getattr(store, "universe_version", 1), universe=dec.details["universe"],
                scored=len(score), names=rows)


def _round(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(float(x), 3)


def shortlist_covers(directory: Path, data_through: str) -> bool:
    """Whether the shortlist on disk is built on data through ``data_through``
    or later (a weekly refresh must not be overwritten by an older plan's)."""
    try:
        on_disk = json.loads((directory / SHORTLIST_FILE).read_text(encoding="utf-8")).get("data_through") or ""
    except (OSError, ValueError):
        return False
    return on_disk >= data_through


def latest_plan(directory: Path) -> Optional[dict]:
    plans = sorted(directory.glob("plan-*.json"))
    if not plans:
        return None
    try:
        return json.loads(plans[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def shadow_start(plan: dict, now: datetime, sessions: list[str], close_after: str = "16:30") -> Optional[Due]:
    """When a shadow declared after ``plan`` starts: on the plan's own date
    and data while that date has not passed; otherwise at the next session,
    from the last completed close (standing rule 2026-10-14)."""
    local = now.astimezone(NY)
    today = local.date().isoformat()
    if plan["rebalance_date"] >= today:
        return Due(rebalance_day=plan["rebalance_date"], data_through=plan["data_through"])
    closed = local.time() >= _hhmm(close_after)
    done = [s for s in sessions if s < today or (s == today and closed)]
    ahead = [s for s in sessions if s > today or (s == today and not closed)]
    if not done or not ahead:
        return None
    return Due(rebalance_day=ahead[0], data_through=done[-1])
