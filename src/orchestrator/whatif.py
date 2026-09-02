"""Config-replay harness (human ruling 2026-09-02): what would have changed?

Given a CANDIDATE ``signals.yaml`` and/or ``risk_limits.yaml``, replay the
recorded funnel through the deterministic stages both configs govern and report
the differences — which signals flip across the prefilter, which researched
verdicts flip across the sizing table — with the forward returns of each flip
group read from the append-only cache. READ-ONLY AND OFFLINE by construction:
the audit log and the forward cache are opened for reading, no LLM is called,
no bars are fetched, nothing is written anywhere.

Honesty about scope, printed in the report header: only rules whose inputs are
recoverable from the audit snapshots replay. Themes/instrument/bare-link
(content), amount, lag, the Form 4 cluster rule, and 13F period staleness all
replay faithfully; report-date staleness and the unheld-sale rule do not (the
snapshot has no report date, and the held-set of a past moment is gone), so
both run identically on both sides and can never appear as a diff. Research
verdicts are LLM output and are never re-run — a candidate config that would
have DISPATCHED different signals shows those signals' forward returns instead,
which is the counterfactual that matters. These numbers argue; humans rule.
"""

from __future__ import annotations

import re
import statistics
from datetime import date
from typing import Iterable, Optional

from audit.records import (
    DecisionRecord,
    SignalSnapshot,
    StageRejectionRecord,
    snapshot_amount_range,
    snapshot_lag_days,
    snapshot_tickers,
    snapshot_transaction,
)
from forward.returns import HORIZONS, ForwardRow
from risk_gate.limits import RiskLimits
from signals import SignalsConfig
from signals.records import Priority, Signal

from orchestrator.prefilter import ResearchPreFilter

KEY_HORIZON = 20

_PERIOD_LINE = re.compile(r"reporting period\s+([0-9\-/]+)", re.IGNORECASE)


def load_cached_rows(path) -> dict[tuple[str, date], ForwardRow]:
    """The forward cache, read-only — no engine, no fetches, no appends."""
    rows: dict[tuple[str, date], ForwardRow] = {}
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = ForwardRow.from_json(line)
            if row is not None:
                rows[(row.symbol, row.observed)] = row
    return rows


def _pseudo_signal(snapshot: SignalSnapshot) -> Signal:
    """A Signal rebuilt from its snapshot, carrying every structured fact the
    prefilter rules read that the snapshot can still supply."""
    tickers = snapshot_tickers(snapshot)
    lag = snapshot_lag_days(snapshot)
    metadata = {
        "tickers": ",".join(tickers),
        "ticker": tickers[0] if tickers else "",
        "transaction": snapshot_transaction(snapshot),
        "amount_range": snapshot_amount_range(snapshot),
    }
    if lag is not None:
        metadata["disclosure_lag_days"] = str(lag)
    period = _PERIOD_LINE.search(snapshot.content)
    if period:
        metadata["period_of_report"] = period.group(1)
    if "CLUSTER:" in snapshot.content:
        metadata["cluster"] = "true"
    elif "single qualifying purchase" in snapshot.content:
        metadata["cluster"] = "false"
    return Signal(
        signal_id=snapshot.signal_id,
        source_id=snapshot.source_id,
        signal_class=snapshot.signal_class,
        observed_at=snapshot.observed_at,
        content=snapshot.content,
        raw_content=snapshot.raw_content or snapshot.content,
        priority=Priority.for_class(snapshot.signal_class),
        external_id=snapshot.external_id,
        metadata=metadata,
    )


def _excess(
    snapshots: Iterable[SignalSnapshot],
    rows: dict[tuple[str, date], ForwardRow],
    horizon: int = KEY_HORIZON,
) -> list[float]:
    values: list[float] = []
    for snapshot in snapshots:
        tickers = snapshot_tickers(snapshot)
        if not tickers:
            continue
        row = rows.get((tickers[0].upper(), snapshot.observed_at.date()))
        if row is None:
            continue
        mark = row.marks.get(horizon)
        if mark is not None and mark.excess_pct is not None:
            values.append(float(mark.excess_pct))
    return values


def _stat(label: str, values: list[float]) -> str:
    if not values:
        return f"  {label}: no cached forward marks"
    note = " (small sample)" if len(values) < 20 else ""
    return (
        f"  {label}: mean {statistics.mean(values):+.2f}% excess at "
        f"{KEY_HORIZON}d (n={len(values)}){note}"
    )


def _skips(
    prefilter: ResearchPreFilter, signal: Signal
) -> Optional[str]:
    """Why this config would skip the signal at dispatch, or None. The same
    three checks the loop runs, in the loop's order."""
    reason = prefilter.missing_instrument(signal)
    if reason is not None:
        return f"no_instrument: {reason}"
    reason = prefilter.bare_link(signal)
    if reason is not None:
        return f"bare_link: {reason}"
    verdict = prefilter.skip_verdict(signal, now=signal.observed_at)
    if verdict is not None:
        return f"{verdict[1]}: {verdict[0]}"
    return None


def render_whatif_report(
    records: Iterable,
    current_signals: SignalsConfig,
    candidate_signals: SignalsConfig,
    current_limits: RiskLimits,
    candidate_limits: RiskLimits,
    rows: dict[tuple[str, date], ForwardRow],
    max_examples: int = 10,
) -> str:
    current_filter = ResearchPreFilter.from_config(current_signals)
    candidate_filter = ResearchPreFilter.from_config(candidate_signals)

    now_skipped: list[tuple[SignalSnapshot, str]] = []
    now_passed: list[tuple[SignalSnapshot, str]] = []
    would_not_trade: list[tuple[SignalSnapshot, int]] = []
    new_trades: list[tuple[SignalSnapshot, int]] = []
    band_moves: list[tuple[SignalSnapshot, int, str, str]] = []
    seen: set[str] = set()
    examined = 0

    for record in records:
        if isinstance(record, DecisionRecord):
            if record.sizing.strategy in ("mechanical", "cash_sweep"):
                continue
            snapshot, research = record.signal, record.research
            decision_id = record.decision_id
        elif isinstance(record, StageRejectionRecord):
            snapshot, research = record.signal, record.research
            decision_id = record.decision_id
        else:
            continue
        if decision_id in seen:
            continue
        seen.add(decision_id)
        examined += 1

        signal = _pseudo_signal(snapshot)
        before = _skips(current_filter, signal)
        after = _skips(candidate_filter, signal)
        if before is None and after is not None:
            now_skipped.append((snapshot, after))
        elif before is not None and after is None:
            now_passed.append((snapshot, before))

        if research is not None and 0 <= research.confidence <= 100:
            old = current_limits.sizing.size_for(research.confidence)
            new = candidate_limits.sizing.size_for(research.confidence)
            if old > 0 and new <= 0:
                would_not_trade.append((snapshot, research.confidence))
            elif old <= 0 and new > 0:
                new_trades.append((snapshot, research.confidence))
            elif old != new:
                band_moves.append(
                    (snapshot, research.confidence, f"{old:%}", f"{new:%}")
                )

    lines = [
        "Config replay — what the candidate config would have changed, over "
        f"{examined} recorded judged funnel entries",
        "READ-ONLY: no LLM re-run, no bars fetched; forward returns come from "
        "the existing cache only.",
        "Replayed rules: themes/instrument/bare-link, amount floor, disclosure "
        "lag, Form 4 cluster, 13F period staleness, and the sizing table on "
        "recorded confidence scores. NOT replayable (identical on both sides): "
        "report-date staleness, unheld-sale, source caps and budget ordering, "
        "and the research verdicts themselves.",
        "",
        f"Prefilter — candidate would now SKIP {len(now_skipped)} previously "
        f"dispatched signals:",
        _stat("their forward returns", _excess((s for s, _ in now_skipped), rows)),
    ]
    for snapshot, reason in now_skipped[:max_examples]:
        ticker = (snapshot_tickers(snapshot) or ("?",))[0]
        lines.append(f"    {snapshot.source_id} {ticker}: {reason[:110]}")
    lines.extend(
        [
            "",
            f"Prefilter — candidate would now DISPATCH {len(now_passed)} "
            f"previously skipped signals (each would spend triage/research):",
            _stat("their forward returns", _excess((s for s, _ in now_passed), rows)),
        ]
    )
    for snapshot, reason in now_passed[:max_examples]:
        ticker = (snapshot_tickers(snapshot) or ("?",))[0]
        lines.append(
            f"    {snapshot.source_id} {ticker}: was skipped as {reason[:100]}"
        )
    lines.extend(
        [
            "",
            f"Sizing table on recorded confidence scores: "
            f"{len(would_not_trade)} researched signals would no longer trade, "
            f"{len(new_trades)} would newly trade, {len(band_moves)} change bands:",
            _stat(
                "no-longer-trade group",
                _excess((s for s, _ in would_not_trade), rows),
            ),
            _stat("newly-trade group", _excess((s for s, _ in new_trades), rows)),
        ]
    )
    for snapshot, confidence, old, new in band_moves[:max_examples]:
        ticker = (snapshot_tickers(snapshot) or ("?",))[0]
        lines.append(f"    {ticker} @ confidence {confidence}: {old} -> {new}")
    lines.extend(
        [
            "",
            "These numbers argue; humans rule. A config change this justifies "
            "lands as a dated ruling, never an auto-tune.",
        ]
    )
    return "\n".join(lines)
