# Session Notes

Rolling handover between sessions. Written at the end of a session, read at the start
of the next one. `CLAUDE.md` is the constitution and does not change here; this file is
only ever a record of where the work got to and what comes next.

**Last updated:** 2026-08-18 · PAPER PERIOD STARTED — Class 3 only; ops layer built

---

## PAPER PERIOD: started 2026-08-18

CLAUDE.md build order step 7 is running: **Class 3 only** (EDGAR 13F), Classes 1/2
pending credentials. Alpaca paper keys landed 2026-08-18; all 6 keyed integration
tests pass. First supervised end-to-end cycle ran the same day: 3 real filings polled,
3 real research passes, all three returned `no_position` at high confidence (82/83/80)
— no order placed, complete audit trail in `data/audit.jsonl`. The step-7 clock
(2–4 weeks minimum before any live-mode discussion) starts from the scheduled runs,
and what it can prove with Class 3 alone is limited: 13Fs anchor conviction, they
rarely trade. The period gets meaningful when Class 1/2 credentials land.

Operating it:

- `python -m orchestrator run` — one trading session: waits for the 9:30 ET open
  (bounds computed in America/New_York at runtime), ticks until 16:00 ET, shuts down
  cleanly. This is what the scheduled task runs.
- `python -m orchestrator health` — the daily ten-second check: positions with armed
  stops, cash/NAV/drawdown, kill switch, budget, last EDGAR poll, last run events,
  last audit record. Strictly read-only (tested byte-for-byte).
- `python -m orchestrator` — the startup checks alone, unchanged.
- `ops/register_paper_task.ps1` — registers the weekday scheduled task (venv python,
  repo working directory, 9:15 ET-equivalent local trigger; the run gates itself so
  the trigger only has to be early). Deliberately not run by the agent — the human
  registers it.
- `data/run.log` — terse STARTED/STOPPED/ERROR/POLL lines, separate from the audit
  trail: "did the scheduled run actually fire" at a glance. `data/orchestrator.log`
  has the full logging.

Crash safety for unattended runs: everything replays (kill switch, budget, daily
deployment, open positions with stops — all tested), plus two additions for the
mid-tick death specifically: startup **orphan-sweeps** any order left working at the
broker by a dead process (its reservation died with the process, unforgeably — tested
in `test_ops.py::test_a_crash_between_submit_and_reconcile_is_swept_at_the_next_startup`),
and startup + health flag **UNMANAGED** positions — held at the broker with no audit
trail, hence no stops — loudly for a human.

---

## Build state

All seven packages in `CLAUDE.md`'s build order exist and are tested. **498 passing, 1
skipped** (the opt-in EDGAR live smoke; run it with `EDGAR_LIVE_TESTS=1`) (the skips are `tests/test_execution_integration.py`, which auto-skips
without Alpaca paper credentials in `.env` — see the queue below).

| Package | What it does |
|---|---|
| `src/risk_gate/` | Order schema + enforcing gate. Deterministic, no bypass path. |
| `src/execution/` | Broker adapter interface + Alpaca paper backend. |
| `src/signals/` | Three scanners, one per latency class; post classification. |
| `src/research/` | LLM scoring layer, structured output through a forced tool call. |
| `src/sizing/` | Confidence table → `SizedProposal`. |
| `src/audit/` | Append-only JSONL trail + weekly attribution. |
| `src/orchestrator/` | The loop that wires the six together, plus the exit engine. |

### The orchestrator is done

One loop: scanner queue → research → sizing → order construction → risk gate → broker →
audit, with a record written at every stop. `python -m orchestrator` runs the startup
checks and reports the reconstructed state without trading.

Startup order is fixed and asserted: the Constraint #4 mode check fires *before* a
config file is opened or the broker is touched, then configs, then broker connectivity,
then replay. Cash and positions come from the broker (it is the account); daily
deployment and the research budget replay from the audit log; the high-water mark and
kill switch come from `data/session_state.json`.

### Scope: long equity only

This is the deliberate current shape, not an oversight, and it is stated in
`src/orchestrator/pipeline.py`'s module docstring. The loop opens long equity positions
and nothing else:

- `short_via_puts` needs an options chain — expiry, strike, OCC symbol. No chain source
  exists, and picking a contract is a sizing-relevant decision, not a detail.
- Event contracts need Kalshi, which the build order puts *after* the equity leg has
  proved itself in paper.
- **Exits are built** (2026-08-18): two layers in `src/orchestrator/exits.py` +
  `src/research/exit_review.py`. Deterministic guardrails (max-loss stop, time stop,
  frozen per position at entry from `config/orchestrator.yaml`) checked every cycle;
  a budgeted LLM thesis review (hold/close via a closed schema) on a configured
  cadence. A failed review is a hold, never a close; the guardrails run regardless,
  so a position cannot become unexitable because the LLM layer is down. Every close
  routes through gate sell-to-close validation, works during a kill-switch halt, and
  finishes the trail: ExitRecord → sell-side FillRecord → OutcomeRecord →
  CredibilityTracker — hit rates are real now. Open positions and pending close
  verdicts replay from the audit log at startup. The paper run is unblocked.

Each unsupported path writes a named `order_construction` rejection rather than being
silently skipped, so the log can say how often it happened.

### Kill-switch persistence is proven

Worth knowing precisely, because the obvious implementation is wrong. The halt is
sticky by design: once tripped it stays tripped through a recovery, because the point is
to make a human look. A restart that recomputed drawdown from a recovered NAV would see
0% and resume opening positions on its own.

So the flag is persisted to `data/session_state.json` and applied to `AccountState`
before the gate is constructed. `test_a_tripped_kill_switch_survives_a_restart_that_recovered`
trips it, shuts down, restarts with NAV **fully recovered**, and asserts the gate is
still halted and refuses an opening order with `KILL_SWITCH_ACTIVE`. There is a control
case alongside it proving it is the persistence doing the work and not the arithmetic,
and another asserting a halted restart still accepts risk-reducing closes.

The orchestrator never calls `reset_kill_switch`. Resuming is a manual human decision;
the state file carries a `_note` field saying so to whoever finds it.

### Other things that landed this session

- `no_position` in `Direction`. Sizes to zero at every confidence including 100, ahead
  of the confidence table rather than through it.
- Manipulation findings truncated to 300 chars for prompt replay; verbatim in the audit
  record.
- `tests/test_topology.py` — one exhaustive allow-list replacing three per-package
  `FORBIDDEN_IMPORTS` tuples. Includes the walk `risk_gate` never had, so Constraint #3
  is now a test rather than a docstring.
- `StageRejectionRecord` — a signal that dies before the gate leaves a complete trail
  under the same `decision_id`.
- `RiskGate.record_fill(..., filled_units=)` — settles a terminal partially-filled
  order.

---

## The seams: two built (2026-08-18), two awaiting credentials

Both interfaces stay explicit arguments to `orchestrator.start()` — the operator wires
the production implementations at the call site.

### `PriceSource` — BUILT: `execution.market_data.AlpacaPriceSource`

Latest IEX quote via `data.alpaca.markets` (paper keys grant the free feed; same key
pair as the trading adapter). Settings in `config/orchestrator.yaml` under
`market_data:`. The safety property, tested from every failure angle: **an outage
never reads as a price** — HTTP errors, timeouts, malformed bodies, quotes with no
priced side, and quotes older than `max_quote_age_seconds` (strictly older; absent or
unparseable timestamps count as stale) all come back as `None`, never `Decimal("0")`,
because a zero would sit below every max-loss stop in the book. Ask preferred (bounds
a buy), bid as the one-sided fallback. Its two integration tests auto-skip without
Alpaca keys, like the broker ones.

### Class 3 `Fetcher` — BUILT: `signals.edgar.Form13FFetcher`

EDGAR full-text search → archives index → cover (`periodOfReport`) → information
table, per watchlist fund, free and keyless. SEC citizenship enforced in code: every
request carries the contact User-Agent from `config/signals.yaml` (refuses to run
without an email in it), requests are throttled to one per half-second (a fifth of the
SEC's 10/s ceiling), and a single 429/5xx gets one logged retry so a transient blip
does not cost a daily-cadence poll a full day. FTS matches filings that merely
*mention* a fund, so hits are kept only when the filer's display name contains the
fund name. Bought puts in a filing are rendered as `(Put)` — a 13F is longs-only in
the equity sense, not the instrument sense. Live smoke test is opt-in
(`EDGAR_LIVE_TESTS=1`, no key needed) to keep the default suite hermetic; it passed
against the real fund on 2026-08-18 (six real filings fetched and parsed).

Topology note: `signals` now has network permission in the map — ingestion is its
job — while its first-party isolation (no risk_gate, no execution, no sizing) is
unchanged and still tested.

### Class 1 and Class 2 fetchers — UNBUILT, awaiting credentials

Truth Social / X (Class 1) and Quiver Quant / Unusual Whales / Capitol Trades
(Class 2) all need credentials not yet procured. Same `Fetcher` protocol; the EDGAR
implementation is the template. A failing fetcher is already handled: the loop logs
it, skips that cycle, and carries on without hot-retrying a feed that is down.

Wiring note: `build_scanners` takes ONE fetcher for all three classes. Production
wiring with only EDGAR built needs a small router (dispatch on `source.id`,
unconfigured sources raising) — deliberately not built until a second real fetcher
exists to shape it.

---

## Next session's queue

### (a) `@jimcramer` fade source — human-authorized

**Authorization:** granted by the account owner this session. `CLAUDE.md` § Requires
Explicit Human Approval covers "adding a new signal source or watchlist account"; this
records that the approval was given, and it is scoped to this one source. It is not a
standing grant for further sources.

A **fade** source: the thesis is that the call is wrong, so the research layer's job is
inverted relative to `@nolimitgains`. Design questions to settle before writing code:

- The inversion belongs in the **research layer**, not in sizing and not in the gate.
  Everything downstream of research reads an integer and a closed enum, and that is what
  makes the caps hold — a fade implemented by negating a size would put a sign flip
  somewhere it must never be.
- The likely shape is a source-level `treatment` in `config/signals.yaml` that reaches
  the prompt as framing ("this source's calls are being evaluated as contrarian
  indicators; form your own view on whether the *opposite* position is warranted"), with
  the model still returning an ordinary `direction` and `confidence`. A fade of a bullish
  call is a `short_via_puts` verdict, which the loop currently cannot execute — see the
  scope note above. Worth deciding whether that makes this item wait on an options chain.
- Post classification: does a fade source need the `forward_call` / `retrospective`
  split? Probably yes and for the same reason — fading a trade that already happened at
  a price that is gone is the same error in the opposite direction.
- Do **not** let "fade" become a second code path through sizing or the gate. If the
  research layer cannot express it, that is a research-layer problem to solve.

### (b) ~~Alpaca paper keys~~ — **DONE 2026-08-18**

Keys landed in `.env`; all 6 keyed integration tests pass. The first live re-run also
surfaced and fixed a real bug: client_order_id must be unique per account across
time, and the gate sequence restarts every process — ids now carry a per-adapter
launch token (`agentic-{token}-{seq}`).

Original queue entry:

`tests/test_execution_integration.py` has four tests that auto-skip when `.env` has no
`ALPACA_API_KEY` / `ALPACA_API_SECRET`. Filling those in turns them on and makes
`python -m orchestrator` complete its preflight — it currently gets through the mode
check and configs and fails at step 3 with `ALPACA_API_KEY is not set`, which is the
correct behaviour and also as far as it can go.

`PAPER_MODE=true` stays as it is. `.env` is gitignored; the keys go there and nowhere
else.

### (c) ~~Exit logic via `invalidation_condition`~~ — **DONE 2026-08-18**

Built as specified below, plus deterministic guardrails the spec discussion settled
on. `AuditLog.record_outcome` is now called by the exit engine on every full close.
CLAUDE.md build order step 7 — the 2–4 week paper run — now measures something real.
Remaining detail from the notes below that is still true: the loop cannot yet
express a puts-based exit hedge, only sell-to-close of long equity.

Original queue entry, kept for the reasoning:

The highest-value item, and the reason to be careful about reading anything into a paper
run started before it exists. **The loop opens positions and never closes them.** A P&L
figure from a run that only ever accumulates exposure is not a measurement of the
strategy; it is a measurement of the market's drift over the window.

`CLAUDE.md` § Research: `invalidation_condition` is "what kills the thesis — feeds
automated exit logic". Every report already carries one and every one is in the audit
trail. What is missing is the thing that evaluates it against a live position, which is
a research-layer job (it is a natural-language condition) on a cadence, feeding
`EquitySellToCloseOrder` — a type the schema and the gate already support and the
orchestrator already has the settlement path for.

Notes for whoever builds it:

- The gate's close-validation path is already correct and tested: never beyond held
  quantity, and permitted while the kill switch is halted precisely so a halt cannot
  trap the account in its positions.
- An exit decision is still a decision and should write an audit record. Consider
  whether it reuses the opening `decision_id` (linking exit to entry, which is what
  `OutcomeRecord` wants) or takes its own.
- `AuditLog.record_outcome` exists and is what turns a source's hit rate from "not yet
  available" into a number. Nothing calls it yet. Closing a position is what should.
- Only once this exists does `CLAUDE.md` build order step 7 — "paper trade the full
  pipeline 2-4 weeks minimum" — measure anything real.

---

## Standing reminders

- `PAPER_MODE=true`. Live needs two variables, both set by a human, and the agent must
  never set, suggest setting, or write code that sets either.
- The kill switch resets manually or not at all.
- Adding a signal source needs explicit human approval, per source.
- `data/` is gitignored and holds the audit trail and session state. It is not backed up
  by the repo and belongs in a backup routine.
