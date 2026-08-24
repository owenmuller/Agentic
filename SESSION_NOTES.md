# Session Notes

Rolling handover between sessions. Written at the end of a session, read at the start
of the next one. `CLAUDE.md` is the constitution and does not change here; this file is
only ever a record of where the work got to and what comes next.

**Last updated:** 2026-08-19 · runtime migrating to VPS (system of record moves at cutover); Robinhood spike complete (GO verdict); first paper session ran on the laptop 2026-08-19 (late start — missed 6:15 trigger, laptop was asleep)

---

## PAPER PERIOD: started 2026-08-18

CLAUDE.md build order step 7 is running: **all three classes wired.** Class 3 =
EDGAR 13F; Class 2 = Quiver congressional disclosures; Class 1 = @nolimitgains plus
the Trump leg via X mirror accounts (decided and human-approved 2026-08-18).

### The Trump leg: live via mirrors; Truth API consciously deferred

Decision 2026-08-18: ride X mirror accounts on the existing fetcher rather than
subscribe to TMTG's Truth API. **The upgrade decision belongs to attribution data**
— if Class 1's Trump-sourced trades earn, mirror latency (~1–3 min measured) is the
quantifiable cost that a Truth API subscription would buy back, and the attribution
report prices that trade. Revisit when Class 1 attribution has a track record.

Mirrors picked (verified active 2026-08-18 against the Truth Social archive, both
text-based, both prompt — relay latency measured via tweet-id snowflake timestamps):

- `trump_mirror_ttox` = **@TrumpTruthOnX** (primary): automated relay, ~1-min claim
  consistent with observed timestamps, original "(TS: ...)" stamp in the text.
- `trump_mirror_tdp` = **@TrumpDailyPosts** (secondary): 28K posts, full text with
  original-timestamp header, ~2.5-min measured relay on a market-moving post.

Mechanics: `type: mirror` + `mirror_of: trump_posts` in signals.yaml. Signals are
**attributed to trump_posts** (research context, credibility, attribution) while the
audit record preserves the deliverer (`SignalSnapshot.delivered_by`). Mirror signals'
external ids are normalised content keys, so the same Truth arriving through both
mirrors is ONE signal and one research pass — and the queue's dedup is now seeded
from the audit log at startup, so a restart cannot re-buy a Truth it already scored
whichever mirror re-delivers it. The research prompt carries a MIRROR PROVENANCE
block (outside the fence): unofficial mirror, verification against Truth Social /
news coverage is part of the pass, and an unverifiable post MUST come back
`no_position`. Mirror health: a mirror silent for 2+ trading days (configurable per
source) gets a MIRROR line in run.log at session start — quiet principal or dead
bot, a human checks which.

**Budget collision — RESOLVED 2026-08-18 (human ruling): the pre-filter, not a
bigger budget.** `orchestrator/prefilter.py`: a trump_posts signal (mirror-delivered
included — the filter keys on the attributed source) is researched only if it names
an instrument (the scanner's own deterministic ticker extraction) or matches a theme
stem from `research_prefilter_themes` in signals.yaml (tariff, energy, defense,
crypto, rate, fed, chip, china, ~30 stems; word-prefix, case-blind). Placement per
the ruling: at research dispatch in the loop, BEFORE the budget — scanners stay dumb
emitters. Every filtered post writes a `stage_rejection` with stage/code
`pre_filter`, so the trail shows every Truth that arrived and why it was skipped; if
attribution ever suggests the filter eats alpha, read what it skipped. Budget replay
excludes pre-filtered ids (they spent nothing), and the seeded queue stops re-
filtering the same Truth daily. `max_research_passes_per_day` stays 40.

**Class 1 blocker — CLEARED 2026-08-18:** the App was attached to the pay-per-use
Project (Production) and `X_BEARER_TOKEN` regenerated. Live smoke green same day:
25 real posts from @nolimitgains over 6 days, 25 posts billed (~$0.13). All three
Class 1 sources (@nolimitgains + both Trump mirrors) poll live from the next
scheduled session. Alpaca paper keys landed 2026-08-18; all 6 keyed integration
tests pass. First supervised end-to-end cycle ran the same day: 3 real filings polled,
3 real research passes, all three returned `no_position` at high confidence (82/83/80)
— no order placed, complete audit trail in `data/audit.jsonl`. The step-7 clock
(2–4 weeks minimum before any live-mode discussion) starts from the scheduled runs,
and what it can prove with Class 3 alone is limited: 13Fs anchor conviction, they
rarely trade. The period gets meaningful when Class 1/2 credentials land.

### Class 2: live 2026-08-18 (attribution clock starts now)

`signals/quiver.py` — QuiverCongressFetcher against api.quiverquant.com (Hobbyist,
$30/mo, Bearer auth from `QUIVER_API_KEY` in `.env`). One request per hourly poll
regardless of watchlist size; same citizenship as EDGAR (0.5s min interval, one
logged retry on 429/5xx). Every signal's content carries BOTH dates labelled —
transaction date and report date — plus the computed lag in days, so the staleness
the priced-in analysis must reason about is inside the fenced data block, not
inferred. Live smoke (`QUIVER_LIVE_TESTS=1`) passed same day: 2 real Pelosi
disclosures parsed from the live feed.

Dedup across restarts is now systemic, not per-fetcher: disclosures get a
deterministic identity hash, filings their accession, and both fetchers seed their
seen-sets at startup from `AuditLog.researched_external_ids()` — research already
paid for is never re-bought, while a signal that was queued but never researched
left no record and correctly re-emits. (This also fixed a real Class 3 defect: every
daily restart had been re-researching the same filings.)

**Feed costs are in the attribution report.** `monthly_cost` per source in
signals.yaml (Quiver 30, everything else 0), prorated at window_days/30; the report
states gross AND net per class and the human-review flag now fires on NET — a class
must out-earn its own feed. A paid class with no decisions still shows its bleed.
Run it: `python -m orchestrator attribution`.

**Single-instance protection** (predates none of this — added first): `orchestrator
run` holds an OS-level lock on `data/orchestrator.lock`. A second concurrent run
refuses with a REFUSED run-log line naming the holder; a crashed process's lock is
released by the kernel, so a stale lock file cannot brick the next scheduled run.
Both tested. `SourceRouter` wires class→fetcher in one place: EDGAR + Quiver routed,
the two Class 1 accounts declared unbuilt (poll nothing, warn once), anything
undeclared raises before it can fail silently at 9:30.

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

### Class 2 `Fetcher` — BUILT 2026-08-18: `signals.quiver.QuiverCongressFetcher`

See the paper-period section above. The router it forced into existence is
`signals.routing.SourceRouter` — the one place to wire a future fetcher.

### Class 1 X fetcher — BUILT 2026-08-18: `signals.x.XRecentSearchFetcher`

One recent-search query per source (`from:<handle> -is:retweet`), since_id-disciplined
because pay-per-use bills per POST RETURNED, not per request: a quiet minute costs
zero, and the first poll of a session uses a 15-minute lookback instead of seven days
of history. `note_tweet` requested explicitly — plain `text` truncates past 280 chars
and a clipped trade call is a corrupted signal; the full-pipeline test walks a
300+-char post through classification, research (full text inside the fence), the
gate, and the audit record. Classification rules apply unchanged (forward_call /
retrospective / other). The billing tripwire: a daily read counter logs cumulative
posts read per UTC day and warns once past `daily_read_warning` (200, in
signals.yaml) into run.log via the warn_sink — a since_id regression must show in the
logs before it shows on the bill. `monthly_cost: 10` charged to Class 1 in
attribution.

### Old Class 1 note, superseded

Only trump_posts remains unbuilt, by decision rather than by gap — see the Class 1
blocker note above and the Truth API question. A failing fetcher is already handled:
the loop logs it, skips that cycle, and carries on.

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

## Live-phase broker: Robinhood Agentic Trading (assessed 2026-08-18)

Robinhood launched official **Agentic Trading** (2026-05-27, beta): a first-party
MCP server (`https://agent.robinhood.com/mcp/trading`) trading a dedicated,
separately funded **Agentic Account**. This retires the ToS objection that routed
execution to Alpaca — the CLAUDE.md Robinhood note is now outdated on that point.
Findings (full report in the 2026-08-18 session):

- **Tools:** comprehensive — portfolio/balances, positions, order status, and
  review→place→cancel order flows for **equities, options, and crypto**, plus
  quotes, historicals, and **option chains** (`get_option_chains` /
  `get_option_instruments` — would fill our missing options-chain seam). Order-type
  specifics (TIF, limit semantics) and client-order-id idempotency are not publicly
  documented; need empirical confirmation.
- **Auth:** OAuth with interactive desktop consent at setup; autonomous operation is
  explicitly permitted after that. Token lifetime/refresh for a headless scheduled
  process is undocumented — the single biggest open question; needs a spike.
- **No sandbox/paper mode.** Mitigation: minimal funding — the dedicated-account
  model is itself a structural blast-radius cap that fits Constraint #1 (margin is
  not enabled on Agentic accounts).
- **Rate limits:** undocumented.
- **Event contracts:** on the roadmap ("event contracts, futures, and more"), not
  reachable today — the prediction sleeve stays Kalshi-bound regardless.
- **Adapter cost:** a `RobinhoodAgenticAdapter` implementing `BrokerAdapter` plus a
  minimal MCP-over-HTTP (JSON-RPC) client in `execution/` (topology already permits
  network there); reads must be scoped to the Agentic account (the MCP can read ALL
  the user's accounts); `permissions()` synthesized from account metadata.

**Verdict: viable live-phase candidate; decision stays open until the paper period
ends.** Paper stays on Alpaca (Robinhood has no paper mode to run it on). Before any
live commitment: a small spike — open an Agentic account, minimal funding, prove
headless token refresh across a full scheduled session, confirm limit-order TIF and
idempotency semantics.

### Spike part 1 — read-only (2026-08-18): HALTED on the scoping check

Setup that worked: `claude mcp add robinhood-trading --transport http
https://agent.robinhood.com/mcp/trading` (human-run), interactive OAuth completed by
the human, Agentic account funded $100 (test harness only). The running Claude Code
session could not hot-load the new server, so the spike spoke MCP streamable-HTTP
directly (scratchpad `rh_mcp.py`, read-only guard baked in, token from Claude Code's
credential store, never printed). Server: `robinhood-trading` v1.1.5, protocol
2025-06-18, session id via `Mcp-Session-Id` header, SSE responses.

**CRITICAL FINDING (spike halted here per the human's standing instruction):
`get_accounts` returns ALL of the user's Robinhood accounts, not just the Agentic
one** — the default individual margin account (option level 2), a traditional IRA,
and the Agentic account. Each row carries `agentic_allowed`; only the Agentic
account is `true`, and the server's own guidance says false-accounts cannot be
ACTED on by this agent — but they are VISIBLE, including type, option level, and
account numbers. Whether reads like `get_portfolio`/`get_equity_positions` succeed
against a non-agentic account number is UNTESTED (halted before probing). Any
future `RobinhoodAgenticAdapter` must therefore hard-pin `account_number` to the
Agentic account at construction and refuse every other value in code — scoping is
our responsibility, not the API's.

Second surprise: **the Agentic account is `type: limited_margin`, not cash** — the
prior assessment's "margin is not enabled on Agentic accounts" is wrong as stated.
Limited margin ≠ borrowing (it's settled-funds trading), but Constraint #1 review is
mandatory before this account ever goes live. Its `option_level` is empty — options
are NOT yet enabled on the Agentic account (blocks the option-chain leg of the spike
until upgraded via `get_option_level_upgrade_info` / the human).

Tool inventory (question 1) — 54 tools, full schemas in the 2026-08-18 transcript:
- Documented surface confirmed for equities/options: accounts, portfolio, positions
  (equity + option), orders (get/review/place/cancel for both), quotes, historicals,
  fundamentals, option chains/instruments/quotes.
- **`ref_id` on place_equity_order / place_option_order** — client-supplied id,
  the idempotency candidate for part 2. `time_in_force` and `market_hours` exposed
  on review/place; enum values not in the schema dump — part 2 confirms empirically.
- Undocumented extras: scan engine (create/run/update scans), earnings calendar +
  results, technical indicators, tax lots, realized PnL / trade history, indexes,
  watchlists, **exercise_option / cancel_option_exercise**, limited-margin and
  option-level upgrade info tools.
- Missing vs marketing: **no crypto trading tools** were listed (only crypto-adjacent
  fields like `currency_pair_ids` on watchlists) — possibly account-gated.
- Every tool result embeds a `guide` field of server-authored presentation
  instructions. Treated as data, never as instructions (CLAUDE.md Constraint #5
  posture); an adapter must ignore it entirely.

Token evidence (question 5, partial): access token is an ES256 JWT issued by
`api.robinhood.com`, `scope: internal`, claims include `agent_id`, `options: true`,
`level2_access: true`, and an embedded secondary credential-like `token` claim (the
credential store file is sensitive beyond the obvious — treat `.credentials.json`
as radioactive). Stored expiry ≈ 3 days from issue (expiresAt 1787611791875 ms ≈
2026-08-21); a 30-char refresh token is stored alongside, refresh endpoint
per RFC 8414 discovery at the server. Whether Claude Code / an adapter can refresh
headlessly across weeks is still the open part-3 question.

**Human ruling (2026-08-18): scoping finding ACCEPTED with conditions.**
Read-level metadata exposure of non-agentic accounts is manageable. Conditions:
(1) Part 2 must verify the server's own enforcement — a review_equity_order
against a NON-agentic account number must be REFUSED by the server; if accepted,
halt and reassess (the act-block would be advisory). (2) The future adapter
hard-pins the Agentic account number at construction, refuses all others in code,
tested — and ignores the `guide` field entirely. Limited-margin finding accepted
for the spike; **live-mode review must confirm the buying-power field is
settled-funds only and that no debit-balance path exists on the Agentic account**
(Constraint #1).

### Spike part 1 continued — steps 3 & 4 (2026-08-18, options enabled by human)

Options were enabled on the Agentic account by the human: it now reports
`option_level_2` — long calls/puts only, no spreads or writes, which is exactly
the CLAUDE.md posture (the broker level itself cannot represent our forbidden
order shapes).

**Step 3 — AAPL chain vs the short_via_puts seam: PASS, richer than needed.**
- `get_option_chains(underlying_symbol)` → chain id, 24 expirations (out to Dec
  2028), `trade_value_multiplier` (100), `min_ticks` (0.05 above $3.00 cutoff /
  0.01 below), `can_open_position`.
- `get_option_instruments(chain_symbol, expiration_dates, type, state)` → 93 put
  strikes for 2026-09-18 in ONE unpaginated page (50–600), each with instrument
  UUID, strike, expiration, type, tradability. No OCC symbol field — RH uses
  instrument UUIDs; OCC symbols are derivable from (symbol, expiration, type,
  strike) if ever needed.
- `get_option_quotes(instrument_ids)` → bid/ask WITH sizes, mark, break-even,
  full greeks (delta/gamma/theta/vega/rho), IV, open interest, volume,
  chance_of_profit_long/short, updated_at. Everything the sizing engine and
  invalidation logic could want from a chain, in one call.
- Schema wart for the adapter: `expiration_dates` is a comma-separated STRING
  while `instrument_ids` is an ARRAY — per-tool conventions are inconsistent;
  validate against each tool's schema, don't generalize.

**Step 4 — review_equity_order semantics: preview commits NOTHING.**
Ran on the AGENTIC account only: AAPL buy 1 @ $1.00 limit gtc (extremely
far-from-market on purpose). Result:
- Returns the echoed order params + `order_checks` (typed alert:
  `EQUITY_EXTREMELY_UNMARKETABLE_LIMIT_PRICE` with entered vs last-trade price) +
  a full quote + a compliance `market_data_disclosure` string. No order was
  created (verified intent: review has NO ref_id and returns NO order id or
  placement token — place_equity_order is a fully independent call that
  re-supplies all parameters; nothing binds a review to a placement).
- Order types: market / limit / stop_market / stop_limit. TIF: **gfd | gtc only**
  — no IOC/FOK. Sessions: regular_hours | extended_hours | all_day_hours (24h);
  non-regular sessions are LIMIT-ONLY (market/stop shapes rejected outside
  regular hours). Fractional shares: market + regular_hours only.
  `dollar_amount` notional only with market orders. Specified-lot selling via
  `tax_lots` (sell only, ≤30 lots).
- **Idempotency confirmed at the schema level:** `place_equity_order.ref_id` —
  "Idempotency key (UUID). Generate once per logical order and re-send on retry —
  the upstream deduplicates by ref_id." Client↔gateway idempotency exists; part 2
  proves it empirically (same ref_id re-sent must not double-place).

### Spike part 2 — order round-trip (2026-08-18, human GO): findings

**Condition 1 PASSED — the act-block is real server enforcement.**
`review_equity_order` against the non-agentic default account returned
`isError: true`, `rh_error_category: unauthorized`, "FORBIDDEN: agent not
authorized to access this account". Non-agentic accounts are readable in
`get_accounts` metadata but rejected at the order seam by the server itself.
The adapter hard-pin remains a required second layer, not the only layer.

**Place semantics:** `place_equity_order` (AAPL buy 1 @ $1.00 limit gtc,
client UUID ref_id) was ACCEPTED at the gateway (state `unconfirmed`,
`placed_agent: "agentic"` — RH stamps agent-placed orders, good for audit) and
then **rejected by the back office ~160ms later** (state `rejected`): the review
alert `EQUITY_EXTREMELY_UNMARKETABLE_LIMIT_PRICE` is backed by a hard
post-placement collar for limits far from market. Consequences for the adapter:
(a) an accepted place response is NOT a resting order — poll
`get_equity_orders(order_id=...)` until a stable state; (b) the rejected order
record carries **no rejection-reason field** — the review alert beforehand is
the only readable why, so an adapter should always review-then-place and store
the alerts in the audit record.

**Idempotency: at-most-once via hard 409, NOT idempotent-return.** Re-sending
the byte-identical place with the SAME ref_id returned
`API error 409: "Reference ID must be unique"` — the server refuses duplicates
rather than returning the original order (unlike Alpaca's client_order_id
convention). Adapter retry recipe after ambiguous transport failure: re-send
same ref_id; on 409, the first attempt registered — reconcile via
get_equity_orders and DO NOT mint a new ref_id.

**Cancel on a terminal order:** `API error 403: "Order cannot be cancelled at
this time"` (`rh_error_category: invalid_request`). The resting-order cancel
leg DID NOT RUN — with a $100 account, no 1-share AAPL limit can both rest
(collar rejects far-from-market) and be affordable (buying power caps at $100
vs $310 stock). Re-run needs re-authorized parameters: a low-priced liquid
symbol with a limit a few % below market (exposure = that price), cancel
immediately.

**Sweep clean:** 1 order total on the account (the rejected one), 0 open,
cash $100.0000 intact. Portfolio shape is a gift for Constraint #1 review:
`buying_power == unleveraged_buying_power == 100.0000` — no leverage on the
limited-margin Agentic account as configured today; live-mode review still must
confirm no debit path exists.

**Rate limits (question 6):** across ~15 calls including writes — zero
rate-limit/retry/throttle headers, no 429s, sub-second responses throughout.
Limits exist but are not advertised; the adapter should keep our
one-logged-retry citizenship pattern and treat 429 handling as untestable
until observed.

### Spike part 2b — resting-cancel leg (2026-08-18, human GO): PASS

F (Ford) at $13.95: buy 1 @ $13.25 limit gtc (~5% below market), fresh ref_id.
State sequence: place returned `queued` immediately (market closed — queued for
next session IS the resting state outside RTH; ~5% survives the collar that
killed the $1.00 order), stable across ~12s of polling. Cancel returned
`{"accepted": true}` with explicitly ASYNC semantics (possible
`pending_cancelled`; a fill can race a cancel → `partially_filled_rest_cancelled`
— the adapter must treat cancel-accepted as "requested", not "done", and poll to
terminal). First post-cancel poll: `cancelled`. Sweep: 2 orders on the account
(both terminal: 1 rejected, 1 cancelled), 0 open, cash $100.0000 intact, no fill.

### Spike part 3 — headless OAuth refresh (2026-08-18): PASS, with rotation

1. **Credential store:** `~/.claude/.credentials.json` → `mcpOAuth["robinhood-
   trading|<hash>"]` holding accessToken (ES256 JWT), refreshToken, clientId
   (public client), expiresAt (ms), and the RFC 8414 discovery URLs. ELEVATED
   SENSITIVITY: the JWT's claims embed a secondary credential-like `token` field
   — treat the file as holding two secrets per server, never print/commit.
2. **Discovery chain (verified live):**
   `GET /.well-known/oauth-protected-resource/mcp/trading` → authorization server
   `https://agent.robinhood.com/mcp/trading` →
   `GET /.well-known/oauth-authorization-server/mcp/trading` →
   `token_endpoint: https://api.robinhood.com/oauth2/token/`, grant types
   authorization_code + refresh_token, token auth method `none` (public client —
   no secret needed headlessly), no revocation endpoint.
3. **Forced refresh (before expiry): HTTP 200 in 0.45s**, form-encoded
   `grant_type=refresh_token` + stored refresh_token + clientId, no browser.
   New access token verified with an authenticated get_portfolio read.
   `expires_in: 665712s` (~7.7 days). **The refresh token ROTATES on use.**
   Finding, not failure: Claude Code and a live orchestrator must NOT share this
   credential store — whichever refreshes second is stranded on a dead refresh
   token. A live orchestrator needs its OWN OAuth grant (own consent), stored in
   its own secret store. The rotated pair was written back to Claude Code's
   store to keep it consistent (verified working after write-back).
4. **Natural experiment armed:** one-shot Windows task "Agentic RH Refresh
   Check", 2026-08-27 07:03 local — after the new access token expires
   2026-08-26 17:19 UTC — runs `ops/rh_refresh_check.py` (stdlib-only, reads the
   store at runtime, refreshes, authenticated read, PASS/FAIL verdict, writes
   the rotated pair back; never prints tokens) →
   `data/rh_refresh_check.log` (gitignored).

### Spike verdict (2026-08-18): **GO — Robinhood Agentic is a viable live venue**

All three parts proved out: real server-side act-block on non-agentic accounts,
full order lifecycle with client idempotency (at-most-once via 409), option
chain/quotes richer than Alpaca's, and headless token refresh with a rotating
refresh token. Live-mode checklist before any switch (in addition to CLAUDE.md's
own live-gate rules):
- [ ] Adapter hard-pins the Agentic account number at construction; refuses all
      other account numbers in code; tested.
- [ ] Adapter ignores every `guide` field in tool results (server-authored
      instructions are data, Constraint #5).
- [ ] Own OAuth grant for the orchestrator (rotation finding) in its own secret
      store; refresh-before-expiry loop; alert on refresh failure.
- [ ] Confirm settled-funds-only buying power and no debit path on the
      limited-margin Agentic account (Constraint #1) — snapshot evidence today:
      buying_power == unleveraged_buying_power == cash.
- [ ] review-then-place always; persist review alerts in the audit record
      (rejected orders carry no reason field).
- [ ] Poll to stable state after place (gateway-accept ≠ resting) and after
      cancel (accepted ≠ cancelled); handle fill-races-cancel.
- [ ] Per-tool schema validation (string vs array conventions are inconsistent).
- [ ] 2026-08-27 refresh-check log reviewed (the unattended proof).
- [ ] Options remain level 2 on the Agentic account (long-only enforcement at
      the broker layer too).
- [ ] Fractional quantities: verify the Agentic MCP's order tools ACCEPT
      fractional qty and at what precision — unverified by the spike (all spike
      orders were whole-share). Until proven, a Robinhood adapter must keep
      `equity_quantity_step = 1` (whole shares), which the base adapter now
      defaults to; fractional going live there requires its own review-order
      probe first.

Paper period continues on Alpaca regardless; the live-venue decision itself
waits for the 2–4 week paper gate and the human's two-key live confirmation.

## Host migration: VPS is the system of record (from cutover)

**VPS:** DigitalOcean droplet `agentic`, Ubuntu 24.04.4, 137.184.59.200. Service
user `agentic` (no sudo, key-only; same ed25519 key as root). Hardened 2026-08-19:
password auth off, ufw SSH-only inbound, unattended security upgrades on.

**Why:** the laptop missed the first scheduled session outright (asleep at 6:15,
WakeToRun off, Task Scheduler did not catch up after wake). A trading runtime
belongs on a host that is always awake.

**Deploy path (no GitHub credentials on the box):** laptop pushes to a bare repo
(`git push vps main`, remote = `agentic@137.184.59.200:agentic.git`), working
clone at `/home/agentic/Agentic`, venv at `.venv`, `pip install -e .[dev]`.
Full suite on the VPS 2026-08-19: **585 passed, 11 skipped** (= the laptop's
593/3 with the 8 keyed Alpaca integration tests auto-skipping until `.env`
exists on the box). Same commit as the laptop.

**Scheduling (systemd, units in `ops/vps/`):**
- `agentic-paper.timer`: `OnCalendar=Mon..Fri 09:15 America/New_York`,
  `Persistent=true` (missed trigger replays at boot; the runtime market-hours
  gate stays the real guard). Verified: next elapse resolves to 9:15 **EDT**.
  Installed but **deliberately not enabled** — enabling a schedule that trades
  is the human's trigger: `systemctl enable --now agentic-paper.timer`.
- `agentic-backup.timer`: nightly 21:07 ET tar of `data/` to
  `/var/backups/agentic/`, 14-day rotation (enabled). Off-box layer: enable
  DigitalOcean droplet backups in the control panel (human, checkbox).
- `agentic-rh-refresh.timer`: one-shot 2026-08-27 10:03 ET (enabled) → runs the
  refresh check with `AGENTIC_RH_CRED=~/.config/agentic/rh_oauth.json`. The
  token file is transferred BY THE HUMAN; if absent the check FAILs loudly in
  `data/rh_refresh_check.log`, which is correct. **Exactly one host may hold a
  live copy of the rotating refresh token** — after transfer, delete the
  laptop's "Agentic RH Refresh Check" task, and using the robinhood MCP from
  laptop Claude Code may rotate the grant out from under the VPS copy (finding
  from part 3). Longer term the VPS orchestrator needs its OWN OAuth grant
  (own consent, own secret store) — noted, deliberately not acted on yet.

**Secrets:** `.env` (same five keys) created directly on the box by the human,
`chmod 600`, never through chat/repo/agent tool calls.

**Ops parity (from the laptop):**
- health:      `ssh agentic@137.184.59.200 'cd ~/Agentic && .venv/bin/python -m orchestrator health'`
- attribution: `ssh agentic@137.184.59.200 'cd ~/Agentic && .venv/bin/python -m orchestrator attribution'`
- run log:     `ssh agentic@137.184.59.200 'tail -20 ~/Agentic/data/run.log'`
- monthly:     re-run the Robinhood MCP tool inventory (spike client,
  `rh_mcp.py tools`, read-only) and diff against the known 54 tools — the
  appearance of event-contract tools triggers the prediction-sleeve Plan A
  design (see the venue plan section below).

**Cutover protocol (no gap day, no double-host day — locks are per-machine, two
hosts would double-trade the paper account):** after the laptop's 2026-08-19
session STOPs at 16:00 ET, in one motion: (1) copy `data/` laptop→VPS (audit
log, session state, run.log — the system of record travels), (2) disable the
laptop task (`Disable-ScheduledTask "Agentic Paper Trading"`), (3) enable the
VPS timer. 2026-08-20 is the VPS verification session (STARTED → polls →
STOPPED); rollback = disable the timer and re-enable the laptop task.

## Cost-efficiency pass (2026-08-19): free filters before paid judgment

The trump_posts principle extended system-wide. All skips write `pre_filter`
stage rejections — visible, revisitable, never silently dropped; all rules fail
OPEN (an unreadable field sends the signal to research, bounded by the budget).

- **Class 2 pre-filter** (`signals.yaml` prefilter block on
  congressional_disclosures): skip when the amount range tops out strictly below
  $15,000, when observed lag exceeds 75 days, or when it is a sale in a name the
  system does not hold (held set comes from the exit engine, deterministic).
- **Class 3 pre-filter** (form_13f): skip filings whose period-of-report is
  older than 120 days.
- **Model tiering** (`research.yaml` tiers block): Class 1 stays on the
  flagship (claude-opus-5, high). Class 2, Class 3, and exit thesis reviews run
  claude-sonnet-4-6 at medium effort. Same schema, same validation gates —
  only model/effort differ. Unknown tier names raise, never fall back silently.
- **Cost instrumentation:** every research pass and exit review stamps estimated
  input/output tokens and estimated dollars onto its audit record (accepted OR
  rejected — a malformed pass was still paid for). Estimates come from the
  pricing table in research.yaml. **BASELINE NUMBERS — replace with real
  console figures after week one of the paper period.** Entry passes are billed
  once per decision_id (a decision record and a later execution rejection share
  one call); each thesis review bills separately.
- **Attribution now nets ALL costs:** gross − feed − research is what the
  keep/cut flag fires on. A class whose every pass died pre-gate still shows
  its research bill in the report.

## Bolt-ons from the open-source landscape (2026-08-19) — no architecture changes

- **Benchmark-relative attribution:** the weekly report now carries SPY's total
  return over the same window (one Alpaca daily-bars fetch, close-to-close) and
  states excess return per class and overall — a bull market must not flatter a
  signal class. Return denominators are resolved buy-fill cost basis; anything
  missing (no benchmark, no resolved capital) renders as unavailable, never 0%.
  The keep/cut flag still fires on net P&L — alpha is context, not the trigger.
- **Deterministic market context in research prompts:** `MarketContextBuilder`
  (execution layer, injected into ResearchPass as a callable — topology intact)
  computes 5d/20d change, distance from 52-week high, latest-vs-20d-average
  volume, and days-to-earnings when a provider is configured (none is today —
  the block says "unavailable", it never invents). Injected INSIDE a data fence;
  guidance outside the fence requires any options thesis to weigh IV crush when
  earnings land inside the assigned time_horizon. A builder crash degrades to a
  sentence in the prompt; a research pass is never blocked by missing context.
- **Sector concentration guard in the risk gate:** `equity_sleeve.
  max_sector_exposure: 0.15` in risk_limits.yaml; membership is the static
  human-editable table `config/sectors.yaml`; an unmapped ticker is its own
  singleton sector (unknown names never share a bucket — the cap degrades to
  per-name, tighter, never looser). Typed rejection `sector_concentration`.
  Equity positions only: options keep their aggregate-premium cap, and mapping
  option symbols to underlyings would smuggle parsing into the gate. The
  property suite gained a per-sector invariant checked after every step.

**Deferred (trigger noted, not built):** if attribution ever shows the 86+
confidence band underperforming the 55–85 bands on hit rate or net P&L over a
60-day window, that is the trigger to revisit an adversarial red-team pass on
top-band trades before they size at the 5% cap.

## Prediction sleeve (10%): venue plan (queued 2026-08-19)

Designed-but-unbuilt: the order schema, sleeve caps (0.5% arb / 2% directional),
and the fee-clearance rule exist; no adapter, no odds feed, no arb engine.

- **Plan A (default): Robinhood Agentic event contracts.** On their stated
  roadmap ("event contracts, futures, and more"), NOT in the current 54-tool MCP
  surface. **Monthly re-check:** re-run the read-only tool inventory via the
  spike client (`rh_mcp.py tools`) and diff against the known 54-tool list —
  event-contract tools appearing is the trigger to design the build.
- **Plan B (fallback): Kalshi direct API.** Only if Plan A has not shipped by
  the time the equity leg passes the paper gate AND attribution justifies
  funding the sleeve.
- **Build trigger regardless of venue:** paper gate passed + a human funding
  decision (deposits are human-only, CLAUDE.md). Directional strategy builds
  first — it reuses the research pipeline end to end; the arb engine builds
  last, because it needs empirical fee-schedule and order-book validation that
  only live venue access provides.

## Mirror integrity fix (2026-08-20): no marker, no principal signal

**The finding:** trump_mirror_tdp delivers its own commentary between genuine
relays, mislabeled as Trump content. Verified live 2026-08-20: 24/24 recent
@TrumpDailyPosts posts were its own replies/commentary/promos — including
market-shaped claims (Nike, Iran, "tariffs paused") — and 33/33 of the day's tdp
deliveries in the audit log were headerless junk. Two reached research
2026-08-19 (~$4.24 of Opus spend catching what ingest should have discarded
free); one mapped to a Ted Cruz post.

**The fix, at ingest (all verified against live posts before pinning):**
- `SourceConfig.required_marker` (regex, per mirror). tdp: the
  "Donald J. Trump Truth Social Post [time] [date]" header — zero of 24 recent
  posts carried it, so ALL current tdp output is rightly discarded; if the
  genuine-relay format has changed, the 2-trading-day mirror-silence warning is
  the watchdog that sends a human to re-verify. ttox: the "( TS: ... )" stamp —
  25/25 recent posts carried it.
- Markerless mirror content is classified "other": logged to the MIRROR's own
  credibility record (reason: required marker absent), never emitted as a
  principal signal. Free at ingest.
- **Bonus live bug found and fixed:** the dedup normaliser's TS-suffix regex
  required "(TS:" with no space; the real format is "( TS: Aug 19 2026, ...)"
  — cross-mirror dedup was silently broken on every real ttox post. Regex now
  space-tolerant; test pins real-format ttox and headered tdp of the same Truth
  deduping to one signal.
- **Flag separation:** `CredibilityTracker.record_report` takes
  `delivered_by`; a manipulation flag on a mirror-delivered signal lands on the
  CHANNEL's record, not the principal's (both keep the report in their
  denominators). The research prompt now shows a DELIVERY CHANNEL RECORD block
  when the deliverer has one — a mirror that previously delivered mislabeled
  commentary is a fact about the delivery, not about Trump.
- Themes: added `iran` to research_prefilter_themes. NOTE: `sanction` and
  `oil` were ALREADY present — the missed Iran post ("tremendous economic
  consequences") matched neither; also observed that the `economy` stem did
  NOT cover "economic" (stem-prefix matching) — ruled 2026-08-20: stem changed
  to `econom`, covering economy/economic/economics.

## Daily cost visibility (2026-08-20): spend is a number you read

- **Health** gains an `est. research cost` line: today / yesterday /
  month-to-date, summed from `est_cost_usd` across audit records (entry passes
  once per decision_id, exit reviews per record, rejected passes included —
  they were paid for; pre_filter records contribute zero by construction).
- **Tripwire:** `daily_cost_warning_usd: 10` in orchestrator.yaml. The CostMeter
  (seeded from the log at startup so a restart cannot reset it) writes ONE COST
  line to run.log the first time a day's cumulative estimate crosses the
  threshold — once per day, not per pass; a mid-day restart may re-warn once.
- **Attribution** report adds a month-to-date research cost total line above the
  per-class window costs.

**Real unit costs observed (2026-08-19, from manual console math — the audit
estimates only start 2026-08-20, so yesterday reads $0.00 in health):**
~$2.12/pass Opus Class 1 (two passes, $4.24 total). Sonnet-tier unit cost still
unmeasured — no Class 2/3 passes have run yet. Pending Friday's console
reconciliation, which is also when the research.yaml pricing table gets replaced
with real numbers.

## Cost reduction pass (2026-08-20): three efficiency changes, no strategy changes

1. **Search budget:** `web_search.max_uses: 2` (was 5). And an honest deviation
   from the requested per-result truncation: the docs (verified 2026-08-20) say
   search-result content is ENCRYPTED and must be replayed byte-identical or
   the request 400s — per-result truncation is impossible by API contract. The
   implementable form: `replay_results_in_report: false` ELIDES the opaque
   payloads from the report-phase replay entirely, keeps the model's own
   written analysis, and appends an explicit marker stating what was cut. One
   config switch restores full replay. Search payloads were 79K–119K input
   tokens/pass; they are now paid for once (search phase), not twice.
2. **Prompt caching:** `cache_control: {type: ephemeral}` on the system block
   of every research, report, and triage request (list-of-blocks form per
   docs; the tools→system hierarchy means the breakpoint covers tools too).
   **Verified live:** two-call probe showed `cache_creation_input_tokens: 1329`
   then `cache_read_input_tokens: 1329`. Cost estimates now price cache tiers
   properly (writes 1.25x, reads 0.1x input rate).
3. **Haiku triage gate:** one forced-tool claude-haiku-4-5 call before any full
   pass — "plausibly tradeable, verifiable, non-stale thesis?" No → stage
   `triage` rejection (~$0.02, reason preserved, own est_cost stamped), full
   pass never starts. Yes → proceeds EXACTLY as before, gate cost folded into
   the pass's record. Counts toward the COST meter, never the 40-pass budget
   (replay excludes triage rejections like pre_filter). Signal content is
   fenced as data; the gate's output has no authority beyond the yes/no —
   smuggled extra fields fail the closed schema, which FAILS OPEN to the full
   pass, as does every other gate failure (a broken gate must not stop the
   research layer).

**Levers held in reserve — revisit ONLY if Friday's console reconciliation is
still uncomfortable:**
- research budget cap 40 → 15 passes/day
- Sonnet-everywhere (move Class 1 off Opus)
- Batch API for Class 2/3 (they carry 45-day lags; batch latency is free money)
- exit-review cadence stretch (24h → 48h)

## Fractional shares (2026-08-20): the bottom confidence band survives a $10K account

Human-authorized design change. Rationale: intended initial live funding ~$10K
means a ~$9K equity sleeve; a 1% (confidence 55-70) position is $90, which
rounds to ZERO whole shares of most large caps — whole-share rounding was
silently deleting the bottom band.

What changed:
- **Schema:** equity `quantity` is now `ShareQuantity` — exact Decimal, >0, max
  9 decimal places (Alpaca's documented fractional maximum). Options and event
  `contracts` remain whole ints: fractional applies to equity shares only.
- **Money path is float-free:** gate arithmetic, position tracking (Position
  quantity/reserved_close/pending_open_units are Decimal), sell-to-close
  validation, partial-fill settlement, exit tracking, and broker replay
  (`position_from_broker` no longer truncates — that int() would have dropped
  fractional holdings on every restart).
- **Rounding is always DOWN, to the venue's precision:** `BrokerAdapter.
  equity_quantity_step` defaults to 1 (whole shares — safe for venues with
  unproven fractional support); `AlpacaAdapter` sets 1e-9 per docs. Order
  construction quantizes capital/price down to the step, so notional can never
  exceed sized capital.
- **Minimum notional floor:** `equity_sleeve.min_order_notional_usd: 5` in
  risk_limits.yaml. Below it: typed rejection `below_min_notional`, enforced in
  the gate AND at order construction. Scope: OPENING EQUITY orders only —
  closes are risk-reducing and never floor-blocked; the prediction sleeve's arb
  strategy is micro-unit by design ($10K account -> 0.5% of the $1K sleeve is
  $5 — a floor there would kill arb entirely). Exactly at the floor passes
  ("below" is explicit; Constraint #6 resolves ambiguity, not stated rules).
  The old `size_below_one_unit` construction rejection is subsumed by
  `below_min_notional`.
- **Broker reality check (docs verified 2026-08-20, not memory):** Alpaca
  fractional supports market, LIMIT, stop & stop-limit orders, time_in_force=
  day ONLY, qty/notional up to 9 decimal places, per-asset fractionable=true
  flag. Our posture already sends every order as a limit with TIF=day, so
  **no conflict with the marketable-limit posture — no ruling was needed**.
  The adapter still guards: a fractional qty with a non-day TIF raises locally
  before the wire. A non-fractionable asset order is rejected broker-side and
  logged like any broker rejection (no pre-check lookup built; revisit if it
  ever actually fires in paper).
- **Tests (+18, suite 693 passed 3 skipped):** hypothesis machine and the
  straight-line overdraw property now mix fractional and whole quantities
  (buying power never negative, no net short, reserved closes never exceed
  held); fractional oversell / double-close rejected; exact-Decimal
  reservation and partial fill; floor edges (below/at, close exemption,
  prediction-sleeve exemption); schema refuses 10dp and fractional contracts;
  wire format sends "0.5" (no trailing zeros, no E-notation); whole-Decimal
  qty doesn't trip the TIF guard. The $1 live-probe order in
  test_execution_integration became $6 (it was our own dust now).

## Allocation change (2026-08-21): 90/10 -> 100/0 until a prediction venue exists

Human-authorized. Rationale: the 10% prediction sleeve has NO execution path
(Robinhood event contracts are roadmap-only, Kalshi is Plan B behind the paper
gate) — reserving NAV for an unexecutable sleeve is dead capital at any
funding size.

- **Config only:** `portfolio.sleeves` in risk_limits.yaml is now 1.00 / 0.00.
  The 90/10 design target, drift/rebalance logic, prediction-sleeve caps, and
  the whole Kalshi-facing order schema are untouched and still tested —
  prediction-mechanism tests are pinned to the design weights via
  `design_limits()` in test_risk_gate (the live config would make them
  degenerate: every cap x $0 sleeve = 0).
- **Zero-sleeve behavior verified, no div-by-zero anywhere:** sleeve math only
  ever multiplies by the weight (sleeve_nav = NAV x weight) and divides by NAV.
  A prediction order under 100/0 dies at the position cap with a typed
  `max_single_position_exceeded` — rejection, never a crash. Sizing on a $0
  sleeve returns capital 0 / no-trade. Attribution with zero deployment renders
  "no resolved outcomes yet" (return_pct is None — never 0% or NaN).
- **Operator rendering:** health/startup `describe()` gained a sleeves line —
  `sleeves: equity 100%, prediction 0% (inactive)` — so the zero reads as a
  deliberate ruling, not dead capital.
- **Restore trigger:** 90/10 comes back when a prediction-market venue ships
  (Plan A: Robinhood event contracts via the monthly MCP tool-inventory
  re-check; Plan B: Kalshi). Flipping back is a human ruling on the
  stop-and-ask list, same as this change. CLAUDE.md § Portfolio Structure now
  states both allocations and the trigger.
- Suite: 699 passed, 3 skipped (+6: zero-sleeve gate rejection typed, equity
  sleeve spans full NAV, drift math never divides by weight, zero-sleeve
  sizing no-trade, zero-deployment attribution render, describe() inactive
  marker). Sleeve-dependent dollar expectations retargeted (2,500 capital /
  17 shares at the 2.5% band, 5,000 at the 5% cap).

## Incident (2026-08-24): elision 400 killed every research pass since 08-20

Surfaced by the operator's health reconciliation: budget showed 5 of 40 spent
but cost was $0.01. Neither counter was wrong — the 5 were RESEARCH-stage
`upstream_error` rejections: triage said yes (the $0.01 is five Haiku yeses),
then every full research call 400ed. One more on 08-20; Friday 08-22 attempted
none (all signals pre-filtered), so the first real exposure was Monday.

- **Root cause:** `_elide_search_results` stripped exactly `server_tool_use` +
  `web_search_tool_result`. But our tool version `web_search_20260209` runs
  search through DYNAMIC FILTERING (docs re-verified 2026-08-24): searches
  execute inside code execution, so transcripts also carry
  `code_execution_tool_result` blocks — whose paired `server_tool_use` we
  stripped, orphaning them -> 400 on the report-phase replay, every time.
- **Fix:** elision is now a keep-list — replayed assistant turns contain ONLY
  plain `{type, text}` blocks (citations dropped too: their encrypted_index
  points into elided results); every `*_tool_result` counts as an elided
  payload for the marker. 400-proof by construction against future block
  types. **Live-validated this time** (the omission that caused the incident):
  a real search+report pass with elision on returned a structured report,
  $0.034 on the Sonnet tier.
- **Visibility fix:** upstream_error rejections now fire an error_sink ->
  run.log ERROR line -> health "last error". Three sessions of 100% research
  failure had shown "last error: none on record".
- **Counters verdict:** budget counts ATTEMPTS (slot spent at try_spend,
  replay-consistent, the tighter reading); cost counts actual estimated spend.
  Both correct; unchanged. The five 08-24 signals are in the seen set and will
  not be retried (Class 1/2 signals were stale within the session anyway).
- **Future lever:** `web_search_20260318` adds `response_inclusion: "excluded"`
  — the API drops consumed search/code pairs from the response server-side.
  Cheaper than client elision (they never come back at all); needs its own
  live validation before adoption.

## Standing reminders

- **Verified pushes (2026-08-21 ruling):** "pushed to both hosts" means CHECKED,
  not attempted — after every push, `git rev-parse HEAD` must match
  `git ls-remote vps refs/heads/main` (and origin). The droplet checkout at
  /home/agentic/Agentic still needs its `git pull` — the bare repo alone is not
  what the service runs.

- `PAPER_MODE=true`. Live needs two variables, both set by a human, and the agent must
  never set, suggest setting, or write code that sets either.
- The kill switch resets manually or not at all.
- Adding a signal source needs explicit human approval, per source.
- `data/` is gitignored and holds the audit trail and session state. It is not backed up
  by the repo and belongs in a backup routine.
