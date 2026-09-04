"""The signal-convergence registry (human ruling 2026-09-01).

One deterministic structure answers three questions about a symbol:

  who else is active on it?     -> source-diversity dispatch bonus + context line
  which other filers bought it? -> cross-filer cluster bonus + context line (4a)
  what did we already conclude? -> prior-verdict context lines, declines included

Two consumers, both bounded by ruling:

  dispatch ordering   the loop adds ``bonus_for`` to the scanner's dispatch
                      weight in its sort key. ORDERING ONLY — the same rule as
                      the 2026-08-26 dispatch weight: it decides who spends
                      limited research slots first and can never touch a cap,
                      a size, or the gate.
  research context    ``context_for`` renders a convergence block the prompt
                      carries as FENCED DATA. Filer names and rejection codes
                      originate in third-party feeds, so they get the same fence
                      the market context gets.

Everything here derives from the system's own records: seeded from the audit
log at startup (a restart must not forget last week's cluster), updated from
drained signals and pipeline outcomes as the session runs. No LLM anywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from audit.records import (
    AuditRecord,
    ConvergenceSnapshot,
    DecisionRecord,
    RejectedStage,
    StageRejectionRecord,
    snapshot_tickers,
    snapshot_transaction,
)
from signals import Signal, SignalClass
from signals.filers import canonical_credibility_key

from orchestrator.config import ConvergenceConfig


def family_of(source_id: str, signal_class: SignalClass) -> str:
    """The signal's source FAMILY (human rulings 2026-09-02, recorded in CLAUDE.md).

    Five families, deterministic: congressional filings, 13F filings (13D
    beneficial-ownership filings join here — Elliott's 13D and Elliott's 13F
    must not count as independent), insider filings (Form 4), Trump posts, and
    X trade-callers — ALL X accounts are ONE family, however many of them post,
    because accounts amplifying each other is not independence.
    """
    if source_id == "congressional_disclosures":
        return "congressional_filings"
    if source_id == "form4_insiders":
        return "insider_filings"
    if source_id == "form_13d":
        return "13f_filings"  # a fund's 13D and its 13F are one identity class
    if signal_class is SignalClass.CLASS_3_THESIS:
        return "13f_filings"
    if source_id == "trump_posts":
        return "trump_posts"
    return "x_callers"

logger = logging.getLogger("orchestrator.registry")

#: Prefilter codes of measurement-only rows: outside convergence by ruling.
MEASUREMENT_CODES: frozenset[str] = frozenset(
    {"bearish_measurement", "overreaction_candidate"}
)


@dataclass(slots=True)
class _Active:
    """One signal's presence on one symbol."""

    key: str  # (source_id, external_id) identity, for dedup
    identity: str  # credibility_key or source_id — the independence unit
    source_id: str
    filer: str
    symbol: str
    observed_at: datetime
    is_purchase: bool
    #: The source FAMILY (2026-09-02): the independence unit one level up.
    family: str = "x_callers"


@dataclass(slots=True)
class _Verdict:
    """What the system already concluded about a symbol, one research pass."""

    symbol: str
    identity: str
    recorded_at: datetime
    #: "traded" | "gate_rejected" | "declined" | rejection-stage bucket
    outcome: str
    confidence: Optional[int]
    code: str = ""

    def line(self) -> str:
        confidence = (
            f", confidence {self.confidence}" if self.confidence is not None else ""
        )
        code = f" ({self.code})" if self.code and self.outcome != "traded" else ""
        return (
            f"{self.recorded_at.date().isoformat()} {self.identity}: "
            f"{self.outcome}{code}{confidence}"
        )


_VERDICT_STAGES = {
    RejectedStage.SIZING: "declined",
    RejectedStage.ORDER_CONSTRUCTION: "declined",
    RejectedStage.TRIAGE: "triaged_out",
}


class SignalRegistry:
    """Active signals and prior verdicts per symbol, inside a rolling window."""

    def __init__(self, config: ConvergenceConfig, clock) -> None:
        self._config = config
        self._clock = clock
        self._active: dict[str, _Active] = {}
        self._verdicts: list[_Verdict] = []

    # -- feeding it -----------------------------------------------------------------

    def seed(self, records: Iterable[AuditRecord]) -> int:
        """Rebuild the window from the audit log at startup."""
        horizon = self._clock() - timedelta(days=self._config.window_days)
        seeded = 0
        for record in records:
            if not isinstance(record, (DecisionRecord, StageRejectionRecord)):
                continue
            if record.recorded_at < horizon:
                continue
            if isinstance(record, StageRejectionRecord) and record.code in MEASUREMENT_CODES:
                continue  # measurement rows are graded, never converged on
            snapshot = record.signal
            if isinstance(record, DecisionRecord):
                if record.sizing.strategy in ("mechanical", "cash_sweep"):
                    continue  # the judged record of the disclosure seeds it;
                    # parked cash is not a signal at all
                outcome = "traded" if record.was_approved else "gate_rejected"
                code = "" if record.was_approved else (record.gate.rejection_code or "")
                confidence = (
                    record.research.confidence if record.research else None
                )
            else:
                outcome = _VERDICT_STAGES.get(record.stage, "")
                code = record.code
                confidence = (
                    record.research.confidence if record.research else None
                )
            identity = canonical_credibility_key(snapshot.credibility_key or snapshot.source_id)
            key = f"{snapshot.source_id}\x00{snapshot.external_id or record.decision_id}"
            transaction = snapshot_transaction(snapshot)
            for ticker in snapshot_tickers(snapshot):
                entry_key = f"{key}\x00{ticker}"
                self._active.setdefault(
                    entry_key,
                    _Active(
                        key=entry_key,
                        identity=identity,
                        source_id=snapshot.source_id,
                        filer=snapshot.filer or "",
                        symbol=ticker,
                        observed_at=snapshot.observed_at,
                        is_purchase="purchase" in transaction.lower(),
                        family=family_of(
                            snapshot.source_id, snapshot.signal_class
                        ),
                    ),
                )
                if outcome:
                    self._verdicts.append(
                        _Verdict(
                            symbol=ticker,
                            identity=identity,
                            recorded_at=record.recorded_at,
                            outcome=outcome,
                            confidence=confidence,
                            code=code,
                        )
                    )
                seeded += 1
        return seeded

    def note_signals(self, signals: Iterable[Signal]) -> None:
        """Register drained signals. Idempotent per (source, external id, ticker).
        Measurement-only rows (sell clusters, overreaction candidates) are NOT
        signals and never enter the window (rulings 2026-09-02/03)."""
        for signal in signals:
            meta = signal.metadata
            if meta.get("measurement_only") == "true":
                continue
            identity = canonical_credibility_key(meta.get("credibility_key") or signal.source_id)
            filer = (meta.get("representative") or meta.get("fund") or "").strip()
            key = f"{signal.source_id}\x00{signal.external_id or signal.signal_id}"
            transaction = meta.get("transaction", "")
            for ticker in self._tickers_of(signal):
                entry_key = f"{key}\x00{ticker}"
                self._active.setdefault(
                    entry_key,
                    _Active(
                        key=entry_key,
                        identity=identity,
                        source_id=signal.source_id,
                        filer=filer,
                        symbol=ticker,
                        observed_at=signal.observed_at,
                        is_purchase="purchase" in transaction.lower(),
                        family=family_of(signal.source_id, signal.signal_class),
                    ),
                )

    def note_outcome(self, signal: Signal, outcome: str, confidence, code: str = "") -> None:
        """Register a verdict produced this session, so a second source arriving
        an hour after a decline is shown that decline."""
        identity = canonical_credibility_key(signal.metadata.get("credibility_key") or signal.source_id)
        for ticker in self._tickers_of(signal):
            self._verdicts.append(
                _Verdict(
                    symbol=ticker,
                    identity=identity,
                    recorded_at=self._clock(),
                    outcome=outcome,
                    confidence=confidence,
                    code=code,
                )
            )

    # -- consuming it ----------------------------------------------------------------

    def bonus_for(self, signal: Signal) -> Decimal:
        """The dispatch bonus: cluster + diversity, capped per component.

        Multi-ticker signals take their best ticker's bonus. Deterministic,
        derived from structured fields only, ordering-only by ruling.
        """
        best = Decimal("0")
        for ticker in self._tickers_of(signal):
            cluster = self._cluster_filers(ticker, self._own_filer(signal))
            diversity = self._other_identities(ticker, self._own_identity(signal))
            bonus = min(
                self._config.cluster_bonus_cap,
                self._config.cluster_bonus_per_filer * len(cluster),
            ) + min(
                self._config.diversity_bonus_cap,
                self._config.diversity_bonus_per_source * len(diversity),
            )
            best = max(best, bonus)
        return best

    def context_for(self, signal: Signal) -> Optional[str]:
        """The convergence block for the research prompt, or None when there is
        nothing to say. The caller fences it — filer names and codes are
        feed-derived text."""
        now = self._clock()
        sections: list[str] = []
        for ticker in list(self._tickers_of(signal))[:3]:
            lines: list[str] = []
            others = self._other_identities(ticker, self._own_identity(signal))
            if others:
                stamps = sorted(
                    {
                        (entry.identity, entry.observed_at.date().isoformat())
                        for entry in others.values()
                    }
                )
                lines.append(
                    f"- independent sources active on {ticker} in the last "
                    f"{self._config.window_days} days (excluding this one): "
                    f"{len({i for i, _ in stamps})} — "
                    + "; ".join(f"{identity} ({day})" for identity, day in stamps)
                )
            cluster = self._cluster_filers(ticker, self._own_filer(signal))
            if cluster:
                lines.append(
                    f"- cross-filer purchase cluster: {len(cluster)} other "
                    f"congressional filer(s) disclosed purchases of {ticker} in "
                    f"the window: {', '.join(sorted(cluster))}"
                )
            verdicts = [
                v
                for v in self._verdicts
                if v.symbol == ticker
                and v.recorded_at >= now - timedelta(days=self._config.window_days)
            ]
            if verdicts:
                recent = sorted(verdicts, key=lambda v: v.recorded_at)[
                    -self._config.max_prior_verdicts :
                ]
                lines.append(
                    f"- prior research verdicts on {ticker} in the window: "
                    + "; ".join(v.line() for v in recent)
                )
            if lines:
                sections.append("\n".join([f"{ticker}:"] + lines))
        return "\n".join(sections) if sections else None

    def snapshot_for(self, signal: Signal) -> Optional[ConvergenceSnapshot]:
        """The signal's convergence state at dispatch, for the decision record.

        Includes the signal's OWN family — the future band-up rule counts
        families present, and this signal is present. The best ticker of a
        multi-ticker signal wins (most families, then most identities). None
        when the signal names no instrument.
        """
        own_family = family_of(signal.source_id, signal.signal_class)
        own_identity = self._own_identity(signal)
        best: Optional[ConvergenceSnapshot] = None
        for ticker in self._tickers_of(signal):
            others = self._other_identities(ticker, own_identity)
            families = {own_family} | {entry.family for entry in others.values()}
            candidate = ConvergenceSnapshot(
                symbol=ticker,
                families=tuple(sorted(families)),
                independent_identities=len(others),
                cluster_filers=len(
                    self._cluster_filers(ticker, self._own_filer(signal))
                ),
            )
            if best is None or (
                candidate.family_count,
                candidate.independent_identities,
            ) > (best.family_count, best.independent_identities):
                best = candidate
        return best

    @property
    def window_days(self) -> int:
        return self._config.window_days

    def verdict_summary(self) -> dict[str, tuple[str, ...]]:
        """outcome -> symbols, the LATEST research verdict per symbol inside the
        window. The review dialectic's opportunity-cost input (2026-09-03):
        names the system actually evaluated, not every ticker that crossed the
        feed — the first live round trip showed 499 "active" names, which is
        raw flow, not a candidate pool."""
        horizon = self._clock() - timedelta(days=self._config.window_days)
        latest: dict[str, _Verdict] = {}
        for verdict in self._verdicts:
            if verdict.recorded_at < horizon:
                continue
            current = latest.get(verdict.symbol)
            if current is None or verdict.recorded_at > current.recorded_at:
                latest[verdict.symbol] = verdict
        grouped: dict[str, list[str]] = {}
        for symbol, verdict in sorted(latest.items()):
            grouped.setdefault(verdict.outcome, []).append(symbol)
        return {outcome: tuple(symbols) for outcome, symbols in grouped.items()}

    def purchase_symbols(self) -> tuple[str, ...]:
        """Names with a purchase-side active signal in the window — the
        overreaction screen's BROAD universe (ruling 2026-09-03)."""
        return tuple(sorted({e.symbol for e in self._in_window() if e.is_purchase}))

    def in_window_symbols(self) -> tuple[str, ...]:
        """Symbols with any active signal in the window — the IV-watch feed
        (ruling 2026-09-02): names the shadow logger should snapshot daily so
        an IV-rank history exists for what the system might actually trade."""
        return tuple(sorted({entry.symbol for entry in self._in_window()}))

    # -- internals --------------------------------------------------------------------

    def _tickers_of(self, signal: Signal) -> tuple[str, ...]:
        raw = (signal.metadata.get("tickers") or "").strip()
        return tuple(
            dict.fromkeys(t.strip().upper() for t in raw.split(",") if t.strip())
        )

    @staticmethod
    def _own_identity(signal: Signal) -> str:
        return signal.metadata.get("credibility_key") or signal.source_id

    @staticmethod
    def _own_filer(signal: Signal) -> str:
        return (
            signal.metadata.get("representative")
            or signal.metadata.get("fund")
            or ""
        ).strip()

    def _in_window(self) -> list[_Active]:
        horizon = self._clock() - timedelta(days=self._config.window_days)
        return [e for e in self._active.values() if e.observed_at >= horizon]

    def _other_identities(self, ticker: str, own: str) -> dict[str, _Active]:
        """Entries on the ticker from identities other than the asker's, one
        representative entry per identity (diversity, never count)."""
        out: dict[str, _Active] = {}
        for entry in self._in_window():
            if entry.symbol != ticker or entry.identity == own:
                continue
            existing = out.get(entry.identity)
            if existing is None or entry.observed_at > existing.observed_at:
                out[entry.identity] = entry
        return out

    def _cluster_filers(self, ticker: str, own_filer: str) -> set[str]:
        """Distinct OTHER filers with purchase disclosures on the ticker."""
        return {
            entry.filer
            for entry in self._in_window()
            if entry.symbol == ticker
            and entry.is_purchase
            and entry.filer
            and entry.filer.lower() != own_filer.lower()
        }
