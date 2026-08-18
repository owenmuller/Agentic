# Session Notes

Rolling handover between sessions. Written at the end of a session, read at the start
of the next one. `CLAUDE.md` is the constitution and does not change here; this file is
only ever a record of where the work got to and what comes next.

**Last updated:** 2026-08-18 · exits built; see queue

---

## Build state

All seven packages in `CLAUDE.md`'s build order exist and are tested. **441 passing, 4
skipped** (the skips are `tests/test_execution_integration.py`, which auto-skips
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

## The two unbuilt seams

Both are interfaces with no production implementation. They are seams rather than stubs
on purpose: an untestable HTTP client is worse than an honest gap, and requiring them as
arguments to `orchestrator.start()` makes the gap visible at the call site rather than
at 09:30.

### 1. `Fetcher` — `src/signals/scanners.py`

```python
def __call__(self, source: SourceConfig) -> Sequence[RawItem]: ...
```

Returns raw items for one configured source. Needs real clients for Truth Social and X
(Class 1), Quiver Quant / Unusual Whales / Capitol Trades (Class 2), and SEC EDGAR
full-text search (Class 3). All need credentials that are not on this machine. Tests
drive the whole pipeline through fixture fetchers on this exact interface.

A failing fetcher is already handled: the loop logs it, skips that cycle, and carries on
without hot-retrying a feed that is down.

### 2. `PriceSource` — `src/orchestrator/pipeline.py`

```python
def __call__(self, symbol: str) -> Optional[Decimal]: ...
```

Returns the per-unit price a buy should be *bounded* at — the offer, not the last
trade, because that is the figure the risk gate cash-secures against. Returning `None`
is a normal answer meaning "no usable quote", and produces no order rather than an order
priced on a guess.

Nothing exists behind it. Alpaca's market-data API (`data.alpaca.markets`) is the
obvious first implementation and shares credentials with the trading adapter.

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

### (b) Alpaca paper keys → un-skip the integration tests

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
