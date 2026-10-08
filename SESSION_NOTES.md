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
- [ ] Options data feed: paper runs on Alpaca's free `indicative` feed (delayed
      trades, modified quotes). Before options trade real money, confirm an
      OPRA subscription and feed=opra — selection gates fed by modified quotes
      are gates fed by fiction.
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

## Options execution build (2026-08-24): leverage earned by timing specificity

Human ruling: equity-only was build-order sequencing, not strategy. Shipped in
one pass, guardrails WITH it. Suite 732 passed 3 skipped (+29 tests).

**The path a levered trade takes:** research report gains
`catalyst_within_horizon` (nullable-but-required, present+description; null ==
false == no leverage) -> pipeline routes: catalyst + long/short_via_puts ->
chain fetch (AlpacaOptionsChain: /v2/options/contracts for OI + universe,
/v1beta1/options/snapshots for bid/ask/IV/greeks, joined on OCC symbol, never
raises) -> deterministic OptionSelector (sizing/selection.py, total ordering:
delta-gap to band midpoint, OI desc, spread, strike) -> gates in order: expiry
floor (days>=14/weeks>=60/months>=180), confidence->|delta| band (55-70:
0.70-0.85 deep ITM per ruling #3; 70-85: 0.60-0.75; 85+: 0.50-0.65; absolute
floor 0.45), OI>=500, spread<=10% of mid, chain-internal IV percentile<=90 ->
limit at mid (rounded up), contracts floored into the HALVED options table,
gate enforces the 20% aggregate premium cap as built.

- **Ruling #1:** options stops = equity stop fraction on premium (tighter).
  Revisit only with attribution data showing premium-stops eating recoverable
  winners.
- **Ruling #2:** long + catalyst + selector-fallback -> equity at the FULL
  equity table (re-sized; no phantom half-size penalty). Puts + fallback ->
  no trade, typed construction rejection with the reason.
- **Ruling #3:** 55-70 band deep ITM [0.70-0.85] confirmed — low confidence +
  leverage is the dangerous quadrant.
- **ExpressionSnapshot on every routed decision:** chosen contract (symbol,
  delta, IV percentile, OI, spread) or fallback reason + NEAR-MISS (occ, delta,
  OI, spread, killed_by) so "are the liquidity gates too tight?" is answerable
  from records (addition #5).
- **IV gate limitation (addition #4, documented in selection.py):**
  chain-internal percentile cannot detect a uniformly panic-priced chain —
  everything elevated together passes. Accepted as the only history-free
  method. FUTURE LEVER: IV-rank-vs-history if attribution shows systematic
  overpaying on entries.
- **Exits:** options ride the existing stop/leash/review machinery (premium
  marks via option_mid; multiplier-correct P&L end to end) plus EXPIRY_CLOSE
  at T-minus-5 (config close_before_expiry_days), quote-independent, permitted
  under a tripped kill switch. NOTE: with current config the leash (7/45/120d)
  is always shorter than the horizon's expiry floor (14/60/180d), so the time
  stop normally fires first — EXPIRY_CLOSE is the backstop for stalled exits
  and config drift, not the common path.
- **Live-validated (both halves):** real AAPL chain -> 2,374 contracts joined,
  greeks/IV on 1,855, selector picked AAPL260911C00305000 (delta 0.657
  in-band, OI 2,764, spread 4.9%, IV 7th pct) and the order constructed (not
  placed). LLM round trip per CLAUDE.md § LLM Request-Path Changes: real
  search->report with the new schema returned catalyst present=false with a
  dated reason (no NVDA earnings in horizon), no_position, confidence 8 —
  validated end to end ($0.11, Sonnet tier).
- **Topology:** OptionQuote is a Protocol in sizing.selection; the concrete
  dataclass lives in execution.options_data. No edge in either direction; the
  DAG test stays honest.

## Hardening + funnel efficiency (2026-08-25): three ingest rules

1. **Invisible-character sanitization, structural:** `Signal.__post_init__`
   strips Unicode Cf (zero-width space/joiner/non-joiner, word joiner, BOM,
   soft hyphen, directional marks) + variation selectors from `content` before
   it can enter ANY prompt (triage, research, review — exits now carry
   sanitized content too). `raw_content` keeps the verbatim bytes; `sanitized`
   flag + `invisible_stripped` count ride the Signal and the audit
   SignalSnapshot. Rationale: ttox payloads carry structured zero-width runs
   (4/4 scored reports flagged them; one pass wasted effort decoding one) — a
   covert channel into LLM context by construction. In-constructor means no
   scanner can forget.
2. **no_instrument (nolimitgains only):** `require_instrument: true` in
   signals.yaml; a forward_call with no extracted ticker is recorded as a
   PRE_FILTER stage rejection with code `no_instrument` and never spends
   triage or a pass — his genuine calls always name instruments. Scoped per
   source: trump_posts legitimately trades ticker-less via sector effects.
3. **bare_link (trump_posts):** `bare_link_min_chars: 120`; a THEME-MATCHED,
   ticker-less post whose content minus URLs is under the threshold is code
   `bare_link` — a headline with a link is not a thesis. INTERPRETATION
   surfaced for ruling: the condition as specified doesn't require a URL to be
   present, so a one-line themed brag with NO link also filters (Constraint #6
   tighter reading). If the intent was link-posts-only, say so and the rule
   gains a has-URL condition. Unthemed short posts stay with the theme rule's
   own rejection (codes stay precise for later analytics).

All three are deterministic, run before triage/budget, cost nothing, and write
readable rejections. Suite 749 passed 3 skipped (+17).

## Cost architecture (2026-08-25): two-stage research, per-source tiers, search budgets

Precedes the breadth expansion by design: widen the funnel only after making
each pass cheaper. Suite 755 passed 3 skipped.

1. **Per-source verification tiers:** SourceConfig.research_tier names a
   research.yaml tier; unset falls back to the signal class. trump_posts and
   nolimitgains stay on the Opus flagship (adversarial prose); structured-
   callout sources declare the Sonnet tier. Typos fail at STARTUP (bootstrap
   validates every declared tier via tier_for), not at 09:31.
2. **Two-stage research:** every full pass runs the Sonnet screen first
   (research.yaml `screen`: sonnet/medium, 1 search, graduation_confidence 55).
   no_position or confidence <55 ends there — that report IS the record and
   rejections get cheap (~$0.08 live vs ~$2.12 single-pass Opus). Actionable
   reports graduate: verification on the source tier with the screen draft
   fenced AS DATA in the prompt; THE VERIFICATION REPORT is what sizes, both
   reports + both costs persist (DecisionRecord/StageRejection
   screen_research + screen_est_cost_usd; est_* totals bill both stages).
   Stage-two failure = rejection, never a fallback to the unverified draft.
   Credibility sees only the final report, never a superseded draft.
3. **Search budgets by class:** per-tier max_searches — class_1 verification
   inherits the global 2 (verification burden); class_2/class_3/exit_review
   and the screen cap at 1. NOTE: screen=1 was my choice (unspecified in the
   ruling) — flag if the screen should search more.

**Live validation (CLAUDE.md § LLM Request-Path Changes):** the screen shape
ran live three times — every scripted probe thesis was HONESTLY refused
(no_position at conf 62/92/55, ~$0.08 each; the model cannot be scripted into
graduating, which is itself the mechanism working). The verification shape ran
live in isolation with a long-61 draft: Opus searched, then OVERRODE to
no_position at 73 ($1.00) — the exact override path the design exists for.
The graduation control flow between the two shapes is deterministic Python
under test (13 new tests incl. override-wins and no-fallback-to-screen).

**Expected economics:** Class 1 rejection ~$2.12 -> ~$0.08; Class 1 trade
~$2.12 -> ~$1.10-1.40 (screen + Opus verify, 1+2 searches); Class 2/3
unactionable ~halved by the 1-search cap.

## Breadth expansion round 1 (2026-08-25): full roster, unusual_whales, source caps

Human-authorized per the stop-and-ask list. Suite 762 passed 3 skipped.

1. **Class 2 full roster:** congressional watchlist is now EMPTY = every filer
   (a non-empty watchlist still narrows; mechanism kept). Per-member
   credibility from zero via `credibility_key` metadata
   (congressional_disclosures/<member>) — research priors, report
   denominators, and OUTCOMES all key per member, so attribution ranks filers
   empirically. Probe of the live full feed (1000 rows, 171 days, 57 members):
   ~41 raw/week -> ~12.4 survivors/week at current filters ($15K floor, 75-day
   lag, unheld-sale rule). Spend at the new two-stage economics: most
   congressional signals screen out on Sonnet (~$0.08-0.25 each) -> ~$1-3/wk.
   NOT noisy — the $50K floor alternative (would cut to ~2/wk) was NOT needed
   and NOT applied.
2. **unusual_whales (Class 1):** free public X account; require_instrument
   true, research_tier class_2 (Sonnet verification — structured callouts),
   daily_research_cap 3, thesis-input-only, credibility from zero. Config
   notes it as the FREE TASTE of the options-flow class; the UW API ($150/mo)
   stays queued behind attribution proving flow-derived signals convert.
   monthly_cost 25 (X read costs at UW volume; estimate, reconcile against
   the console).
3. **Per-source daily caps (cost governance):** SourceConfig
   daily_research_cap; beyond it, signals record as pre_filtered code
   `source_cap`. unusual_whales 3/day, congressional roster 5/day (the ruled
   defaults; volume probe says congressional averages ~1.8/day so the cap
   binds only on batch-filing bursts — recommended values unchanged). Counts
   seed from the audit log at startup (restart cannot reset a cap) and roll
   at the UTC day boundary.
4. **Lag analysis (report-only):** the feed's lag distribution is BIMODAL —
   63% within 45 days, 36% in a 101-150d batch-filing cluster, ~nothing at
   76-100. Extending 75 -> 90-100 would admit ~zero additional signals: keep
   75. Zero lag kills in paper so far (watchlist era had little flow); the 2
   researched congressional disclosures show priced_in reasoning handling
   82-day-old trades correctly.
5. **Proposals awaiting ruling (in the 2026-08-25 report):** fintwit — Citrini
   (strong), OptionsHawk (probation), TraderStewie (marginal), Brandt/Kobeissi
   avoid; 13F — Appaloosa, Duquesne (theme-level reads), Altimeter, Pershing,
   TCI; Scion DEREGISTERED Nov 2025, do not wire. Nothing wired without
   confirmation.

## Breadth round 2 (2026-08-25): rulings on the round-1 proposals

All five rulings received and wired. Suite 769 passed 3 skipped.

1. **citrini (@Citrini7) — wired at class_2 cadence** per the ruling: medium
   latency, hourly polling, not 60s. require_instrument, research_tier
   class_1 (Opus verification — prose caller), daily_research_cap 3,
   credibility from zero. Build note: the trade-call classification path
   (classify, discard retrospectives to the credibility log, emit forward
   calls only) moved from the Class 1 scanner to the shared base so the
   class-2 scanner runs it for classification-flagged sources; class-2
   placement makes priced_in_analysis MANDATORY for his calls (an
   hourly-polled call can be hours stale), and the class-2 prompt guidance
   now distinguishes trade calls from disclosures.
2. **optionshawk (@OptionsHawk) — the system's FIRST PROBATION source.** New
   `probation: true` source flag: signals classify, research, and accrue
   credibility exactly as normal, but sizing short-circuits to zero with
   rejection code `probation`. The check runs AFTER the honest verdicts
   (no_position / below_floor keep their own codes), so probation records
   are exactly the trades that WOULD have happened, each carrying the
   research report and the proposed size. Opus tier, require_instrument,
   cap 3/day.
   **REVIEW TRIGGER: 2026-10-24 → 2026-11-23 (60-90 days from wiring) — a
   promote-or-drop ruling on the accumulated probation records' hit rate.
   Query: stage_rejections with code `probation`, join their research
   snapshots against subsequent price action.**
3. **Fintwit dispositions (recorded in signals.yaml against re-litigation):**
   @traderstewie DEFERRED (real public entries since 2009 but paid-room brag
   volume demands a stricter classifier first); @PeterLBrandt REJECTED
   (documented deletion of failed calls; forced retraction of an "audited"
   claim); @KobeissiLetter REJECTED (index-level engagement bait; published
   scam allegations).
4. **13F round 1 — wired:** Appaloosa (Tepper), Altimeter Capital Management
   (Gerstner), Pershing Square Capital Management (Ackman), TCI Fund
   Management (Hohn). TCI sits at ~$53B, above the $50B proposal ceiling —
   admitted by PER-FUND OVERRIDE (the chosen mechanism: the ceiling itself
   stays at $50B for future proposal rounds; less-risk reading of the
   raise-or-override ruling). Fund names chosen to substring-match EDGAR
   filer display names, which is how the fetcher attributes filings.
   **Duquesne DEFERRED pending a theme-cluster consumption mode — QUEUE
   ITEM, not a build.** Scion recorded DEREGISTERED (Nov 2025) / do-not-wire.
5. **unusual_whales monthly_cost 25 — approved**, no change needed.

Wiring: both new X sources route through the shared XRecentSearchFetcher
(one seen-set, post ids globally unique); production router + seen-set in
orchestrator/__main__.py updated; probation set wired bootstrap → pipeline.
Feed-cost expectations updated (class_1 $45, class_2 $35).

## Day-one expansion fixes (2026-08-26 rulings)

Origin: day-one review. The full-roster first poll flooded 952 rows; 268
filter-survivors hit the 5/day cap and were permanently sealed by dedup
seeding — including a $1M-5M Pelosi Bloom Energy purchase reported 08-21,
the highest-conviction disclosure the system had seen. Separately, UW's 12
fetched posts left zero persistent trace (all classified away in-memory),
and the fixed 15-minute X lookback silently lost all overnight posts.

1. **source_cap unseal:** `researched_external_ids()` now excludes
   source_cap rejections — the cap must never permanently discard signals it
   didn't pay to evaluate (same exclusion the cap count itself makes). Capped
   signals re-emit at the next startup and compete for that day's slots.
   **Disclosed deviation (pending veto): the exclusion also covers
   `upstream_error`.** Post-deploy verification found the Pelosi Bloom Energy
   tranches were never among the capped 268 — both died 2026-08-24 to the
   elision-400 bug and were sealed by their upstream_error records, so the
   ruled fix alone could not deliver the demanded research pass. A failed
   research call produced no verdict — the same "nothing was spent" principle
   the ruling stated. Total collateral unsealed: 7 records (4 Pelosi
   disclosures incl. both BE tranches, 3 stale Trump posts that die free at
   the theme prefilter or cheap at triage).
2. **Staleness prefilter:** class-2 `max_report_age_days: 14` —
   disclosure->today staleness, distinct from the trade->disclosure lag rule.
   Steady-state report ages are 0-1 day, so it only bites backfill floods,
   which now die free at prefilter instead of consuming cap slots. Strictly
   above 14, matching the sibling rules' documented convention; unparseable
   dates fail open.
3. **Session-gap X lookback:** first-poll lookback = gap since the newest
   audit record, floor 15min, cap 24h (`first_poll_lookback_seconds` in
   orchestrator/ops.py, LOOKBACK line in run.log). max_results raised to the
   API's 100 so a gap-sized first poll doesn't truncate the backlog to 25.
4. **Classification visibility:** per-poll CLASSIFY counts in run.log
   (e.g. `CLASSIFY unusual_whales forward_call=0 other=3`), "other" posts now
   land in the credibility log with markers, and the credibility log persists
   to `data/credibility.jsonl` — fetched-but-discarded is reconstructable, so
   a misclassifying rule and an organically quiet account no longer look
   identical.
5. The 08-25 18:54 restart with no STOPPED marker was the human's bounce —
   confirmed, no investigation.

## Dispatch weight for cap-constrained ordering (ruling 2026-08-26)

Origin: BE "re-capped at today's open" — actually the human's mid-session
bounce re-emitting the backlog into an already-spent daily cap (the capped
order matched the simulation exactly, BE first). The review surfaced the real
structural gap: within a poll batch, dispatch order was feed order
(newest-report-first), so re-emitted older signals lost to every newer
arrival, and the 14-day guillotine could kill a starved signal unevaluated.

- **`dispatch_weight = log10(amount_range_max) − report_age_days / 7`** —
  one order of magnitude of disclosed size is worth one week of report age,
  so a $1M-5M disclosure from 5 days ago outranks a $1-15K one from today;
  same size → fresher wins; same day → larger wins. Computed by the class-2
  scanner from structured feed fields (amount range, report date), NEVER
  content; 0.0 for every other source (simple global version, per ruling).
- Sort key: (class priority, −dispatch_weight, observed_at, arrival order).
- **Ordering-only invariant, tested explicitly:** the weight is read exactly
  once (the loop's dispatch sort) and cannot touch caps, sizing, budget, or
  the gate — two signals identical except weight produce identical sizing.
- Fail-safes err toward dispatch: unparseable amount scores at the $15K
  prefilter floor; missing report date counts as age 0.
- No aging boost, per ruling: the staleness guillotine bounds waiting, and
  old small signals should lose.
- **`aged_out_capped`:** a signal killed by the report-staleness rule that
  previously lost a slot (source_cap, or budget-deferred this process) gets
  its own rejection code instead of plain pre_filter — that record means the
  cap actually cost an evaluation, a tuning signal the human reads directly.
  Prior caps seed from the audit log across restarts (capped_external_ids);
  prior budget-defers are known within a process only (deferrals write no
  records, by design).

## Market context: 200-DMA distance + below-streak (2026-08-26)

Two new lines in the deterministic market-context block, pure arithmetic over
the daily bars already fetched (zero LLM cost, data-fenced as always):
distance from the 200-day moving average (%) and consecutive sessions below
it. A streak that runs past fetched history renders as "N+ (fetched-history
limit)", never an understated exact count; under 200 sessions of history
renders "unavailable". Prompt guidance added beside the IV-crush note: a
quality name well below its 200-DMA may support a mean-reversion reading,
but the model must distinguish temporary dislocation from structural decline
— cite evidence either way, and this context alone is never a thesis.

## Defect fix: congressional passes ran without market context (2026-08-27)

Root cause of the BE record defect (decision 5362628673f34e84): a metadata
key mismatch — Quiver's structured field is `ticker`, singular; the context
builder and the prompt's extracted-tickers line read `tickers`, plural. So
EVERY congressional research pass since market context shipped (08-19) ran
context-less, and the model reasoned structurally ("price data unavailable").
Unnoticed until now because congressional volume was near zero before the
full roster. 13F remains context-less by design (no single extracted ticker).

- Fix at the producer: the class-2 scanner now stamps `tickers` from the
  Quiver `ticker` field — one uniform consumer contract, no fallback keys.
- New context line for lagged signals: **change since the disclosed trade
  date** (anchor = first session on/after `transaction_date`) — the number
  the priced-in analysis is actually about, stated instead of inferred.
  Missing/future anchor renders unavailable; signals without a trade date
  get no line.
- One-time human-authorized re-evaluation of both BE tranches (they were
  sealed by the defective verdicts): run through the production research
  path with context, recorded normally, with CorrectionRecords naming the
  superseded originals. Execution deliberately disabled on the re-run —
  the verdict is the deliverable; any trade is a separate human decision.

## Mechanical disclosure follower (human ruling 2026-08-27)

The controlled experiment: deterministic diversified copying of congressional
purchase disclosures, NO LLM in the path, alongside the judged system so
attribution says which shape produces alpha. All five design recommendations
approved + two amendments (sector slot cap via the existing sectors.yaml;
sleeve-level circuit breaker).

- **Allocation 75/25/0** (CLAUDE.md § Portfolio Structure amended). The
  mechanical sleeve is its own `Sleeve.MECHANICAL`: equity orders carry a
  sleeve tag (schema still cannot express writes/shorts/margin), positions
  key by (sleeve, symbol) — overlap is separate positions, a judged exit can
  never sell mechanical shares. Gate enforces the mechanical cap table
  (risk_limits.yaml `mechanical_sleeve`): 5% single-position backstop, own
  daily deployment budget (15% of sleeve), own sector budget (30% backstop),
  25%+drift allocation ceiling. Weight 0 switches the whole arm off.
- **Funnel identity (the ruling's hold-the-line):** the engine qualifies with
  the SAME ResearchPreFilter INSTANCE the judged loop dispatches with —
  identical by construction, tested by object identity. On top: purchase-only,
  parseable ticker, tradeable-equity check (Alpaca /v2/assets:
  active+tradable+fractionable, fails CLOSED).
- **Slots:** 30 equal-weight slices (sleeve NAV/30), 6/filer, 8/mapped-sector
  (unmapped names are singletons, the sectors.yaml convention), one slice per
  name. Qualified-but-no-slot writes `mechanical_capacity`; a tripped breaker
  writes `mechanical_halted` — both excluded from dedup sealing, like
  source_cap.
- **Exits: time only, 365 days, no price stop** — the stop is the slice size.
  ExitReason.MECHANICAL_TIME_EXIT. Kill switch halts mechanical entries
  globally; closes still pass.
- **Circuit breaker:** sleeve value (virtual cash ledger anchored at first
  entry + open mechanical market value) down >25% from its OWN high-water
  mark halts new entries; positions ride. Sticky, persisted in
  session_state.json (mechanical_halted — human reset only), surfaced in
  health's mechanical line.
- **Audit:** entries are DecisionRecords with research=None (no fabricated
  verdicts), sizing.strategy="mechanical", a MechanicalSnapshot (filer,
  ticker, amount, report date, ruleset_version 2026-08-27.1). Mechanical
  records NEVER seal signals for the judged arm — independence is what makes
  the comparison valid. Restart: broker positions split between sleeves from
  the audit log (broker authoritative on totals); ledger/HWM/halt persist in
  session state; own deployed-today replay.
- **Attribution:** its own bucket (never a signal class) + measured overlap
  symbols in the weekly report.
- Suite green throughout; judged-sleeve tests recalibrated to the 75% sleeve
  (13 shares where 17 were, 1,875 where 2,500 was, etc.).

## Day-one mechanical incident + the three fixes (2026-08-27)

**The reported defect was not one.** Health showed 4 unmanaged positions,
"0 positions / ledger unseeded", and deployed-today $3,333 — all from a
snapshot taken in the ~30s window between the broker filling four mechanical
orders (17:53:23) and the loop's settle tick (17:53:53). Records were complete
throughout (4 DecisionRecords + 4 FillRecords, $3,287.05 matching the cash
delta); a fresh process attributes all four to the mechanical sleeve with the
ledger correct. The apparent contradiction is the fingerprint:
`mechanical_deployed_today` replays from APPROVALS, positions/ledger from
FILLS. Steady state agrees; mid-flight does not.

**The real latent bug it revealed:** a process dying inside that window leaves
an approved decision with no fill — so every fill-keyed replay skips it (no
stop, no time exit, ledger blind) while the broker holds the shares. Small
window, permanent consequence. Three fixes shipped:

1. **Startup settlement recovery** (`orchestrator/recovery.py`, runs in
   preflight before state is seeded, both sleeves). Entry orders now carry a
   durable `client_reference` = their decision id (the old client_order_id
   embedded a per-process launch token — unreconstructable after a crash);
   exits already log their broker order id. At startup every unfinished order
   is asked about: terminal+filled → write the missing FillRecord (+ partial
   note); terminal+unfilled → write the release; still working → left to the
   existing orphan sweep; **no answer → left untouched, loudly** — "cannot
   tell" is never recorded as "did not fill". The gate is NOT re-settled: it
   seeds from broker cash/positions, which already include the fill.
   `MechanicalEngine.replay` reconstructs an unseeded ledger as
   allocation-minus-spent for the crash case.
2. **Pending settlement vs unmanaged** (health): an approved order with no
   recorded fill now renders under its own heading and is EXCLUDED from
   unmanaged exposure — a mid-flight snapshot reads as transient, and a line
   that persists across startups is the actionable alarm (recovery could not
   reach the venue).
3. **`entries_enabled` switches** for both arms (`equity_sleeve` and
   `mechanical_sleeve` in risk_limits.yaml). False stops new opening orders at
   dispatch — judged signals are recorded (`entries_disabled`) with NO research
   bought, mechanical qualifiers are recorded (`mechanical_disabled`) — while
   exits and time exits keep firing. Neither code seals a signal, so backlogs
   return when the switch goes back on. This exists because halting an arm
   previously meant editing allocation weights or racing a running process's
   session_state.json writes.

Also fixed: `logger` was used but never defined in execution/alpaca.py — a
latent NameError on the `tradeable_equity` failure paths.

## Congressional feed: instrument type, and the catalyst-bypass ruling (2026-08-27)

**The defect.** Quiver has always carried `TickerType` (`OP` / `Stock Option`)
and `Description` (free text: side, strike, expiry, contract count). We
consumed neither. Every congressional disclosure this system has ever
researched rendered as bare `transaction: Purchase` — 963 distinct signal
contents in the production log, **zero** mentioning options — including
decision `5362628673f34e84`, the Pelosi BE tranche, which is in fact 100 call
options struck at $100 expiring 2027-06-17.

Live feed at the time of the fix (1000 rows, 2026-03-04 → 2026-08-25):
`TickerType` Stock 499 / ST 488 / OP 12 / Stock Option 1; `Description`
non-null on **50 rows (5%)**; 14 option rows (7 purchases, 7 sales); 3 in the
14-day window the report-staleness prefilter admits, all Pelosi calls.

- **Detection is the OR of type and text.** A BAC row of 2026-07-19 reads
  "CALL OPTION CONTRACTS." under `TickerType: ST` — the type alone hides an
  option. The text detector is the word `option`, not a loose call/put match,
  so "SHARES PUT INTO TRUST" stays stock. An unknown type with no description
  renders `instrument: not stated by the filing` — **never** inferred as
  equity.
- **Terms are extracted, never invented.** Three description formats parse
  (Pelosi's prose, Gottheimer's semicolons, the Senate form's labels);
  anything else degrades per-field to "not stated by the filing". Measured
  coverage over the 14: side 14/14, strike 10/14, expiry 10/14, contracts
  5/14. Two-digit years are this century.
- **Prompt guidance** (outside the fence, normalised values only — the filer's
  prose stays inside the content block): the instrument is evidence about
  conviction and the filer's own view of timing, not a recommendation. A
  short-dated option is a timing claim, and the one most likely to have
  decayed in the disclosure lag. **A long-dated deep-ITM call is stock
  replacement — conviction and size, not a view about when.** The disclosed
  amount range on an options trade is PREMIUM, not notional, so it is not
  comparable to a stock purchase's range.

**Dedup identity now includes the description — conditionally.** Without it,
36 identities in the current feed collide and **42 rows are silently dropped**,
including Pelosi's 10,000 BE shares vs her 100 BE calls (same filer, same day,
same amount band, indistinguishable in the log — we cannot now tell which of
the two we researched). Appending it *unconditionally* would change the digest
of every undescribed row and re-emit the entire feed; appending it only when
present leaves the 95% byte-identical. Pinned in a test against the
pre-2026-08-27 digest.

Consequence, accepted: the ~50 described rows re-emit and are re-researched
(human ruling: one-time authorized re-evaluation). Recent purchase rows among
them are also visible to the mechanical arm, which may open slices on them —
per the ruling below, that arm is unchanged and buys stock.

### One-time authorized re-evaluation: both BE tranches, 2026-08-27 22:4x

Human ruling: re-research authorized for the re-emitting described rows, BE
specifically. Both Pelosi BE call tranches re-run through the **production**
research path (the ResearchPass bootstrap builds, real prompt, real API, real
search) — which is also the live end-to-end round trip CLAUDE.md § LLM
Request-Path Changes requires for the prompt change. Execution deliberately
not reached: verdicts and CorrectionRecords only, no orders. Audit log
1411 → 1413 (exactly the two corrections); health clean afterwards.

| tranche | verdict | conf | cost |
|---|---|---|---|
| $500,001–$1,000,000 (trade 07-28 @ $167.035) | `no_position` | 72 | $0.18 |
| $1,000,001–$5,000,000 (trade 07-24 @ $185.17) | `no_position` | 72 | $0.15 |

Both now reason from the instrument: "a classic stock-replacement structure
expressing multi-month recovery conviction, not a timing bet", delta ~0.90,
premium consistent with intrinsic-plus-minimal-time-value — and both state
unprompted that "the option expiry (2027-06-17) is a deadline, not a
catalyst". The decline is on measured priced-in movement (+30.41% and +17.64%
since the respective trade dates; ~67% and ~38% of the intrinsic gain already
realised), not on elapsed time. Confidence 82 → 72 against the 08-27
market-context re-run: the same conclusion, now argued from the option's
economics rather than from the stock's alone.

The corrections do **not** seal the new identities, so both rows re-emit into
the normal loop at the next session and get an ordinary pass with execution
enabled. That is deliberate: this run was a validation and a record, and
acting on a verdict is the loop's job, not a script's.

### Catalyst bypass for disclosed option purchases — DECLINED (human ruling)

The proposal: an option purchase supplies timing specificity by itself (the
expiry is the deadline), so it should permit options expression without
`catalyst_within_horizon`. Declined on the data, recorded here so it is not
relitigated from intuition:

1. **An expiry is a deadline, not an event.** It says when the filer's thesis
   must resolve, never why it will. The catalyst gate wants a nameable event.
2. **The argument inverts on our own filings.** DTE at disclosure: Pelosi BE
   ×2 + INTC 300 days, INTC + UBER 269 days, Gottheimer MSFT 15 days. Five of
   seven purchases are 9–10-month LEAPS. With BE at $217.83 against a $100
   strike (INTC $91.42 against $50), those are ~0.9-delta stock replacement —
   she bought *away* timing pressure. The "expiry ≥ disclosed expiry" clause
   would have put us in 300-DTE vega.
3. **It would import the filer's judgment as a substitute for ours.** Class 2
   is defined as a thesis input, not a copy-trade trigger (CLAUDE.md), and
   Constraint #5 makes signals data, not commands. Constraint #6 points the
   same way: a gate bypass is the more-risk reading.
4. **The IV gate would not have covered it.** `max_iv_percentile: 0.90` ranks
   the pick inside its own chain, not against history — a disclosure that
   lifts the whole surface passes unremarked (limitation accepted 2026-08-24;
   IV-rank-vs-history remains the queued lever). The delta band (0.45 floor,
   0.65 cap) and liquidity gates (OI ≥ 500, spread ≤ 10% of mid) do the real
   work — and would have given us a near-ATM contract, a materially different
   position from the filer's deep-ITM one.

The gate was never the broken part; the input was. With the instrument
visible, a research pass can now reach `present=true` on a named event of its
own reasoning — same outcome, via judgment rather than override.

### Mechanical sleeve unchanged (human ruling)

Equity-only, no instrument-type rule, shared prefilter untouched. An options
purchase disclosure remains a stock-slice candidate for that arm. The
identical-funnel constraint is the reason: an instrument filter would have to
live in the shared prefilter and change both arms, and the experiment must
vary only judgment and exits.

## Feed costs bill from each source's start date (human ruling 2026-08-28)

The first attribution render charged $240 of feed cost over a 90-day window
(class_1 $135, class_2 $105) against feeds that had existed for about two
weeks — proration was `monthly_cost × window_days / 30`, with no notion of
when a subscription began. The keep-or-cut flag therefore fired on months the
experiment was never running.

- `SourceConfig.start_date` (per source, in signals.yaml); every source now
  declares one, taken from the commit that wired it (`git log -S "id: <src>"`).
  Unset still means "bill the whole window" — the conservative default, and
  what every source did before.
- Proration moved to `SignalsConfig.feed_cost_for_window` /
  `feed_cost_breakdown`, because it needs dates the audit package does not
  have. `build_attribution`'s parameter is renamed `feed_costs_for_window` and
  now takes **dollars already computed for the window**, so a stale caller
  passing a monthly rate fails loudly instead of undercharging silently.
- Days are elapsed days from the later of start_date and window start: a
  source wired today costs nothing yet. The 30-day proration month keeps the
  existing conservative lean.
- The report renders the arithmetic per source (rate, start, days, dollars) —
  a small bill on a big window looks exactly like a mis-prorated one from the
  outside, so the number has to be auditable.

Same 90-day window, recomputed: **class_1 $135.00 → $7.17, class_2 $105.00 →
$11.50** (quiver $30/mo × 11d = $11.00, nolimitgains $10/mo × 11d = $3.67,
unusual_whales $25/mo × 3d = $2.50, optionshawk $10/mo × 3d = $1.00, citrini
$5/mo × 3d = $0.50).

## Risk-on calibration, judged sleeve (human ruling 2026-08-28)

Posture change, dated so attribution can partition before/after. **Nothing
structural moved**: never-negative, no margin, long-options-only, the 12% kill
switch, sector caps, deployment caps, the catalyst gate and the verification
rules are all unchanged.

| Confidence | was | now |
|---|---|---|
| floor | < 55 no trade | **< 50 no trade** |
| first band | 55–70 → 1% | **50–70 → 1%** |
| 70–85 | 2.5% | 2.5% |
| 85+ | 5% | **7%** |

- `sizing.hard_cap` 0.05 → 0.07; options still halved on premium at risk, so
  the top option position is 3.5% of sleeve NAV.
- **`equity_sleeve.max_single_position` 0.05 → 0.07 as well.** This is the one
  inference the ruling required: sizing clamps against `sizing.hard_cap`, not
  against the gate, so a 7% band under a 5% gate cap does not produce a 5%
  position — it produces `max_single_position_exceeded` on exactly the
  highest-conviction signals. The two numbers have to move together.
  CLAUDE.md § Portfolio Structure updated to match.
- In account terms the top position is 7% of a 75% sleeve = **5.25% of NAV**
  (was 3.75%). Worst single-name loss is still far inside the 12% kill switch.
- Boundary rules unchanged: bands stay `(lower, upper]` so exactly-70 takes 1%
  and exactly-85 takes 2.5% (Constraint #6); the floor alone is
  lower-inclusive, because "< 50 | No trade" is explicit rather than ambiguous.

## Adaptive exits: the review layer owns the clock (human ruling 2026-08-31)

Before: the leash was set at entry from a three-value horizon bucket, the review
could only say hold/close, nothing responded to price, and the stop never moved.
The INTC position made it concrete — a thesis resolving mid-2027 leashed to 120
days, against a mechanical arm holding the same disclosure for 365.

- **`expected_resolution_date` on the ENTRY report** is now the primary leash
  source. Clamped per horizon: days 3/21, weeks 14/90, months 60/365. **Bounds
  are measured FROM ENTRY**, never from the review asking — a ceiling measured
  from "now" lets thirty modest extensions walk the leash out forever. The
  months ceiling matches the mechanical arm's 365 days deliberately, so the
  leash stops being a confound. No date → the horizon fallback, which must now
  sit inside its own bounds or the config refuses to load.
- **Widened verdict**: validity (intact/invalidated/**displaced**), progress
  (ahead/on_track/stalled), resolution (unresolved/partial/substantial),
  revised_resolution_date, continuation_thesis. Three contradiction rules
  resolve a hold toward the exit — invalidated, displaced, and substantially
  resolved with no continuation written down (holding a played-out thesis is a
  NEW bet and has to be stated). **All derived properties, never schema
  validation**: a validation failure becomes a rejection and a rejection means
  HOLD, so enforcing them in pydantic would invert their intent.
- **Extension gated** on validity == intact AND progress != stalled.
  Shortening always free.
- **Triggered reviews**: +15% / −10% from the LAST REVIEW's price (debounce —
  a position parked above the threshold must not re-trigger every cycle), cap
  5/day replayed from the log, jumping the review queue, prompt stating which
  move woke it. The trigger sets a flag and nothing else; it never closes.
  **25% of the daily research budget is reserved for reviews** so entries
  cannot starve the exit layer.
- **Ratchet**: arms at +20% unrealised, trails 10% from the high-water mark,
  monotonic, own `ExitReason.TRAILING_STOP` so attribution can separate "the
  thesis played out" from "a reversal was caught between reviews". The
  per-position high-water mark is persisted in session_state.json — a mark
  resetting to entry on restart silently loosens an armed stop.
- **Attribution**: P&L by exit reason, counterfactual hold-to-365-days per
  judged exit (partial comparisons labelled as such), overlap set as its own
  section.
- **Mechanical arm untouched.** No LLM in its path stands.

### The live round trip earned its keep — twice now (2026-08-31)

**Second time the CLAUDE.md round-trip rule has caught a silent failure.** The
first was the 2026-08-24 elision incident: doc-verified, unit-tested, and 400ing
on every production research pass for three sessions. This one is the same shape
— a change that every offline test passed, broken on the one path nobody had run.

The CLAUDE.md-required round trip found a production bug on the first real review
this system has ever attempted. `AnthropicResearchClient._request_report`
hardcoded `REPORT_TOOL_NAME` in three places — the phase-2 `tool_choice`, the
nudge prose, and the `tool_use` block `_to_result` reads back. Entry passes and
exit reviews share that method, so every review reaching phase 2 sent:

```
400 invalid_request_error: Tool 'submit_research' not found in provided tools
```

A failed review is a HOLD, so this would have presented as an exit layer that
ran, cost money, and silently never closed anything. Nothing was lost — zero
`thesis_review` records exist and the first judged position opened 08-31 — but it
would have failed at the next session. Phase 2 now forces, names and reads back
whatever tool it was handed, tested against a recording stand-in for the SDK that
asserts what goes on the wire. **The faked-client tests could not see this**,
which is exactly the case the 2026-08-24 ruling was written for.

Validation, both paths, production objects, real API, nothing written:
entry pass returned `expected_resolution_date: 2027-06-17` (the API accepts the
`format: date` property); review of the live INTC position returned
`hold / intact / on_track / unresolved`, `may_extend=True`. Audit log 1593 →
1593. Note the INTC position keeps its 120-day leash for now: its decision record
predates the field, so replay uses the horizon fallback — the next review can
carry it to 365.

## @DiligentPlane evaluated and NOT wired (human ruling 2026-08-31)

Verified before wiring, per the ruling's own condition. **90 posts pulled over 4
days (08-28 → 08-31), ~22/day.** Recorded here with the numbers so the question
is not relitigated from a screenshot later.

| | count | share |
|---|---|---|
| Names any instrument | 12 | **13%** |
| Instrument **and** direction | 8 | **9%** |
| Of those, carrying entry / stop / target / strike | **0** | **0%** |
| Replies to other accounts | 46 | 51% |

`require_instrument` would discard **78 of 90 posts after we had paid to read
them**. Tickers across the whole window: NVDA 15, RVII 4, MU 4, RVI 2 — one idea.
The eight "calls" are five variants of *"Bought more $NVDA"*, two MU trims inside
replies, and one commentary post.

Three reasons this is a do-not-wire rather than a close call:

1. **Our own classifier would discard most of them anyway.** They are past tense
   — *"I bought A LOT of $NVDA Friday"*, *"Bought 2.6M in $NVDA today"*. Under the
   Class 1 rules already in force those are `retrospective`, and ambiguous posts
   default to retrospective. Realistic yield: one or two passes a week, on a
   mega-cap researchable from anywhere.
2. **Zero setup detail.** Not one names an entry, stop or target. These are
   position disclosures, not calls.
3. **The mechanism is forbidden, not merely un-replicable.** Asked directly, he
   confirms *"Yes I use margin"* — Constraint #1. He also states *"I don't buy
   options — everything I do is through buying stocks."*

Also noted: the reported figure has moved. His post of 08-31 reads *"1% away from
500% returns YTD"*, not the 426% the proposal cited. Nothing was wired, no config
changed, no credibility record created.

## Earnings shadow logger — observation only (human ruling 2026-08-31)

The Class 4 trading path is NOT built and waits on two things: this logger's data
over two earnings seasons, AND the paper period's verdict. What shipped is the
measurement.

**The claim under test:** does the realised post-earnings move systematically
exceed the implied move the ATM straddle was charging, on screened names? If not,
the strategy dies having cost nothing but read fees.

- **`src/earnings/`, isolated by construction.** The topology map grants it
  `execution.environment`, `execution.market_data` and `execution.options_data` —
  market data and the .env loader — and nothing else first-party. No gate, no
  adapter, no order schema, no LLM. Nothing imports it either; it is a leaf, run
  from its own entry point (`python -m earnings pass`) rather than from the loop.
  "It places nothing" is a disconnection, not a discipline, and a test reads the
  source to keep it that way.
- **Implied move** = `(call_mid + put_mid) / spot` at the strike nearest spot on
  the first expiry outliving the print, from the chain the options selector
  already fetches. Deliberately the naive estimator: the point is to compare the
  market's price against what happened, not two models against each other.
- **Settle marks the SAME two OCC symbols** the session after the print, so the
  straddle P&L is a real mark rather than a payoff model.
- **Daily ATM IV snapshot per tracked name** — and this is the piece worth most.
  `options_selection.max_iv_percentile` ranks a contract inside its own chain,
  which by construction cannot see a whole surface lifted together (the
  limitation accepted 2026-08-24). An IV rank against history needs stored
  history; nobody sells us ours cheaply; this is where it starts accumulating.
  Note the correction to my earlier framing: the realised-move series is a
  *realised-volatility* reference, which is a different and weaker thing. The IV
  rank foundation is the snapshot, not the realised series.
- **Snapshots run even when the calendar does not.** They need no Finnhub key, so
  a missing key costs the prints, not the series.

**Finnhub, verified 2026-08-31:** free tier is **60 calls/min** and includes the
US earnings calendar, but over a **short forward window (~1 month), not deep
history** — hence building the history forward rather than backfilling it, which
is the right shape for a shadow logger anyway. The free tier is documented as
personal/non-commercial; fine for a personal paper account, worth re-reading
before live money. **`FINNHUB_API_KEY` is not set**, and a missing key raises
rather than returning an empty list: "we cannot see the calendar" and "no
earnings this fortnight" are different facts, and conflating them is the one
failure this exercise cannot afford — an incomplete series looks exactly like a
real one. A human adding a free key (no card) starts the earnings half.

First live pass, no key: **15 IV snapshots written**, e.g. AAPL spot 317.14, ATM
IV 0.24845, implied move 1.47%; AMD 470.66, IV 0.4908, 2.90%.

## Filer-event triggered reviews (human ruling 2026-09-01)

The gap the ruling named: a disclosure event on a HELD name never reached that
position's review — Pelosi selling INTC 20 days after the disclosure we bought on
would arrive as a new-position question while the position it bears on rode its
cadence untouched.

**Judged arm:** when a Class 2/3 signal arrives whose filer matches a tracked
position's ORIGINATING filer in the same name — sale or additional purchase,
anything the filer discloses in it — the position is flagged for a triggered
review through the SAME mechanism as the price triggers: queue-jumping, the 25%
review budget reserve, the shared 5/day cap. The prompt states what happened and
frames it as evidence, not a verdict ("they may be taking profit on a position
entered earlier and at a different price... the event is the question, not the
answer"). The trigger decides nothing; the review does.

**Mechanical arm: recorded, never acted on.** The strategy under test is
hold-a-year regardless, and filer-exit logic would be judgment in the control
arm. Every match still writes the same record, so attribution can later price
"what did ignoring the filer's exit cost the mechanical arm" — the genuinely
interesting number the ruling asked to keep measurable.

Mechanics worth remembering:

- **`FilerEventRecord`**, keyed by the held position's entry decision_id,
  threaded into `AuditTrail.filer_events` — carries filer, transaction, both
  dates, amount, the disclosure's identity, and `arm`. Seals nothing.
- **One filing = one event.** Unresearched disclosures re-emit at startup by
  design; engines dedup on `(decision_id, disclosure_external_id)` seeded from
  `AuditLog.filer_event_keys()`.
- **The flag survives a restart.** A price trigger is rediscovered from marks
  every cycle; a filing arrives exactly once, so replay restores
  `review_due_reason` from any filer event recorded after the last review.
- **A filing outranks a pending price flag** (never re-arrives beats
  rediscoverable), and an option position matches on its UNDERLYING, not the OCC
  symbol.
- `SignalSnapshot` gained a structured `filer`; old records fall back to parsing
  the per-member `credibility_key`.
- Loop wiring runs on the raw drained queue BEFORE the prefilter: the sale may be
  prefiltered as an entry signal and must still reach the held position's review.

**Live round trip ran 2026-09-01** (CLAUDE.md § LLM Request-Path Changes): the
production `ExitReviewPass` + `AnthropicResearchClient`, exit_review tier, a
filer_event-triggered `PositionUnderReview`. Clean structured verdict; the model
addressed the filing directly (noticed the hypothetical sale predated our entry
and reasoned from the invalidation condition). ~47K in / 2.6K out, $0.098.

Suite: 919 passed, 3 skipped.

## Accountability upgrades, BUILD NOW set (human rulings 2026-09-01)

Five-upgrade design review ruled on; the BUILD NOW set shipped this session. The
principle across all of it: **the judgment layer becomes accountable to data,
and nothing auto-tunes — data argues, humans rule, rulings are dated.** Safety
architecture untouched: gate, caps, never-negative, kill switch, mechanical
arm's no-LLM rule.

### #1 Forward-return tracking — `src/forward/`, in full

Every signal that ever entered the funnel gets forward returns at 1/5/20/60/120
CALENDAR days from observation. The design insight that shaped it: **no daily
job** — bars are historical, so the whole thing is a lazy lookup at report time
with an append-only cache (`data/forward_returns.jsonl`, last row per key wins,
complete rows never refetched).

- Marks land on the first session close ≥ D+n; base = first close ≥ observation
  date. Split-adjusted closes (price returns, dividends excluded, same basis
  both sides). Raw AND SPY-excess. **Absent is absent, never zero.**
- `SignalSnapshot` gained structured `tickers`; old records recover theirs via
  `snapshot_tickers` fallbacks (the labelled `ticker:` line for congressional,
  the scanner's own cashtag extraction for posts).
- Topology: `forward` is a NEW package, deliberately **offline** — bars arrive
  as a callable from the orchestrator; it may import `audit.records` (frozen
  types, no write path — the only-orchestrator-imports-audit invariant was
  narrowed, not broken) and `signals`. No research, no execution, no gate.
- Rendered by `python -m orchestrator attribution` after the attribution
  report: coverage, declined-vs-taken at 20d (does research add value over the
  funnel?), by-bucket, per-source decay curves, per-filer, and lag-bucket
  validation. Degrades to a stderr sentence on any data failure.

### 2a Confidence calibration — attribution section

Hit rate per sizing-table band (<50 / 50-70 / 70-85 / 85+, edges matching the
table's own inclusivity), all resolved judged positions since inception —
calibration measures the scorer, not the quarter. **Cells under n=20 render
"insufficient"** and say they must not tune the table. Free — no schema change;
2b's quantified fields wait for #1's data by ruling.

### 4a + #5 cheap 80% — the convergence registry (`orchestrator/registry.py`)

One deterministic structure, seeded from the audit log at startup (a restart
remembers last week's cluster), fed by the loop as signals drain and verdicts
land. Two consumers, bounded by ruling:

- **Dispatch ordering:** cross-filer cluster bonus (other members' PURCHASES of
  the same name, 4a) + source-DIVERSITY bonus (distinct identities, never
  volume — ten posts from one account are one source; nothing converges with
  itself). Caps in `config/orchestrator.yaml` `convergence:`; scale chosen so
  bonuses move a signal days of freshness up the queue, never across a class.
  ORDERING ONLY, same rule as the 2026-08-26 dispatch weight. Note: the sorted
  queue is shared with the mechanical arm's consider() exactly as it was under
  the 08-26 weight — the mechanical RULESET is untouched.
- **Research context:** a fenced SIGNAL CONVERGENCE block — who else is active,
  the cross-filer cluster, and prior verdicts on the name **including
  declines** (ruling: a second source arriving after a decline is shown that
  decline). Guidance outside the fence: convergence is context, never
  corroboration by itself; prior verdicts are not authority.

#5's narrow re-dispatch rule (declined name + real diversity increase, new
record referencing old, cap 2/day) is BUILD AFTER #1 HAS DATA, with 2b/4b/4c.

### Ruled for later, recorded so they don't relitigate

- **#3 correlation gate:** unmeasurable names admitted but capped at the 55-70
  band's size regardless of confidence. **Regime scalar:** orchestrator-computed,
  post-table, ≤1.0 only, LLM-unreachable, CBOE CSV, mechanical exempt, weekly
  forgone-size line. Measurement may ship anytime, report-only.
- 4d (legislative calendar) deferred.

### Live round trips (CLAUDE.md § LLM Request-Path Changes)

The entry research prompt changed (convergence block), so the production path
ran live three times: the two-stage screen at production graduation ($0.146,
77.6K in), a second screen run ($0.156), and the stage-two verification request
— production `build_verification_prompt` over the new user prompt, production
`_call`, class_2 tier — because the screen honestly said no_position both times
and no_position never graduates. All three returned clean structured reports;
the verifier overrode a long draft to no_position, which is the override
discipline working. The registry context visibly reached the model's reasoning.

Suite: 951 passed, 3 skipped.

## Fallback-leash reviews must date themselves (ruling 2026-09-02)

INTC's first review reasoned explicitly about a mid-2027 horizon, returned
`revised_resolution_date: null`, and left the 120-day months-fallback in place —
an intact eleven-month thesis scheduled to close in December by a clock with no
connection to it. The defect was in the guidance, not the machinery: "null when
your view has not changed" reads as "no revision warranted", which is only a
meaningful answer when a date is already on record.

Fix, in all three places the model reads: the system prompt's TIMELINE
paragraph (null means "the date on record is right"; a fallback position whose
analysis names a horizon must WRITE THE DATE DOWN — leaving the fallback clock
to close an intact thesis is a contradiction), the position lines (a no-date
position is labelled NOT STATED with the fallback called generic), and the tool
description. A position WITH a date on record gets none of this — null still
means "unchanged" there.

Mechanics confirmed, not changed: `_apply_revision` runs inside the SAME review
pass — a supplied date re-derives the leash immediately (clamped from entry,
extension gated on intact-and-not-stalled), the guardrails read it from that
tick on, `leash_days_after` lands in the review record, and replay restores it
after a restart. `test_an_intact_progressing_thesis_may_extend_within_the_
ceiling` already pinned exactly this path (fallback 45 → dated 76).

**Live round trip ran 2026-09-02** (production ExitReviewPass + client,
exit_review tier, the INTC shape with `expected_resolution_date=None`): the
verdict came back intact/on_track with `revised_resolution_date: 2027-06-20` —
leash would move 120 → 293, clamped from entry within the months bounds. $0.102.

Suite: 953 passed, 3 skipped.

## Alpha/efficiency rulings, built 2026-09-02 (build order 1, 2, 3, 5)

### 1. Idle-cash yield sweep — SHIPPED as ruled

- **`Sleeve.CASH_MANAGEMENT` + `orchestrator/sweep.py`.** SGOV is NEVER buying
  power: the gate's cash model is untouched, every sweep buy reserves settled
  cash like any order, and the never-negative check binds identically. The
  sleeve is exempt from the ALPHA caps only (single-position, sector, daily
  deployment, drift) — parked cash is not the exposure those bound.
- **Buffer** = judged daily cap + mechanical daily cap + working reservations
  + `buffer_margin_usd` ($2,500), recomputed live (it scales with NAV — the
  unsweep defends the buffer exactly, never lot-flattens). Config:
  `risk_limits.yaml cash_management` (enabled, SGOV, min notional $500).
- Kill switch: sweep buys pause (one note, not a record per tick); unsweep
  sells permitted. Sweep buys are lots (DecisionRecords, strategy
  `cash_sweep`, synthetic signal, no external_id → seals nothing); unsweeps
  close lots oldest-first via ExitRecords (`cash_unsweep`); a flat lot's
  OutcomeRecord realises captured yield. Own attribution line ("cash
  management (SGOV): +X accrued..."), partitioned out of every signal class,
  the funnel, and the registry. Startup splits broker holdings three ways now
  (judged/mechanical/cash_management) from the log.
- One working order at a time; PDT day-trade note in the module docstring;
  settled/unsettled split deferred to the live-gate review as ruled.
- **Expect the next paper session to park ~$80K.** NAV is unchanged by
  parking; paper Alpaca may not simulate SGOV dividends, so the paper phase
  proves mechanics, not dollars.

### Defect fix surfaced by the sweep work (2026-09-02)

`research_passes_on` and `research_passes_by_source_on` were counting
MECHANICAL DecisionRecords — no LLM ever ran on them — so every restart
replayed mechanical entries as spent research passes and consumed the judged
congressional source cap. Both counters now skip strategies
`("mechanical", "cash_sweep")`. This had been quietly shrinking post-restart
budgets since 08-27.

### 2. Tax-aware holding — SHIPPED except lot selection (deferred as ruled)

- **mechanical hold_days 365 → 367** and **judged months leash ceiling
  365 → 367 in the same commit** — long-term treatment needs MORE than one
  year, trade date to trade date; 367 is the boundary plus a one-day cushion.
  CLAUDE.md text updated. `MECHANICAL_HOLD_DAYS` (counterfactual horizon)
  moved with them.
- **`long_term_boundary(acquired)`** (audit.records): anniversary + 1 day,
  Feb-29 → Mar 1, never day-count arithmetic (leap years shave nothing).
- **`OutcomeRecord.long_term`**: stamped at close from the first buy fill.
  Old records parse as None.
- **Review tax factor:** a non-option position within 45 days of its boundary
  WITH an unrealised gain gets one prompt line — "a FACTOR in timing judgement
  only... NEVER overrides a triggered invalidation, a dead or displaced
  thesis, or the deterministic stops and ceiling... a thesis that only
  survives because selling would be taxed is a dead thesis held for the wrong
  reason." Losses get nothing (nothing to defer). Options excluded (theta
  endgame owns that clock).

### 3. Disclosure-reaction: MEASUREMENT ONLY, as ruled

- Forward horizons now (1, **3**, 5, 20, 60, 120) — existing complete cache
  rows recompute once. FunnelEntry carries transaction + amount range.
- `spotlight_filers` in signals.yaml (Pelosi + 10 most-tracked; token-subset
  matching, same as the watchlist). The forward report gains "Disclosure
  reaction — spotlight-filer PURCHASES": 1/3/5d excess, by amount band (range
  max: ≤15K...1M+). No latency work, no trading path — the section's verdict
  decides whether those ever exist.

### 4. Scaling — DEFERRED as ruled; design recorded for its return

Adds keyed off EXISTING verdict fields only (validity==intact AND
progress==ahead — no new verdict vocabulary), deterministic size, max one add,
only INTO STRENGTH (current > entry, unfakeable), each add a NEW decision_id
with its own stop/leash/ratchet (per-lot; merging would loosen the original
stop), aggregate bounded by the per-symbol 7% cap; trims on
resolution==partial from the triggering lot, own Entry/ExitReasons. Waits on
2a calibration showing progress:ahead predicts continuation.

### 5. Groundwork — SHIPPED

- **Source families (recorded in CLAUDE.md, load-bearing):** FOUR —
  congressional filings, 13F filings, X trade-callers (ALL X accounts are ONE
  family), Trump posts. `family_of()` is deterministic: congressional by
  source id, 13F by class, Trump by source id, everything else x_callers. Any
  future band-up: ≥3 families with ≥1 filing family — the lever stays UNBUILT
  pending forward-return evidence.
- **`ConvergenceSnapshot` stamped on judged DecisionRecords at dispatch**
  (families incl. own, independent identities, cluster filers; best ticker of
  a multi-ticker signal). Mechanical records deliberately never stamped.
- **IV-watch widening:** the loop writes `data/iv_watch.json` each tick (held
  names first — option positions by UNDERLYING — then the registry's in-window
  funnel names, cap 60); the earnings logger unions it with its universe for
  DAILY IV SNAPSHOTS ONLY (never armed for prints), bounded by
  `iv_watch_max_names` (default 60). A file, never an import — the logger
  stays a leaf and the topology is untouched.

### Live round trips (§ LLM Request-Path Changes)

The exit-review prompt changed (tax factor), so the production review path ran
live: a gaining months position 36 days from its boundary. Verdict intact /
on_track / partial / hold, and the model used the factor precisely as designed
— it refined the resolution date TO the boundary date rather than overriding
anything. $0.102. (The sweep, funnel, and stamp changes touch no LLM path.)

Suite: 971 passed, 3 skipped.

## Methodology rulings, build 1 of 4: Form 4 insider clusters (human ruling 2026-09-02)

Sequence ruled 1 (Form 4) → 5 (drawdown ladder + regime scalar) → 3 (13D) →
2 (ATR sizing/stops); #4 PEAD DEFERRED to the earnings shadow's first-season
verdict (design at the bottom of this entry). One commit per build, both hosts
verified, droplet pulled, bounce between.

### Families amended to FIVE (CLAUDE.md updated)

congressional_filings, 13f_filings (13D joins here), **insider_filings (new)**,
x_callers, trump_posts. PEAD-style market-data screens sit OUTSIDE convergence.
`family_of()` in orchestrator/registry.py is the deterministic mapping.

### The source (`signals/form4.py`, new; shared base extracted first)

`EdgarFetcherBase` extracted from the 13F fetcher (throttle, contact UA, one
retry, JSON/date helpers); both EDGAR fetchers ride it. `Form4InsiderFetcher`:

- **Listing** via the same FTS endpoint (`forms=4`, 100 hits/page, `from=`
  paging; probed live 2026-09-02 — 549 Form 4s filed 2026-09-01, quiet season;
  earnings windows run 2-3x). 4/A amendments skipped, stated in code.
- **One further request per filing**: the full-submission `.txt`, the
  structured `ownershipDocument` XML sliced out of it. A per-poll fetch budget
  (default 150) bounds wall time; the remainder carries in a backlog, newest
  first, warned, never silently dropped.
- **The ruled recipe, all in the fetcher:** code P non-derivative only;
  10b5-1 excluded via the structured `aff10b5One` checkbox (a field read, not
  footnote mining); roles captured; **$50K per-insider floor AND $150K cluster
  aggregate**; cluster = ≥2 distinct insiders within 15 days; routine =
  same-calendar-month Form 4s in 3+ consecutive prior years (v1 PROXY: filing
  months from the owner's data.sec.gov submissions index — over-suppresses,
  the less-risk direction; one cached request per insider); **unknown history
  defaults OPPORTUNISTIC** (stated choice in the ruling). A multi-owner filing
  is ONE identity — co-filers must not manufacture clusters.
- **Singles failing only the cluster test are emitted** marked
  `cluster: false` → prefilter records them code **`no_cluster`** (new
  `require_cluster` prefilter rule): the control group. The forward report
  gained a "Form 4 cluster rule" section comparing clustered vs singles at
  20d — the data tests the cluster rule itself.
- **State** (window, seen accessions, routine cache, backlog) persists to
  `data/form4_state.json`: a restart neither re-buys filings nor forgets a
  half-formed cluster.
- Class 2 hourly; `daily_research_cap: 5`; `monthly_cost: 0`; prompt branch
  anchors priced-in to the TRANSACTION date (2 business days), never the
  45-day congressional framing; mechanical arm never sees it (source filter is
  structural). Dispatch weight = same log10(amount) − age/7 as congressional.

### Live round trip (§ LLM Request-Path Changes — the prompt gained a branch)

Production two-stage ResearchPass (verify stage forced) on a representative
2-insider INTC cluster built through the real scanner: the model anchored
priced-in to the 2–5 day transaction lag and explicitly distinguished it from
the 45-day convention; long / 61 / weeks, CFO-credibility reasoning. $0.325.

## Methodology rulings, build 2 of 4: drawdown ladder + regime scalar (2026-09-02)

Built at ONE composition point as ruled: `size x regime x drawdown`, both ≤1.0
BY VALIDATION (`orchestrator/scalars.py`, config `orchestrator.yaml
risk_scalars`), applied by the pipeline to every judged proposal after the
confidence table — including the option→equity fallback re-sizings, which
reach the table through the same two helpers so no path skips the scalars.

- **Ladder:** 1.0 / 0.75 @ ≥4% / 0.5 @ ≥8%, inclusive toward less risk,
  reading the gate's own `drawdown()` through a NEW read-only accessor (the
  kill switch's number; the switch's code untouched, the ladder's last rung
  deliberately below it). Stateless — recovery restores the tick it happens.
- **Regime:** last VIX close from CBOE's free daily CSV
  (`execution/vix.py CboeVixSource` — execution, not orchestrator, because
  orchestrator stays offline per topology; the loop's scalars get it as an
  injected callable, tests inject nothing and get x1.0 regime with the ladder
  still armed). Rungs 0.75 @ VIX ≥25, 0.5 @ ≥35, inclusive. One fetch per UTC
  day. **Missing/stale (>7d) data runs x1.0 and is LOGGED** — a CDN outage
  must not silently halve the book; the choice is stated here for the human.
  Live close at build time: 16.34 (2026-09-01) → regime x1.0 today.
- **New entries only, by construction:** held positions, exits, the mechanical
  arm (never passes through pipeline sizing) and the cash sweep untouched.
  LLM-unreachable — no prompt or schema mentions any of it.
- **Attribution:** scaled proposals carry `table_capital` on the
  SizingSnapshot; the weekly report renders ONE forgone-size line (dollars
  shaved, entries touched — or "x1.0 throughout").

### SPY put overlay — PASSED, the four reasons (ruling 2026-09-02)

1. Systematic put buying pays the variance risk premium (~−1 to −2%/yr
   expected); funding it from SGOV yield changes the mental account, not the EV.
2. The puts would live inside the 20% aggregate premium cap, directly
   displacing alpha capacity.
3. The ladder + kill switch already bound the path risk the puts would hedge,
   at zero premium.
4. In the vol regimes where the hedge matters its cost triples and the yield
   no longer covers it. Revisit only if the account grows to where a 12%
   drawdown is money that matters beyond the experiment.

## Methodology rulings, build 3 of 4: activist 13Ds (2026-09-02)

`signals/form13d.py` on the shared EDGAR base, Class 2 hourly as ruled.

- **Access (probed live):** since the SEC's structured-data rule the forms file
  as `SCHEDULE 13D` / `SCHEDULE 13D/A` (935 + 671 market-wide in the probe
  month) with machine-readable `primary_doc.xml` — stake %, aggregate shares,
  date of event, amendment number are field reads. The FTS hit's display name
  carries the subject's TICKER, so no CUSIP mapping exists anywhere. Listing is
  market-wide (two throttled requests per poll), filtered client-side against
  the watchlist by filer-display-name substring — the 13F convention.
- **Watchlist (~15, proposed in the commit for confirmation):** Elliott,
  Starboard Value, ValueAct, Third Point, Icahn, JANA Partners, Ancora, Trian,
  Pershing Square, Engaged Capital, Sachem Head, Legion Partners,
  Land & Buildings, Politan Capital, Sarissa Capital. Adding/removing is a
  human ruling per name.
- **Fields in the signal:** filer, stake (the activist's own percentOfClass —
  the max, not a group member's slice), shares, date of event, amendment no.;
  `credibility_key = form_13d/<activist>` so attribution ranks activists like
  congressional members. An unparseable XML degrades to hit-level facts —
  under-filtering surfaces, a drop would not.
- **Honesty about latency, in the prompt branch:** initial 13D due 5 business
  days after crossing 5%, amendments 2; the filing-day pop is public before we
  poll — the guidance says measure it, never chase it; the tradeable claim is
  the campaign (drift + amendment trail), an amendment REDUCING a stake is
  evidence against. Family: 13f_filings (a fund's 13D and its 13F are one
  identity class) — `family_of()` updated. daily_research_cap 3, monthly_cost
  0. Mechanical arm never sees it (structural source filter).

### Live round trip (§ LLM Request-Path Changes — the prompt gained a branch)

The REAL fetcher ran against real EDGAR first (5-day watchlist window: quiet —
zero filings, itself a good volume datum), so the representative Sarissa/AMRN
13D/A shape stood in, built through the real scanner, production two-stage
ResearchPass with verify forced. The model measured the filing-day pop as
absorbed (~6 days stale), weighed amendment-4-of-an-old-campaign vs an initial
filing, and returned no_position/42 — the measure-don't-chase discipline the
guidance asks for, on the first live read. $0.161.

## Methodology rulings, build 4 of 4: ATR sizing and stops (2026-09-02)

As ruled, all parameters in `orchestrator.yaml atr_sizing`: k=2.5, stop bounds
8%/20%, risk budget = band × 0.15, size = min(band cap, budget/stop),
trigger-down = 0.66 × the position's own stop, options excluded, NEW ENTRIES
ONLY — INTC keeps its 15% (stop_fraction is frozen at entry and pre-ruling
positions replay with None → the fixed regime).

- **ATR(14)** = SIMPLE mean of the last 14 true ranges over split-adjusted
  Alpaca daily bars (`execution/atr.py` — execution owns the I/O, orchestrator
  stays offline; injected into the pipeline like the VIX source; cached per
  symbol per UTC day). Stated choice: not Wilder's recursive smoothing, whose
  value depends on where the recursion started. Missing/short history → None →
  the fixed-15% regime untouched (status quo ante, never a fabricated vol).
- **Order of operations:** confidence table → ATR risk-parity (per-name) →
  risk scalars (book-level). Each step only shrinks; the option→equity
  fallback re-sizings pass through the same helpers, so no path skips either.
  Arithmetic note: with these numbers the band cap binds at any stop tighter
  than 15%, so the mechanism is one-sided by construction — quiet names at the
  cap, volatile names shaded (20% stop → 0.75 × band).
- **Exit engine:** TrackedPosition gained `stop_fraction`; the entry stop arms
  from the proposal's fraction at fill, replay restores it from the
  DecisionRecord's SizingSnapshot (old records parse None → 15%), and the
  ratchet layers on top unchanged (effective stop only ever rises). The
  adverse trigger for ATR positions = 0.66 × own stop; fixed-regime positions
  keep the config's 0.10 exactly.
- **Stamps for attribution:** `atr_fraction`, `stop_fraction`, and
  `counterfactual_fixed_capital` (the fixed-15% regime's dollars) on every
  sized decision — the honest warning stands that stop-fire events are rare
  and this section needs 2–3 quarters before n clears 20.
- **No LLM request-path change** — stop distances appear in no prompt (kept
  that way deliberately), so no round trip is owed on this build.

### #4 PEAD — DEFERRED (design recorded for its return)

Deferred to the earnings shadow's first-season verdict, so earnings gets ONE
verdict, not two adjacent efforts on different clocks. The design as reviewed:
deterministic screen on the existing Finnhub plumbing (`/calendar/earnings` +
`/stock/earnings` are free-tier; guidance direction is NOT — premium — so v1 is
EPS surprise magnitude + day-1 gap-and-hold from Alpaca bars, guidance left to
the research pass's own search). Proposed thresholds: |surprise| ≥ 10% of
estimate AND gap ≥ 4% from prior close holding above the open at the close —
both boundaries resolving toward fewer candidates. Routes to research as a
weeks-horizon, catalyst-passed, EQUITY-ONLY candidate (the shadow ruling owns
pre-catalyst options); daily post-close batch, violently seasonal (3–10/day in
peak weeks, ~0 off-season); NO convergence family (no filer — outside
convergence per the family amendment). Evidence honestly weak-to-moderate:
textbook PEAD is arbitraged away in liquid names; the day-1-reaction
conditioning is the modern survivor. Expected 0–2%/yr, low end likely.

## Operational hardening tier (human ruling 2026-09-02; sequence 2,1,3,5,4)

None of it touches trading logic; suite grew to 1032.

### 2. Off-box backups — Spaces push shipped, panel toggle recommended

The audit log had NO off-box copy. Decision proposed and built: **nightly
encrypted tarball push to a DO Spaces bucket as the primary layer** (RPO 24h
matching the existing 01:07 UTC on-box job; client-side gpg AES256; targeted
one-object restore; cents/month at our sizes) **plus enabling weekly DO
droplet backups in the control panel as the belt** (one click, whole-box
disaster recovery, but weekly RPO and no encryption control — insufficient
alone). `ops/vps/push_backup.sh` + the updated `agentic-backup.service` run the
push as a second ExecStart; **unconfigured = a no-op with one syslog line**, so
nothing breaks before setup. RUNBOOK (human, one-time): create the Space +
access key; on the droplet write /home/agentic/.backup_env (chmod 600 — vars in
the script header), `apt install rclone gnupg`, then
`sudo cp ops/vps/agentic-backup.service /etc/systemd/system/ && sudo install
-m755 ops/vps/push_backup.sh /usr/local/bin/agentic-push-backup && sudo
systemctl daemon-reload`. Restore drill in the script header — run it once.

### 1. Alerting — execution/alerts.py, two tiers over Gmail SMTP

`[AGENTIC URGENT]` / `[AGENTIC DAILY]` subject prefixes for mail filters.
Credentials ALERT_SMTP_USER / ALERT_SMTP_PASSWORD (App Password) / ALERT_TO —
already present in the droplet .env; missing = disabled with one log line.
One daemon worker thread over a bounded queue (the loop's cost is a queue
put), send failures logged never raised, same key rate-limited 4h. Wired in
`__main__ run()`: run-log observer routes ERROR/COST/READS and a MECH line
containing BREAKER to urgent; the tick loop alerts kill-switch trips (state
transition), every tick with exits_started (unique key per tick — each exit
event alerts, storms don't), first judged and first mechanical entry of the
day (daily); startup alerts UNMANAGED positions and orders still pending
settlement after recovery; the close writes one daily summary (NAV, drawdown,
positions, research spend). The alerter lives in execution because
orchestrator stays offline.

**Test message: DELIVERED 2026-09-02 — from the DEV BOX.** Found at build:
**DigitalOcean blocks outbound SMTP (25/465/587) on the droplet** — IPv4
connects time out on both 465 and 587 (probed), so production alerts from the
droplet CANNOT send until either (a) a DO support ticket lifts the SMTP block
for the account (usually granted; the standing ask), or (b) a ruling picks an
HTTPS transport instead. The code path is verified end-to-end (creds valid,
App Password works, message arrived — set up the two filters on it); the
droplet's sends will fail-and-log, never block, until the port opens. ACTION
(human): DO ticket to unblock SMTP, then re-run
`execution.alerts.Alerter().send_test()` on the droplet.

### 3. Execution fidelity — every fill now carries its own honesty

FillRecord gained `intended_price` (the limit), `spread_pct_at_submission`
(AlpacaPriceSource.spread_pct at order submission; None when unmeasurable,
never zero; options use the chain spread already on the expression snapshot),
and `seconds_to_fill` (broker acceptance → terminal settlement). Exit fills
record intended_price; their spread/time stay None — exits re-submit every
cycle until flat, and a per-order clock would misstate time-to-close. Old
records parse with all three None. **Haircut:** `slippage_haircut_bps: 5` in
orchestrator.yaml; the attribution report now renders judged P&L RAW and with
bps x window fill notional deducted ("net live-ish"). Reporting only; the
recorded spreads/fills will eventually argue the right number from our own data.

### 5. Friday delivery — `python -m orchestrator weekly`

Attribution + forward report emailed on the DAILY tier (stdout/journal
fallback when alerting is unconfigured — the report is never silently lost).
`ops/vps/agentic-weekly.{service,timer}`: Fri 17:00 ET, Persistent=true (a
missed Friday sends Saturday). ENABLE (human):
`sudo cp ops/vps/agentic-weekly.{service,timer} /etc/systemd/system/ && sudo
systemctl daemon-reload && sudo systemctl enable --now agentic-weekly.timer`.

### 4. Config-replay harness — `python -m orchestrator replay`

`--signals candidate.yaml` and/or `--limits candidate.yaml` → replays the
recorded judged funnel through both configs' deterministic stages
(orchestrator/whatif.py) and reports prefilter flips (now-skipped /
now-dispatched) and sizing-table flips on recorded confidence scores, each
group with its cached 20d forward excess. READ-ONLY AND OFFLINE: audit log and
forward cache opened for reading only, no LLM, no bars. Honest scope printed
in the header: report-date staleness, unheld-sale, caps/budget ordering, and
research verdicts do not replay. Smoke-tested against the local log (29
entries, zero flips on an identical candidate — correct).

## Live-agent-evidence tier (human ruling 2026-09-02): built 1, 3, 4; designs 2, 5

### 1. Expectancy metrics + reward:risk gate — SHIPPED

- **Metrics** (`ExpectancyStats` in attribution): avg win, avg loss, profit
  factor, expectancy/trade now render per confidence band (calibration rows),
  per source (new since-inception section), per class, and per exit reason —
  n<20 renders "expectancy insufficient (n=X)", same discipline as calibration.
- **target_price** joined the entry report schema (2b accelerated): a CLAIM,
  graded, and read by exactly ONE deterministic consumer. Tool description
  frames it honestly: required for tradeable directions, cannot raise a size,
  inflating it only records a claim the track record carries. The screen
  draft passes it to the verify stage.
- **The gate** (`reward_risk` in orchestrator.yaml): equity longs must clear
  `(target − entry) / (entry × stop) ≥ 1.5` before sizing; typed rejection
  `insufficient_reward_risk` (SIZING stage → funnel bucket declined). The stop
  is the one the position would actually get (ATR-derived clamp; 0.15 fallback
  mirroring exits). VETO-ONLY by construction — a gamed target passes into
  ordinary sizing, never past it. No target on a long = rejected (Constraint
  #6). Missing quote fails OPEN (order construction already blocks unpriced
  orders). Options excluded (premium is the risk; delta bands + halved table
  govern). Live round trip: the golden baseline below IS the production
  two-stage path under the new schema.

### 3. Model pinning — SHIPPED (and the pricing-table correction)

Confirmed against the live Models API 2026-09-02: `claude-opus-5`
(created 2026-07-24) and `claude-sonnet-4-6` (created 2026-02-17) are the
CANONICAL COMPLETE ids — the current generation has no dated snapshot variants
(new capability ships as new ids) and no floating aliases; the bare id IS the
pin. `claude-haiku-4-5-20251001` (created 2025-10-15) is its generation's
dated-snapshot form, already pinned. Enforcement: `pinned_models` allow-list in
research.yaml; ResearchConfig validation HARD-FAILS startup on any configured
model outside the list or any `-latest` alias. CLAUDE.md § LLM Request-Path
Changes now carries the ruling: any model change = dated ruling + attribution
partition + golden replay before ship; same replay before any prompt/tier
change. Also corrected en route: the opus pricing row had carried a 3x-over
baseline ($15/$75 vs the published $5/$25) since wiring — estimates only fed
the COST tripwire, which the error made fire EARLY (conservative direction);
now matches the published table.

### 4. Golden set — SHIPPED, baseline run below

`config/golden/golden_set.jsonl`: 20 graded cases — 16 real decisions mined
from the droplet audit log (the BE call declines, the stale SA-13F with NVDA
puts, the taken INTC calls entry long/54, the AAPL dividend-reinvestment
non-signal, the Tim-Cook BREAKING verification case, max-lag/priced-in
declines, Trump noise at several confidence levels) + 4 fixtures (Sarissa
13D/A and the Form 4 INTC cluster from this week's live round trips, a prompt
injection that must be declined AND flagged, a fabricated mirror relay that
must fail verification). Grading = direction set + confidence band
(+ mandatory manipulation flag where ruled); prose is not graded; context
builders deliberately absent — the replay isolates prompt × schema × model.
`python -m orchestrator golden [--only name] [--limit N]` replays through the
PRODUCTION two-stage pass; exit code 3 on drift. The set skews decline-heavy
because the log does; new graded cases join by ruling.

**BASELINE (2026-09-02, opus-5 / sonnet-4-6, full run $3.64): 17/20**, and all
three drifts taught something that is now in the harness:

- Replays initially ran at "now" — time-sensitive cases drift as the calendar
  moves. Fixed: fixtures carry the ORIGINAL `observed_at`, so the prompt's lag
  arithmetic matches the graded frame (web search still sees the present — a
  stated limitation; the set leans on time-robust declines).
- `sarissa-amrn-13da` "failed" by declining at confidence 82 — which is GOOD
  calibration, not drift; the band had conflated decline confidence with
  weak-long size. Fixed: optional `traded_confidence` band applies only to
  tradeable verdicts. Re-run: no_position/72 PASS ($0.15).
- `pelosi-uber-priced-in` re-ran long/42 with the frame fixed — below the 50
  sizing floor, behaviorally a no-trade. Regraded BEHAVIORALLY: decline at any
  confidence or a sub-floor long both pass; a long that would actually trade
  is drift. (Its first run long/60 WOULD have traded a stale May disclosure —
  that flagging was correct and stays possible.)
- `pelosi-intc-calls-entry` replayed no_position/30 against the taken long/54
  from two days earlier: genuine verdict instability at the sizing-floor
  boundary, not a mis-grade. Kept as a BOUNDARY-WATCH case (both verdicts
  grade, wild confidence does not) — run-to-run flapping here is exactly what
  the set should make visible over time.

The schema round trip rode the baseline: form4-intc-cluster returned
long/63-68 WITH target_price (28.995-30) through the new tool schema, twice.

### 2. Exit-authority probation — DESIGN REVIEW (not built)

For 90 days from a dated ruling: a review CLOSE verdict on a position that is
BOTH profitable (mark > entry) AND validity-intact becomes SHADOW — recorded
(new ShadowCloseRecord: decision_id, mark, the verdict verbatim), NOT
executed; the position keeps running under stops/ratchet/leash/invalidation,
which retain full authority, as do close verdicts on invalidated/displaced
positions (validity != intact executes as today). Bounded risk by
construction: the worst a wrong shadow costs is what the ratchet/leash already
permit. Counterfactual via the forward engine: mark at shadow time vs
5/20/60d after — "would the close have saved money?" Authority grant/deny:
after 90 days with n≥20 shadowed verdicts, if the shadowed closes' 20d
counterfactual shows the review saved money (positions fell after it said
close), review-close authority on intact winners returns by dated ruling; if
positions kept rising, probation extends or the verdict class is demoted
permanently. Edge rules: one counterfactual per position per verdict streak
(dedup on first shadow); a shadowed position that later stops out records
which layer actually exited. Build cost ~1 session; nothing touches the
guardrails. AWAITING RULING.

### 5. Directional-bias audit — DESIGN REVIEW (not built)

- **Measurement (cheap, report-only):** per-position beta from 120d daily
  bars vs SPY (deterministic regression, absent-never-guessed), book beta =
  position-weighted; attribution renders beta and beta-adjusted excess
  (return − beta × SPY) alongside the current 1.0-beta excess. ~1 session.
- **Why short_via_puts has never fired:** the sources are structurally long —
  congressional sales are prefiltered unless held (someone's exit ≠ entry
  thesis), 13Fs are longs-only by law, Form 4 emits code P buys only, 13D
  stakes are long activism, the X callers overwhelmingly call longs. The
  research layer CAN return short_via_puts; it is almost never handed a
  bearish fact pattern. Not a bug — a documented sampling bias.
- **Bearish paths that would need rulings (report, not built):** (a) Form 4
  SELL clusters — the mirror-image recipe exists in the fetcher's parser
  already (code S legs are parsed and discarded); groundwork could log sell
  clusters as measurement-only prefiltered rows so forward returns grade the
  bearish signal BEFORE any trading path exists, exactly like the disclosure-
  reaction slice; (b) 13D stake REDUCTIONS as bearish evidence (amendment
  deltas are already in the signal); (c) risk-off regime: the VIX scalar
  currently only shrinks longs — a puts-overlay expression would be a new
  strategy needing its own ruling and collides with the passed SPY-put
  assessment. Recommendation: build (a)'s measurement groundwork first if any;
  no trading path until forward returns argue for one. AWAITING RULING.

## CORRECTION (2026-09-02): every dollar figure for Opus research spend above is ~3x OVERSTATED

The research.yaml pricing table carried a baseline estimate of $15/$75 per MTok
for claude-opus-5 from wiring (2026-08-17) until today's correction to the
published $5/$25. Every per-pass cost in earlier entries that ran on the Opus
tier — the $0.325 form4 round trip, the $0.146/$0.156 convergence screens, the
$0.102 reviews, and the cumulative daily research spend the COST tripwire and
attribution reported — is therefore roughly THREE TIMES the real bill (Sonnet
and Haiku rows were correct). Actual research spend to date is materially lower
than recorded. Past audit records are append-only and keep their as-written
estimates; from today the estimates match the published table, and the console
bill remains the truth. The error's only operational effect was conservative:
the $10/day tripwire fired early.

## Exit-authority probation — APPROVED and BUILT (ruling 2026-09-02)

As designed, 90 days from 2026-09-02 (`orchestrator.yaml
exits.review_close_probation`): a review CLOSE on a position both profitable
AND validity-intact writes a `ShadowCloseRecord` (mark, entry, days held,
verdict prose) and does NOT execute; stops/ratchet/leash/invalidation retain
full authority, and closes on invalidated/displaced theses execute unchanged
(tested each way, plus outside-window behavior). The forward report gained a
"Shadowed review closes" section grading each shadow by the move at 5/20/60d
AFTER the verdict — negative excess means the close would have been right. The
grant/deny ruling is due ~2026-12-01 with n≥20.

## Directional-bias measurement — APPROVED SCOPE BUILT (ruling 2026-09-02)

- **Beta:** `beta_from_closes` (OLS over date-aligned daily returns, n≥40 or
  absent) in attribution; the weekly report renders per-position betas, the
  value-weighted book beta, and a beta-adjusted excess line (return − β×SPY —
  the honest alpha line for a long-biased book). Options positions excluded
  (OCC symbols are not equity betas).
- **Form 4 sell clusters:** the fetcher now parses code-S legs and runs the
  MIRROR recipe (same floors/window/routine exclusion) into a separate sell
  window (persisted); a completed sell cluster emits ONE row marked
  `measurement_only` → prefilter code `bearish_measurement` — recorded, never
  researched, never traded. Sell singles are not emitted.
- **13D stake changes:** derived report-side from successive recorded filings
  per (activist, name) via the new `snapshot_stake_percent` parser — no new
  state, replay-safe. The forward report slices reductions vs increases.
- **(c) risk-off puts overlay: DECLINED** — recorded alongside the 2026-09-02
  put-overlay pass (same four reasons apply; the VIX scalar remains the only
  risk-off response, and it only shrinks).

## Boundary confirmation — DIAGNOSED STOCHASTIC, BUILT (ruling 2026-09-02)

**Diagnosis first, as ruled:** three additional identical-input replays of the
INTC golden entry (original observed_at, same content, same config):
no_position/52, no_position/72, long/38 — with the baseline's no_position/30
and the originally-taken long/54, five runs of the same inputs span BOTH
directions and 42 confidence points. Verdict: the sizing-floor band admits
stochastic noise; the 09-02 replay difference was not the model updating on
new inputs. ($0.55 of diagnosis spend.)

**Built:** `boundary_confirmation` in orchestrator.yaml (band_width 10; the
floor itself read from risk_limits sizing at bootstrap). A tradeable entry
verdict with confidence in [floor, floor+10) buys ONE second independent pass
(same tier, fresh context, the production `ResearchPass.run`); confirmed =
same direction at or above the floor, and the LOWER of the two confidences
sizes — the second pass can only block or shrink, never enlarge. Disagreement
or a failed second pass = typed rejection `unconfirmed_boundary` (a verdict
that does not replicate is not sized, Constraint #6). Only floor-band
candidates pay the extra pass; its cost rides the same dispatched budget pass
(bounded by the band's rarity — note for the budget ledger).

**Live round trip (§ LLM Request-Path Changes):** first attempted on the INTC
case itself — five more production first-passes all declined (68/72/32/52/42;
yet more stochasticity evidence, ten runs of these inputs now on record) so no
tradeable band verdict arose there. Completed on the form4-intc-cluster case
with band_width temporarily 25 (a valid config value; production ships 10; the
request shape is identical at any band): first pass long/62 → the REAL
`_confirm_boundary` bought the second production pass → CONFIRMED, sizes on
the lower confidence (62). ~$0.30.

## Position management — re-underwrite, trim, visible state (ruling 2026-09-02)

Closing the gap between "reviewed daily" and "actively managed". Three pieces,
one ruling.

**Re-underwrite (`would_open_today`):** every thesis review now answers a
fifth question — under TODAY's entry rules (the sizing floor with its boundary
confirmation, and the reward:risk minimum measured from the CURRENT price to
the target against the CURRENT stop, all stated in the prompt from the live
config, never from the model's memory), would this position be opened now? —
in two new schema fields, `would_open_today` + reason. Contradiction rule 4:
a "no" on a position whose progress is not `ahead` closes it whatever the
action field says, and that close is INVALIDATION-ADJACENT — it executes
normally and is exempt from the probation shadow (the position no longer
clears the bar that admits positions; it is not the profit-taking class the
shadow measures). A "no" on a position running ahead is recorded, visible in
health, and does not close. The field defaults to True so a rejected review
stays a HOLD and old fixtures cannot manufacture a close.

**Trim (the TRIM half of scaling; ADDs stay deferred as ruled):** a HOLD
verdict reporting `resolution=partial` on a position in profit sells
`review_trim_fraction` (0.5 shipped) of the position — whole units rounded
down, own `ExitReason.REVIEW_TRIM`, sell-to-close through the gate like every
order, risk-reducing so exempt from the probation shadow — AT MOST ONCE per
position. **Ambiguity RULED same day: once per position.** "Sell a configured
fraction of the lot" was read as once-per-position (the fewer-trades reading);
the human confirmed it, adding that a position re-triggering partial AFTER its
trim is to be asked to close, not re-trimmed — the review prompt now says so
(system prompt rule + an "already trimmed: YES" fact line built from the
position's latch). The latch arms on the trim FILL and is restored at replay
from the trail's submitted review_trim exits (a submitted-but-unfilled trim
under-trims after a restart — stated, not silent). The system prompt discloses
the trim consequence to the reviewer so its "partial" means what it thinks it
means.

**Visible management state:** `orchestrator health` now renders a second line
per position — last verdict (validity/progress/resolution), would_open_today,
the expected resolution date, days to leash, and a `trimmed` flag — restored
across restarts from the trail, so active management is read, not inferred.

**Golden replay (§ LLM Request-Path Changes):** the change touches only the
exit-review path (exit_review.py; the entry pass files are untouched), and the
variance experiment running this same day provides three full golden replays
of the production entry path at this code. The exit-review prompt change's own
live round trip ran on the droplet's real INTC position — the first
`would_open_today` answer on record (see the ruling report).

Suite 1074 passed / 3 skipped.

**Live round trip of the exit-review prompt (2026-09-02, droplet, production
`ExitReviewPass`, real INTC position, read-only, $0.13):** entry 90.75, stop
77.14, day 3/274, entry confidence 54; quote 5.5h stale so the prompt said
UNAVAILABLE and the model used entry as its proxy. Verdict: **would_open_today
= False** — R:R to a conservative $110 target = (110 − 90.75)/(90.75 − 77.14)
= 1.41 < 1.5 (target would need ≥ ~$111.53), and confidence 54 sits in the
boundary band; the model went to action=close outright (validity intact,
progress on_track, invalidation not triggered). The first would_open_today
answer on record. The already-trimmed prompt line was exercised in a second
round trip on the same real position facts with the latch forced on (no
trimmed position exists yet): the model cited the rule verbatim — "the
once-per-position trim has already been taken. System rules are explicit: a
further partial resolution calls for close, not another trim" — and returned
action=close, resolution=partial, would_open_today=False ($0.14). The prompt
line lands.

**RULING (2026-09-02): let the production loop close INTC on its next review.**
The re-underwrite is working as designed and the human wants the LIVE proof
that the management layer exits for "not good enough", not only for "wrong".
**Overlap consequence, recorded:** the mechanical arm still holds INTC to day
367 — this becomes the two-arm overlap comparison's FIRST data point: the same
name, one arm exited by judgment on day ~4, the other holding mechanically to
its clock. Attribution should be read with that pairing in mind.

**Effort on the sonnet tiers — assumption checked (ruling 4, 2026-09-02):**
docs (effort page, verified 2026-09-02): "The effort parameter affects ALL
tokens in the response ... it works whether or not thinking is enabled. Lower
effort also means fewer and terser tool calls." So `effort: medium` on the
sonnet tiers is NOT inert — my same-day aside that it was "likely inert" was
wrong and is withdrawn. What WAS wrong in the config's framing: research.yaml
describes effort as "Thinking depth / token spend", and on claude-sonnet-4-6
thinking defaults OFF (per-model table) and the client sends no `thinking`
parameter — so on every sonnet tier (screen, class_2, class_3, exit_review)
effort has been buying token economy and tool terseness, never thinking depth.
Every sonnet verdict to date, the ten noisy INTC runs included, was produced
with zero reasoning in thinking blocks. That is the pricing/quality assumption
that was wrong: the sonnet tiers were priced and judged as "cheap thinking"
and were in fact "no thinking". The variance experiment's third arm (adaptive
thinking on the sonnet report phase) measures what that reasoning is worth.

## Standards audit — liquidity gate, panic button, stress test, restore drill (ruling 2026-09-02)

**1. Liquidity gate — BUILT, 1% PROPOSED (awaiting confirmation).** The
resulting position (held + order) may not exceed `liquidity.max_position_fraction_of_adv`
(0.01) of the name's 20-day average DOLLAR volume; both arms, opening equity
orders only; options and the sweep ETF exempt with the other alpha caps; a
missing ADV FAILS CLOSED (`illiquid_position`) — a name whose volume cannot be
read is the name this exists to keep out. Gate stays offline: it receives an
`adv(symbol)` callable; the production one (execution/liquidity.py) reads the
**SIP feed** — probed 2026-09-02, IEX bars carry IEX-venue volume only (AAPL
~1M vs ~34M consolidated), so an IEX-derived ADV would understate liquidity
~30x and block nearly everything. A gate built without a source (offline
tests, read-only commands) skips the check.
**Retrospective (droplet, 13 approved equity entries on record, 1 judged / 12
mechanical): the gate would have blocked NONE.** Every entry sits at
0.000–0.001% of its name's ADV — the largest footprint is ESAB, $833 against
$63.5M/day. At $100K paper NAV the gate binds nothing today; it is armour for
the two cases it was asked for — mechanical slices landing in microcaps, and
the NAV this is meant to scale to.

**2. Panic button — BUILT.** `orchestrator halt [reason]` / `orchestrator
resume "<name>: I CONFIRM MANUAL RESET"`; runbook ops/EMERGENCY.md. Design
point worth recording: the live session owns session_state.json and rewrites
it every tick, so an outside edit of `kill_switch_tripped` would be clobbered —
the durable channel is a `data/HALT` marker the loop reads at the top of every
tick (trips its own gate via the new safe-direction `RiskGate.trip_kill_switch`,
cancels every working order, persists, logs HALT → urgent alert). `halt` also
cancels every open order at the broker directly (immediate, loop or no loop),
edits the session file itself only when no session is live (the instance lock
says), writes an `operator_action` audit record, emails the urgent tier and
prints state. `resume` refuses while a session is live and without the exact
phrase, then calls the gate's own documented operator reset (re-basing the
high-water mark so the reset is not inert), persists, clears the marker, and
records the acknowledgement — the name-on-every-reset the gate's docstring has
promised since it was written and never actually had a record kind for.
`reset_kill_switch` itself is unchanged and reachable only through the human's
command.

**3. Stress test — BUILT, first run recorded (droplet, 2026-09-03 02:03 UTC,
today's book: INTC judged + 12 mechanical names, SIP bars):**
- covid_crash_2020 (2020-02-19 → 03-23): total NAV −2.33%; judged −0.25%;
  mechanical −9.42% (ESAB, TOST held flat — no history). Ladder not reached.
- rates_selloff_2022 (2022-01-03 → 06-16): total NAV **−4.17% → ladder ×0.75
  would have engaged**; judged −0.21%; mechanical **−15.90%** (breaker at 25%
  not reached).
- q4_2018: total −1.70%; judged −0.07%; mechanical −7.23% (ESAB, TOST flat).
Kill switch: not reached in any window. Reading: the book is ~75% cash-and-
one-name on the judged side, so total-NAV drawdown is dominated by the
mechanical sleeve's 12 equal slices — and that sleeve would have given back
16% in 2022 without touching its 25% breaker. The judged sleeve's numbers are
not reassurance; they are a statement that it holds one position.

**4. Restore drill — RUNBOOK + SCRIPTS WRITTEN, DRILL BLOCKED.**
ops/RESTORE_DRILL.md, ops/vps/restore_drill.sh, ops/vps/restore_verify.py.
`/home/agentic/.backup_env` does not exist on the droplet (checked 2026-09-03),
so no encrypted tarball has ever left the box and there is nothing off-box to
restore. The script's `--local` mode can exercise restore+verify against an
on-box nightly tarball today; that proves the tarball, not the off-box path.
Drill to be run ONCE and recorded here once the human creates the bucket, keys
and passphrase.

**5. Legal-disclosure roadmap — DESIGN REVIEW ONLY (nothing built):**
- **8-K item filtering.** Value: the only one of the three that hands the
  judged arm a dated CATALYST, which the catalyst gate and options path are
  built around. Volume: hundreds of 8-Ks a day — unusable unfiltered; a
  whitelist of items with documented post-filing drift makes it tractable:
  5.02 (unscheduled CEO/CFO departure), 4.02 (non-reliance on prior
  financials — restatement; strongly negative drift), 2.05 (restructuring),
  1.01 (material definitive agreement), 1.05 (cybersecurity incident). Lag:
  filed within 4 business days, usually same-day; the announcement pop is
  forfeit by design, the claim is item-specific drift. Cost: $0 (shared EDGAR
  base), plus research passes — needs its own daily cap and triage.
  Bearish items (4.02, 1.05) are measurement-only until a bearish path exists.
- **Government contract awards.** Value: known mover of small/mid-cap defense
  and government-services names; a clean catalyst with a public timestamp.
  Sources: DoD daily contract announcements (defense.gov, ~5pm ET, awards
  >$7.5M, 10–30/day) — same-day, tradeable next open; USAspending/SAM.gov
  APIs are complete but lag weeks and are noisy. Cost: $0. The real cost is
  the contractor→ticker mapping: awards name subsidiaries and JVs, so a
  curated, human-editable mapping table (like sectors.yaml) is the build.
- **Form 144.** Value: the bearish mirror to Form 4 P — a notice of PROPOSED
  affiliate sale, filed when the sell order is placed, so it LEADS the Form 4
  S by up to two business days; electronic and full-text searchable on EDGAR
  since 2023. Volume: high, dominated by 10b5-1 plan sales (the same
  exclusion Form 4 uses applies). Cost: $0, shares the EDGAR base and the
  sell-cluster recipe. But there is no bearish trading path (ruled), so today
  it can only feed the measurement rows; its case is entirely conditional on
  the Form 4 sell-cluster forward returns showing signal.
- **Recommended order once Form 4 has a month of data:** (1) 8-K item
  filtering — the judged arm's catalyst supply, volume controlled by the
  whitelist; (2) DoD contract awards — narrow, dated, needs the mapping
  table; (3) Form 144 — only if the sell-cluster measurements earn a bearish
  path. Each is a new signal source and therefore its own human approval.

Suite 1100 passed / 3 skipped; shipped as 44d9b69, verified on all three,
droplet pulled and green.

## SGOV tracking mismatch after the first live sweep — DIAGNOSED AND FIXED (2026-09-03)

Health after the first live sweep logged `audit log says 25c3dac81c9d4f8f
holds 716.825 SGOV but the broker does not; not tracking` while cash had
dropped 89,492 → 17,508 and NAV carried ~72K of ETF. **Cause:** the exit
engine's replay skipped only `strategy == "mechanical"`, so the `cash_sweep`
decision fell through, looked itself up under the JUDGED key
`("equity", "SGOV")`, missed the gate's `("cash_management", "SGOV")` position
(seeded correctly from the sweep ledger), and logged a false "broker does not
hold". It never tracked the lot — correct by accident — and the message was
wrong. **Fix:** replay skips every non-judged strategy. **The sweeper's own
ledger was always right:** `CashSweeper.replay` looks under the
cash-management key; health now proves it with a `cash management (SGOV)`
line — 716.825021972 units parked, value 71,983.57, cost 71,983.57, accrued
+0.00 (mark-to-market; distributions monthly), `[log agrees]` — and the
warning is gone from the droplet's health output.

## Review layer revised to the dialectic (ruling 2026-09-02, revising the same day's re-underwrite)

**would_open_today is EVIDENCE, not a trigger.** Contradiction rule 4 (no +
not-ahead → close) lasted one day: the entry bar is stricter than the exit bar
by design, and a selection-threshold difference is not a thesis problem. In
its place every review must produce `case_for_holding` and
`case_for_selling` — the strongest honest version of each, weighing expected
return to target vs risk to the CURRENT stop, what has CHANGED since entry
(information, not price), opportunity cost against the registry's other
candidates, exit costs (quoted spread, tax boundary), and whether a "no" on
would_open_today is a thesis problem or a threshold difference — then a
verdict `hold / trim / close` with a one-line `verdict_reason` naming which
argument won. Both cases are schema-required AND validated when omitted
(pydantic skips validators on defaults unless told; `validate_default` is
load-bearing) — a one-sided review is a rejection, which is a HOLD by the
existing fail-safe. TRIM is now the review's own verdict (the
resolution=partial auto-trigger is retired); the engine honours it once, in
profit, and otherwise records it as given and stands as a hold. Deterministic
layers untouched. Probation still shadows an argued close on a profitable,
intact position — the dialectical structure is what the shadow period is
evaluating.

**Live round trip (droplet, production `ExitReviewPass`, the real INTC
position, $0.19):** verdict **HOLD**; validity intact / on_track / unresolved;
would_open_today = **False** — but for a different reason than the two
pre-dialectic runs: R:R to a ~$115 target vs the 77.14 stop = **1.78, which
clears 1.5**; the "no" rests entirely on the near-floor second-pass rule
(confidence 54). Case for holding (1,354 chars): invalidation ~$17 away, T+3
on a 9-month thesis, the disclosure's structure (50 deep-ITM calls to June
2027) matches the resolution horizon, exit costs minimal, the "no" is a
threshold rule not a thesis failure. Case for selling (1,385 chars):
razor-edge confidence, a single lagged disclosure, structural headwinds, the
quote unavailable at review, and opportunity cost against "499 competing
signals". Verdict reason: "closing a months-horizon position at T+3 with no
new adverse information would be premature capitulation — hold wins."
**Consequence:** the ruled "let production close INTC" no longer follows —
under the dialectic the same facts hold. Recorded as the ruling intended: the
structure decides, and the shadow period grades it.

**A finding from that round trip, fixed same session:** the opportunity line
told the model "499 name(s) carry active signals in the convergence window
(candidates competing for capital and slots)" — the Form 4 market-wide feed's
footprint, not a candidate pool — and the selling case leaned on it. The line
is now built from VERDICTS (names the system actually researched in the
window: opened / declined / gate-refused / triaged out) with the raw active
count stated separately and labelled "raw feed flow, not vetted candidates".

**Golden set gains review cases** (kind `review`, replayed through the
production ExitReviewPass, graded on STRUCTURE: both cases argued past a
120-char bar and distinct, a verdict reason, the re-underwrite arithmetic):
the real INTC day-3 facts, a synthetic resolved winner (+32%, day 90), a
synthetic near-stop (78.50 vs 77.14, day 60). First replay, all three PASS:
day-3 → hold (would_open False); resolved → **trim** ("the thesis has
substantially resolved"); near-stop → **close** ("the stop is 1.7% from
price, reducing this to a mechanical stop-wait"). Three different verdicts
from three fact patterns, each argued both ways — the structure is doing what
it was built to do. ~$0.37.

Suite 1102 passed / 3 skipped; shipped as 638c6ee, verified on all three,
droplet pulled and green.

## Verdict-variance experiment — three arms, RESULTS (2026-09-02/03; ~$35)

The golden set's 20 entry cases, 3 identical-input runs each, through the
PRODUCTION ResearchPass, in three arms: **baseline** (shipped shape — sonnet
report phase with no thinking at T=1.0), **temp0** (sonnet report phase at
temperature 0), **thinking** (sonnet report phase with adaptive thinking, effort
honoured). Opus (class 1 verification) untouched in every arm — it rejects
non-default sampling outright. Full per-case table:
ops/experiments/verdict_variance_2026-09-02.txt.

| arm | mean spread | max | unanimous | graded | $/pass |
|---|---|---|---|---|---|
| baseline | 8.2 | 30 | 19/20 | 60/60 | 0.186 |
| temp0 | 5.8 | 14 | 20/20 | 54/60* | 0.184 |
| thinking | 15.2 | 47 | 19/20 | 55/60 | 0.185 |

*temp0's six "fails" are three runs each of injection-cashtag and
mirror-fabricated-buyback returning upstream_error at the same wall-clock
window. Reproduced clean afterwards: both are Class 1 cases that run entirely
on opus — the requests never carried the temperature parameter — so the
episode is a transient API failure, not the arm; every graded temp0 verdict
passed (54/54).

**Where the noise lives (baseline, by the case's mean confidence):** the ≥70
bucket (14 cases) is stable — mean spread 4.4, max 10, 14/14 unanimous. The
60–70 bucket (6 cases) is where the stochasticity is: mean spread 17.2, max
30, and the one direction split (pelosi-intc-calls-entry: long/58, no/58,
no/65). No case had a mean inside the [50, 60) boundary band — the noisy
verdicts have means in the 60s and only LAND in the band some of the time.
**Boundary confirmation's [floor, floor+10) band is therefore too narrow:**
a case that runs 52/72/75 is caught once in three. The evidence supports
widening to [50, 70) (band_width 20); it does NOT support confirming all
tradeable verdicts — ≥70 is stable.

**temp0 (the stability lever):** spread in the noisy bucket 17.2 → 4.8, max
30 → 14, every case unanimous, identical cost. Determinism is not accuracy:
T=0 SHIFTS distributions rather than centring them — the INTC entry went from
long/58, no/58, no/65 to no/42, 52, 38; UBER from 42/72/72 to 80/82/82. The
golden grades held (18/18 in the noisy bucket), so no quality regression is
visible at n=60, but the shift is real and should be watched, not assumed
away.

**thinking (the reasoning lever):** worse on every axis that matters here —
noisy-bucket spread 30.8, max 47 (taylor-ibp-small 28/25/72), a NEW direction
split (pelosi-uber-priced-in: long/42, long/62, no/72), 5 graded drifts, and
the same cost as no thinking (at effort medium the adaptive thinker spent
almost nothing — the tokens show it barely thought). Reasoning did not buy
stability or quality on these cases. The two levers are distinguishable:
temperature stabilises; thinking at this effort destabilises.

**PROPOSAL (not changed — an LLM request-path change; needs a ruling, the
golden replay, and a live round trip before any of it ships):**
1. `sampling.report_temperature: 0.0` applied ONLY to sonnet-4-6 report-phase
   calls, validated at preflight so a model that rejects non-default sampling
   (opus-5) can never receive it. Same cost, ~⅔ less spread, no splits.
2. Keep the sonnet tiers thinking-OFF; correct research.yaml's "thinking
   depth" comment on those tiers (effort there is token economy, not
   reasoning).
3. Widen `boundary_confirmation.band_width` 10 → 20 so the [50, 70) region
   the data identifies as noisy buys the second pass — and keep boundary
   confirmation even with T=0: opus cannot take temperature, and the residual
   sonnet spread (~5) is not zero.
4. Re-run the baseline arm after any of these ships (~$11) so the partition is
   measured, not assumed.

Spend: baseline $11.2, temp0 $11.0, thinking $12.8 (includes ~$1.5 of the
three review cases that joined the golden set mid-experiment and were run as
entry signals — excluded from every table above), diagnosis $0.15.

## Variance proposal — ALL FOUR RULED AND BUILT (2026-09-03)

1. **`sampling.report_temperature: 0.0` on claude-sonnet-4-6 report-phase
   calls ONLY.** `research.yaml sampling` block; `SamplingConfig` with the
   model list explicit; `_sampling_is_legal` runs at config load (= preflight,
   like pinning) and hard-fails on a listed model that rejects non-default
   sampling (the documented set: opus-5, opus-4.7/4.8, sonnet-5, the
   fable/mythos family — `REJECTS_NON_DEFAULT_SAMPLING`) or that is not
   pinned. The client adds `temperature` to the forced-tool REPORT request
   for the configured model and nothing else — never the search phase, never
   opus. Legal only because the client sends no `thinking` parameter; the
   comment at the injection point says temperature must go if one is ever
   added. **CAVEAT carried by ruling:** T=0 SHIFTS verdict distributions
   rather than centring them (INTC entry long/58,no/58,no/65 → no/42,52,38;
   UBER 42/72/72 → 80/82/82). Determinism is not accuracy; calibration
   watches it.
2. **Sonnet tiers stay thinking-OFF.** research.yaml's effort comment
   corrected: on opus-5 effort steers thinking depth and spend; on the
   sonnet-4-6 tiers it steers spend and tool terseness only, because thinking
   is off there. **Recorded:** the thinking arm at effort medium was a
   THROTTLED test — the tokens show the adaptive thinker barely thought — not
   a test of reasoning depth. Revisit only if calibration shows a sonnet
   judgment-quality problem.
3. **`boundary_confirmation.band_width` 10 → 20.** The noise lived in the
   60–70 mean-confidence bucket, not [50, 60); ≥70 was stable and stays
   unconfirmed. The code default stays 10 (test harnesses); the shipped yaml
   is 20 and a test pins it.
4. **After shipping, in order:** golden replay (required), live round trip on
   the sonnet report path, then the baseline variance arm re-run (~$11) so the
   partition is measured. Results below.

### Post-ship results (2026-09-03) — shipped as 5eb9e80, verified, droplet green

**Live round trip, sonnet report path — OK.** Production pass on
pelosi-be-calls-decline: two sonnet calls, the search phase WITHOUT
temperature, the forced-tool report WITH T=0.0, accepted; no_position/82,
graded PASS, $0.21.

**Golden replay — 21/23, ~$4.20.** Two drifts, both reviewed:
- `pelosi-be-calls-decline` → long/52 (graded set: no_position). The same
  noisy case (post-ship arm: no/45, no/72, no/72); 52 sits inside the widened
  [50, 70) confirmation band, so in production this verdict would have had to
  replicate before sizing — the replay does not run confirmation. Not caused
  by the change; it is the instability the band was widened for.
- `nolimitgains-instrumentless` → REJECTION upstream_error, API 400: "Code
  execution requested a client tool that is not included in this request's
  code execution tool function definitions." OPUS class-1 SEARCH phase — no
  temperature anywhere near it. Same failure class as the temp0 arm's six
  upstream_errors on injection-cashtag / mirror-fabricated-buyback: the
  web_search_20260209 dynamic-filtering machinery, during the search phase
  (where only web_search is offered), tries to call a client tool that is not
  in that request. It clusters on adversarial/synthetic content. In production
  it is a typed rejection (no trade) plus an ERROR line and urgent alert — safe,
  but a research pass lost. See the finding below.

**Baseline variance arm re-run on the shipped shape — the temp0 arm's
stability DID NOT REPLICATE.**

| arm | all 20: spread mean/max, unanimous, graded | 60–70 bucket: spread mean/max, unanimous |
|---|---|---|
| baseline (pre-ship) | 8.2 / 30, 19/20, 60/60 | 17.2 / 30, 5/6 |
| temp0 arm | 5.8 / 14, 20/20, 54/54 | 4.8 / 14, 6/6 |
| **post-ship (T=0 live)** | **10.7 / 42, 18/20, 60/60** | **16.0 / 42, 4/6** |

Same cost ($0.181), no errors, every verdict graded — and no stability gain:
INTC entry no/47, no/63, long/58 (split); UBER long/38, no/72, no/30 (a new
split, 42 points); BE 45/72/72. **Reading:** the temp0 arm's 4.8 was an
18-pass sample that got lucky. T=0 makes the report call deterministic GIVEN
its transcript, but the transcript comes from the search phase — still T=1.0
over live web results — and that is where the variance originates. The
report phase was never the noise source; the proposal overstated the lever.
What survives: the change is harmless (no drift, no cost, no splits it
caused), and boundary confirmation at band 20 is clearly the operative
protection — two of the three splits sit in 55–63. Raw verdicts archived:
ops/experiments/variance_baseline_post_T0_2026-09-03.jsonl.

**Options for a ruling (nothing acted on):** (1) keep T=0 as shipped with
the honest label — not a stability lever; rely on band-20 confirmation.
(2) extend T=0 to the sonnet SEARCH phase (legal: no thinking on sonnet) —
the untested lever; needs its own ~$11 arm before any claim. (3) revert T=0
to keep the request shape minimal; nothing in the data argues for it.

**Finding — the code-execution 400 (opus search phase):** seven occurrences
today across the experiment and replay, all on class-1 cases with adversarial
or synthetic content, none on class 2/3. Candidate mitigation is a
request-path change and therefore a ruling: offer `submit_research` in the
search phase too (tool_choice auto, phase 2 still forces it) so a model that
reaches for the report tool mid-search has a legal target — OR accept the
intermittent typed rejection as the cost of the two-phase design and watch
its frequency in production: ZERO occurrences of this message in the
production audit log to date (its 7 upstream_error records carry other
messages; checked 2026-09-03).

## Overreaction-fade hypothesis — DESIGN REVIEW ONLY, nothing ships (2026-09-03)

Staged so the free measurement answers the prior question before any prompt
exists. Every step below is deferred; step 1 is itself a NEW SIGNAL SOURCE and
needs explicit approval when it ships.

**1. Deterministic detection (measurement-only).** Universe in two logged
tiers: CORE = held judged positions + registry names with a research verdict
in the window + names carrying a qualifying purchase signal (cluster-passing
Form 4, prefilter-passing congressional purchases, 13D initial/increase,
13F-family longs); BROAD = every purchase-side active registry name, raw
Form 4 singles included (the control). Event = completed SIP daily bar with
close-to-close return ≤ −7% (6% and 8% logged as flags for tuning) AND
session volume ≥ 1.5× the 20-day average share volume; detection ~16:15 ET or
at next start. Logged with SPY's same-day return and the mapped sector so
market/sector days split from idiosyncratic days deterministically. Row:
source `overreaction_screen`, code `overreaction_candidate`, class 2, OUTSIDE
convergence (a market-data screen, like PEAD); rides the existing `measurement`
prefilter rule and funnel exactly as bearish_measurement does; no research,
no trade, no dispatch weight, no registry note, never seals the name.

**2. Grading.** Forward engine at 1/5/20/60d excess (1d is a small addition).
Slices: core vs broad, market-day (SPY ≤ −2%) vs idiosyncratic, held vs
signalled, by threshold flag. No reversion anywhere = stop, free. Because
detection is close-to-close on SIP bars the measurement can be BACKFILLED over
every name that entered the registry since 2026-08-17 and over the stress
windows — weeks instead of months to an answer.

**3. The LLM half (only if reversion shows).** A classifier pass: fundamental
cause (earnings/guidance/legal/contract loss — drift, don't fade; the same
territory as the invalidation condition, which is why the step cannot be
skipped) vs non-fundamental (sympathy, forced flows, headline noise — fade
candidate). New prompt + new trade path → approval, golden cases (fabricated
headline drop, real guidance cut, sector-sympathy day), live round trip; after
the freeze lifts and only once step 2 earns it. Cost trivial; sample size is
the constraint — 60d grading of the FADED subset reaches n≥20 in ~a year.

**4. Averaging-down tension, stated.** On held names a fade add contradicts
the into-strength scaling rule directly and lands inside the adverse-review
trigger zone by construction. A carve-out, if the data ever justifies one:
one add per position lifetime; non-fundamental classification required AND
the same-cycle dialectical review concluded hold with would_open_today = yes
at the post-drop price; price above the stop with R:R ≥ 1.5 recomputed from
the new price; no add under the drawdown ladder (<×1.0) or any halt; total
position ≤ the band cap for current confidence (an add fills room the table
already allows, never enlarges it), through the gate, liquidity check and
daily deployment like any entry; tagged `fade_add` for separable attribution
and killed if it fails to beat the un-added counterfactual over 60d; mechanical
arm excluded.

**Expected volume.** ≤ −7% close-to-close base rate ~0.4–0.6% of stock-days
(large cap), 1–2% (small/mid). Core (~20–40 names): ~0.1–0.3 events/day →
2–6/month → 60d n≥20 in 4–8 months (backfill shortens it). Broad (~500 names
with the Form 4 feed): ~2–5/day → n≥20 within a month; broad answers "does our
universe revert at all", core answers "where we would act". If step 3 ever
ships: ~1–3 non-fundamental core candidates/month — a low-frequency overlay,
not a book.

## Rulings on the post-ship results (2026-09-03) — RECORDED

1. **T=0 stays as shipped, honestly labelled:** harmless, not the stability
   lever it appeared to be. No search-phase arm — live web results vary
   regardless of temperature, and band-20 boundary confirmation is the
   operative guard. Revisit only if production shows floor-band verdicts
   sizing unconfirmed.
2. **Code-execution 400 (opus search phase): accepted as an intermittent
   typed rejection.** Its production frequency is now a line in the weekly
   report (upstream errors in window; code-execution 400s among them —
   currently zero). No request-path change during the freeze; if it recurs in
   production, the submit_research-in-search-phase design is the fix.

**Freeze holds:** no research-layer changes and no non-production API runs
until the paper period has data on the shipped config.

## Overreaction-fade — RULINGS (2026-09-03): measurement half BUILT, LLM half DEFERRED, carve-out RECORDED

**1. Measurement half — built, and this is the source's approval.** Source
`overreaction_screen` (registered in orchestrator.yaml as a market-data screen,
not a fetched feed: no scanner, no research tier, outside convergence), code
`overreaction_candidate`, class 2, measurement-only rows that never seal the
underlying name (their external id is `SYMBOL:SESSION` under their own
source). `python -m orchestrator overreaction` scans completed SIP daily bars:
CORE = held judged positions + names with a research verdict in a 60-day
window; BROAD = every other purchase-side active registry name (raw Form 4
singles included — the control). Event = close-to-close drop reaching the
lowest flag (6%) AND volume ≥ 1.5× the prior 20-day average; the ruled X = 7%
and 6%/8% ride the row as flags; SPY's same-day return, market-day flag (SPY
≤ −2%), sector, tier and held are stamped as labelled content lines the
funnel parses back. Idempotent per (name, session). The registry now ignores
measurement-only rows on both paths (note_signals and seed) — this also takes
the Form 4 sell-cluster measurement rows out of convergence, where they never
belonged. Forward report: "Overreaction candidates" with 1/5/20/60d excess
(POSITIVE = reverted) for all events and the four slices — core/broad,
market/idiosyncratic day, held/signalled, ≥7%/≥8%. Timer:
ops/vps/agentic-overreaction.timer, weekdays 16:45 ET (after the earnings
shadow). No LLM, no prompt, no API spend beyond Alpaca bars — consistent with
the freeze. Backfill from 2026-08-17 and across the three stress windows:
results below.

### First read (2026-09-03, droplet): NO REVERSION — the prior question answers "stop"

Universe 404 names (17 core, 387 broad). Live period 2026-08-17 → 09-03:
17 events (1 core, 16 broad; 4 names returned no bars — dead tickers in the
raw flow). Stress windows (today's universe replayed over 2018-Q4, 2020-02/03,
2022-01/06; 204 sessions): 1,369 events (67 core), 76 unmeasurable
name-sessions, 16 names without bars. Forward report, excess vs SPY,
POSITIVE = reverted:

| slice | n | 1d | 5d | 20d | 60d |
|---|---|---|---|---|---|
| all (≥6% flag) | 1,386 | +0.09 | −0.21 | −0.83 | −0.94 |
| core (held or researched) | 68 | −0.01 | −0.46 | −1.42 | +0.33 |
| broad | 1,318 | +0.09 | −0.20 | −0.80 | −1.00 |
| market day (SPY ≤ −2%) | 971 | +0.30 | +0.13 | −0.40 | −0.29 |
| **idiosyncratic day** | 415 | **−0.41** | **−1.03** | **−1.89** | **−2.56** |
| held positions | 6 | −0.11 | −0.10 | −0.22 | −0.71 |
| ≥7% (the ruled X) | 1,056 | +0.07 | −0.13 | −0.60 | −0.67 |
| ≥8% | 801 | +0.01 | −0.27 | −0.56 | −0.53 |

**Reading.** Sharp drops on volume in our universe do not revert; they DRIFT.
The idiosyncratic-day drops — exactly the fade candidates the hypothesis cared
about — are the worst slice at every horizon (−1.0% at 5d, −2.6% at 60d).
Market-day drops show a faint 1–5d bounce (+0.3/+0.1%) that fades to
negative by 20d; that is the market bouncing, not the name. Deeper drops do
not revert more. By the ruling's own test — "if they don't revert, no
materiality judgment saves the strategy and we stop here for free" — the LLM
half's precondition (5/20d reversion) is NOT met.

**Caveats, stated.** (1) The sample is dominated by crash regimes (2020-03,
2022) where continuation is the norm; 971 of 1,386 events are market days and
cluster on the same sessions, so they are far fewer independent observations
than n suggests. (2) Today's universe on historical bars carries survivorship
in the direction that would FAVOUR reversion — and there is still none.
(3) The live, in-regime sample is 17 events with 1–5d marks only. The screen
keeps running at zero cost; the question is re-asked when the live sample
reaches n≥20 at 20d, and the answer would have to invert to reopen step 3.

**2. LLM half — DEFERRED** until the freeze lifts AND step 2 shows 5/20d
reversion. **Golden classifier test set, recorded now for then:**
(a) a fabricated-headline drop — a real name, a −8% session on a rumour later
denied, cause NON-FUNDAMENTAL (fade candidate); (b) a real guidance cut — a
−9% session on lowered full-year guidance, cause FUNDAMENTAL (drift, do not
fade; and it is the invalidation condition's territory); (c) a
sector-sympathy day — a −7% session when the name's sector ETF fell −6% on a
peer's print with no company-specific news, cause NON-FUNDAMENTAL (fade
candidate, market/sector day). Each graded on the classification, the
must-cite evidence, and a structural bar like the review cases.

**3. Averaging-down carve-out — RECORDED, NOT BUILT,** bounded as designed:
one add per position lifetime; non-fundamental classification required AND
the same-cycle dialectical review concluded hold with **would_open_today = yes
at the post-drop price — the condition that matters**; price above the stop
with R:R ≥ 1.5 recomputed from the new price; no add under the drawdown
ladder (<×1.0) or any halt; total position ≤ the band cap for current
confidence (an add fills room the table already allows, never enlarges it),
through the gate, liquidity check and daily deployment like any entry; tagged
`fade_add` for separable attribution and killed if it fails to beat the
un-added counterfactual over 60d; mechanical arm excluded.

## INCIDENT AVERTED (2026-09-03): SIP bars refuse queries into the last 15 minutes

The overreaction backfill's first run scanned 404 names over 14 sessions and
found ZERO events with zero unmeasurable name-sessions - not a quiet market, an
empty bar list for every name. Probed: Alpaca's free data plan returns HTTP 403
for a SIP daily-bars query whose end is inside the last 15 minutes (end=now ->
403 and 0 bars; end=now-20min -> 22 bars). AlpacaDailyBars honestly returns []
on any error, so the failure was silent.

THE SAME PATTERN WAS LIVE IN THE LIQUIDITY GATE: AdvSource fetched
bars(symbol, now - lookback, now) on the SIP feed, so at the next session every
ADV would have come back None and the gate - which fails CLOSED by ruling -
would have rejected EVERY opening order as illiquid_position (both arms; the
sweep is exempt). Caught before the 2026-09-03 session opened, by the screen's
implausible zero. Fix: AdvSource ends its window YESTERDAY (a 20-day average is
a completed-sessions fact) and the screen clamps its fetch end to now - 1h;
both pinned by tests; the screen now counts and shouts when every fetch returns
nothing ("this run measured nothing"). Lesson recorded: a SIP request's end
must never be "now"; and a data source that returns [] on error needs its
consumers to distinguish "quiet" from "blind".

## Funnel hygiene (2026-09-04): non-US symbols, unserved-symbol memo, filer-name normalisation

**AXIA3.** A Brazilian foreign private issuer (AXIA Energia S.A.) filed a
Form 4; `issuerTradingSymbol` = AXIA3 (a B3 symbol) entered the funnel as a
no_cluster control row and the bars API returned HTTP 400 on every weekly
report. It was the only non-US-shaped symbol in the funnel. Two layers, both
deterministic: a US-listed symbol-shape rule (1–5 letters, optional .A/-A
class suffix) at the structured-filing ingest points — Form 4 issuer symbols
(purchase and sale paths; tallied `foreign_symbol`), 13D display-name tickers,
the overreaction screen's universe — and `UnservedSymbols` in the bars client:
a permanent client error (400/404/422) memoises the symbol for the process
and persists it to `data/unserved_symbols.json` so no later run asks again
(403 data-plan, 429 and 5xx stay transient). Verified on the droplet: one
AXIA3 line, then the memo. The mechanical arm's broker `tradeable_equity`
check at entry is unchanged and remains the authority at the point of entry.

**Filer names.** "John J Mr Mcguire Iii" (8 records) and "John Mcguire" (6)
were two credibility keys for one person — a split track record; the log also
carried "A. Mitchell Jr. McConnell" and "Richard Dean Dr Mccormick". Rule
(`signals.filers.normalize_filer`): drop honorifics and generational suffixes
wherever they sit, drop single-letter initials, title-case, then a small alias
table for what the rule cannot see (`filer_aliases` on the congressional
source; default: "Mitchell Mcconnell" → "Mitch Mcconnell"). Applied at
INGEST (Quiver keys and `representative` are canonical; the feed's spelling is
kept as `representative_filed_as` when it differs) and ON READ everywhere a
congressional key is grouped — the forward funnel, the convergence registry's
identities, the credibility tracker, the filer-event match in the exit engine
— because the audit log is append-only: existing records are re-keyed as they
are read, never rewritten. Under-merging is the chosen failure mode: two keys
for one person split a record; one key for two people corrupts two.

**Report legibility.** By-filer rows with no resolved marks collapse into one
count line ("N filer(s) with no resolved 20d marks yet (M signals): …") instead
of 57 rows of "no resolved marks yet".

## INCIDENT (2026-09-04 → 09-08): first live unsweeps never filled — DIAGNOSED AND FIXED

**What health showed.** After five mechanical entries took cash to $13,960
(buffer ≈ $17.5K), the sweeper correctly triggered an unsweep, and health
listed two orders "pending settlement": `25c3dac81c9d4f8f sell 0 SGOV`, twice.

**What actually happened, from the audit log and the venue.**

| attempt | deficit | units asked | limit | venue outcome |
|---|---|---|---|---|
| 2026-09-04 17:20Z | $870.14 | 8.660669726 | 100.47 (the ask) | `canceled` 20:00:00Z, filled 0 |
| 2026-09-08 13:40Z | $4,046.29 | 40.269611249 | 100.48 (the ask) | `canceled` 20:00:00Z, filled 0 |

Three separate defects, none of them the sizing arithmetic:

1. **"sell 0" was a rendering bug.** `pending_settlement` hard-coded
   `quantity=ZERO` for exit rows. The orders asked for 8.66 and 40.27 units —
   deficit ÷ limit, rounded UP at the venue's 1e-9 step, capped at the lot.
   That math was and is correct. (A $4,046 hole is a ~$3.4K deficit, not
   $4K: the buffer is NAV-scaled and moves with the cash, so the ~35-unit
   estimate was right for the deficit the sweeper actually measured.)
2. **The sells rested at the ask and never printed.** The price source
   returns the ASK (the correct bound for buys; the sweep buy at
   `ROUND_UP(ask)` filled in five minutes). An unsweep sell limited at the
   ask on a T-bill ETF quoted a cent wide that moves a cent a day sits on
   the book all session; Alpaca cancels day orders at the 16:00 close. Both
   day sells died that way, zero filled, and cash stayed below the buffer for
   four days.
3. **Nothing closed the attempt in the log.** On a terminal-unfilled order
   the sweeper (and the judged exit engine) released the gate reservation
   and moved on; the `ExitRecord` stayed "submitted, no fill" forever, so
   health listed it as pending on every run — and startup recovery, seeing
   `canceled / 0`, wrote nothing either ("nothing sold" was silently
   returned as "nothing to record").

**Fixes.**

- **Unsweep sells price at the BID**, rounded down (`AlpacaPriceSource.bid`,
  same staleness rules as the ask, never substituted by the ask; wired via
  `CashSweeper(bids=…)`). With no bid quoted: one cent under the ask. Either
  way the limit is a floor — a marketable limit fills at the best available
  price, so pricing under the bid costs nothing and pricing at the ask cost
  the fill.
- **Release records.** `AuditLog.record_unfilled_order` writes an
  execution-stage rejection naming the broker order id (new optional
  `StageRejectionRecord.broker_order_id`) whenever an order terminates
  unfilled — from the sweeper's settle, the exit engine's settle, and
  startup recovery. `pending_settlement` treats a released order as
  settled, renders the quantity the order ASKED for, and carries the
  broker order id so a human can look it up at the venue. Release records
  are an order's fate, not a signal: funnel (no bucket for EXECUTION),
  registry and what-if skip them explicitly.
- **Sweep buys are named** (`client_reference=decision_id`) so recovery can
  ask the venue about one by name if the process dies mid-flight.
- **Zero-quantity orders were already unrepresentable** — `ShareQuantity`
  is `gt=0`, ≤9 dp — and the sweeper never constructed one. A test now
  pins that the sweeper's order types refuse 0, negatives and sub-step
  quantities, alongside the existing `test_share_quantity_refuses_zero_and_negative`.

**Tests** (7 new): deficit → units at the bid (35 units on the whole-share
venue for the test's $3.4K deficit, `qty × limit ≥ deficit`); no-bid
fallback; zero/negative/sub-step quantities refused by the schema; a sell
cancelled unfilled at the close is released, disappears from pending, and
the same pass retries at the bid; startup recovery clears the droplet's
exact state and the first pass retries; sweep buys carry the decision id;
`bid()` freshness and never-the-ask; the judged exit engine's unfilled exit
is released and re-fires.

**Confirmation still owed.** The droplet's two dead orders are released by
recovery on the next `health`/startup (both venue answers are `canceled / 0`).
Cash returning to the buffer needs a market session: the retry prices at the
bid at the next tick after 09:30 ET on 2026-09-09 and should print like the
buy did. Check health then — `cash management` line and no pending rows.

## Record-keeping from INTC's close and RWT's entry (2026-09-15) — three items, behaviour unchanged

Freeze-compatible: no prompt, tier, model or research-layer change; no
non-production API run. The golden set gained a case but was NOT replayed.

### 1. Exit reasons: rule-forced closes stop recording as invalidations

INTC closed 2026-09-09 via contradiction rule 2 (`validity=displaced`, hold →
close) with `invalidation_triggered=false`, and the ExitRecord said
`thesis_invalidated`. Two new members of the log vocabulary:

- `ExitReason.THESIS_DISPLACED` — rule 2, the position moved for reasons the
  thesis never predicted.
- `ExitReason.THESIS_RESOLVED` — rule 3, `resolution=substantial` with no
  `continuation_thesis`: the thesis WORKED and nobody wrote down the new bet.

`orchestrator.exits.review_close_reason` maps a closing review's own findings
to the reason with the rules' precedence (dead thesis → invalidated; displaced
→ displaced; resolved without continuation → resolved; otherwise the historical
name). The reason lives on the tracked position (`close_reason`) so a re-fire
after a broker refusal or a restart records the same reason the review earned
— replay rebuilds it from the ThesisReviewRecord. Attribution's
"judged exits by reason" table grows the rows automatically. INTC's existing
record is NOT rewritten (append-only); the golden case below carries the
correction.

**Ruled the same day: `ExitReason.REVIEW_CLOSE`.** An EXPLICIT `action=close`
on a thesis the review still calls intact and unresolved — the model's own
judgment, no contradiction rule — is its own reason, because it is exactly the
category the exit-authority probation (ruling 2026-09-02) grades, and
attribution must read it apart from the rule-forced reasons. Four review-driven
reasons now: `thesis_invalidated` (dead thesis), `thesis_displaced` (rule 2),
`thesis_resolved` (rule 3), `review_close` (judgment). Records before
2026-09-15 carry `thesis_invalidated` for all four.

### 2. Golden review case: `review-intc-day9-post-blowout-real`

INTC's 2026-09-09 facts frozen from the audit log: entry 90.75, price 104.52,
day 9/274, confidence 54, resolution date 2027-06-01, stop 77.1375, 50% trim
taken 09-08, Q2-2026 blowout public. Graded expectation `validity ∈ {intact}`,
verdict `hold` or `close` (trim is off the table after the trim), structure
still graded. The live review called it "displaced" because INTC had peaked at
~141 in June BEFORE entry — pre-entry price history is not displacement, and a
blowout quarter is exactly the appreciation-into-mid-2027 move the thesis
underwrote. `GoldenCase.expect_validity` / `grade_review` gained the validity
grade; the summary line now shows `validity=…`. This is the test the next
prompt revision runs against; the review prompt is unchanged today.

### 3. RWT (ef97909b502143f5): the second pass ran; the record could not show it

Confidence 60 landed inside [50, 70). The log at 18:36:02Z says "buying a
second independent pass"; four further Anthropic calls follow (18:37:53 →
18:40:08Z) before the decision at 18:40:08Z. The entry SIZED, which under the
lower-confidence rule means the second pass returned `long` at ≥ 60 — but its
actual confidence is unrecoverable: the confirmed branch logged nothing and
the record stamped only the sized report. Two visibility gaps, both closed:

- `DecisionRecord.boundary_confirmation` (new, optional): floor, band width,
  both directions and confidences, `sized_from` ("first"/"second"), and the
  second pass's estimated cost. Absent = the verdict sat outside the band.
- The second pass's usage now folds into the decision's `est_cost_usd` /
  token estimates. RWT's record carries one pass's cost (est. $0.35: screen
  $0.16 + verification) for two passes' calls — the month-to-date estimate is
  understated by roughly one full pass per floor-band entry since 2026-09-02.
  Console bill is truth.
- The confirmed branch logs `boundary confirmed on …: first long/60, second
  long/N; the first pass sizes`.

## RISK-ON RECALIBRATION + SHORT-DATED OPTIONS TEST — human ruling 2026-09-15

The prompt/experiment freeze LIFTS for this bundle only and re-closes after it.
One golden replay and one live round trip for the whole bundle (step 2). Dated
2026-09-15 for attribution partitioning (before/after). Four steps, a commit
each, pushed to both hosts verified, droplet pulled:

1. config changes + tests (THIS ENTRY)
2. both options doors + theme→ETF map + prompt (golden replay + live round trip)
3. Form 4 C-suite singles
4. 8-K Class 1 source

Hard review dates: **2026-10-15** for the sources — if no source shows positive
forward excess by then, that is the finding. **2026-10-30 or n≥10 resolved
short-dated contracts**, whichever first: keep / widen / kill on the
counterfactual-equity line.

### Step 1 — config (shipped this entry)

| knob | was | now |
|---|---|---|
| sizing bands 50-70 / 70-85 / 85+ | 1% / 2.5% / 7% | **2% / 5% / 10%** |
| sizing.hard_cap | 0.07 | **0.10** |
| equity_sleeve.max_single_position | 0.07 | **0.10** (moves with the table) |
| equity_sleeve.max_daily_deployment | 0.15 | **0.25** |
| equity_sleeve.max_sector_exposure | 0.15 | **0.25** |
| reward_risk.min_ratio | 1.5 | **1.3** |
| exits.ratchet.arm_at_gain | 0.20 | **0.12** (trail 0.10 unchanged) |
| risk_scalars.drawdown_steps | 0.04→0.75, 0.08→0.5 | **0.06→0.75, 0.10→0.5** (multipliers unchanged) |
| equity_sleeve.max_short_dated_premium_at_risk | — | **0.05** (new; validated ≤ the 0.20 aggregate; ENFORCED in step 2) |

Unchanged: floor 50, boundary confirmation [50, 70), ATR stops, options
halving, never-negative, no margin, long-only options, deterministic gate,
kill switch 0.12, mechanical arm caps, verification rules, exit-authority
probation. CLAUDE.md position caps and sizing table updated with the dated
ruling. `options_selection.min_expiry_days.days` 14 → 7 is deliberately NOT
in this step: alone it would let 7-DTE contracts through the existing
catalyst path before the T-1 close rule and the 0.05 short-dated cap exist
(Constraint #6) — it ships with the doors in step 2.

**Consequences to expect after the bounce.** (a) The cash-sweep buffer is
`0.25 × judged NAV + 0.15 × mechanical NAV + reservations + $2,500` ≈ $25K on
a $100K account (was ≈ $17.5K): the first tick will unsweep ~$11K of SGOV to
refill it — expected, not a fault. (b) Every held position keeps the stop and
size it was opened with; the new table applies to NEW judged entries only.
(c) With the ladder's first rung at 6%, the current 0.44% drawdown is far
from any scalar; the ratchet arms at +12% on new and existing positions
(the ratchet reads config at each check, not at entry).

Tests: 41 pinned quantities recomputed (26 shares at 5% of the harness
sleeve, 747-unit sweep under the $25K buffer, 7 contracts on the halved 5%
band, sector-cap and single-cap boundaries at 25%/10%); the ladder default in
code mirrors the ruling.

### Step 2 — options doors, theme→ETF, prompt (shipped this entry)

**Conviction door.** `options_selection.conviction_min_confidence: 80`. A report
at or above it with a stated `time_horizon` (every report states one by schema)
reaches the option selector WITHOUT a catalyst — same chain fetch, same delta /
OI / spread / IV gates, same halved table, same expiry close. Recorded as
`expression.door = "conviction"`, tagged `conviction_option`. The catalyst door
is unchanged and now records `door = "catalyst"`.

**Short-dated test.** `min_expiry_days.days` 14 → 7 (weeks 60 / months 180
unchanged). A contract under `short_dated_dte` (21) days to expiry AT ENTRY is
tagged `short_dated_option` (the tag wins over `conviction_option` when both
apply; the door stays on the record), closes at
`short_dated_close_before_expiry_days` (1, T-1) instead of the standard 5, and
draws on `equity_sleeve.max_short_dated_premium_at_risk` 0.05 of sleeve NAV
inside the 0.20 aggregate — new gate rejection
`max_short_dated_premium_exceeded`. Gate positions now carry `expiration` and
`short_dated_at_entry`; a contract seeded from the broker after a restart has
unknown entry timing and COUNTS toward the pool when inside the window today
(Constraint #6: over-counting is the safe error). The exit engine rebuilds the
flag at replay from expiry and entry date.

**Theme → ETF expression.** `signals.yaml trump_posts.theme_etf_map` (PROPOSED
list, awaiting confirmation): tariffs XLI, energy XLE, defense ITA, financials
XLF, rates TLT, semis SMH, broad SPY, tech QQQ (the ruling's "SPY/QQQ" split
into broad-market and big-tech stems). A no-ticker post from a configured source
matching exactly ONE theme is stamped `theme`/`theme_etf` in metadata before
research (`signals.themes.ThemeEtfMap`, deterministic, applied in the pipeline);
the prompt carries a "theme -> ETF proposal" line saying the model may DECLINE;
a decision whose report named the ETF is tagged `theme_etf`. Two matching
themes = ambiguous = no mapping. Posts that name a ticker are never mapped.

**Prompt (research-layer change, freeze lifted for this bundle).** SYSTEM_PROMPT
gained "HOW A VERDICT IS EXPRESSED" naming the three doors and the T-1 rule,
with the instruction not to inflate confidence or invent a catalyst to reach an
option. Two stale numbers corrected in the same pass: "hard 5% cap" → 10%
(the table has been 7% since 2026-08-28 and is 10% since step 1), "confidence
below 55" → 50 (floor since 2026-08-28).

**Measurement.** `ExpressionSnapshot` gained `door`, `tag`, `theme`,
`underlying_price` (the underlying's quote when the expression was chosen);
option exit fills record `underlying_price`. Attribution renders "Options doors
and theme->ETF expressions" — one row per tag (open/closed/won/P&L/deployed, by
exit reason) plus a line per contract: option P&L vs the SAME dollars in the
underlying between the entry and exit quotes. The forward report gains "By
expression tag"; `FunnelEntry.expression_tag`.

**Validation.** Golden replay and live round trip: results recorded below once
run on the droplet. `python -m orchestrator golden` exit 3 = drift for human
review.

### Step 3 — Form 4 C-suite singles (shipped this entry)

A single code-P open-market purchase of **>= $250,000** by an insider whose
structured `officerTitle` names the **CEO, CFO, COO or President** qualifies
for research WITHOUT a cluster. `signals.form4.is_c_suite_title` is the rule:
the title is split on its separators and any part naming one of the four
offices qualifies unless it carries vice / VP / assistant / deputy / interim,
and a divisional / regional / subsidiary president never qualifies — so
"Executive Vice President and Chief Financial Officer" passes on the CFO part
and "Senior Vice President" does not. The filer's OWN purchase must clear the
floor (not the window aggregate). Items carry `c_suite_single: true` and
`qualifies: cluster | c_suite_single | single`; the content gets a "C-SUITE
SINGLE:" line. The prefilter's `require_cluster` honours the mark; singles
below the floor stay the `no_cluster` control group exactly as before. Prompt
guidance for Form 4 now describes both doors and says a single, however
senior, is one person's judgment. Forward report: "Form 4 doors" grades
clustered / C-suite singles / control singles apart (older records grade too:
the door is parsed from the content the fetcher wrote). Floor
`c_suite_single_min_usd` lives in the fetcher as part of the recipe.

### Step 4 — Form 8-K item-filtered Class 1 source (shipped this entry)

`signals.form8k.Form8KFetcher`, source `form_8k` in class_1 (human approval =
this ruling). Probed EDGAR full-text search live before building: 8-K hits
carry the filing's `items` list, the issuer's ticker in `display_names`,
`period_ending` (event date) and `file_date` — 124 hits for "Item 5.02" over
four days, so the whitelist is what makes the source tractable. Per poll: one
throttled listing per whitelisted item (5.02 / 4.02 / 2.05 / 1.01 / 1.05),
unioned by accession; a filing with no US-listed ticker in its display name is
dropped (nothing to trade); items outside the whitelist never emit. Bearish
items 4.02 / 1.05 → `measurement_only: true` → the prefilter's existing
measurement rule (code `bearish_measurement`, outside convergence); a filing
carrying a bearish AND a researchable item is measurement-only (Constraint
#6). The primary document is fetched from the Archives index and a 2,500-char
text excerpt rides the content (fenced as untrusted like every source);
excerpt failures degrade to the hit's facts, never drop the filing. The
fetcher self-throttles to one EDGAR sweep per 5 minutes (the FTS index moves
in minutes; Class 1 polls in seconds). `daily_research_cap: 6`,
`max_report_age_days: 3`. Class 1 scanner now prefers a fetcher-provided
`ticker` field over cashtag extraction. Prompt: an 8-K guidance branch (the
filing-day reaction is not the trade; item-specific drift is; a scheduled
follow-on inside the horizon is the catalyst, the filing having happened is
not; 5.02 cuts both ways). Family `issuer_filings` (sixth). Forward report:
"8-K by item" rows, bearish items labelled. Hard review **2026-10-15**.

### Validation — the bundle's live round trip and golden replay (2026-09-15/16, droplet)

**Live round trip — theme→ETF path through the production pass: OK.**
`ops/experiments/theme_etf_round_trip.py` ran a synthetic no-ticker steel/aluminum
tariff post with the "tariffs → XLI" proposal stamped exactly as the pipeline
stamps it, through the production ResearchPass (opus-5 verification, sonnet
screen, the revised SYSTEM_PROMPT). Request shape accepted; typed verdict
**no_position / 74**, time_horizon days, catalyst present, est. $0.31. The model
**declined the mapping on instrument grounds**: "XLI holds steel/aluminum
CONSUMERS (CAT, DE, BA, HON, RTX, UNP, UPS), not producers — domestic mills
(NUE, STLD, CLF, X) sit in Materials, and the pure-play tariff beneficiaries
would be XME/SLX" — and declined the trade on information grounds (Section
232 duties have been law since 2025; policy staleness, not reporting lag).
The decline path works as designed. **For the ETF confirmation:** the
tariffs→XLI row is arguably the wrong instrument for a steel/aluminum tariff
(the model's XME/SLX point stands); no change without your ruling — the list
is marked PROPOSED in signals.yaml.

**Golden replay — run after step 3 so the Form 4 guidance change was inside it
(step 4's 8-K branch is exercised by no golden case).**

**Golden replay — 23/24 PASS, ~$4.38, exit 3 (drift for review).** Run on the
droplet at step 3 (commit fcf5090). One drift, the SAME noisy floor-band case
as the 2026-09-03 replay:

- `pelosi-be-calls-decline` → **long/62, target 310** (graded set: no_position;
  2026-09-03 replay: long/52; the post-ship variance arm ran no/45, no/72,
  no/72). 62 sits inside the [50, 70) boundary band, so in production this
  verdict would have bought a second independent pass before sizing — the guard
  the band exists for. Reviewed, not acted on: the graded decline stands.

Passes worth noting for this bundle:
- `review-intc-day9-post-blowout-real` (new, step 2 of the previous session):
  **close, validity=intact** — the current review prompt did NOT reproduce the
  2026-09-09 "displaced" mislabel; the verdict reason cites R:R 1.33 < 1.5 from
  the frozen facts. The case now guards against regression rather than
  documenting a live defect.
- `form4-intc-cluster`: long/62 target 30 under the revised Form 4 guidance
  (both doors described) — unchanged direction, same band.
- All four review cases argued both sides past the structural bar
  (749–1157 chars each) with a named winner.
- Every no_position decline held (13F stale, priced-in, non-market, injection,
  fabricated buyback), all at 62–95 confidence.

Cost per case $0.07–$0.32; the sonnet-tier structured sources (13D, Form 4,
UW) at the low end, opus-tier prose at the high end.


The freeze re-closes with this entry: no research-layer changes and no
non-production API runs until the paper period has data on the shipped bundle.

## Theme → ETF shortlists — CONFIRMED (human ruling 2026-09-16)

The ETF list moves from PROPOSED to ruled, and from one ETF per theme to a
SHORTLIST the research pass picks from (or declines): tariffs [XLI, XME, SLX];
china/trade [FXI] (new theme; stems china / chinese / beijing / trade deal /
trade war / trade talks — moved off the tariffs theme, whose stems are now
tariff / import duty / Section 232 / Section 301); energy [XLE, XOP]; defense
[ITA, XAR]; financials [XLF, KRE]; rates [TLT, IEF]; semis [SMH, SOXX]; broad
[SPY]; tech [QQQ]. Prompt guidance per the ruling: the shortlist is a
proposal; the model names the ONE instrument whose holdings actually bear the
theme's exposure and says why ("an index of the theme's CONSUMERS is not the
theme's expression" — the 2026-09-15 round trip's finding, now in the
prompt), or declines. `theme_etf` metadata carries the comma-joined shortlist;
a decision is tagged `theme_etf` when the report names exactly one shortlisted
ETF. A post matching two themes (a China tariff post touches both tariffs and
china_trade) still gets NO mapping under the 2026-09-15 ambiguity rule — a
one-line ruling could union the shortlists instead; not done without it.

Validation: the theme line is prompt text no golden case exercises, so the
change was validated by re-running the theme→ETF live round trip, not by a
golden replay (which would spend ~$4.40 to exercise nothing new).
**Round trip (2026-09-16, droplet, commit 37ef087): OK.** Same synthetic steel/
aluminum tariff post; shortlist stamped (XLI, XME, SLX); production pass
accepted (opus-5 verification, sonnet screen), est. $0.34. Verdict
**no_position / 80**, tickers **[XME]** — the model did exactly what the ruling
asks: it named the shortlisted instrument whose holdings bear the exposure
(XME, domestic metals & mining), rejected XLI as steel CONSUMERS and SLX as
dominated by FOREIGN producers who are the tariff's targets, and then declined
the trade on policy staleness (the 25%/50% Section 232 duties were priced in
2025). The decline path and the pick-one path both work; a live tariff post
with genuinely new policy would express through XME.

## Dual-theme posts: UNION the shortlists (human ruling 2026-09-16)

Replaces the 2026-09-15 ambiguity rule (two themes → no mapping). A post
matching several themes now gets the deduplicated union of their shortlists in
config order, capped at `MAX_CANDIDATES` = 6; if the union would exceed the
cap, the two highest-weighted matched themes' lists are kept (new optional
`weight:` per theme in `theme_etf_map`, ties by config order) and truncated to
six. `theme` metadata joins the matched names ("tariffs+china_trade"); the
prompt line says "theme(s)" and is otherwise unchanged in spirit — pick the
one instrument whose holdings bear the post's actual exposure, or decline.
Deterministic mapping change: no golden replay (ruled); validated by one live
round trip on a tariffs-on-China fixture (`theme_etf_round_trip.py --china`,
proposal XLI, XME, SLX, FXI).

**Round trip (2026-09-16, droplet): the pick-or-decline path over a union
WORKS — on the third attempt.** Verdict **no_position / 62, tickers [FXI]**,
days horizon, catalyst present, est. $0.28. The thesis names FXI as "the
correct single mapping from the shortlist: it holds the tariff's target
economy … not the theme's consumers", rejects XLI (ambiguous sign: buyers of
Chinese inputs and the retaliation target), XME and SLX on holdings grounds,
then declines the trade on execution timing (US cash closed at 02:07 UTC while
Hong Kong was absorbing the headline live). Exactly the ruling's rule.

**Watch item — two schema failures first.** Runs 1 and 2 on the same fixture
returned `schema_validation_failed` ("1 error(s)", est. $0.46 and $0.32); the
single-theme fixture has never failed (0 of 2). The pass keeps only a 500-char
excerpt of the payload (`_EXCERPT_CHARS`), so the failing field was NOT
recoverable from either run; the script now intercepts validation and prints
the full payload and pydantic errors, but the instrumented third run passed
cleanly. Failure mode is safe (a typed rejection = no trade, one research pass
spent). If production shows `schema_validation_failed` clustering on
theme-mapped signals, the fix is diagnostic first: raise `_EXCERPT_CHARS` on
the rejection record (a research-layer change → its own ruling under the
freeze). n=3; not evidence of anything yet, recorded so it is not forgotten.

### Same-name entries are ADD DECISIONS — human ruling 2026-09-16 (revising rejection to a convergence add)

**Origin.** CELH, 2026-09-16 13:30 UTC poll: the Form 4 fetcher emitted TWO cluster
signals on the same name in one drain — accession 0001628280-26-062057 (Fieldly CEO +
DeSantis + Kravitz, 3 insiders, $1.83M) and 0001628280-26-062134 (Fieldly + Kravitz, a
strict SUBSET of the first). Nothing stopped the second from being researched and sized
as its own entry: decision 8ba9299d416f4ce0 (long/60 after boundary confirmation 65→60)
filled 52.807 @ 28.41 at 14:05:27, decision 1eb9c63f8e6141c4 (long/62, confirmed 62/62)
filled 52.975 @ 28.32 at 14:05:28 — two 2% lots, $3,000.50, 4% of the sleeve, both from
`form4_insiders` (family **insider_filings**), two review streams, two stops, two leashes
(122d and 181d). The originating signals were the same family repeating itself.

**The ruling, as built (all six points):**

1. **One judged position per symbol.** `SignalPipeline` asks the exit engine
   (`HeldPositions` seam: `context_for`, `context_for_symbol`, `note_add_signal`) whether
   the signal's scanner-extracted tickers name a held position. If so the research pass
   runs as an **ADD DECISION**: `research/add_decision.py` renders the position from the
   system's records (symbol, lots, blended entry, cost, current price, size as % of sleeve
   NAV, originating family vs this signal's family, confidence/horizon/resolution/leash/
   stop, the last three reviews, convergent signals already recorded, thesis and
   invalidation at entry) ABOVE the fenced signal; the tool schema gained `add_verdict`
   (add|hold, null on ordinary passes) and `add_fraction` in (0, 1] (validated; a
   fraction above one is a schema error). SYSTEM_PROMPT gained an ADD DECISIONS section.
   When the entry pass names a held symbol WITHOUT having been asked (a theme post, a
   call with no ticker), the add pass runs as a second pass with the position in view and
   ITS verdict stands (`AddSnapshot.second_pass`). Boundary confirmation applies to adds
   (the second pass must replicate as an add). Reward:risk applies to adds.
2. **Combined size cap.** `_propose_add`: the band of the NEW verdict's confidence,
   bumped ONE band when `family_of(new signal) != position.originating_family`
   (`add_decisions.family_bump_bands: 1`), `min(·, hard_cap 0.10)`. Held value counted
   against the cap = max(cost basis, market value) — a drawdown cannot manufacture
   headroom (Constraint #6). Add = headroom × add_fraction, then the same ATR risk-parity
   and post-table scalars as every judged entry. Equity only; a held OPTION position
   records the signal and holds (`not_built`).
3. **One position, many lots.** `TrackedPosition.lots: list[Lot]` (decision, signal,
   source, family, quantity, entry, cost, opened_at, confidence, thesis, invalidation,
   horizon, resolution date, ATR stop fraction, filer, proceeds). On a filled add
   (`_add_lot`): quantity/cost/blended entry updated; the stop re-derived at the
   capital-weighted blended stop fraction from the blended entry and applied ONLY where
   it tightens (an add below the blend would otherwise loosen the earlier lot's stop;
   Constraint #6; a trailing stop never falls); the leash moves to the LATER resolution
   date among the lots' theses, clamped in the winning lot's horizon bounds from the
   ORIGINAL entry (`_releash`; a review may still shorten). Sells relieve lots FIFO
   (`_allocate_sale`); a full close writes ONE OUTCOME PER LOT against the lot's own
   decision (own realised P&L, own long-term boundary — the tax lots). Trims and closes
   are otherwise unchanged. The review prompt shows LOTS and CONVERGENT SIGNALS blocks;
   health shows a lots line.
4. **Hold.** Any non-qualifying reading (verdict hold, no_position, add without a
   fraction, wrong direction, wrong symbol) → `stage_rejection already_held_no_add`; an
   add verdict with nothing left under the cap → `add_no_headroom`; both carry an
   `AddSnapshot` (position id, verdict, families, cap, held value, headroom). The exit
   engine records a `ConvergenceNote` on the position and flags a review
   (`review_due_kind="add_signal"`, framed in the review prompt); the review ran in the
   same tick in every test (reviews follow dispatch). Replay restores notes and any owed
   review from the records. The registry shows later passes "held (code)", not
   "declined". Forward report: "Add decisions" section (adds taken vs held).
   Attribution: `adds` row — adds against the originating lots they joined.
5. **CELH merged — by replay, from the log.** `ExitEngine.replay` now groups every open
   judged trail by (kind, symbol) and rebuilds ONE position with one lot per trail in fill
   order. The merge is therefore deterministic and needs no record surgery: at the next
   startup CELH is one position keyed 8ba9299d416f4ce0 with lots [8ba9 52.807 @ 28.41
   (insider_filings, 60), 1eb9 52.975 @ 28.32 (insider_filings, 62)], 105.782 shares,
   $3,000.50 = 4.00% of the sleeve (kept as ruled; under the new rule the second signal
   would have been a hold — same family, and at 62 the 2% band was already full),
   blended entry 28.365, stop 25.06 (both lots carried the same ATR stop fraction
   0.1166), leash 181 days to 2027-03-16 (the later of the two dates), one review stream.
   `python -m orchestrator health` renders the merged view before the bounce.
6. **Mechanical arm unchanged** — it never enters pipeline sizing; test pins it.

**Attribution:** `DecisionRecord.add` / `StageRejectionRecord.add` (`AddSnapshot`),
`ResearchSnapshot.add_verdict/add_fraction`, `FunnelEntry.is_add`,
`AttributionReport.adds` (`AddDecisionAttribution`). Golden set: two `kind: "add"` cases
graded on STRUCTURE plus the verdict set — `add-celh-same-cluster-repeat-real` (the real
second cluster signal against the single-lot position; graded **hold**: the same family
repeating itself is not information) and `add-celh-cross-family-congressional-synthetic`
(the merged position asked about a SYNTHETIC congressional purchase; add or hold, shape
graded). `ops/experiments/add_decision_round_trip.py` runs the production pass on the
merged CELH context. Tests: `tests/test_adds.py` (15). Config: `orchestrator.yaml
add_decisions`. Freeze: lifted for this ruling's prompt/schema change only, re-closes
with the validation entry below.

**The 16:04 UTC restart — operator bounce, not a crash.** `systemctl show
agentic-paper.service`: `ExecMainStartTimestamp=2026-09-16 16:04:44 UTC`,
`NRestarts=0`, `Result=success`, `ExecMainCode=0` — systemd did not restart it on
failure. The previous process (PID 447943, up since 13:15:44) wrote no `shutdown
complete` marker, consistent with SIGTERM from `systemctl restart`; the new process
(449145) came up at 16:04:44 with the code pulled at bf7d3c7, and an `agentic` login
session started nine seconds later. Same shape as the 08-25 18:54 bounce (SESSION_NOTES
§ dispatch weight, item 5): the human's bounce after the union step shipped. No
investigation beyond this.

**FOUND WHILE READING THE JOURNAL — the X feed has been dead since 2026-09-01.** Every
X poll since 2026-09-01 13:30:00 UTC (the first poll of September) has returned
`HTTP 401 Unauthorized` — "X refused the request (HTTP 401): Unauthorized. Check
X_BEARER_TOKEN in .env." — 109 today by 16:04, 347 on 09-15, 367 on 09-14, 332 on 09-11,
365 on 09-09, 377 on 09-08; the last successful `api.x.com` call in orchestrator.log is
2026-08-31 19:59:01. That is ELEVEN sessions with no @nolimitgains, no Unusual Whales, no
Trump mirrors (`trump_mirror_ttox` last delivery 08-31, `trump_mirror_tdp` 08-20 — the
startup warning has been saying "trump_posts may be quiet, or the bot may be dead; a
human should check which", and the honest answer was neither: the token is dead). The
failure surfaced only as `ERROR orchestrator.loop: Class1RealtimeScanner failed to poll`
in the journal; run.log shows no POLL line for the X sources and health said nothing.
**Human action:** regenerate/replace `X_BEARER_TOKEN` in the droplet's `.env` (never
displayed; the agent does not touch credentials), then bounce. **Shipped hardening:** the
loop now routes every scanner poll failure to the operator error sink (run.log
`ERROR ... failed to poll: XError: ...`, and therefore health's last-error line), so a
feed failing every poll cannot again hide behind a "mirror silent" warning. Class 1 X
attribution from 09-01 onward is a hole, not a quiet feed — the forward and attribution
reports should be read with that in mind.

### Validation — add decisions: two live round trips and the golden replay (2026-09-16, droplet, dc7a2ee/26d18df)

**Shipped:** `dc7a2ee` (the ruling), `26d18df` (round-trip script path fix); both VERIFIED
on origin, vps and HEAD; droplet pulled, suite green there (1242 passed, 3 skipped, locally and
on the droplet). `python -m orchestrator health` on the droplet, from the log,
BEFORE the bounce, already renders the merge:

    8ba9299d416f4ce0  CELH   105.782039550 @ 28.3649  stop 25.0571  leash 0/181d (months)
               lots: 8ba9299d 52.807110172@28.41 (insider_filings, 60) | 1eb9c63f 52.974929378@28.32 (insider_filings, 62)

**Round trip 1 — cross-family, synthetic congressional purchase against the merged two-lot
position (`ops/experiments/add_decision_round_trip.py`):** typed verdict **hold / 72**,
direction no_position, add_fraction null, tickers [CELH], $0.11. Request shape accepted
(tool schema with `add_verdict`/`add_fraction`, the ADD DECISION block, POSITION HELD with
two lots). The model held because the fixture SAYS it is synthetic ("SYNTHETIC ROUND-TRIP
FIXTURE, not a real filing") and dismissed it as noise — a correct reading of the content,
and a limit of synthetic fixtures for this question: the pick-or-add path was not exercised
by it.

**Round trip 2 — same family, the REAL second cluster signal (1eb9c63f8e6141c4's content)
against the single-lot position it originally opened a second lot on (`--same-family`):**
typed verdict **hold / 60**, no_position, $0.12. The thesis is exactly the ruling's rule:
"a subset re-publication of the same transactions that opened the existing CELH position
... No new insider, no new transaction date, no new dollar amount." Under the new rule the
second CELH lot would not have been bought. Both verdicts came from the SCREEN tier
(sonnet-4-6; a no_position screen is the record and does not graduate), so the opus
verification tier has not yet answered an add prompt live — the golden entry cases below
did exercise the new tool schema on the verification tier (every traded screen verdict
graduates through it), which is the request-shape claim; the first live add verdict will
come from production.

**Golden replay (26 cases: 20 entry, 4 review, 2 add):**

**21/26 passed, ~$4.80** (`data/golden_2026-09-16.log` on the droplet). Both add cases PASSED:
`add-celh-same-cluster-repeat-real` → **hold/60**, no_position, tickers [CELH] ("a re-emission of
the same transactions that opened the existing position") — the graded verdict; and
`add-celh-cross-family-congressional-synthetic` → hold/72, structurally complete. All four review
cases passed (day-9 post-blowout: hold, validity intact). Every Class 1 entry case passed.

**Five DRIFTs, for human review — none of them a direction change on a case that traded:**

- `pelosi-be-calls-decline`: long/62 target 75. Drifted IDENTICALLY yesterday (long/62 target 310
  on the 2026-09-15 replay) — the known noisy case, unchanged by this ruling.
- `appaloosa-13f-stale` no_position/30, `pelosi-intc-may-backfill` no_position/28,
  `moskowitz-amat-max-lag` no_position/18, `taylor-ibp-small` no_position/22: the DIRECTION is the
  graded one in all four; what moved is the confidence the model attaches to a decline (yesterday
  72 / 82 / 82 / 72). A decline's confidence sizes nothing — it is a calibration number. Because
  all four are lagged Class 2/3 declines and all four moved the same way in one run, I bought one
  targeted re-run (`golden --only`, ~$0.40): `pelosi-intc-may-backfill` **72** and
  `moskowitz-amat-max-lag` **72** came back exactly where they were yesterday; `taylor-ibp-small`
  stayed low at 32; `appaloosa-13f-stale` returned nothing inside its 280s timeout (unscored).
  Reading: the shift is mostly the known run-to-run noise in no_position confidence (the
  2026-09-02 variance experiment saw one case span 30-72 on identical inputs), with taylor-ibp-small
  low twice — enough to WATCH the calibration of lagged-class declines under the new tool schema
  (two extra required-nullable fields, one new SYSTEM_PROMPT section), not enough to call a shift.
  Nothing in the drift touches a sized verdict.

**Ruling asked of the human:** the bounce (which activates the prompt, the schema and the CELH
merge together) is yours; the drift above is the evidence for it. If the lagged-class decline
confidence keeps landing under 50 in production records, that is a research-layer question for the
next ruling, not something to tune under the freeze.

**Freeze re-closes** with this entry. Review the add decisions with the other 2026-09-15
sources on 2026-10-15: adds taken vs `already_held_no_add`/`add_no_headroom` holds in the
forward report, and the `adds` attribution row against the originating lots.

**Human actions outstanding:** (1) bounce `agentic-paper.service` so the running process
(bf7d3c7) picks up the ruling — the CELH merge takes effect at that startup; (2) replace
`X_BEARER_TOKEN` in the droplet's `.env` — the X feed has 401ed on every poll since
2026-09-01 (see the previous entry).

### Drift review closed: the leaky request shape, fixed and re-observed (2026-09-16, 7e08b0f)

**Finding.** Rendering the full request for the four drifted lagged-class declines from
bf7d3c7 and dc7a2ee: the USER prompt was byte-identical, but the SYSTEM_PROMPT (the new ADD
DECISIONS section) and the tool schema (add_verdict/add_fraction as required-nullable fields
plus a description sentence) had changed for EVERY pass, add or not. A request-shape change
on the exact cases that drifted — the confound had to go before the drift could be read.

**Fix (`7e08b0f`, VERIFIED origin/vps/HEAD, droplet pulled, 1244 passed / 3 skipped both
sides).** `system_prompt_for(add_decision)` and `report_tool_definition(add_decision)`:
an ordinary entry pass now sends the pre-ruling system prompt and tool schema byte-for-byte
(proved by `cmp` on system/user/tool for all four cases); only an add decision carries the
section and the fields. `ResearchPass._call` selects per request on both the screen and the
verification tier. Add requests keep the shape the two round trips exercised. Tests pin the
invariant on both sides (an ordinary schema offers no add fields; the add tests' fake LLM
asserts the schema matches the prompt on every call).

**Third observation, byte-identical shape (`golden --only`, ~$0.64):**

| case | 09-15 (bf7d3c7) | 09-16 replay (dc7a2ee) | 09-16 rerun (dc7a2ee) | 09-16 fixed (7e08b0f) |
|---|---|---|---|---|
| appaloosa-13f-stale | no_position/72 | no_position/30 | (timed out, unscored) | **no_position/72** |
| pelosi-intc-may-backfill | no_position/82 | no_position/28 | no_position/72 | **no_position/72** |
| moskowitz-amat-max-lag | no_position/82 | no_position/18 | no_position/72 | **no_position/75** |
| taylor-ibp-small | no_position/72 | no_position/22 | no_position/32 | **no_position/72** |

Every case is back inside its band under the restored shape; the direction never moved in
any observation. The 18-30 cluster appeared only under the leaky shape (five of six
observations there landed low; all four under the restored shape landed 72-75). Read: the
extra required-nullable fields and section in an ordinary pass depressed the confidence the
screen model attached to lagged-class declines — a calibration effect on numbers that size
nothing, but real, and now gone by construction rather than by argument. The remaining
golden drift is the known `pelosi-be-calls-decline` case only.

**X feed after the token replacement and 18:33 UTC bounce:** all six X accounts return 200,
zero 401s; first-poll lookback 2.4h (gap since the newest audit record — the 09-01→09-16
hole is not refetched, by design); every fetched item landed once in the credibility log
(none was a forward call or a relayed Truth; @TrumpDailyPosts' items were the bot's own
replies, marker absent); mirror-silence warnings remain honest — they count relayed
principal posts, and none has been relayed yet. **@TrumpTruthOnX returned nothing in five
polls while @TrumpDailyPosts posts: a human should check whether that bot is dead.**

Freeze re-closes. Service still to be bounced onto 7e08b0f by the human.

### The ttox mirror is alive; today's zero-item polls were correct; TrumpDailyPosts no longer relays (2026-09-16 diagnosis)

Asked because @TrumpTruthOnX visibly posted marked relays 1-2 hours before the question while the
service had polled it five times with items=0. Three questions, answered against the live API from
the droplet (probe scripts printed bodies only, never the credential):

1. **Handle.** `GET /2/users/by/username/TrumpTruthOnX` → id 1922803520019013632, username
   unchanged; only the display NAME changed to "Commentary Donald J. Trump Truth Social Posts On X"
   (bio: "Commentary, not associated with President Trump"). `search/recent from:TrumpTruthOnX`,
   `from:1922803520019013632` and `users/{id}/tweets` return identical results. The config handle is
   right; nothing to change.
2. **since_id.** The fetcher sets `since_id` only from `meta.newest_id` on a 200 that returned posts;
   a 401 raises inside `_fetch` before that line, and the map is per-process memory (reset at every
   bounce). No since_id could have advanced during the outage. The journal confirms every ttox
   request since the 18:33 bounce carried `start_time = now - 8817s` (the gap-sized first-poll
   lookback) and NO since_id — because every poll returned zero posts, so newest_id was never set.
   @TrumpDailyPosts, which did return posts, advanced its since_id normally (…207838982287 →
   …296409044152712 → …299122477768904 → …302046297829478 → …302937776103509 → …311257022906853).
3. **Raw response.** `search/recent from:TrumpTruthOnX -is:retweet start_time=2026-09-16T13:30Z
   end_time=20:00Z` → `{"meta": {"result_count": 0}}`. The account posted NOTHING inside today's
   polled session (Class 1 polls only 13:30-20:00 UTC). Its relays today were 20:28, 20:33, 21:11,
   22:03 and 22:50 UTC — all after the close, and exactly the posts you saw. Nothing was dropped
   downstream because nothing arrived. Seven-day profile: 88 posts, 63 genuine relays (marker
   present), of which only 10 fell inside a session window (10th: 2, 11th: 4, 14th: 3, 15th: 1,
   16th: 0). Off-session relays are fetched at the next session's first poll (lookback = gap since
   the newest audit record, floor 15 min, cap 24h), so tonight's relays are read at tomorrow's open.
   The "silent since 08-31" warning was the dead token, nothing else.

**The real finding is the OTHER mirror.** @TrumpDailyPosts: 348 posts in 7 days, ZERO carrying the
"Donald J. Trump Truth Social Post" header the config requires, and none whose text matches a
TrumpTruthOnX relay within ±15 minutes — it now posts news items and commentary in its own voice
("🔴JUST IN VIDEO: …", "CNN even had to admit …"), never relaying Truths. Every one of its 27
in-session posts today was correctly dropped to its credibility record as commentary (this is the
2026-08-19 $4.24 lesson working as designed). The secondary mirror delivers nothing; the tdp
mirror-silence warning ("last delivery 2026-08-20") is honest and is pointing at this. **Human
ruling wanted:** retire trump_mirror_tdp (it costs reads and adds noise to the credibility log) or
keep it as a dormant fallback; the Trump leg currently rides @TrumpTruthOnX alone, which relays
within a minute but delivers ~1-4 in-session relays a day, most of them media-only ("New media post
from Donald J. Trump") that the no-ticker/no-theme prefilter drops. No code or config changed.

### Ruling 2026-09-16: trump_mirror_tdp retained as a dormant fallback; the Trump leg's real yield; age at read

**Ruling.** @TrumpDailyPosts stays configured (marker enforcement makes it free; it covers the
single point of failure on @TrumpTruthOnX). Its silence line now states what the silence means
instead of asking a human to check: `SourceConfig.silence_note`, set in signals.yaml, renders
"mirror trump_mirror_tdp (@TrumpDailyPosts) has relayed nothing with a valid marker for N trading
days (last delivery …) — currently posting its own commentary; retained as fallback (human ruling
2026-09-16)". ttox's line is unchanged (a real alarm). Tests in test_mirrors.

**Q1 — the Trump leg's real yield, session gate removed (report only).** ttox's 88 posts since
2026-09-10T12Z contain 63 genuine relays (marker present). Through the REAL Class 1 scanner they
become **24 distinct trump_posts signals** — mirror signals dedup on a normalised content key, so
the five "New media post from Donald J. Trump" relays stamped in the same minute collapse to one,
and media-only relays with identical text collapse likewise. Through the real prefilter:

    18  dropped: no ticker and no configured policy theme
     3  dropped: bare link
     3  SURVIVE -> research  (2 in-session on 09-14; 1 overnight 09-13/14)

The three survivors: "Ukraine has agreed not to hit Russian Energy targets. Russia has agreed to
do, likewise! The World's Diesel pr…" (09-14 15:07Z, in-session, theme **energy** → ETF shortlist
[XLE, XOP] would have been proposed — the ONE post in seven days the theme→ETF map would have
touched); "The failing Nation of Iran wants to make a deal…" (09-14 15:33Z, in-session, prefilter
theme stem matched but no ETF-map theme); "First interior renderings of the magnificent new
Ballroom…" (09-13 23:54Z, overnight, prefilter stem matched — a false positive of the broad
research_prefilter_themes stems, no ETF theme). Zero relays carried a cashtag. **Yield: 3 research
passes per week from 63 relays, one of them ETF-mappable, none naming a ticker.** The ETF map
moved the needle by exactly one candidate post in seven days; the leg's supply is the constraint,
not the mapping. (Dead-token weeks excluded — this is one live week.)

**Q2 — age at read.** Class 1 polls only 13:30-20:00 UTC; off-session relays are read at the next
open's gap-sized lookback. Age of each of the 63 relays when the system would read it:

    10  under 5 min (in-session relays)
    19  0.1-4h   (posted 09:30-13:30Z, read at the 13:30Z open)
    10  8-12h
    14  12-16h
     3  16-24h
     7  24h+     (Friday-evening / weekend posts read Monday 13:30Z; max 65.3h)
    median age of an off-session relay at read: 9.6h

Of the three survivors: two read at ~0h, one at 13.6h. **What the model is told:** a Class 1
signal's `observed_at` is the POLL time, and `created_at` sits in metadata but is not rendered in
the prompt — so an overnight Truth read at 13:30Z looks to the research pass like a post from a
minute ago, and the Class 1 guidance says "priced_in_analysis may be null: no disclosure lag to
reason about". For a policy post that is wrong by 10-16 hours of overnight futures, Asian and
European trading. Recommendation (a research-layer change → its own ruling under the freeze, golden
replay + round trip): render the post's own timestamp and its age in the SIGNAL METADATA block,
and for a Class 1 signal older than N minutes (proposed: 30) make priced_in_analysis mandatory with
the same measurement framing the lagged classes get — the lag is hours, not days, but the
question is identical: has the move already happened? No change made.

### Class 1 staleness — human ruling 2026-09-16 (freeze lifted for this alone, re-closed here)

**Ruling, as built (`4a364cd`, VERIFIED origin/vps/HEAD, droplet pulled, 1252 passed / 3
skipped both sides):**

1. Every Class 1 prompt renders the post's OWN timestamp and its age at observation:
   `- posted at: 2026-09-14T15:07:02+00:00 (age at observation: 10h 00m — STALE for a real-time
   signal; see the guidance below)`. The scanner's `_build` stamps `published_at` (the item's own
   time) into every signal's metadata; the X fetcher's `created_at` is left alone and read first.
   `observed_at` stays the poll time, as before — the prompt now shows both.
2. `class1_is_stale`: Class 1 and age ≥ 30 min (`CLASS1_STALE_AFTER`, inclusive at 30). Stale →
   the "class_1_stale" guidance replaces the fresh one: markets have traded on it since;
   priced_in_analysis is MANDATORY and must MEASURE what the named or implied instruments did since
   the post's timestamp; lag alone is not disqualifying; decline for DEMONSTRATED movement, never
   for elapsed time. `ResearchPass` rejects a stale Class 1 report without the analysis
   (`missing_priced_in_analysis`), exactly as it does the lagged classes.
3. Under 30 minutes the fresh path is unchanged (tests pin "priced_in_analysis may be null" on a
   2-minute-old post). Unknown age (records/fixtures without a timestamp) renders "not provided by
   the scanner (age unknown)" and reads as fresh — the status quo, so old records replay unchanged.
4. Golden: `expect_priced_in` grading (non-blank AND carrying a number — a measurement, not a
   suspicion) and the new case `trump-energy-relay-read-10h-later`: the REAL 09-14 15:07Z relay
   ("Ukraine has agreed not to hit Russian Energy targets… The World's Diesel price rise…"), the
   one ETF-mappable policy relay of the live week, observed ten hours later with theme energy →
   [XLE, XOP] stamped as the pipeline would. Any verdict may be right; a null or numberless
   analysis is drift.

**Live round trip (`ops/experiments/class1_staleness_round_trip.py`, production pass, screen →
verification, $0.27):** typed verdict **no_position / 78**, tickers [XOP], horizon days. The
model read the age line and reasoned about it: "The headline landed mid-session, so roughly five
and a half hours of NYMEX crude and NYSE equity trading plus the settle … elapsed before
observation … the front-month reaction window is seconds-to-minutes … the repricing window closed
intraday; the honest read is priced-in-by-session-close." It also read the post's economics
correctly against the proposal — bearish crude and diesel cracks, so the long-oil shortlist
[XLE, XOP] carries the exposure the WRONG way — and declined. Honest limit, stated by the model
itself: "I did not complete direct quote measurement of XOP/XLE/Brent prints since 15:07 UTC, so
I am NOT claiming a measured percentage move." The golden digit check passed on the timestamps in
the analysis, not on a price move; the case's bar is "engages the move", which this does, but a
stricter grade (a percent or a price) is a reasonable tightening if the pattern repeats.

**Golden replay (27 cases: 21 entry, 4 review, 2 add):**

**24/27 passed, ~$5.24** (`data/golden_2026-09-16b.log` on the droplet, finished 02:23Z 09-17).
The new case **`trump-energy-relay-read-10h-later`: PASS, no_position/80** — the analysis carried
the measurement the case demands (the round trip above was 78; two observations, same verdict).
All Class 1 cases passed (trump-* 76-88, nolimitgains 92, uw 85, injection 93, mirror 91): the
age line renders on every one of them now, and none of those fixtures carries a timestamp older
than 30 minutes, so they read the unchanged fresh guidance plus one metadata line. Reviews 4/4
(hold / trim / close / close — day9 back to close after one hold), adds 2/2 (hold/60, hold/72,
identical to the 09-16 run).

Three drifts, all **Class 2 congressional** cases. The ruling touches Class 1 only, and the
system prompt, user prompt and tool schema for these three cases are **byte-identical** between
`7bc183c` (pre-ruling) and `4a364cd` (checked with a worktree at 7bc183c: `diff -r` on the three
dumps, silent). So this is the model's variance on unchanged requests, not the change:

- `pelosi-uber-priced-in`: **long/58, target 90** — the case's behavioural grade is "no position
  is taken", and 58 would trade at 2%. Prior observations no_position/62 (09-15) and
  no_position/74 (09-16). First traded verdict on this case in three full replays; the May
  purchase read in August is the canonical priced-in decline. One observation; the fixture is
  unchanged. Watch on the next replay — a second traded verdict here would be a real question
  about how the prompt frames a three-month backfill, not noise.
- `pelosi-be-calls-decline`: **no_position/38** — direction right (the graded decline) for the
  first time in three full replays (long/62 on 09-15 and 09-16); confidence outside [40, 95].
  The known drift on this case is the traded long; a low-confidence decline is the better failure.
- `taylor-ibp-small`: **no_position/35** — direction right; confidence 72 (09-15), 22 (09-16,
  leaky shape), 72 (third observation, byte-identical shape), 35 now. The no_position confidence
  band is the set's known noise source; the verdict itself has never drifted on this case.

No prompt change follows from this run. Human review of the three lines above is the CLAUDE.md
requirement; the pelosi-uber line is the one that earns attention.

**Test harness note.** `tests/test_orchestrator.fresh_feed` stamps fixture posts at the harness
clock: the fixture's published_at was the constant 2026-08-17, and nine September-clock tests
(position management, probation bias, the halt marker) saw a weeks-stale relay owing an analysis —
which is the rule working; the harness now delivers posts as a live feed does, at posting time.

**Freeze re-closes.** Bounce to activate (the running service predates `7bc183c` and `4a364cd`).

### Golden replay exercises the boundary confirmation; first concurrence read — 2026-09-17 (two human approvals)

**Measurement that prompted it (report-only, ~$1.19).** The three Class 2 priced-in cases rerun
3x each under the byte-identical shipped shape, giving six observations per case:
`pelosi-uber-priced-in` no/62, no/74, **long/58**, no/35, no/30, no/32 (1 traded in 6);
`pelosi-be-calls-decline` **long/62, long/62**, no/38, no/32, no/72, no/55 (2 in 6; with the 09-03
history long/52 and the post-ship arm no/45, no/72, no/72: 3 in 10); `taylor-ibp-small` no/72,
no/72, no/35, no/82, no/72, no/72 (0 in 6). Every traded verdict any of these cases has ever
produced (52, 58, 62, 62) sat INSIDE the boundary-confirmation band [50, 70) — none cleared 70.
So on priced-in Class 2 fixtures the band is the only thing between model variance and a 2%
position, engaged on roughly one first pass in six, and the golden harness could not say how often
the second pass would concur because it ran a single pass and stopped one step short of the guard.

**Approval 1 — the golden replay runs the production confirmation (`60be09b`, VERIFIED
origin/vps/HEAD, droplet pulled, 1255 passed / 3 skipped both sides).** The rule moved out of the
pipeline method into ONE function, `orchestrator.boundary.confirm_boundary`; the pipeline
delegates to it unchanged (a test pins the delegation). The golden runner calls the same function:
a tradeable first-pass verdict inside `[no_trade_below, + band_width)` buys the production second
pass, the case grades on what would SIZE — the lower-confidence report when the second replicates,
no position when it does not — both passes stay on the line (`long/58 target=95 | boundary: second
no_position/72 -> REVERSED, nothing sizes`), and the summary tallies upheld against reversed with a
concurrence rate. `python -m orchestrator golden --single-pass` restores the one-pass replay for
variance measurement. Graded on the sized outcome deliberately: a reversed in-band long is the guard
working, and a drift list that flagged it would carry the noise the rewrite below removes; the
tally line is where the guard's workload is read.

**Approval 2 — BE-calls and IBP graded behaviourally.** Both now match the Uber case: directions
no_position|long, confidence [0, 100], a traded verdict only in [0, 49]. A decline at any
confidence passes; a long that would size is drift. Decline confidence on identical requests
(38/32/72/55; 72/72/35/82/72/72) is recorded on every line and graded on none.

**First concurrence read (9 full-path runs, 3 per case, ~$2.14, `data/variance_full_2026-09-17.log`):**

| case | run 1 | run 2 | run 3 |
|---|---|---|---|
| pelosi-uber-priced-in | no/35 | **long/58 → 2nd no/72, REVERSED** | no/18 |
| pelosi-be-calls-decline | no/38 | **long/62 → 2nd long/52, UPHELD — sizes long/52 (DRIFT)** | **long/52 → 2nd no/42, REVERSED** |
| taylor-ibp-small | no/72 | no/72 | no/72 |

Nine first passes, three tradeable, all three inside the band, **1 upheld, 2 reversed —
1-in-3 concurrence, n=3**. Across every byte-identical observation now on file (27 first passes
on these three fixtures) the first-pass tradeable rate is 6/27 (22%), all six in the band; the one
full-path pass-through that would have become a live position is a 2% BE long at 52 on a
28-day-lagged options disclosure the human graded a decline on 2026-08-27. Read for 2026-10-15:
the guard is doing real work and is catching most of what reaches it, but not all — with n=3 the
concurrence rate is between "mostly reverses" and "coin flip", and it needs the tally from every
routine replay between now and the review before the floor question (is 2% at 50 safe, or does the
floor itself move) is answerable. Every golden run now prints that line; the replay logs are the
accumulating record. No prompt change follows.

**Also:** the pipeline's `BoundaryConfirmationSnapshot` import, orphaned by the extraction, removed.

### AGGRESSION RULING 2026-09-18 — step 1 shipped (prefilters, Class 1 horizons, class-pool caps)

Four levers ruled; sequence (1) prefilters + horizons -> (2) 8-K widening -> (3) baseline
sleeve -> (4) new sources, report after each. **Revised the same day: cost discipline binds** —
budget stays 40 (not 60) with class pools 15 / 20 / 5-reserve; Class 2/3 horizons NOT shortened;
the baseline sleeve freezes both ways under a tripped kill switch; ambiguous tense admits behind a
realized-P&L guard. Steps 2-4 gated on the spend/crowding report below.

**Shipped (`579f0f1`, VERIFIED origin/vps/HEAD, droplet pulled, 1279 passed / 3 skipped both sides):**

- `trump_posts` bare-link floor 120 -> 60; `research_prefilter_themes` 31 -> ~150 stems (multi-word
  stems as phrases; short generic prefixes deliberately avoided — "port" would match "report"). The
  theme -> ETF map is unchanged: a newly themed ticker-less post (healthcare, immigration, labor,
  housing, regulation) researches WITHOUT a shortlist unless a human adds a map entry.
- X trade-callers: `default_when_ambiguous: forward_call` on all four (nolimitgains, unusual_whales,
  optionshawk, citrini), now actually wired — the field existed since 08-18 but the classifier
  hard-coded retrospective. The **realized-P&L guard** (RETROSPECTIVE_PATTERNS) discards regardless
  of tense: realised amounts, `+N%`, reported moves, dollar / "few K" results, multiples (3x, not
  "3x leveraged"), entry-to-exit moves, exit and profit-taking language, P&L/screenshot, claimed past
  calls, past timeframes, and celebration of a finished trade ("nice one", "great tell", "that
  $META trade was beautiful", "caught"). Only bare was/were/had/got flips. Function-level default
  stays retrospective (Constraint #6); the scanner passes the source's rule.
- Class 1 prompts carry `FAST_CLASS_HORIZON_GUIDANCE` after the class guidance (fresh, stale and
  8-K alike); Class 2/3 prompts proven byte-identical to `9b4477d` across all 11 lagged golden
  entry cases (line diff: the 10 Class 1 cases differ by exactly the block; system prompt unchanged).
- `TrackedPosition.signal_class` stamped at open and at replay; `exits.fast_class_leash_bounds`
  (weeks floor 7) applies to Class 1 positions only via `ExitsConfig.leash_bounds_for`; unstamped
  legacy positions read as not-Class-1.
- Budget: `review_budget_reserve_fraction` 0.25 -> 0.125 (5 passes); `research_class_caps`
  class_1 15 / class_2_3 20 in the loop after the per-source cap, before triage, code `class_cap`,
  seeded from the log by source class on restart, validated <= entry ceiling.

**Projected candidate increase (21-day funnel dump, 16 trading days of records):**

| source | now researched/day | mechanism | after |
|---|---|---|---|
| trump_posts | 1.25 | 4 of 9 bare_links pass at 60; ~6 of 40 theme-misses match the new stems | ~1.9 (+0.6) |
| unusual_whales | ~0 | 11 ambiguous discards in 7d flip, 0 carry a cashtag | +0 (die free at require_instrument) |
| optionshawk | ~0.15 | 8 flip, 3 with cashtags; 2 of those 3 now hit the guard | +~0.15 |
| nolimitgains | ~0 | 16 flip, 0 with cashtags | +0 |

Net: roughly +1 pass/day, ~$0.20. The caller change is almost free because `require_instrument`
still kills instrument-less prose at the prefilter; it only matters on the day a caller phrases a
real, instrumented call in the past tense.

**Spend and crowding under the 40 cap (revised ruling item 5).** Paid passes/day rose 4-10 (early
Sept) -> 16 / 19 / 22 on 09-16/17/18 at a mean $0.213 per paid pass (screen + verification;
reviews separate, median $0.16). Slot winners on 09-18: form_8k 6 (its cap), form4 5 (cap),
congressional 5 (cap), trump 5, optionshawk 1 — i.e. Class 1 pool 12/15, Class 2/3 pool 10/20.
Today's funnel does NOT exhaust either pool; the pools bind only if a source cap is raised. What
the caps crowd out is already visible in `source_cap` rejections: **form_8k 87 / 254 / 67 per day
against a cap of 6** (20-40x oversupply under the CURRENT five-item whitelist), congressional
70-213/day against 5. Ceiling spend at full pools: 35 x $0.213 + 5 x $0.16 = ~$8.30/day; expected
~$5-6 at today's volume. Within the $5-8 target; well under $20.

**Golden replay (10 Class 1 entry cases, production path with boundary confirmation) + live round trip:**

10/10 PASS, ~$2.19 (`data/golden_step1_2026-09-18.log`): trump-nike-woke no/78, trump-micron-announcement
no/78, trump-clemens-nonmarket no/91, trump-ford-tariff-brag no/74, trump-venezuela-spr no/88,
nolimitgains-instrumentless no/90, uw-tim-cook-breaking no/85, injection-cashtag no/92,
mirror-fabricated-buyback no/90, trump-energy-relay-read-10h-later no/74 (priced-in analysis
measured, digit check passed). No tradeable first-pass verdict, so the boundary tally is 0/0.
The horizon block did not move a single verdict on the decline set — expected, since none of these
cases has a horizon to shorten; the lever's effect shows up on the day a Class 1 signal is taken.

**Live round trip** (`ops/experiments/class1_staleness_round_trip.py`, production two-stage pass
under the new Class 1 prompt, $0.38): typed **no_position / 72, horizon days**, priced_in_analysis
present and measuring the 10h window ("both legs ... wired into price before observation";
"declined for demonstrated absorption plus a contradicted premise, not for elapsed time per se"),
the ETF mapping declined on sign, mirror provenance verified. Honest limit stated by the model:
search budget exhausted before it could pull XLE/XOP prints, so no percentage asserted — the same
limit as the 09-16 round trip; a stricter numeric grade remains the reasonable tightening if it
repeats a third time. Golden grade PASS. Request shape accepted: shipped.

**Steps 2-4 status.** 8-K widening: the six new items (1.02, 2.02, 3.01, 5.01, 7.01, 8.01) are
the HIGH-volume items — 2.02 (earnings) and 8.01 (other events) alone dwarf the current five — so
widening adds candidates to a source already 20-40x over its 6-pass cap; volume and crowding are
reported before it ships. Baseline sleeve and new sources: pending, in order.

### AGGRESSION RULING 2026-09-18 — step 2 REVISED: 8-K widening cancelled; cross-source dispatch scoring shipped

**Revised ruling (same day).** The crowding data changed the diagnosis: selection-constrained, not
supply-constrained (form_8k 87 / 254 / 67 candidates a day against a cap of 6; congressional
70-213 against 5; every cap filled by arrival order). Widening the 8-K whitelist would only dilute
the pool with the least informative items (2.02, 8.01). Whitelist stays at five. Replaced by:
(1) global competition for the 40 slots on a comparable score, per-source caps kept as ceilings;
(2) priors grounded in the forward-return engine, flagged where n does not permit; (3) a
what-if of yesterday's slots under scoring versus what they were.

**Shipped (`4751c42` code, `202eead` merge fix, `3d200a2` grounded priors; VERIFIED origin/vps/HEAD,
droplet pulled, 1289 passed / 3 skipped both sides):**

- `orchestrator/scoring.py`: `score = prior(slice) − age_days/7 + convergence bonus`, one unit
  (expected 5d excess, pct-points). Slices from the structured fields each fetcher stamps, with
  content parsers as fallback so audit records grade identically: 8-K by highest-ranked whitelisted
  item (5.02 > 1.05 > 4.02 > 2.05 > 1.01 by ruling); Form 4 by door × aggregate-size band;
  congressional by amount band × lag band; 13D by stake band; posts/callers at the source fallback.
  Resolution: exact facet → each single facet → source fallback → 0, and every answer says
  grounded or ruled.
- `config/dispatch_priors.yaml`: every value ruled and flagged `grounded: false` until
  `python -m orchestrator priors --write` replaces it with the measured mean where n ≥ 20.
  Merge fix (`202eead`): an ungrounded compound slice is never written at the fallback — it would
  shadow the single-facet resolution (cluster|size at 0.0 hid cluster at 0.5); the loader ignores
  prior-less entries.
- Loop: with `dispatch.scored` the sort key is the score (Class 1 still first). Pooled filing
  sources (8-K, Form 4, congressional, 13D, 13F) wait for 30-minute release windows, are ranked
  together at each window with class priority set aside, and each source spends at most
  ceil(remaining cap / remaining windows today) per window — slots spread across the session, and
  a strong 14:00 filing is not shut out by a mediocre 09:31 one. Posts and callers dispatch at once,
  scored. `scored: false` (the harness default) is the 2026-08-26 behaviour byte for byte. Honest
  limit: the competition is per window, not per day — a full-day ranking would mean researching
  nothing until the close.
- `python -m orchestrator priors [--write] [--refresh]`, `python -m orchestrator dispatch-whatif
  --day D`. Tests: `tests/test_dispatch_scoring.py` (10).

**First grounding pass (forward cache refreshed by the weekly report to 5,238 rows; 27 slices
grounded at 5d):**

| slice | n | mean 5d excess | hit |
|---|---|---|---|
| congressional, all | 1878 | −0.73 | 34% |
| congressional amount ≤15K (82% of candidates) | 1541 | −0.79 | 33% |
| congressional ≤50K | 223 | −0.67 | 34% |
| congressional ≤250K | 34 | **+0.58** | 59% |
| congressional ≤1M | 36 | **+0.76** | 69% |
| congressional ≤1M & lag ≤35d | 23 | **+1.01** | 70% |
| congressional lag ≤7d (freshest) | 238 | −1.47 | 26% |
| congressional lag ≤35d | 400 | −0.42 | 40% |
| Form 4 single (the control) | 98 | −1.80 | 43% |
| Form 4 cluster | 12 | −0.14 (n<20, ruled +0.50 stands) | 50% |
| Form 4 C-suite single | 0 | ruled +0.25 stands | — |
| 8-K, every item | 427 | no 5d marks yet (first candidates 09-16) | — |
| 13D | 0 | — | — |

Findings that matter beyond dispatch: (a) the congressional source as researched is a NEGATIVE
5-day-excess population, and the bulk of it (≤15K) is the worst part; the size effect the old
log10(amount) weight assumed is real but only above $100K, and the freshest disclosures (lag ≤7d)
are the worst, not the best — the −age/7 freshness term runs the wrong way for this source at 5d
and is now dominated by the grounded lag cells; (b) Form 4 singles at −1.80 (n=98) confirm the
control group; the cluster claim is unmeasured at n=12; (c) 8-K priors cannot be grounded before
~09-23, so the item ordering is ruled until then. 20d marks exist for almost nothing yet — the
5d horizon is the grounding horizon by necessity, stated in the file.

**Item 3 — what the slots would have been (`dispatch-whatif`, grounded priors):**

| day | candidates | slots | same picks | summed prior actual → scored | realised 1d excess actual vs scored |
|---|---|---|---|---|---|
| 2026-09-17 | 476 | 19 | 7 of 19 | +2.70 → +3.50 | −0.06 vs **+0.68** (n=15 each) |
| 2026-09-18 | 166 | 23 | 9 of 23 | +2.50 → +4.64 | unresolved |

Composition by source is identical both days — the per-source caps are ceilings and the total
slot count is fixed, so scoring changes WHICH candidates each source spends its ceiling on, not
how many. That is the ruled shape (no source takes all 40); the reallocation lever, if wanted, is
the ceilings themselves. Within sources the change is large: on 09-17, twelve of nineteen picks
differ, and the picks scoring would have made beat the actual ones by 0.74 pct-points at one day
on n=15 — one day, one horizon, suggestive not conclusive; the 5d and 20d marks land next week and
every replay accumulates the tally. Also visible in the what-if: production researched SBLK three
times on 09-17 (three separate Form 4 filings on the same name); scoring would have done the same.
Same-name-same-day de-duplication is a separate ruling — flagged, not built.

**Service note.** The running service still predates every commit of the last two days
(staleness, silence wording, boundary extraction, step 1, step 2) and needs a bounce.

### REDIRECT 2026-10-08: RISK-ON — item 4 design and build plan; PRE-REGISTRATIONS for items 1, 2, 3 and 6 (written BEFORE any of those backtests ran)

Supersedes the maintenance-mode and search-closed rulings. Kept: contract awards measuring, the
mechanical control arm, the weekly scorecard. Constraint #6 holds throughout: nothing below is
sized off P&L or a target.

**ITEM 4 — THE LIVE AGGRESSIVE PAPER SLEEVE (design; built in increments, each behind the deploy
gates: suite green, golden replay where a prompt changes, live dry-run of the changed path, no
deploy inside the pre-open hour).**
- **Allocation:** a new `aggressive` sleeve at **10% of NAV, carved from the judged sleeve** (55 →
  45; mechanical 15 and baseline 30 unchanged). The ruling says "open it"; the weight was not stated,
  so the smaller measurement-first size applies (Constraint #6); raising it is a one-line ruling.
- **Gate (deterministic, own cap table `aggressive_sleeve`):** max single position 25% of the
  sleeve's NAV; max 5 concurrent; daily deployment 100% of the sleeve (it turns over intraday);
  sector 50%; long calls/puts only, premium at risk ≤ 50% of the sleeve, short-dated allowed
  through the existing selector (7 DTE floor, T−1 close); cash-secured, no margin, kill switch,
  drawdown ladder and never-negative exactly as the judged sleeve. Day trades: the account is a
  cash account — the gate already reserves settled cash per order; a same-day round trip on
  settled cash is permitted and the proceeds are not reused until settled (no good-faith
  violation); PDT counting applies only to margin accounts and stays as written.
- **Sizing — a fixed risk budget, not the confidence table:** R = **1% of the aggressive sleeve's
  NAV per trade** ($100 on $10k). Equity: shares = R / (entry − stop), stop = the LLM's named
  structure stop (ORB low, pre-event high) clamped into [1%, 8%] of price, or 2.5 × ATR(14) when
  none is named; capped by the single-position cap. Options: premium at risk = R. The verdict's
  confidence gates entry (floor 50, unchanged) but never scales size. The self-consistency vote
  runs on swing candidates (attention momentum) and is OFF for intraday candidates (a vote is
  minutes; the sleeve's protection there is the risk budget).
- **Candidate sources, LLM-judged:** (1) **`attention_momentum`** (item 6f, eligible now): events
  already in the funnel from the last 1–3 sessions (congressional, Form 4, contract awards, 8-K)
  confirmed by the item-6 thresholds below; prompt guidance: priced-in and staleness checks OFF,
  judge room to run (theme, catalyst pipeline, crowding); horizon 20–60 sessions; exit by an ATR
  trailing stop. (2) **`opening_gap`** (item 3's live counterpart): a liquid universe (the 150
  largest US equities by trailing-60-session dollar volume plus SPY/QQQ/IWM and the nine sector
  SPDRs, rebuilt nightly from the bars cache) screened at 09:35–09:45 ET for a gap ≥ 3% vs the prior
  close with relative volume ≥ 2× in the first 15 minutes; judged as a day trade; exit at the
  stop or by **15:50 ET**, never held overnight. (3) Either source may be expressed as a short-dated
  long option through the existing doors when the LLM names a catalyst inside the window.
- **Measurement:** own attribution bucket `aggressive`, partitioned out of the judged alpha line;
  the weekly prints the sleeve's return vs SPY since inception and over the trailing four weeks,
  and the forward engine grades its candidates by source like any other.
- **Increments:** **A** — sleeve + gate caps + risk-budget sizing + `attention_momentum` source +
  prompt branch + ATR trailing stop + attribution bucket + tests + golden case + live round trip.
  **B** — `opening_gap` screen, the 15:50 close rule, minute-bar context in the prompt. **C** —
  the weekly lines. A ships first; nothing trades in the sleeve until A is deployed, and a mid-session
  deploy does not touch the running process, so the sleeve's first session is the one after the
  deploy.

**ITEM 6 — ATTENTION-MOMENTUM, PRE-REGISTERED (backtest on the harvested event history).**
- **Universe:** every event already harvested: contract awards 2019–2026 (3,728 mapped events, all
  tiers), news catalysts 2015–2026 (research + measure tiers, 23,500 ticker-days), congressional
  and Form 4 funnel events since 2026-08 (thin, included), 8-K 1.01/8.01 2024–2026 (40,446). One
  event per ticker per day; t0 = the event's first tradeable session.
- **Confirmation (exact thresholds):** on the first session s ∈ {t0+1, t0+2, t0+3} where ALL hold:
  close(s) > max close of the five sessions before t0 (the pre-event high); volume(s) ≥ 2.0 ×
  the 20-session average volume ending at t0−1; close(s)/close(t0−1) − 1 > SPY's return over the same
  span (positive excess since the event). Unconfirmed events are counted, not traded.
- **Entry:** next open after s. **Exit:** trailing stop = highest close since entry − 2.5 × ATR(14)
  (ATR at entry, recomputed daily), evaluated at each close, filled at the next open; time cap 60
  sessions; no profit target. Net 15bp per side. Faders included: every confirmed event is a trade.
- **Reported:** n events, n confirmed, hit rate, average winner vs average loser (payoff ratio),
  mean and median net return, mean excess vs SPY and vs QQQ over each trade's own window, equal-weight
  strategy max drawdown (one unit per trade, overlapping), by year and by source; cluster bootstrap
  by ticker; **BE and VST** as named cases with their event, confirmation session, entry, exit and
  return.
- **Success:** mean excess vs SPY > 0 with the cluster-bootstrap CI excluding 0; payoff ratio ≥ 1.5;
  positive in ≥ 60% of calendar years; and the same read vs QQQ reported (not a gate).

**ITEM 1 — OPTIONS PREMIUM SLEEVE, PRE-REGISTERED.**
- **Data (the honest limit):** Alpaca serves historical option-contract bars only from 2024-02; no
  free source has 2016–2023 chains. So two segments: **2024-02→ real chains** (Alpaca options bars,
  the selector's own feed) and **2016→2024-01 synthetic premiums**: Black–Scholes from the daily
  close, the risk-free rate (3-month T-bill from FRED), and an IV proxy — VIX for SPY; for single
  names VIX × (the name's trailing-60-day realized vol / SPY's). The synthetic segment is labelled
  as such everywhere and the 2024+ segment is the check on it (synthetic vs real premium on the same
  days, reported). Universe: SPY plus the 30 largest US equities by market cap at each year-start
  (point-in-time list from the bars cache and SEC shares).
- **Strategies:** (A) cash-secured puts: write the 30-delta put nearest 30 DTE at the monthly open,
  hold to expiry; assigned → hold the shares and write 30-delta covered calls monthly until called
  (the wheel); capital = strike × 100 reserved at write (fully cash-secured, no margin — the
  backtest never writes more than cash covers). (B) covered calls on a buy-and-hold position
  (30-delta, 30 DTE, monthly). Costs: $0.65 per contract plus half the spread (real segment) or
  1% of premium (synthetic).
- **Filters, tested JOINTLY as a block, not one at a time (the LLM-filter proxy):** F1 no earnings
  inside the expiry window (earnings dates from the earnings-shadow log and 8-K 2.02 history); F2
  IV rank ≥ 50% (trailing 252-day rank of the IV proxy); F3 no write within 3 sessions after a ≥ 5%
  down day; F4 skip names with a news-catalyst event (the harvested news set) in the prior 3
  sessions. Report unfiltered, each filter alone, and all four together.
- **Reported:** total return vs SPY, CAGR, max drawdown, worst month, Sharpe, assignment rate, by
  year, by segment (synthetic / real). **Success:** CAGR ≥ SPY − 2% with max drawdown ≤ 0.6 × SPY's,
  or CAGR > SPY; the filtered block must beat the unfiltered on CAGR without raising drawdown.

**ITEM 2 — LEVERAGED TREND SLEEVE, PRE-REGISTERED.**
- **Data:** SPY, SSO (2×), UPRO (3×) daily bars 2016→ (Alpaca); VIX daily close (CBOE). One regime
  (2016–2026 bull with 2018/2020/2022 drawdowns) — flagged.
- **Filters evaluated daily at the close, executed at the next open, 15bp per switch, tested as
  ONE rule family:** F1 SPY close > 200-day SMA; F2 50-day SMA > 200-day SMA; F3 VIX close < 20;
  F4 SPY 20-day realized volatility < 20% annualized; F5 SPY close > its 10-month SMA at the last
  session of the month (Faber). **Rule:** 3× (UPRO) when F1 ∧ F3 ∧ F4; 2× (SSO) when F1 ∧ (F3 ∨ F4);
  1× (SPY) when F1 only; cash (T-bill rate) otherwise; F2 and F5 reported as alternates for F1.
  Also reported: SPY-calls expression (synthetic 60-delta, 60 DTE, rolled monthly) for the 3×
  state, as the capital-efficient variant.
- **Reported:** CAGR vs SPY, max drawdown, time to recover from the max drawdown, worst month,
  turnover, time in each state, by year. **Success:** CAGR ≥ SPY + 3% with max drawdown ≤ SPY's and
  recovery time ≤ SPY's.

**ITEM 3 — INTRADAY SLEEVE, PRE-REGISTERED.**
- **Data:** Alpaca SIP 5-minute bars 2019-01→2026-10 (1-minute for 2019+ is ~100M bars; 5-minute
  is 8M and fits the droplet); universe: the 60 largest US equities by trailing-60-session dollar
  volume, rebuilt each January (point-in-time), plus SPY and QQQ.
- **Strategies, each pre-registered, long-only:** (S1) **opening gap, follow:** gap ≥ 2% up at the
  open vs the prior close; buy when the first 15 minutes' high is broken; stop at the 15-minute
  low; exit at the stop or 15:50 ET. (S1f) **opening gap, fade** (reported, the mirror): gap ≥ 2%
  down, buy at 09:45 if price is above the 09:30 open, stop at the day's low so far, exit at the
  stop or 15:50 — long-only, so only down-gaps are faded. (S2) **15-minute opening-range breakout:**
  any universe name whose first-15-minute relative volume ≥ 1.5× its 20-day average for that
  window, buy on the break of the range high, stop at the range low, exit at 2R or 15:50. (S3)
  **intraday momentum:** at 10:00 the five largest gainers since the open with RVOL ≥ 2×, buy,
  trailing stop 1 × ATR(5-min, 20 bars), exit at the stop or 15:50.
- **Costs:** 5 bps per side plus half the quoted spread (5-minute bar high-low/4 as the proxy),
  $0 commission; **sizing:** equal risk, 1% of capital per trade, capital 100% of the sleeve.
- **Reported, by year and pooled:** net return, CAGR, Sharpe, hit rate, average win vs average
  loss, max drawdown, trades per day, vs SPY buy-and-hold. **Success:** net CAGR > SPY with max
  drawdown below SPY's over 2019–2026 and positive in ≥ 5 of 7 full years.

All four backtests run under the capped user service, detached, restart-safe; each writes to
`~/Agentic/data/<name>_2026-10-08.*`. Order of launch: 6 (uses harvested data), then 2 (cheapest),
then 3, then 1 (the options data pull is the heaviest).

### MERGER-ARBITRAGE BACKTEST — RESULT (2026-10-08 07:13 UTC, the pre-registered run on the corrected universe): criterion NOT MET; the spread premium is real but it is a beat-cash return, not a beat-SPY one

**Universe and coverage (check 2 first, because it frames everything):** 3,082 single-filer
DEFM14A / SC 14D9 filings 2016-01..2026-10 (228–345 a year). Ticker from the EDGAR index 825,
recovered from the filing's cover text 685, **no ticker recoverable 1,572 (51%)**. Of those 1,572,
**1,001 stopped filing within 9 months of the proxy (absorbed — completed or taken private), 457
kept filing a year on (broke, or the filer was the surviving company), 114 indeterminate.** So the
measured set still under-represents COMPLETED deals (completions have short holds and small
positive returns, so the missing mass would raise the completion share and the hit rate, lower
the unresolved share, and leave the annualized level roughly where it is). Excluded in addition:
acquirer-side proxies 452, equity < $100M or unknown 237, price < $1 23, still inside their 12
months 103. **Measured: 772 deals.** Measured break rate **2.7%** against the 5–10% historical
reference — low for two reasons: breaks that were never announced with the registered phrases sit
in "unresolved" (294 deals held to the 12-month cap), and the dropped no-ticker set is
completion-heavy, which cuts the other way. The by-source split: index-ticker deals annualized
net ROC +2.14% (excess −13.3%), text-recovered deals +4.36% (excess −7.7%).

| headline (772 deals, entry next open after announcement, exit at delisting / termination / 12-month cap, long-only, net of 15bp) | value |
|---|---|
| completed / break / unresolved | 457 (59%) / 21 (3%) / 294 (38%) |
| net return per deal: mean / median / hit | +2.02% / +1.40% / 67% |
| median hold | 178 days |
| **annualized net return on capital** (capital-day weighted) | **+3.37%** (per-deal annualized median +4.85%) |
| **annualized net excess vs SPY** | **−10.18%**, 5,000-draw bootstrap CI **[−15.0%, −4.8%]**, P(≤ 0) = 1.000 |
| worst year (annualized net excess) | **−27.65% (2023)**; 7 of 11 years below −10% |
| breaks: loss mean / median / p10 / worst | −25.4% / −19.6% / −68.3% / −72.3%; 81% of breaks lose |

By year, annualized net excess: 2016 −19.1, 2017 −13.8, 2018 −19.9, 2019 −24.0, 2020 −13.7, 2021
+7.3, 2022 −10.5, 2023 −27.7, 2024 −7.7, 2025 −4.8, 2026 +10.8 (n 32–157). **Criterion (annualized
net excess ≥ +3% with the bootstrap lower bound > 0; no year below −10%): NOT MET on all three
legs.** Read: the deal spread is captured — cash deals complete 75% of the time at a median +1.39%
in 126 days, and the 0–5% entry-spread bands complete 93% with hit rates of 88–92% — but that is
a ~3–5% annualized return on capital, i.e. a risk premium of T-bill-plus size, and the registered
benchmark is SPY over a decade that returned ~13% a year. Long-only, unhedged, the strategy cannot
beat SPY; it would beat cash. A cash or T-bill benchmark, or a hedged stock-deal leg, would be a
different, unregistered criterion and is not claimed here.

**By consideration:** cash n=446 (75% complete, ROC +2.3%, excess −9.0%); stock n=109 (37% complete,
breaks 7%, mean +10.8% on a +1.15% median — a right tail of acquirer rallies, excess 0.0%); mixed
n=136 (ROC +6.2%); unknown n=81 (ROC −12.2%).

**Item 2c — do stamped determinants sort outcomes?**

| stamp | YES: n, complete, breaks, ann. ROC | NO: n, complete, breaks, ann. ROC |
|---|---|---|
| HSR mentioned at announcement | 527, 66%, 3%, +4.9% | 245, 44%, 2%, +0.9% |
| second request within the window | 83, 59%, **6%**, +3.1% | 689, 59%, 2%, +3.4% |
| CFIUS mentioned | 61, 64%, 2%, **−7.2%** (mean −4.8%) | 711, 59%, 3%, +4.4% |
| "financing condition" text present | 308, 80%, 1%, +9.8% | 464, 45%, 4%, +0.6% |
| unsolicited / hostile language | 465, 63%, 2%, +2.6% | 307, 53%, 4%, +4.5% |

Caveat on the financing stamp: it is text presence, and the phrase almost always appears as "NOT
subject to a financing condition" — the YES group is the well-documented, fully-financed deal, which
is why it completes 80% of the time. A real stamp needs the negation parsed. The **spread at entry**
is the stamp that sorts (cash deals with a parsed price, n=364):

| entry spread | n | complete | breaks | net ret mean / median | ann. ROC | ann. excess vs SPY |
|---|---|---|---|---|---|---|
| < 0% (above the offer) | 32 | 59% | 3% | −6.4% / −0.9% | −11.2% | −15.5% |
| 0–2% | 98 | **93%** | 0% | +1.1% / +1.2% | +3.2% | −9.1% |
| 2–5% | 82 | 93% | 2% | +0.5% / +2.2% | +1.4% | −10.6% |
| 5–10% | 38 | 79% | 0% | +1.2% / +1.2% | +2.5% | −7.8% |
| 10–20% | 52 | 77% | 2% | +3.7% / +2.5% | +7.5% | −3.1% |
| **> 20%** | 62 | 69% | **6%** | +8.2% / +2.6% | **+17.1%** | **+2.9%** |

Wide spreads predict both breaks (6% vs 0%) and return (the classic gradient): the only band with
a positive excess vs SPY is > 20% (n=62, 5 years' worth of ~12 deals a year), and that is where a
judged filter would have a job — sorting the wide-spread deals' break risk (regulatory, financing,
hostile) rather than the whole flow. On this evidence that is a 60-deals-a-decade niche with a
−72% worst case, not a sleeve.

**Volume (item 2d):** 103 targets trading inside their 12-month window today; 85 proxies/14D9 filed
in the last 120 days; ~20–25 new definitive deals a month in the universe. Capital at a typical
judged position ($1.1–2.75k) over the median 178-day hold: $113k–$283k if every open deal were
held — far more than the sleeve; at 15–25 positions it is $17–69k, i.e. the whole judged sleeve for
a ~3–5% annualized return. Outputs `~/Agentic/data/marb_backtest_2026-10-08.{txt,deals.jsonl,facts.jsonl}`.

### RULINGS 2026-10-07 (news and direction) — ITEM 3: the OCT-15 SCORECARD (one table) and the retirement PROPOSAL

Item 1 recorded as ruled: across congressional, Form 4, 8-K, contract awards, PDUFA and news the
move happens in the print and the post-publish drift this system can reach is flat or negative;
the judged arm cannot win on information speed.

**Scorecard (production audit log + cached forward rows, read 2026-10-08 02:30 UTC; excess vs SPY;
"all rows" = every funnel row incl. prefiltered and capped, "researched" = rows with a verdict):**

| source | live since | funnel rows | researched / longs / traded | all rows 5d | all rows 20d | researched 20d | registered result / read | status |
|---|---|---|---|---|---|---|---|---|
| congressional_disclosures | 08-19 | 3,289 | 83 / 3 / 1 | −0.98% (hit 31%, n=3,144) | −2.10% (32%, n=1,877) | **+5.00% (57%, n=54)** | ≤$15K band −0.79% 5d (n=1,541) → prefiltered; verdict ruled for **10-27** on the 20d read | live, cap 1 (demoted); mechanical control arm live |
| form4_insiders | 09-03 | 595 | 77 / 44 / 9 | −1.91% (31%, n=484) | −3.26% (31%, n=209) | **−4.78% (25%, n=8)**; longs 20d −10.25% (n=4) | clusters vs control singles: both negative; C-suite door n small | live, cap 2 (demoted); review 10-15 |
| form_8k | 09-16 | 506 | 96 / 0 / 0 | −0.47% (29%, n=431) | not yet due | −3.22% 5d (30%, n=46) | backtest 2024–26: 1.01 −0.56, 8.01 −0.63 size-matched; **no widening**; rows before 10-08 cap-selected (18%) | live, cap 6; whole flow recorded from 10-08 |
| gov_contract_awards | 10-07 | 18 | 0 / 0 / 0 | n=0 | n=0 | — | backtest rule +0.65 (CI [+0.15, +1.20]); go-live n≥25 live events | measurement-only (probation) |
| trump_posts | 08-19 | 318 | 99 / 0 / 0 | n=0 (theme/ETF rows carry no ticker mark) | n=0 | — | 99 passes, zero longs | live, Class 1 |
| X callers (unusual_whales, optionshawk, nolimitgains, citrini) | 08-25.. | 123 | 14 / 0 / 0 | n≤4 | n≤1 | — | 0 trades; citrini had no trial (wrong handle to 09-30); feed-spend ruling 10-15 | live, graded 10-15 on spend |
| form_13d | 09-22 | 4 | 4 / 0 / 0 | n=1 | — | — | too thin | live |
| form_13f | 08-18 | 10 | 10 / 0 / 0 | — | — | — | quarterly; zero longs | live |
| overreaction_screen | 09-03 | 1,504 | 0 | −0.12% (49%, n=1,471) | −0.19% (53%, n=1,414) | — | measurement rows only; no fade signal at 5d/20d | measurement-only |
| PDUFA calendar | — | — | — | — | — | — | backtest: criterion not met | retired before build |
| news catalysts | — | — | — | — | — | — | backtest: intraday −0.25, t+5 −0.20; no drift | retired before build |

**Check 1 (2026-10-08): the congressional +5.00% at 20d, split by FINAL verdict** (`cong_split.py`;
83 researched rows: decline 80, long 2, boundary/vote-overturned 1; clustered bootstrap by ticker;
QQQ-relative = raw return − QQQ over the identical window; mega-cap tech = a fixed list of 38 names):

| rows | n (5d) | 5d vs SPY | 5d vs QQQ | n (20d) | 20d vs SPY | 20d vs QQQ |
|---|---|---|---|---|---|---|
| ALL researched | 80 | −0.86 (hit 35%) CI [−2.02, +0.09] | −1.54 CI [−2.92, −0.33] | 54 | **+5.00, med +1.64, hit 57%, CI [−2.94, +12.12]** | +2.85, med −1.55, hit 43%, CI [−5.66, +10.56] |
| **decline** | 77 | −1.13 CI [−2.27, −0.10] | −1.79 CI [−3.20, −0.62] | **52** | **+4.64, med +1.64, hit 58%, CI [−2.83, +11.91]** | +2.51, med −1.55, hit 42%, CI [−5.47, +10.51] |
| long | 2 | +8.71 | +8.92 | 2 | +14.52 (INTC +35.2, FITB −6.2) | +11.83 |
| mega-cap tech rows (35% of researched) | 29 | −0.22 | −0.78 | 23 | **+7.23, med +3.47, hit 74%, CI [+1.13, +12.95]** | +5.33, med +1.81, CI [−1.05, +11.47] |
| ex mega-cap tech | 51 | −1.23 | −1.96 | 31 | +3.35, med −0.31, hit 45%, CI [−8.28, +14.09] | +1.01, med −4.11, hit 32% |

**Said plainly: the +5% is the DECLINES.** 52 of the 54 rows with a 20d mark were declined and
those names rose +4.64% vs SPY afterwards; the two longs are one INTC (+35%) and one FITB (−6%).
Three qualifications before "anti-skill": the decline mean is not significant (CI straddles zero,
median +1.64); 23 of the 54 are mega-cap tech and carry most of it (+7.23% vs SPY, +5.33% vs QQQ,
median vs QQQ +1.81 — a chunk is the period's tech beta, not stock selection); ex-mega-tech the
median is negative vs both benchmarks. So this is not evidence the source works; it is weak evidence
that the judge declined names that went up in a strong tech tape, with n=52 and a wide interval.
The 10-27 read decides; the retirement proposal below is unchanged by this.

**Retirement PROPOSAL (proposal only, for the 10-15 review):** move **congressional** and **Form 4**
research to **measurement-only rows at research cap 0** (every candidate still listed, prefiltered,
stamped and graded by the forward engine; no LLM pass; the mechanical control arm keeps copying
congressional purchases exactly as now, so the judged-vs-mechanical comparison survives). Grounds:
Form 4 researched rows are −4.78% at 20d and the four longs −10.25% — the judged selection is worse
than the flow; congressional's judged rows are the ONE positive line on the table (+5.00% at 20d,
n=54, hit 57%) but on a demoted cap of 1 a day the sample cannot grow to a verdict before 10-27, and
the ruled verdict date is 10-27 on the 20d read of the full flow. **Suggested sequencing:** retire
Form 4 research on 10-15; hold congressional research at cap 1 until the 10-27 verdict, then retire
or restore on that read. Spend freed: ~3 passes a day (Form 4 cap 2, congressional 1), roughly a
dollar a day. The 8-K cap 6 stays as ruled (negative drift but the whole flow is only measurable from
10-08). Nothing here is applied.

### RULINGS 2026-10-07 (news and direction) — ITEM 4: the two root command blocks (paste-ready)

**A. 2 GB swap, persistent across reboot** (jobs carry `MemorySwapMax=0`, so swap serves
production only):
```
sudo bash -c 'set -e; fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab; sysctl -w vm.swappiness=10; grep -q "^vm.swappiness" /etc/sysctl.conf || echo "vm.swappiness=10" >> /etc/sysctl.conf'
```
Verify: `swapon --show && free -m | grep -i swap && grep swapfile /etc/fstab` — expect one 2G line,
`Swap: 2047` total, and the fstab line.

**B. Backup push script install** (the script is in the droplet's checkout; until
`/home/agentic/.backup_env` exists it logs one line and exits 0, so the unit turns green now and
pushes once the Spaces keys are written — vars listed in the script header):
```
sudo bash -c 'set -e; install -m 755 /home/agentic/Agentic/ops/vps/push_backup.sh /usr/local/bin/agentic-push-backup; apt-get install -y -q rclone gnupg >/dev/null; systemctl daemon-reload; systemctl reset-failed agentic-backup.service; systemctl start agentic-backup.service'
```
Verify: `systemctl status agentic-backup.service --no-pager | head -5 && ls -la /usr/local/bin/agentic-push-backup && journalctl -t agentic-backup --no-pager | tail -2` — expect
`Active: inactive (dead)` with `status=0/SUCCESS` on both ExecStart lines, the script at 755, and the
syslog line "off-box push not configured (no /home/agentic/.backup_env); on-box tarball only" (or
"pushed … to spaces:…" once configured).

### PRE-REGISTERED MERGER-ARBITRAGE BACKTEST (ruling 2026-10-07 item 2; registered BEFORE it ran)

- **Universe:** US-listed TARGETS of definitive deals, 2016-01..2026-10, from the target's own
  filings on EDGAR full-text search: **DEFM14A** (definitive merger proxy — a signed agreement) and
  **SC 14D9** (the target's tender-offer recommendation — the target files it, so the subject is
  unambiguous; SC TO-T lists the bidder as filer and is used only to confirm tender deals). Amendments
  excluded; one deal per target CIK per 180 days (the earliest filing). The target must carry a
  listed ticker in EDGAR's display names. **Announcement** = the target's 8-K carrying item 1.01 whose
  text names an "Agreement and Plan of Merger" (or "merger agreement" / "tender offer") filed within
  the 150 days before the proxy/14D9; without one, the proxy/14D9 itself is the announcement.
  **Exclusions (as ruled):** equity value at entry < $100M (SEC shares × entry open) and entry open
  < $1 per share.
- **Entry:** the next session's open after the announcement filing's acceptance (file-date open if
  accepted before 09:30 ET). **Exit:** (a) completion = the target's last bar within 12 months
  (delisting) → that last close; (b) break = the target's 8-K within the 12 months whose text says the
  merger agreement was terminated (item 1.02 or "terminat…" + "merger agreement") → the next open
  after that filing; (c) otherwise the close 12 months after entry (unresolved, held to the cap).
  Long-only, no hedge for stock deals (as ruled). **Return** net of 15bp per deal; **excess** = net
  return minus SPY over the identical window; **annualized return on capital** = Σ(net return) /
  Σ(holding days) × 365 (capital-day weighted) and the per-deal annualized median.
- **Reported:** n deals, completion / break / unresolved rates, mean and median net return, hit
  rate, annualized net return on capital and its excess over SPY, the loss distribution on breaks
  (mean, median, p10, worst), by year, by cash vs stock consideration (from the announcement text),
  and 5,000-draw event bootstrap on the annualized net excess (deals cluster on nothing larger
  than the deal; a by-year table shows regime dependence).
- **Success criterion:** annualized net excess over SPY ≥ **+3%** with the bootstrap lower bound
  above zero, AND no calendar year below −10% net excess.
- **Determinants, reported separately (item 2c, the LLM's potential role):** stamped per deal from
  the filings' text — regulatory: "second request" (HSR) or "CFIUS" in the target's 8-Ks/proxy within
  the window; financing condition ("financing condition" / "subject to financing"); hostile vs
  friendly (SC 14D9 recommending against / "unsolicited"); cash vs stock; spread at entry for cash
  deals (announced per-share price / entry open − 1, from "$X.XX per share"). Outcomes (break rate,
  net return) sorted by each; if wide spreads at entry predict breaks, a judged filter has a job.
- **Volume:** live deals open now = DEFM14A / SC 14D9 filed in the last 120 days whose target still
  trades; capital tied up at a typical judged position (2–5% of a $55k sleeve = $1.1–2.75k each)
  and at the measured median holding period.
- Memory-capped, detached, restart-safe (per-deal cache). Script `~/scratch/marb_backtest.py`,
  outputs `~/Agentic/data/marb_backtest_2026-10-08.*`.

**Corrections made BEFORE the registered run completed (2026-10-08 02:45–03:05 UTC; the rule,
entry, exit, criterion and determinants above are unchanged):**
1. Two smoke runs (2023-10..2024-03, 30 deals) showed: DEFM14A is also filed by ACQUIRERS voting on a
   share issuance → the target is identified from the proxy's and announcement's wording (target
   cues vs acquirer cues; an unresolved DEFM14A filer with no target cue is excluded as unverified);
   terminations are announced under 8.01 or in press releases, not item 1.02, and the loose phrase
   "termination of the merger agreement" appears in outside-date amendments of deals that then
   CLOSE (HA, AXNX, HAYN, JNPR were marked breaks) → break detection uses tight phrases
   ("terminated the merger agreement", "mutually agreed to terminate", …) and **delisting inside the
   window always wins (completed)**; partial tenders (SC 14D9 without "all outstanding shares") are
   excluded; par values ("$0.01 per share") no longer parse as deal prices.
2. **Survivorship (check 2, found by probe 03:00 UTC and fixed before the run):** EDGAR full-text
   search attaches a ticker ONLY to current listings — the definitive proxies of Xilinx, Twitter
   and Pioneer show no ticker; Hawaiian and Juniper (recently closed) still do. The first launch
   required an index ticker and would have DROPPED most completed targets: 826 deals 2016–26
   against ~3× that once every single-filer proxy is kept. Direction of the bias in the dropped
   run: fewer completions, more breaks/unresolved, headline annualized excess biased DOWN and the
   break rate biased UP. The run was stopped at "deals processed 0" and relaunched on every
   single-filer DEFM14A / SC 14D9 with the ticker taken from the index when present, else from the
   filing's own cover text ("under the symbol 'XLNX'", "(NASDAQ: XLNX)"); `ticker_source` is stamped
   and the report prints results by source, the no-ticker remainder, and for that remainder whether
   the filer stopped filing within 9 months of its proxy (absorbed) or kept filing (broke / was the
   survivor), beside the measured break rate against the 5–10% reference.

### MORNING SUMMARY 2026-10-08 — everything landed; nothing running; what waits on the human

**Shipped to production (droplet HEAD = origin = vps):** item 2 funnel holes + pelosi-uber band
(e470c4a, 10-07 midday); item 1 OOM audit, capped research jobs, weekly CAUTION and slot_lost
sections (9540220); notes through this entry. The paper unit runs the whole bundle from the 10-08
session start.

**Backtests, all DONE (results on disk, recorded above):** 8-K items 1.01/8.01 — no widening, cap
stays 6, size-matched restatement confirms; PDUFA — no source; news catalysts — no source (intraday
leg fixed as a data correction and recorded before/after); **merger arbitrage (2026-10-08 07:13
UTC) — criterion not met: +3.4% annualized net ROC, −10.2% vs SPY, a beat-cash return.** No research
job is running; the `research-*` user services are inactive. One status command, unchanged:
`ssh agentic@137.184.59.200 'cat ~/Agentic/data/item6_backtests.log; systemctl --user list-units --type=service --no-pager | grep research; ls ~/Agentic/data/*2026-10-07*.txt'`

**Waiting on the human:** (1) 2 GB swap — root commands in the item-1 entry; (2) the backup push
step (`/usr/local/bin/agentic-push-backup` missing, root); (3) nothing else is queued — the next
ruling decides where the judged arm's candidate flow should come from, given that congressional
(demoted), Form 4, 8-K, contracts (measurement-first), PDUFA and news have each been measured.

### NEWS-CATALYST BACKTEST — THE INTRADAY LEG, FIXED (2026-10-08 02:06 UTC); and the size-matched secondary read: NO SOURCE

**The defect and the fix (data correction only; rule, groups, timing and criterion exactly as
pre-registered).** The registered run fetched the 1-minute print at publish with
`adjustment=raw` while its daily closes were split-adjusted. Rows touched: **345 of 3,526
in-session research/measure rows carried |publish → close| > 50 points, 388 > 20** (reverse splits:
WKHS 2021–22, TNXP 2023, PFSA 2025, VSA 2025 …). The fix pass (`news_fix.py`, capped user service,
restart-safe cache) refetched the print at publish split-adjusted for the ticker, SPY and IWM plus
the ticker's split-adjusted close that day for all 3,576 in-session research/measure rows (3,526
had a print; 50 none) and recomputed the same measure; **5 rows still inconsistent (|raw| > 60) are
excluded.** Nothing else about the run was changed.

| INTRADAY publish → close, research tier, in-session, vs SPY | mean | trimmed 5% | median | hit | n | clustered CI |
|---|---|---|---|---|---|---|
| **before the fix** (registered run, artifact) | +168.01 | — | +0.00 | 50% | 2,870 | [+85.5, +288.8] (meaningless) |
| **after the fix** | **−0.25** | −0.09 | −0.02 | 49% | 2,866 | SE 0.10, t −2.58, **[−0.45, −0.07]** |
| measure tier, after the fix | −0.16 | −0.04 | −0.03 | 48% | 655 | [−0.39, +0.05] |

**Against the registered bar first:** the intraday leg is NEGATIVE — mean below zero, the cluster
CI entirely below zero, hit 49% — so it fails on every leg, as the next-open path did (t+5 −0.20,
CI [−0.38, −0.01]). **The decisive split (research tier, in-session, fixed):** intraday −0.25 | gap
+0.06 | next-open → t+1 −0.20 | → t+5 −0.38. Nothing after the publish is positive. The catalyst's
move is in the print itself (and, for the 80% of items published out of session, in the GAP: all
research-tier items, next open vs reference close, mean +2.03, median +0.40, hit 64%, n=19,729 —
the announcement reaction, which no next-open entrant receives). A real-time path would have to be
in the trade within the minute; this system's extraction plus research pass is minutes.

**Size-matched read, secondary (IWM under $10B — 14,227 of the 19,735 research-tier events — SPY
above):** intraday −0.24 (vs −0.25); next-open → t+1 −0.06 (vs −0.10), t+5 **−0.15, CI [−0.33,
+0.05]** (vs −0.20), t+20 −0.45, CI [−0.78, −0.12] (vs −0.63). By year, t+5 size-matched: 2016 +0.43,
2017 +0.48, 2018 +0.42, 2019 +0.44, 2020 +0.20, 2021 +0.60, 2022 +0.90, 2023 +0.49, 2024 +0.33,
**2025 −0.80, 2026 −1.94** — the same shape as vs SPY. By cap, t+5 size-matched: < $2B −0.21,
$2–10B −0.09, > $10B −0.10. **The small-cap lag does not explain the last two years**; the drift
after a news catalyst turned negative in 2025–26 across cap bands. The size-matched read confirms no
signal in the next-open path either, and the eight positive years before 2025 (+0.2 to +0.9) are
not a path this system can take today in any case.

**VST check items (fixed):** 10-02 15:27 ET DOE-loan headline, research tier ($4.0B / cap):
publish → close **+0.59**, gap to Monday's open +2.86, next-open → t+1 +10.21 (10-05 open → 10-06
close: the Google deal day); 10-05 12:29 ET 'Vistra Gets $4.2B Federal Loan': publish → close
−1.05, gap +4.11, t+1 +10.19. The headline session captured little; the moves were the next open
and the second catalyst.

**Conclusion for item 1 (report; nothing built):** on the pre-registered criterion and on the
secondary read, **the news-catalyst class has no exploitable post-publish drift** at any horizon
this system can reach — intraday, next open, t+5, t+20 are all ≤ 0 since 2025, and the only
positive leg is the announcement gap itself. Not pursued. Outputs
`~/Agentic/data/news_backtest_2026-10-07.{txt,fixed.txt,events.jsonl,fixed.events.jsonl}`.

### NEWS-CATALYST BACKTEST, STAGE A — RESULT (2026-10-08 00:41 UTC, the pre-registered run): the next-open path has no signal; the intraday leg needed a fix

**The run:** 2015-01..2026-10, **2,225,683 items read, 49,795 candidates (2.2%), 57,363 ticker-day
events, 55,931 measured.** Tiers: research (≥ 1% of cap, issuer named) **19,735**, measure 3,765,
below_floor 2,495, not_named 7,069 (the issuer-must-be-named amendment), unsized 22,794 (no SEC CIK
for the ticker: delisted names, ADRs, ETFs, crypto tickers), no_bars 73. **Per session day: 16.8
events, 5.9 in the research tier**; 20% of events publish in session. Excess vs SPY, points:

| research tier (19,735 events, 3,602 tickers) | mean | median | hit | n |
|---|---|---|---|---|
| gap: next open vs reference close (in-session) | +0.06 | +0.06 | 53% | 2,909 |
| next-open → t+1 | −0.10 | −0.11 | 49% | 19,704 |
| **next-open → t+5** | **−0.20** | −0.24 | 48% | 19,653 |
| next-open → t+20 | −0.63 | −0.77 | 46% | 19,431 |
| t+5 clustered (3,596 tickers) | SE 0.10, t −2.07, **CI [−0.38, −0.01]** | | | |
| t+5 in-session only / out-of-session | −0.38 / −0.17 | −0.34 / −0.22 | 46% / 48% | 2,891 / 16,762 |

By year, t+5: 2016 +0.66, 2017 +0.39, 2018 +0.31, 2019 +0.37, 2020 +0.34, 2021 +0.47, 2022 +0.85,
2023 +0.39, 2024 +0.19, **2025 −0.78, 2026 −2.00** — positive for eight years and sharply negative
for the last two (n=3,374 and 3,362, the largest years). Measure tier (3,765): t+5 +0.04, CI
[−0.43, +0.60]. **The pre-registered criterion (research tier next-open → t+5 mean > 0, CI excluding
0, hit > 50%, net of 15bp > 0) FAILS**: the pooled mean is negative and the recent two years drive
it. The size-matched secondary leg (below) says whether 2025–26 is the small-cap lag.

**Intraday leg — a defect in the registered run, fixed in a second pass (2026-10-08, before any
reading):** the 1-minute print at publish was fetched UNADJUSTED while the daily closes are
split-adjusted, so 345 of 3,526 in-session rows (reverse splits — WKHS, TNXP, PFSA, …) carried
absurd values: "mean +168 on a median of 0.00". Trimming |x| > 20 gives mean −0.02 / median −0.01 on
3,138 rows — i.e. about nothing — but the honest number is a refetch: `news_fix.py` re-fetches the
print at publish split-adjusted for the ticker, SPY and IWM plus the ticker's adjusted close that
day, recomputes the leg, adds the size-matched daily leg, and writes `.fixed.txt`. Reported below.

**VST check items (registered run; next-open legs not yet due):** 10-02 15:27 ET 'US to Offer $4B
Loan…' (Bloomberg) — research tier, $4.0B / cap; publish → close +0.59 vs SPY (raw move ran +1.6%
into the headline and faded), **gap to Monday's open +2.86**; 10-05 12:29 ET 'Vistra Gets $4.2B
Federal Loan…' — research tier; publish → close −1.05; gap to 10-06 open +4.11. Both moves were in
the GAP, not in the session that carried the headline — the 10-06 +10.8% day was the Google deal.

### RULINGS 2026-10-08, 8-K — accepted; and the SIZE-MATCHED BENCHMARK LEG, pre-registered

1. **Accepted and recorded:** no 1.01 or 8.01 widening, the 8-K cap stays at 6, stage B closed.
2. **Size-matched benchmark, pre-registered as a SECONDARY report (not a change to any criterion;
   SPY stays the deployment benchmark):** every 8-K group including the earnings comparison ran
   negative vs SPY over 2024–26, which looks like the small-cap lag of the period. Each excess is
   therefore restated against a size-matched benchmark: **IWM for point-in-time caps under $10B,
   SPY at or above $10B** (unsized rows keep SPY and are labelled); cap-tercile matching is not
   built. Method: the ticker's raw return over the exact same window (next open → close t+h, and
   for the news intraday leg the 1-minute print at publish → close) minus the benchmark's return
   over the same window, computed from the benchmark's own bars — i.e. the SPY excess already
   stored plus (SPY − IWM) over the window. Registered here BEFORE the news backtest's measurement
   stage ran (its harvest was at 2017 when this was written) and before the 8-K restatement was
   computed. The size-matched read decides whether a signal exists; the pre-registered SPY
   criterion decides the trading path, unchanged.

**8-K groups restated, one table (`size_matched.py`, 40,446 events; IWM-benchmarked share 76–78%):**

| group | n | vs SPY t+1 / t+5 / t+20 | size-matched t+1 / t+5 / t+20 | t+5 CI vs SPY | t+5 CI size-matched |
|---|---|---|---|---|---|
| 1.01 | 12,866 | −0.26 / −0.61 / −2.83 | −0.24 / **−0.56** / −2.74 | [−1.42, +0.75] | [−1.39, +0.80] |
| 8.01 | 20,390 | −0.67 / −0.69 / −1.71 | −0.63 / **−0.63** / −1.54 | [−1.07, −0.24] | [−1.02, −0.18] |
| both | 3,826 | −0.95 / −1.32 / −2.44 | −0.92 / −1.25 / −2.25 | [−2.05, −0.49] | [−2.01, −0.40] |
| earnings 2.02 | 3,364 | −0.63 / −0.79 / −1.03 | −0.55 / −0.64 / −0.74 | [−1.21, −0.36] | [−1.06, −0.24] |

Within the IWM-benchmarked rows (caps under $10B) the t+5 restatement moves by +0.06 to +0.19 points
(1.01 −0.39 → −0.33; 8.01 −0.66 → −0.57; earnings −0.84 → −0.65); the SPY-benchmarked rows are
unchanged by construction. **Read: the small-cap lag of 2024–26 is not what made these groups
negative** — over five-day windows IWM and SPY differ by a few basis points on average, and the
post-filing drift stays negative with the CIs where they were. The size-matched leg confirms no
signal; the SPY-based ruling stands.

### 8-K ITEMS 1.01 / 8.01 BACKTEST, STAGE A — RESULT (2026-10-07 18:04 UTC, the pre-registered run): NO WIDENING; and item 2b

45,776 filings 2024-01-02..2026-10-02, 40,446 with bars; point-in-time acceptance times (before-open
filings trade at that day's open), caps = SEC shares × close(t0). Excess vs SPY, points,
next-open → close (oc) unless stated:

| group | events / tickers | gap | oc t+1 | **oc t+5** | oc t+20 | oc5 clustered CI | by year oc5 (24/25/26) |
|---|---|---|---|---|---|---|---|
| 1.01 (no 8.01/2.02) | 12,866 / 3,546 | +1.17 (med −0.02) | −0.26 | **−0.61, med −1.23, hit 40%** | −2.83 | SE 0.59, t −1.0, **[−1.45, +0.84]** | +0.71 / −1.32 / −1.36 |
| 8.01 (no 1.01/2.02) | 20,390 / 3,850 | +0.50 | −0.67 | **−0.69, med −0.81, hit 43%** | −1.71 | SE 0.22, t −3.2, **[−1.07, −0.21]** | −1.07 / −0.28 / −0.79 |
| both 1.01 + 8.01 | 3,826 / 1,974 | +2.27 | −0.95 | **−1.32, hit 39%** | −2.44 | SE 0.41, t −3.2, [−2.06, −0.42] | −0.72 / −1.48 / −1.93 |
| earnings 2.02 (comparison) | 3,364 / 1,321 | +0.38 | −0.63 | −0.79, hit 45% | −1.03 | SE 0.22, t −3.7, [−1.21, −0.38] | −0.47 / −1.14 / −0.72 |

By cap: 1.01 small −0.24 / mid −0.89 / large −2.77; 8.01 small −0.55 / mid −1.04 / large −1.18.
With 5.02 on the filing: 1.01 +0.16 (n=917), 8.01 −1.25. **The pre-registered criterion (oc5 mean > 0
with the CI excluding 0, hit > 50%, beats 2.02) fails for both items on every leg**: 1.01 is
indistinguishable from zero and 8.01 is reliably NEGATIVE after the open, as negative as the
earnings-day comparison group. Post-filing drift on these items is downward, not upward. The ruling's
default (no 8.01 widening) stands with the data behind it; stage B (dollar extraction) is not run
because stage A did not pass. Outputs `~/Agentic/data/k8_backtest_2026-10-07.*`.

**Item 2b — researched vs would-have-been-discarded, same window (item 1.01 filings
2026-09-15..10-02 that are in the backtest universe, 354; `k8_2b.py`):**

| group | filings / tickers | gap | oc t+1 | oc t+5 | oc5 clustered CI |
|---|---|---|---|---|---|
| researched (an audit record carries a verdict) | 13 / 12 | −1.08 | −4.42 | **−4.38**, med −4.75, hit 31% | [−11.6, +2.9] |
| recorded, not researched (prefilter / cap codes) | 68 / 65 | −0.18 | −3.71 | −5.66, hit 25% | [−8.9, −2.6] |
| would have been discarded (listed, no record) | 273 / 252 | +1.15 | −1.89 | **−4.48**, med −2.53, hit 29% (n=225) | [−6.3, −2.6] |

The discarded group's t+5 mean (−4.48) equals the researched group's (−4.38) within noise (the
researched SE is 4.0 on n=13), and every group is sharply negative in this window (an 11-session
window; the full 2024–26 1.01 group is −0.61). **Proposal: NO 8-K cap increase.** The condition for
proposing one — the discarded set being at least as good — is met only in the sense that both are
equally bad; researching more item-1.01 filings would buy more passes on a flow whose post-filing
drift is negative at every horizon. The cap stays at 6 and the `slot_lost` rows now make the whole
flow measurable going forward. Vote cost for sizing (2b): 0 votes, 0 extra passes on the log so
far — the vote shipped at 05:xx UTC and today's session produced no tradeable or near-threshold
verdict; the ~7.6/day projection stands until measured.

### RULINGS 2026-10-08 — ITEM 1: the OOM audit; research jobs now run memory-capped; swap needs root

**1a. What was killed.** The kernel log is unreadable to the `agentic` user (`dmesg_restrict=1`,
`journalctl -k` empty, sudo needs a password), so the OOM kills are inferred from exit codes: the
8-K backtest died twice with exit 137 (SIGKILL) — 05:45 UTC while holding a 135 MB acceptance-time
map for 5,290 filers, and 16:03 UTC loading that map back — on a 961 MB droplet with no swap. **No
production unit was killed or restarted:** `agentic-paper` NRestarts=0, Result=success, one start per
session (10-07 13:15:01 UTC), OOMPolicy=stop untouched; every timer fired on schedule (earnings
20:30, overreaction 20:45, backup 01:07, weekly Fri 21:00). **No session lost data:** audit records
10-05 n=48 (13:31→19:49 UTC), 10-06 n=70 (13:33→19:49), 10-07 running (13:31→). The only failed
unit is pre-existing and unrelated: `agentic-backup` exits 203/EXEC because
`/usr/local/bin/agentic-push-backup` does not exist (the local tar step succeeds; the push step
needs root to fix or remove).

**1b. Built, no root needed.** `~/scratch/research_job.sh NAME LOG -- CMD` runs any research job as a
transient USER service: `MemoryMax` (default 600M, per-job override), `MemorySwapMax=0`,
`OOMPolicy=kill`, `OOMScoreAdjust=1000`, `CPUWeight=20`, `IOWeight=20`, `Nice=19`, output appended
to the job's log. The user cgroup has the cpu/memory/pids controllers delegated and lingering is now
enabled for `agentic` (`loginctl enable-linger` was permitted), so the services outlive the ssh
session. **Tested:** a 400 MB allocation under a 200M cap was OOM-killed inside its own unit
(`research-captest.service: Failed with result 'oom-kill'`) while `agentic-paper` stayed active.
Both backtests were stopped and relaunched under caps (k8 350M, news 300M; checkpoints resumed:
acceptance 39,805 of 45,776 known, news at 2016-04). Production units are system units outside this
slice; the kernel's victim is always the job. **Swap: cannot be added without root** — the commands
for you: `fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile &&
echo '/swapfile none swap sw 0 0' >> /etc/fstab`; with `MemorySwapMax=0` on the jobs, swap would serve
production only.

**1c. Peak memory, normal session.** `agentic-paper` cgroup: MemoryPeak **137 MB**, current 117 MB
(10-07 session). The timers' units report no peak (short-lived, `[not set]`). The box: 961 MB total,
~350 MB used by OS + production with no jobs running, ~506 MB with both backtests alongside.
**Production alone is far below 600 MB; no resize needed for production.** Two capped jobs at once
(350 + 300) plus production fit inside 961 MB; three would not.

**2a/2c built (weekly):** the 8-K by-item table now prints a CAUTION line splitting rows before and
since 2026-10-08 (the first session with the whole listed set) and names the cap-selected ~18%; a
new "Slot-lost candidates by source" section prints `slot_lost` rows per source with 20d/5d excess
beside the same source's researched rows, so a binding cap is visible the week it binds. Tests in
`tests/test_form8k.py`.

### MORNING SUMMARY 2026-10-07 (overnight rules) — what shipped, what's running, what waits on the human

**Shipped to production (droplet HEAD e470c4a = origin = vps; the paper unit was mid-session on
ae11e1e and loads the new code at tomorrow's start):**
- 8-K funnel holes (ruling item 2): `slot_lost` records at the day's last window and at shutdown;
  the lister reads every page. Gates: full suite green (3 skips); golden replay with the vote live
  **28 PASS / 1 DRIFT**, the drift (`review-intc-day9-post-blowout-real`, a review verdict reading
  `displaced`) rerun 3× per the 2026-10-06 drift protocol → **PASS, PASS, PASS** → nondeterministic
  review noise, not a code effect (the changed code touches no prompt); live lister dry-run
  DRY RUN OK (190 records end to end, pagination exercised, Vistra present). Deployed 16:1x UTC
  (12:1x ET) — outside the 60-minute pre-open window; a mid-session `git pull` changes nothing in
  the running process.
- pelosi-uber-priced-in traded band [0, 55] (item 4). Notes: item 1 report, PDUFA result, both
  pre-registrations, the 21-day hole count.
- Earlier the same night (before the overnight rules): the self-consistency vote (69ca3f5), the
  calibration block (637b23a), the 2b interval and the behavioural grading.

**On a branch:** nothing — `overnight-2026-10-07` was fast-forwarded into main once the gates held.

**Running detached on the droplet (restart-safe, results on disk):**
- 8-K items 1.01/8.01 backtest, stage A — relaunched twice: the first run kept every accession of
  5,290 filers in memory (135 MB) and the 1 GB droplet killed it (exit 137); now universe-only.
  Listing cached (45,776 filings, 144 weeks); acceptance times → caps → bars → report.
  Output `~/Agentic/data/k8_backtest_2026-10-07.txt` when done.
- News-catalyst backtest, stage A, 2015-01..2026-10 (~6 h): `~/Agentic/data/news_backtest_2026-10-07.txt`.
- PDUFA backtest: DONE, criterion not met (entry below).

**One command for job status:**
```
ssh agentic@137.184.59.200 'cat ~/Agentic/data/item6_backtests.log; tail -1 ~/Agentic/data/k8_backtest_2026-10-07.log; tail -1 ~/Agentic/data/news_backtest_2026-10-07.log; ls ~/Agentic/data/*_2026-10-07.txt'
```

**Waiting on the human:**
1. 8.01 widening: HOLD per ruling until the k8 report lands (default no widening).
2. News source (item 1): the design is a report; building it is a new source (approval), and the
   backtest's intraday-vs-gap split decides whether a real-time path is worth it.
3. The slot_lost rows start accruing tomorrow; the forward report's 8-K rows will roughly 5× —
   the 10-15 review of form_8k now has the whole candidate set behind it, but only from 10-08.
4. Calibration-block golden replay when the first voted cell reaches n ≥ 20 (standing reminder).
5. Vote stability read at ~20 votes per source (standing reminder).

### RULINGS 2026-10-07 (second set) — 8-K funnel holes BUILT; pelosi-uber band; the general class behind VST

**Item 2, both holes built** (`orchestrator/loop.py`, `signals/form8k.py`; tests in
`tests/test_dispatch_scoring.py` and `tests/test_form8k.py` with the two Vistra 1.01 filings as
fixtures — accessions 0001140361-26-037577 and -038468):
- **(a) Every listed filing leaves a record.** A pooled candidate still held at the day's LAST
  dispatch window is written as `stage_rejection slot_lost` ("held through the day's last dispatch
  window") and marked a slot loser; anything still deferred when the loop shuts down (pool or budget)
  is written the same way ("still deferred when the session shut down"). No LLM, one record each;
  the forward engine grades them like any prefiltered row. Before this the held queue died with the
  process and the seen ledger stopped a re-list.
- **(b) The lister reads every page.** `Form8KFetcher._list` pages `from` 0, 100, 200 … until the
  response's total is reached (cap 40 pages), per item per poll; a failed page keeps what was read.
- **How much the holes dropped, last 21 poll days (2026-09-08..10-06; `k8_holes.py`, every page of
  every whitelisted item over each day's [D−1, D] window, as production queried):** 1,259 tickered
  whitelisted 8-K filings listed. **Hole b** (never on page 1 of any window): **10** (8 carrying
  1.01, 3 carrying 5.02; 5 windows exceeded 100 hits). **Holes a+b together — listed but no audit
  record names the accession: 1,027 of 1,259 (82%)**, 1,017 of them on page 1, i.e. hole a: by
  item 1.01 546, 5.02 508, 2.05 19, 1.05 2, 4.02 2. Both Vistra filings: page 1, NOT recorded. So the
  8-K funnel has been researching 6 a day and silently discarding ~48 a day; the forward report's
  8-K rows were the 18% that got a slot or a prefilter. Measurement-first did not hold for 8-K
  until this entry. The slot_lost rows will start accruing the first session after deployment.
- **Live dry-run (deploy gate, `ops/experiments/form8k_dry_run.py`, scratch clone against live
  EDGAR, 3-day lookback, no LLM, temp audit log):** listed 203, pages read for "Item 1.01" [0, 100],
  190 signals emitted → 190 records → 190 funnel entries read back with items parsed, Vistra present.
  DRY RUN OK. Full suite green on the droplet.

**Item 4:** `pelosi-uber-priced-in` traded band [0, 49] → [0, 55]: a decline, a sub-floor long, or a
REPLICATED long in the 45–55 floor band all pass; a long above 55 is drift. Test pin updated.

**Item 1, the class behind VST (report follows in its own entry once the backtests are read, per
the ORDER):** large, dated, company-specific catalysts that arrive as news before any filing. Alpaca
news (Benzinga) probed on this account: HTTP 200 with the account keys, **200 requests/minute**
(`X-Ratelimit-Limit`), symbol-tagged (~97% of items carry symbols), `include_content` returns the
body (5–6k chars), **history back to at least 2015-01**, ~830–860 items a session (2026-09-29..10-01).
The Vistra DOE-loan item: `2026-10-02T19:27:03Z` (15:27 ET, in-session) tagged VST — 'US to Offer
$4 Billion Loan for Vistra to Boost Nuclear Output' – Bloomberg; the Google deal 10-06 13:32 UTC
tagged across CEG/VST/TLN/OKLO/SMR. Finnhub is not needed.

### ITEM 1 — NEWS-CATALYST SOURCE REPORT (2026-10-07; report, nothing built; backtest running)

**a. The source.** Alpaca news (Benzinga) on this account, probed live: HTTP 200, `X-Ratelimit-Limit`
200/min, items carry `created_at`/`updated_at`, `symbols` (~97% tagged), `source`, `url`, and the
body with `include_content=true`; history back to at least 2015-01 (paged, `next_page_token`);
volume ~830–860 items per session day, 16,435 in September 2026. Real-time: the Vistra DOE-loan item
is stamped 2026-10-02T19:27:03Z (15:27 ET), the Bloomberg headline itself. Finnhub not needed.

**b. Design (not built).** A Class 1 source `news_catalysts`: poll every 60 s (one request, `sort=desc`,
since the last `created_at`); drop items tagged with more than six symbols (wraps) or whose headline
is an analyst action, options flow, earnings, dividend, listicle or market wrap (deterministic
exclusions, as the backtest); the rest go through an **LLM extraction call** (haiku, forced tool,
~$0.001–0.003 each): event type ∈ {deal, contract/award, loan/financing, partnership/offtake,
acquisition, other/none}, counterparty, dollar value, whether the ISSUER is the party (not a
third-party deal the issuer is tagged on), and the date the catalyst is effective. Relative size =
dollars / point-in-time cap (Finnhub live, SEC shares × close in the backtest); tiers as contracts:
≥ 1% research, 0.2–1% measure, < 0.2% dropped with the reason stamped (`news_below_floor`). One
event per ticker per day (the largest). The research prompt gets the extraction as data and the
publish timestamp (the Class 1 staleness rule applies as written: ≥ 30 min old → priced-in
mandatory). Extraction and tiering are deterministic inputs to dispatch; nothing reads the
scoreboard. Families: a new seventh family `newswire` (a wire story and its 8-K are not
independent; convergence with `issuer_filings` is stamped, not counted twice).

**c. Backtest:** pre-registered above; the one-month smoke (2026-09) read 16,435 items → 670
candidates (4.1%) → 811 ticker-day rows, and showed one defect fixed BEFORE the registered run: a
six-symbol wire story ("Anthropic signs $35B cloud agreement with Lambda", tagged AMZN/GOOG/…)
attributed $35B to every tagged ticker, putting AMZN in the research tier at 1.2% of cap. Stage A
now requires the headline or summary to name the issuer (a distinctive token of its EDGAR title);
rows failing it are counted as `not_named`. The in-session intraday leg works (CRWD 09-01 15:21 UTC
$2B lifetime contract, measure tier, publish → close +0.67 vs SPY). Full run launched 16:0x UTC
(detached, month checkpoints, ~6 h: 46k news pages, caps per CIK, bars per ticker, minute bars for
in-session research/measure rows). Results in `~/Agentic/data/news_backtest_2026-10-07.txt`.

**d. Volume, latency, spend (projection; the backtest's tier counts replace these):** ~850 items a
day → after the deterministic exclusions and the six-symbol rule roughly 30–40 candidates a day
(4%) → LLM extraction ~$0.05–0.10 a day → research tier (≥ 1% of cap, issuer named) expected low
single digits a day, measure tier similar; against the Class 1 pool of 15 passes this is a
candidate for a cap of 3–4. Latency: poll 60 s against `created_at`; the probe's `updated_at`
equals `created_at` on the wire items (no edit lag); the system's own latency is the poll plus the
extraction call (~5–15 s) plus the research pass (minutes) — the backtest's intraday-vs-gap split
says what that costs. The VST items are the check cases in the report.

### PDUFA CALENDAR BACKTEST — RESULT (2026-10-07; the pre-registered run): criterion NOT MET, no source

454 events (ticker, PDUFA date) on 162 tickers, dates 2019-03..2026-09, mined from 8-K text; labels
approved 134, CRL 61, unlabelled 259; median lead 145 days. Excess vs SPY, points:

| measure | mean | median | hit | n |
|---|---|---|---|---|
| **R1 run-up** (close at announcement → close t−1) | +2.57 | **−3.47** | 45% | 454 |
| R1 clustered (162 tickers) | SE 2.57, t 1.00, cluster-bootstrap CI **[−2.17, +7.72]** | | | |
| R1 by lead: 7–60d / 61–180d / 181–400d | +2.37 / +4.18 / −0.37 | +2.72 / −5.81 / −5.12 | 55% / 43% / 45% | 53 / 262 / 139 |
| decision gap (close t−1 → next open) | −2.64 | −0.10 | 47% | 454 |
| approved: gap / post5 / post20 | +4.43 / −2.21 / −0.68 | +1.10 / −1.56 / −0.35 | 60% / 43% / 49% | 134 |
| CRL: gap / post5 / post20 | **−27.19** / −2.53 / −0.44 | −19.94 / −5.43 / −7.53 | 16% / 41% / 41% | 61 |
| R2 post (all): post5 / post20 | −1.25 / −0.04 | −1.15 / −1.57 | 42% / 44% | 454 / 449 |
| R2 post5 clustered | SE 0.72, t −1.73, CI [−2.68, +0.16] | | | |

Read: the run-up is a right tail (cap < $1B mean +17 on median +1.8; 2019/2020/2025 means +13/+10/+19
against medians near zero) on a 45% hit rate with a CI straddling zero — the pre-registered R1
criterion (mean > 0 with CI excluding 0, hit > 50%) fails on two of three legs. The decision day is
an approval/CRL lottery (+4 vs −27 at the open) that no filing-based or news-based reader sees in
advance, and the post-decision drift is negative. **No PDUFA source**; the AdCom calendar is not
pursued either. Outputs `~/Agentic/data/pdufa_backtest_2026-10-07.*`.

### PRE-REGISTERED NEWS-CATALYST BACKTEST (ruling 2026-10-07 item 1c; registered BEFORE it ran)

- **Source and universe:** Alpaca news (Benzinga), ALL symbols, 2015-01..2026-10 (the full history
  the API serves). **Stage A is deterministic, no LLM** (the live design's LLM extraction is item 1b;
  the backtest proxies it): a candidate is an item whose headline or summary names a company-specific
  dated catalyst (deal, agreement, contract, award, loan, partnership, offtake, acquisition, merger,
  financing, order, supply, license, collaboration, investment, buyback) AND carries a dollar figure
  ≥ $1M; headlines that are analyst actions, options-flow, earnings, dividend, "if you invested" or
  market-wrap pieces are excluded; items tagged with more than six symbols are dropped (wraps).
  **One event per ticker per day**, the largest dollar figure.
- **Relative size:** dollars / point-in-time market cap (SEC `dei` shares × the prior close; ticker →
  CIK from company_tickers.json, so delisted names are "unsized"). Tiers as contracts: ≥ 1% research,
  0.2–1% measure, < 0.2% below_floor (counted, dropped with the reason).
- **Timing and measures (excess vs SPY):** in-session items (09:30–16:00 ET): INTRADAY = first
  1-minute bar at/after publish → that day's close (research and measure tiers), prior close → close,
  the GAP (next open vs that close), next open → close t+1/5/20. Pre-open items: first open that day →
  t+h, gap vs the prior close. Post-close items: next open → t+h, gap vs that day's close.
- **Statistics:** mean/median/hit, ticker-clustered SE, 3,000-draw cluster bootstrap on oc5 and on
  the intraday leg; by tier, in- vs out-of-session, year; net of 15bp stated in the write-up.
- **The decisive number:** research tier, in-session: mean INTRADAY (publish → close) against mean
  GAP against mean next-open → t+5. If the move is intraday, only the real-time path (Class 1, 60-s
  poll, LLM extraction in minutes) can trade it and the next-open row is the lag cost; if it is in
  the gap and the next-open drift, a slower path suffices.
- **Success criterion for a trading path:** research tier next-open → t+5 mean > 0 with the
  cluster-bootstrap CI excluding 0, hit > 50%, n ≥ 150, ≥ 4 calendar years, net of 15bp > 0; the
  intraday leg reported alongside with its own CI. Volume per day by tier and the VST items (10-02
  DOE loan, 10-06 Google deal) reported as check cases.
- Script `~/scratch/news_backtest.py` (droplet), outputs `~/Agentic/data/news_backtest_2026-10-07.*`
  (month checkpoints, restart-safe).

### ITEM 6 (2026-10-07) — 8-K widening and FDA calendar: the VST check, volume, design, and the PRE-REGISTERED backtests (registered here BEFORE either ran)

**The VST 8-K check.** Vistra filed two 8-Ks in the window: 2026-09-24 (accepted 21:13 ET, items
1.01/8.01/9.01) — the closing of a $1.5B junior subordinated notes offering; and 2026-10-05
(accepted Sat 2026-10-03 00:12 ET, items 1.01/2.03/9.01) — an amendment extending a commodity-linked
revolver's maturity. **Neither is the DOE Loan Programs Office loan (Bloomberg 10-02 15:27 ET) nor
the Google/Constellation nuclear deal (10-06), and through 10-06 no 8-K for either exists.** The
move: 10-02 close +0.2% (gap +1.65% into the 15:27 story, faded), 10-05 +3.5%, 10-06 +10.8% (open
+4.5%), SPY +0.7/+0.6%. A widened whitelist (8.01) would have caught nothing: the catalysts were a
newswire story and a press release, not filings. Two filing-side findings instead:
1. **The whitelisted 1.01 filings never reached the funnel.** EDGAR full-text search listed both
   (VST on the first page both days, 88 and 121 hits for "Item 1.01" in the windows); the paper
   unit's 8-K polls ran (journal 09-25 13:30, 10-05 13:30); no Vistra record exists in the audit.
   The pooled dispatch holds low-scored candidates for the next window ("released 3, holding 37"
   on 10-05) and 1.01 ranks below 5.02/1.05 by ruled prior; a candidate still held at the close
   leaves NO record and is never re-listed (the seen ledger). **Measurement gap:** capped and held
   8-K candidates are invisible to the forward engine, so "measurement-first" does not hold for 8-K
   today. Proposed (not built): write `stage_rejection slot_lost` for pooled candidates still held
   at session close — no LLM, one record — so the forward report grades the whole 8-K set.
2. **The lister reads ONE page (100 hits) per item per poll**, relevance-ordered, lookback 1 day: on
   a day with >100 filings carrying an item (8.01 runs 63/day on a 2-day window; 1.01 reached 121
   in the 10-03..10-06 window) filings are silently dropped. Proposed (not built): page `from`
   until the total is reached, or query per calendar day.

**8-K volume, 2026-09-08..2026-10-06 (21 filing days, EDGAR full-text search, item carried in the
filing's own item list):**

| item | filings | per day | with a listed ticker | distinct filers | item-only (± 9.01), tickered | top co-items |
|---|---|---|---|---|---|---|
| 1.01 | 908 | 43 | 651 (72%) | 152 | 141 | 9.01 838, 2.03 290, 7.01 231, 3.02 220, 8.01 165, 5.02 76 |
| 8.01 | 1,331 | 63 | 861 (65%) | 248 | 522 | 9.01 946, 7.01 210, 1.01 163, 3.02 126, 5.02 73 |

Both items: 165. Against the current five-item list (~87–254 candidates/day arriving, cap 6 researched)
the widening adds ~70 tickered filings a day, 8.01 the larger half. **What 8.01 carries** (14 random
8.01-only tickered filings read): dividend declarations (3), a trial-timing update, a topline-result
press release, a litigation note, a proxy/meeting note, an ETF cover-page filing, a nuclear-framework
press release ($120B, BAM), a small M&A agreement, two cover-page-only XBRL stubs. **Dollar figures
appear in 3 of 14 and a counterparty in 2** — 8.01 is a grab bag whose signal, where it exists, lives
in the exhibit 99.1 press release, not the item text. Counterparty / dollar extraction is feasible
for 1.01 (the item text names the agreement and usually the party) and weak for 8.01.

**Design for the widening (report, nothing built):** add 1.01 (already listed) and 8.01 to the
whitelist with a RELATIVE-SIZE stamp as for contracts: dollars extracted from the item text and
exhibit 99.1 (first $ figure within 400 chars of "agreement"/"contract"/"award"/"financing"),
counterparty = the capitalised party after "with"/"between"; tiers dollars / market cap ≥ 1%
research, 0.2–1% measure, < 0.2% or none extracted below_floor (measurement rows). 8.01 without an
extracted dollar figure is below_floor by construction (the dividend/proxy/ETF noise never researches).
Keep the 8-K cap at 6; the pooled slot competition decides; fix the two filing-side findings first or
the measurement rows never exist.

**PRE-REGISTERED 8-K BACKTEST (stage A, no document fetch), registered before it ran:**
- Universe: every 8-K (not 8-K/A) filed 2024-01-02..2026-10-02 carrying item 1.01 or 8.01, filer with
  a listed ticker in EDGAR's display names (the production lister's own source). Groups by item set:
  `1.01` (no 8.01, no 2.02), `8.01` (no 1.01, no 2.02), `both_1.01_8.01`, `earnings_2.02` (any
  filing of the universe carrying 2.02 — the comparison group, not a candidate).
- Timing point-in-time: acceptance time from the filer's submissions JSON; the first tradeable print
  is the file-date open when accepted before 09:30 ET, else the next session's open. oc = that open →
  close t+1/5/20; cc = close(t0) → close; pre5; gap. Excess vs SPY. Caps = SEC shares × close(t0),
  bands < $2B / $2–20B / > $20B. Slices: cap band, with 5.02, with 7.01, by year.
- Statistics: mean/median/hit, ticker-clustered SE, 4,000-draw ticker-cluster bootstrap CI on oc5.
- **Success criterion (rule candidate = `1.01` group, then `8.01`):** oc5 mean > 0 with the
  cluster-bootstrap CI excluding 0, hit > 50%, n ≥ 300, ≥ 2 calendar years, and beats
  `earnings_2.02` at t+5. Stage B (dollar extraction, relative-size tiers on the 1.01 group with cap
  ≥ $1B) is registered as the follow-up only if stage A passes; its criterion is the contracts rule's
  (≥ 1% of cap beats < 1% at t+5, n ≥ 100).
- Script `~/scratch/k8_backtest.py` (droplet), outputs `~/Agentic/data/k8_backtest_2026-10-07.*`.

**FDA / PDUFA calendar — sources and volume.** There is no official PDUFA calendar. Free, official:
the FDA Advisory Committee calendar page (fetchable, HTTP 200, ~31KB, parseable). PDUFA target action
dates are disclosed by the issuers themselves in 8-Ks and press-release exhibits: EDGAR full-text
hits for "PDUFA" + "target action date" per year — 2019 57, 2020 83, 2021 131, 2022 109, 2023 79,
2024 106, 2025 117, 2026-to-date 99 (documents, so a filing and its exhibit count twice; many are
earnings releases restating a known date). Distinct (ticker, date) events ≈ 50–70 a year, ~1–2 a
week. Paid calendars (BiopharmCatalyst, RTTNews) are not needed for the measurement. A live source
would be: EDGAR FTS daily for new PDUFA/goal-date statements (same lister as 8-K) plus the AdCom page
weekly; family `issuer_filings`; measurement-first like contracts.

**PRE-REGISTERED PDUFA BACKTEST, registered before it ran:**
- Events: distinct (ticker, PDUFA date) mined from 8-K documents 2019-01..2026-10 whose text states a
  PDUFA / target action / goal date 7–400 days ahead of the filing; the announcement is the FIRST
  filing naming the date. Outcome label: a follow-up 8-K by the same filer within 7 days after the
  date whose text says "approved" (approved) or "Complete Response Letter" (CRL); else unlabelled.
- Measures (excess vs SPY, point-in-time caps at announcement): **R1 run-up** = close(announcement)
  → close(t−1 before the date); **decision** = close(t−1) → next open / close t+1; **R2 post** =
  next open → close t+5 / t+20, by label. Slices: lead time (7–60, 61–180, 181–400 days), cap band,
  year.
- Statistics: mean/median/hit, ticker-clustered SE and 4,000-draw cluster bootstrap on R1 and R2.
- **Success criterion:** R1 mean > 0 with the cluster-bootstrap CI excluding 0, hit > 50%, n ≥ 150,
  ≥ 4 calendar years; R2 reported by label, no criterion (the decision outcome is not knowable in
  advance; a post-approval drift rule needs the label, which arrives with the move).
- Script `~/scratch/pdufa_backtest.py` (droplet), outputs `~/Agentic/data/pdufa_backtest_2026-10-07.*`.

Both scripts were syntax-checked and the 8-K script smoke-run on a 40-filing random sample for code
correctness only (no numbers read) before this entry was committed; the registered runs launch after.

### RULINGS 2026-10-07 — 2b interval adopted; the self-consistency vote BUILT (replaces boundary confirmation); sampling settings in CLAUDE.md; calibration block BUILT; all pure-decline golden cases graded behaviourally

**1. 2b interval → [+0.15, +1.20], adopted in full.** The deduped backtest is a bug correction (one
event per ticker per digest date), not a re-fit on live data, so pre-registration is not breached.
`UNLOCK_BACKTEST_CI` moved; the weekly `SIZING UNLOCK` line now prints the net MEDIAN beside the net
mean with the backtest's +0.01 stated, so nobody reads the mean as typical.

**2. Self-consistency vote — SHIPPED as designed** (`orchestrator/vote.py`, config
`orchestrator.yaml self_consistency: k 2, margin 10, thresholds [45, 50, 70], reviews true`).
- **Trigger:** a first-pass long or puts, an add verdict, an exit review that would close or trim,
  or ANY verdict (declines included) whose confidence is within 10 of 45/50/70. Confident declines
  (the 72–95 cluster) buy nothing. **Decision:** majority of the three; confidence = median among the
  majority samples, rounded DOWN; the sized report is the majority sample nearest the median with
  its confidence set to the median; no majority, an unfunded or failed sample on a tradeable
  trigger, or an add that does not replicate as an add → `vote_overturned` rejection / hold.
  Reviews: majority action, no majority → HOLD, an incomplete vote on a close/trim → HOLD recorded
  as `vote_incomplete`.
- **Boundary confirmation RETIRED, not run alongside:** `orchestrator/boundary.py` deleted; the
  pipeline's hook, the golden harness's `_confirm` and the CLI all call `run_vote`; the config key
  stays (`enabled: false`) so old configs load; `BoundaryConfirmationSnapshot` stays for old records.
- **(a) Persistence:** `VoteSnapshot` (trigger, k, margin, thresholds, floor, first verdict,
  majority, median, held, failure, extra passes and dollars, and every sample's direction,
  confidence and 16-hex search-transcript hash) on `DecisionRecord.vote`,
  `StageRejectionRecord.vote` and `ThesisReviewRecord.vote`. The hash is SHA-256 of the search-phase
  transcript before elision (`LLMResult.transcript_hash`, `ResearchPass.last_transcript_hash`,
  `ExitReviewPass.last_transcript_hash`).
- **(b) Weekly:** `orchestrator/vote_report.py` — votes triggered, held, overturned (and incomplete),
  by direction (first → majority), by source, by trigger, extra passes and estimated dollars; appended
  under the deployment line. The golden summary line reads "N first-pass reports, M triggered a
  vote -> held, overturned".
- **Budget:** every extra sample is one pass against the daily budget, drawn through
  `try_spend(for_review=True)` (the review reserve, so a vote never starves tomorrow's entries); a
  refusal leaves a tradeable verdict unconfirmed. `research_passes_on` replays `vote.extra_passes`
  so a restart cannot refill them. Mechanism tests run the vote OFF through the harness default
  (`tests/test_orchestrator.orchestrator_config`); the shipped yaml has it ON, pinned in
  `test_accountability2`.
- **(d) Topology:** `orchestrator/vote.py` is in the scoreboard fence (imports only
  `audit.records` and `research.reports`); `tests/test_vote.py` (10) pins the rule.
- **(c) Golden replay with the vote LIVE** (production passes, k=2, 29 cases, $10.66, one run each,
  `~/scratch/gvote/index.log`): **28 PASS / 1 DRIFT; 14 votes triggered → 12 held, 2 overturned.**

| case | samples | vote | grade |
|---|---|---|---|
| form4-intc-cluster | long/68, long/62, long/60 | HELD long/62 | PASS — INTC holds, as expected |
| pelosi-be-calls-decline | long/52, no/72, no/48 | OVERTURNED → no_position | PASS — the minority long dies |
| pelosi-intc-calls-entry | no/38, long/52, long/58 | OVERTURNED → long/55 | PASS (long allowed) — a near-threshold DECLINE that was the minority became a long |
| pelosi-uber-priced-in | long/61, long/52, long/52 | HELD long/52 | **DRIFT** on the traded band [0, 49] — it replicated 3 of 3 today (2 of 5 yesterday) |
| sa-13f-stale-heavy-puts | no/72, no/82, no/72 | HELD no/72 | PASS — the 20–82 case declined three times today |
| moskowitz-amat-max-lag | no/72, no/72, no/35 | HELD no/72 | PASS (behavioural) — the 35 is the noise, outvoted |
| 8 other near-threshold declines (72–80) | all declines | HELD | PASS |
| add-celh ×2 | hold ×3 each | HELD hold | PASS |
| 11 confident declines, 4 reviews | no vote (reviews: single, no close/trim) | – | PASS |

  Read against the ruling's expectation ("the four minority longs die, INTC holds"): INTC holds;
  pelosi-be dies; pelosi-uber did NOT die — it drew long three times in a row today, and a verdict
  that replicates is exactly what the vote is built to let through; pelosi-intc-calls was the
  mirror case, a minority decline outvoted into a long/55. The vote kills what fails to replicate,
  not what was a minority yesterday. The one DRIFT is a floor-band long that replicated; the golden
  case's traded band [0, 49] predates the vote and now grades a replicated long/52 as drift — a
  grading decision for the human, not a code defect.
- **Live round trip:** the replay above IS the production request path — `ResearchPass.run` (screen
  + verification, real API) three times per triggered case through the same `run_vote` the
  pipeline calls; 14 votes ran live. The pipeline-side hook is covered by `tests/test_exits.py`
  (vote on the record, overturned → `vote_overturned`, confident long still votes) and the full
  suite (green on the droplet, 3 skips).

**3. Temperature: no change.** The settings are now recorded in CLAUDE.md § LLM Request-Path
Changes (T=0 on the sonnet report call only; opus verification and every search phase at the API
default; no top_p/top_k/thinking parameters) so they cannot drift silently.

**4. Calibration block — BUILT** (`orchestrator/calibration.py`): cells per source × VOTED-median
confidence band (<45, 45–55, 55–70, 70–85, 85+) × stated horizon × direction, from the funnel
entries (`FunnelEntry.time_horizon / direction / voted_confidence`) and the forward engine's cached
rows; a cell renders only at n ≥ 20 marks at 5d (n, 5d hit, mean 5d and 20d excess), fenced under
`MEASURED RECORD ... evidence to weigh, nothing more`, for the signal's source only, through
`ResearchPass(measured_record=)` → `build_user_prompt(measured_record=)`. Built at startup from
the cached rows (no fetch; the unit is a fresh process each morning), any failure renders nothing.
Fence: imports `forward` (allowed: a measured record) and nothing from attribution, spend, the
audit log or a target module (`tests/test_calibration.py`, 6). **Today it is inert and verified
so:** production log 6,314 funnel entries, 3,620 cached rows, **0 voted entries, 0 cells**; all 23
golden entry prompts byte-identical with the provider wired. The largest single-pass cells (NOT
rendered, by ruling) are congressional 70–85/weeks/no_position n=55 and form_8k 70–85/days n=23;
the first VOTED cell needs 20 voted marks in one cell — at ~2 longs a day, months away. Because
the rendered prompt is byte-identical until then, no golden replay was spent on this step; the
replay is owed when the first cell crosses n ≥ 20 (the prompt changes then), flagged in the
standing reminders.

**5. All pure-decline golden cases graded behaviourally:** ten cases' expect confidence → [0, 100]
(sa-13f, appaloosa, pelosi-intc-may-backfill, case-aapl, trump-nike, trump-micron, trump-clemens,
trump-ford, nolimitgains, uw-tim-cook); directions unchanged, so a long still fails.

**Shipping:** vote bundle (items 1, 2, 3, 5) in one commit, the calibration block (item 4) in the
next; both verified on origin, vps and the droplet.

### RULING 2026-10-06 ON 2a AND 3, ITEMS 2–4 — confidence noise measured five-fold; the self-consistency design; sampling settings; the calibration block was missed

**2a. Every golden case five times, single pass, production passes (model, screen, tiers as
shipped), 145 runs, $28.42, `~/scratch/g5/index.log` on the droplet, `golden5_stats`.**
Single-pass means the boundary second pass was NOT bought — this is the raw first-pass noise.

| case | majority | flips | sd | range | crosses 45/50/70 | verdicts (5 runs) |
|---|---|---|---|---|---|---|
| sa-13f-stale-heavy-puts | no_position | 1/5 | 22.8 | 20–82 | 45, 50, 70 | long/52, no/75, no/20, no/82, no/75 |
| sarissa-amrn-13da | no_position | 0/5 | 16.7 | 28–72 | 45, 50, 70 | no/30, 28, 32, 32, 72 |
| pelosi-be-calls-decline | no_position | 1/5 | 12.9 | 38–72 | 45, 50, 70 | no/72, long/52, no/38, no/72, no/62 |
| pelosi-uber-priced-in | no_position | 2/5 | 10.0 | 30–58 | 45, 50 | no/42, no/38, long/58, no/30, long/52 |
| pelosi-intc-calls-entry | no_position | 2/5 | 8.4 | 57–78 | 70 | no/78, no/72, long/58, no/72, long/57 |
| add-celh-cross-family (synthetic) | hold | 0/5 | 8.2 | 70–92 | – | hold/92, 70, 72, 72, 72 |
| trump-nike-woke | no_position | 0/5 | 6.1 | 72–86 | – | |
| appaloosa-13f-stale | no_position | 0/5 | 5.2 | 72–87 | – | |
| trump-micron-announcement | no_position | 0/5 | 5.2 | 72–85 | – | |
| moskowitz-amat-max-lag | no_position | 0/5 | 4.9 | 72–82 | – | no/82, 72, 72, 82, 82 (yesterday: 18–82 over 6 runs) |
| pelosi-intc-may-backfill | no_position | 0/5 | 4.9 | 72–82 | – | |
| trump-energy-relay-read-10h-later | no_position | 0/5 | 3.4 | 72–82 | – | |
| award-amtm-sole-source-new-1.8pct | no_position | 0/5 | 3.2 | 80–88 | – | |
| award-mck-va-1.1pct-fpds | no_position | 0/5 | 3.1 | 80–88 | – | |
| case-aapl, clemens, uw-tim-cook, ford, venezuela, mirror, nolimitgains, injection | no_position | 0/5 | 0.4–2.8 | within 72–95 | – | |
| form4-intc-cluster | long | 0/5 | 0.8 | 62–64 | – | long/62, 63, 62, 64, 62 |
| taylor-ibp-small | no_position | 0/5 | 0.0 | 72–72 | – | |
| add-celh-same-cluster-repeat-real | hold | 0/5 | 0.0 | 60–60 | – | |
| review-intc-resolved-synthetic | trim | 2/5 | – | – | – | trim, hold, hold, trim, trim |
| review-intc-day3-real | hold | 1/5 | – | – | – | close, hold ×4 |
| review-intc-day9-post-blowout-real | hold | 1/5 | – | – | – | hold ×3, close, hold |
| review-intc-near-stop-synthetic | close | 0/5 | – | – | – | |

Totals: verdict flipped at least once **7 of 29** (4 of 25 entry/add, 3 of 4 reviews); mean
confidence sd 4.6, median range width 8; confidence range crosses 45: 4 of 29, crosses 50: 4,
crosses 70: 4; **crosses at least one live threshold: 5 of 29 (17%)**. Read:
- Noise is concentrated. Twenty cases sit in a tight 72–95 confident-decline cluster and never
  move across anything. The five noisy cases are all Class 2/3 filing cases where the thesis is
  genuinely marginal (Pelosi, Sarissa, the SA 13F) — exactly the ones a floor-band long comes from.
- **Every long outside form4-intc-cluster was a minority verdict**: 1 or 2 of 5 runs. A majority
  vote over three samples kills every one of them; the one stable long (form4-intc, 62–64) survives.
- The noise is itself unstable: AMAT ranged 18–82 over yesterday's six runs and 72–82 today.
  Five runs bound the sd loosely; the share-crossing-a-threshold number is the robust one.
- Exit reviews flip too (trim/hold 2 of 5 on the resolved case; a stray `close` on both real
  cases): any self-consistency rule should cover reviews that would CLOSE or TRIM, not just entries.

**2c. Sampling settings — what is pinned (`config/research.yaml`, `research/client.py`):**
- `sampling.report_temperature: 0.0` applies ONLY to the forced-tool REPORT call and ONLY on
  `claude-sonnet-4-6` (`report_temperature_models`). That covers the screen (sonnet, effort
  medium), the Class 2/3 verification tier and the exit review (all sonnet, medium).
- The Class 1 verification tier is `claude-opus-5`, effort high: NO temperature is sent (opus-5
  400s on any temperature), so it runs at the API default 1.0 with thinking on by default.
- The web-search phase (the tool loop before the forced report call) is never given a temperature
  on any model — default 1.0. Triage (haiku, 200 tokens) sends none. `top_p`/`top_k` are never
  sent anywhere. No `thinking` parameter is sent on any call.
- Consequence shown by the data: the five noisiest cases are all Class 2/3, i.e. screen AND
  verification at T=0 — and still 20–82. T=0 on the report call does not pin a verdict because the
  transcript it conditions on differs every run (different searches, different results) and the
  API's T=0 is not bit-deterministic. Pinning temperature further has no lever left to pull; the
  design below treats the pass as a noisy sample and votes.

**2b. Self-consistency pass — DESIGN, not built (the budget has never bound: 15.2 first passes a
day over the last ten sessions, 2026-09-23..10-06, 152 passes, 21 longs, 131 declines; cap 40).**
- **Trigger:** after the first production pass (screen + verification as today), buy `k` further
  INDEPENDENT full passes (same path, fresh context, no knowledge of the first) when (i) the verdict
  is a long (or an add verdict, or a review that would close or trim), or (ii) the verdict's
  confidence lies within `margin` of a live threshold — the floor 45, the band edge 50, the band top
  70. Confident declines (the 72–95 cluster, 20 of 29 cases) buy nothing.
- **Decision:** direction = majority of the k+1 samples; confidence = median of the samples that
  carry the majority direction; a long sizes only if the majority is long AND the median clears the
  floor. A tie or a majority of errors is a `no_position` (Constraint #6). This SUBSUMES boundary
  confirmation: today's second pass is k=1 on [45,70) with "the lower sizes"; the new rule is a
  wider trigger with a vote. The boundary code path is kept and the vote replaces it in one place.
- **Proposed k = 2, margin = 10** (three samples; trigger band on confidence [35, 80) for declines).
  Why k=2: a case that goes long 2 of 5 times is majority-long with 3 samples 35% of the time and
  with 5 samples 32% — five samples buy almost nothing over three, at twice the cost. Why 10: the
  five noisy cases' ranges are 20–40 points wide; a margin of 5 misses declines at 60 that would
  have been 52 on the next draw (pelosi-be: 62, 72, 72 and long/52).
- **Projected spend, last ten sessions' distribution** (`verdicts10`): candidates = 21 longs + 17
  declines within 10 of a threshold = 38 over ten sessions.

| setting | extra passes/day | total passes/day vs cap 40 | extra $/day at $0.13–0.34 a pass |
|---|---|---|---|
| k=2, margin 5 | 6.4 | 21.6 | ~$1–2 |
| **k=2, margin 10** | **7.6** | **22.8** | **~$1–2.5** |
| k=4, margin 5 | 12.8 | 28.0 | ~$2–4 |
| k=4, margin 10 | 15.2 | 30.4 | ~$2–5 |

  The 6 boundary second passes already bought in those sessions are inside the k=2 figure, not on
  top of it. Reviews that would close or trim add ~0.5/day at today's position count.
- **What it would have done to the golden longs:** pelosi-be, pelosi-uber, pelosi-intc-calls and
  sa-13f go to no_position by vote (each long was 1–2 of 5); form4-intc stays long at ~62. Fewer
  trades, and the ones left replicate — the direction Constraint #6 wants. It cannot manufacture a
  long the first pass did not find; it only confirms or kills.
- **Topology:** the vote reads only the samples; it imports nothing from attribution, spend or any
  target (the scoreboard test extends to it). The prompt is unchanged — no golden replay needed for
  the vote itself; the live round trip is owed because the request path gains calls.
- **Golden grading under the vote:** the replay buys the same vote (as it buys the boundary pass
  today) and grades on what would size.

**3. Calibration block — MISSED, not deferred.** The adaptive-standards ruling approved it ("a
fenced MEASURED RECORD block per source × confidence band × horizon class, rendered at n ≥ 20,
framed as evidence"). The step-2 build entry shipped the hurdle, the demotions and the topology
tests and does not mention the block at all; nothing was recorded as deferred. The first time its
absence was written down was the post-ship item 3 ("never built"). Queued after item 2, behind the
self-consistency decision, as ruled: a measured record per band is meaningless until the bands'
noise is known — and today's table says a decline's band carries no information at all.

**4. AMAT graded behaviourally — applied.** `moskowitz-amat-max-lag` expect confidence [50, 100] →
[0, 100] with the ruling in its note; direction stays `no_position` only, so a long still fails.
**Flagged, not applied:** ten other pure-decline cases still carry a lower band on the decline —
sa-13f [60,100] (today drew no_position/20 → DRIFT), appaloosa [50,100], pelosi-intc-may-backfill
[50,100], case-aapl [60,100], trump-nike [60,100], trump-micron [50,100], trump-clemens [70,100],
trump-ford [40,100], nolimitgains [50,100], uw-tim-cook [50,100]. CLAUDE.md already says decline
cases grade behaviourally; these bands predate that sentence. One word applies the same change.

**5. Incumbent stamp:** limitation accepted; FPDS prior-award lookup noted, not queued.

### RULING 2026-10-06 ON 2a, ITEM 1 — the OSK double count, deduped; 2a restated on 170 events

**Correction first.** The post-ship 2a report called OSK 2020-03-27 "one award parsed from two
paragraphs". The digest shows FOUR distinct Oshkosh orders that day (W56HZV-20-F-0035 $173.8M,
-0149 $100.9M, -0150 $46.1M, -0177 $25.7M), two of them above 1% of cap. The parser was right; the
backtest counted one forward return twice because two awards on one ticker-day are one observation.
The ruled key (awardee + digest date + contract number, or dollar figure) would not have caught it,
and keyed on the contract number alone it would merge distinct orders against one parent (the
parser's number is the last in the paragraph, usually the parent basic ordering agreement: Boeing
2019-09-30 took two orders against N00019-16-G-0001, $17.6M and $15.5M). Two layers shipped:

1. **Parser dedup, true duplicates only:** key = awardee + contract number + dollar figure (awardee +
   dollar figure where no number parses). Across the 1,899 digests the ruled key as written matched
   126 pairs; nearly all are distinct orders against a shared parent. A genuine repeat exists (HII
   2019-09-24: the USS Columbus $20M modification listed twice, once with an "(Awarded Sept. 23,
   2019)" trailer) and is now one award. Indices stay contiguous, so external ids are unchanged.
2. **One event per ticker per digest date, both paths.** `collapse_same_ticker` in
   `signals/contracts.py`: the LARGEST single award is the primary and decides the tier
   (Constraint #6 — tiering on the day's total would admit more); the count and the day's total are
   stamped (`same-day awards: N (this is the largest; the day's total is $X, Y% of market cap)`,
   fields `same_day_awards`, `same_day_total`, `rel_mcap_same_day`), the siblings are marked seen
   with the primary so a re-read never re-emits them. DoD digest and FPDS (within a poll batch, by
   signing date). `GovAwardFacts.same_day_awards` parses it back; the weekly slices "several awards
   on one day". The backtest folds the same way (`_collapse_same_ticker_day`) and reports the fold.
   Regression test on the verbatim OSK/HII/Boeing paragraphs, parser, live path and backtest
   (`tests/test_contracts.py`, 3 new; 16 pass; full suite green on the droplet).

**2a rerun on the deduped set** (`contracts_backtest_2026-10-06c`, 1,899 digests, 507 same-ticker-day
awards folded across all mapped events; rule group 174 → 170, 46 tickers, `rule_stats`):

| statistic | before (174) | deduped (170) |
|---|---|---|
| mean / median next-open → t+5 | +0.74 / +0.27 | **+0.65 / +0.16** |
| hit rate | 54% | 53% |
| ticker-clustered SE (46 clusters), t | 0.31, 2.41 | 0.26, 2.49 |
| bootstrap 95% CI, ticker-cluster (10,000 draws) | [+0.17, +1.37] | **[+0.15, +1.20]**, P(mean ≤ 0) = 0.004 |
| bootstrap 95% CI, event resampling | [+0.22, +1.27] | [+0.13, +1.15] |
| mean excluding top 5 | +0.39 (169) | +0.34 (165) |
| net of 15bp: mean / median / hit | +0.59 / +0.12 / 51% | **+0.50 / +0.01 / 50%** |
| recompete language | 7 of 174, ex −0.07 → +0.77 | 7 of 170, ex +0.68 |

The top five are now OSK 2020-03-27 once, MRNA 2022-07-29, OSK 2020-04-24, KTOS 2021-03-25 and BA
2020-09-24. Net of cost the median event is now flat. Split by day shape: rule events on
single-award days +0.58 (n=145), on multi-award days +1.05 (n=25). Tiering on the day's TOTAL
instead of the largest award would add 7 events (mean +0.83) — reported, not adopted.

**2b:** the lower bound moved +0.17 → +0.15, a 0.02 move, under the ruling's 0.05 trigger, so the
pre-registered interval [+0.17, +1.37] STANDS and the weekly `SIZING UNLOCK` line is unchanged. Noted
for the human: the upper bound moved more (1.37 → 1.20); the trigger as ruled reads the lower bound
only. The success criterion is still met by letter (rule +0.65 vs ii −0.03, iii −0.15, 8 years).

Not fixed, noted: one awardee string parses as "lot 2)" (a Navy multiple-award paragraph with two
lot numbers in parentheses, 2020-02-18) — unresolved, harmless, a parser nit for later.

### POST-SHIP RULINGS 2026-10-06 — multiplier retired; backtest accepted as modest; the go-live criterion re-registered

**1. Multiplier:** Constraint #6 governs permanently; the 1.3× deployment multiplier is RETIRED. The
weekly 0–1% target is a reporting line only and may never feed a threshold, prompt or sizing input.
Recorded in CLAUDE.md beside the constraint.

**2a. The rule group's next-open → t+5 mean, properly (174 events, 46 tickers, `rule_stats`):**

| statistic | value |
|---|---|
| mean / median | **+0.74 / +0.27** points |
| naive SE / **ticker-clustered SE** (46 clusters, LMT 22, OSK 14, SAIC 13, HII 12) | 0.27 / **0.31**, clustered t = +2.41, normal CI [+0.14, +1.34] |
| 10,000-draw bootstrap 95% CI, event resampling | [+0.22, +1.27], P(mean ≤ 0) = 0.003 |
| **10,000-draw bootstrap 95% CI, ticker-cluster resampling** | **[+0.17, +1.37]**, P(mean ≤ 0) = 0.005 |
| mean excluding the top 5 events (OSK 2020-03-27 +15.3 ×2 — one award parsed from two paragraphs, MRNA 2022-07-29 +12.5, OSK 2020-04-24 +9.9, KTOS 2021-03-25 +8.7) | **+0.39** (n=169) |
| mean / median / hit **net of a 15bp round trip** | **+0.59 / +0.12 / 51%** |
| ticker-weighted mean, gross / net | +1.01 / +0.86 |

Read: significant at the cluster level, small, and a quarter of it is five events; net of cost the
median event earns twelve basis points over SPY in a week.

**2b. GO-LIVE CRITERION, PRE-REGISTERED NOW (replaces the backtest criterion):** sizing unlocks only
when LIVE rule events (new, single awardee, ≥1% of point-in-time cap, not ceiling-suspect) reach
**n ≥ 25** AND the pooled live mean next-open → t+5 excess **net of 15bp is ≥ +0.40** AND the live
mean (gross) sits **inside the backtest ticker-cluster bootstrap CI [+0.17, +1.37]**. Until then:
measurement-only with research passes as built. The weekly's award section prints the three
conditions against the live rows every Friday (`SIZING UNLOCK` line); meeting them is evidence for a
human ruling, never an automatic unlock. Live events accrue from 2026-10-07; at ~1 rule event a
week the earliest n = 25 is ~2027-04.

**2c.** Competed vs sole-source is NOT a filter (found after the fact). It is stamped (`sole source:`
line) and measured live like every other determinant.

**2d. Recompete / incumbent-retained:** the digest almost never says it — the only language the
174 rule paragraphs carry is "bridge contract" (7 events, all SAIC facilities-maintenance bridges
2019–2020; oc5 mean −0.07, hit 43%). **Rule mean excluding them: +0.77 / median +0.33 / hit 54%
(n=167).** Stamped from now on: `recompete: yes` when the paragraph or the FPDS action carries
recompete / follow-on / incumbent / bridge / continuation / renewal language, `unstated` otherwise;
reported as its own slice, no filter. **Limit stated:** McKesson's VA award is the prime-vendor
recompete the ruling suspects, and neither feed SAYS so — the honest stamp needs a prior-award
lookup (same vendor, same contracting office, same product code, within ~5 years, on FPDS), a
deterministic recipe for a later build; the text stamp will under-count.

**3. Golden drifts — NONDETERMINISTIC, both.** The two cases do NOT carry the calibration block (it
was item 3 of the adaptive-standards design and was never built), so the ruling's rerun branch
applied: three runs each, production path, ~$1.10.

| case | history before 2026-10-06 | today's replay | rerun 1 | rerun 2 | rerun 3 |
|---|---|---|---|---|---|
| `pelosi-uber-priced-in` (expects: decline, or a long that does not trade) | no_position/62 · no_position/74 · **long/58 DRIFT (09-17)** | long/62, boundary UPHELD → sizes → DRIFT | long/58, boundary second pass no_position/72 → REVERSED → PASS | no_position/72 → PASS | long/52, boundary no_position/72 → REVERSED → PASS |
| `moskowitz-amat-max-lag` (expects: no_position in [50, 100]) | no_position/82 · **no_position/18 DRIFT** · no_position/82 | no_position/28 → DRIFT | no_position/72 → PASS | no_position/30 → DRIFT | no_position/35 → DRIFT |

Reading: the first-pass verdict on the Pelosi/Uber case flips between a floor-band long and a
decline from run to run — exactly the stochastic band the boundary confirmation was built for
(diagnosed 2026-09-02), and the second pass caught all three longs today. The Moskowitz/AMAT case
never flips direction; its CONFIDENCE swings 18–82 on an identical prompt. Both flip, so per the
ruling they are marked nondeterministic and no leakage investigation is owed. **Proposal for the
human (no change made):** grade `moskowitz-amat-max-lag` behaviourally like the other declines (a
decline at any confidence passes) — the golden set's own 2026-09-17 convention says a decline's
confidence is recorded, not graded; a [50, 100] band on a decline is grading noise. Leave
`pelosi-uber-priced-in` as it is: it passes whenever the boundary pass does its job and drifts only
when two stochastic longs coincide, which is the event the pass exists to measure.

**4.** No DOE Loan Programs Office source. The VST lesson folds into step 4's 8-K widening: Items
1.01 and 8.01 with counterparty and dollar extraction and relative size on the contract tiers; the
volume report checks whether VST filed an 8-K for the Google deal or the DOE loan, the filing time
against the move, and what the widened list would have done.

**5.** Evening timer and Chromium libraries stay uninstalled; the morning fetcher suffices while
measurement-only; re-raise the timer when 2b unlocks sizing.

### CONTRACT AWARDS BACKTEST — the pre-registered run (2026-10-06, 1,899 digests 2019-01-02..2026-10-02)

Run through the production parser on the Wayback harvest (`data/contracts_backtest_2026-10-06.{json,log,events.jsonl}`
on the droplet). 31,202 awards parsed, 12,316 ≥ the $50M parser floor, **3,669 mapped to a US-listed
parent (30%)**, 3,234 with a point-in-time cap (SEC shares × that day's close), 3,660 with bars.
Resolution by year is flat at 26–36%: of ≥$50M awards, ~15% are small-business (`*`), ~10% known
non-public, ~40% unresolved — construction (RQ, Korte, Nan, BL Harbert, Harper), logistics (Farrell,
APL, Liberty Global, National Air Cargo), private services (DCS, SLSCO, Radiance, American Systems,
M.C. Dean) and JVs (Bell-Boeing, DZSP 21, NAS, BFBC). The three public names in that list (US Foods,
ECS Federal → ASGN, and the digest's recurring "Northrup Grumman") were added to the map after the
run; a re-run with them (`contracts_backtest_2026-10-06b.*`) maps 3,728 events and leaves the rule
group byte-identical (174 events, +0.74 / +0.27 / 54%); groups (ii) and (iii) move by a hundredth.

**Pre-registered groups, excess vs SPY in points, next-open → close (the tradeable path) with
close → close beside it:**

| group | events / tickers | pre t-5→t0 | gap | t+1 oc | t+5 oc (cc) | t+20 oc | oc5 hit | ticker-wtd oc5 |
|---|---|---|---|---|---|---|---|---|
| **(i) RULE: new, single, ≥1% of cap** | **174 / 46, 8 years** | −0.07 | +0.22 (61%) | +0.42 (56%) | **+0.74 med +0.27 (cc +0.97)** | +0.11 | **54%** | **+1.01** |
| (ii) modifications / options | 1,168 / 48 | −0.28 | +0.04 | −0.03 | −0.03 (cc +0.01) | −0.11 | 49% | +0.49 |
| (iii) new single < 1% of cap | 604 / 41 | −0.02 | +0.09 | −0.01 | −0.15 (cc −0.06) | −0.55 | 50% | −0.01 |
| (iv) multi-award / IDIQ / ceiling stated | 1,594 / 130 | −0.22 | +0.05 | −0.07 | +0.06 (cc +0.12) | −0.12 | 48% | +0.31 |
| unsized (no point-in-time cap) | 129 / 18 | +0.46 | +0.03 | −0.14 | +0.63 | +0.58 | 46% | +0.17 |

**The pre-registered success criterion is MET by its letter:** rule group next-open→t+5 positive
(+0.74; SE 3.54/√174 ≈ 0.27, ~2.8 SE above zero), hit 54% (> 50%), n = 174 (≥ 100), eight calendar
years (≥ 4), and it beats (ii) −0.03 and (iii) −0.15 at the same horizon (difference vs (ii) +0.77,
~2.7 SE). **What the letter does not say and the ruling should weigh:** the edge is modest — a quarter
of a point at the median, three-quarters at the mean — and it is a one-week effect: t+20 is +0.11
(hit 50%). Year by year (oc t+5): 2019 +0.26 / 2020 +1.71 / 2021 +1.07 / 2022 +0.60 / **2023 −0.45
(hit 35%)** / 2024 +0.74 / 2025 +0.97 (hit 65%) / 2026 +0.23 (median −0.95) — positive in seven of
eight years, 16–30 events a year. No pre-announcement leakage (−0.07) and only a small overnight gap
(+0.22), so the digest is not structurally late. The out-of-sample read is weaker than the 2026
in-sample read that motivated the source (+2.3 at five days on 16 events): that sample was the top of
a noisy distribution, as pre-registration exists to show.

**Slices inside the rule group (reported, not used to pick a rule):** competed/unstated **+0.98
(hit 56%, n=119)** vs sole-source language +0.22 (49%, n=55); Army +1.03 (n=80), Air Force +0.64
(n=38), DLA +1.24 (n=10), Navy +0.21 (47%, n=34); awardee cap small +1.23 (median +0.03, n=40), mid
+0.41 (n=80), mega +0.86 (n=54); relative size 1–2% +0.71 (n=79), 2–5% +0.67 (n=52), 5–20% +0.50
(hit 58%, n=33), ≥20% +2.11 (hit 80%, n=10 — the band the ceiling audit says to distrust).

**Ceiling audit (ruling 1d):** the largest award/cap ratios are shared-pool ceilings reported per
awardee — TLS $12.5B at 61× its cap, WKC and GEO on a $55B pool, CXW / VVX / KBR / AMTM on a $45B
pool, PAE / VEC / FLR on LOGCAP-style $6.4–14B vehicles, the TRANSCOM $4.2B pool on JBLU at 2.7×.
Group (iv) absorbs them when the paragraph carries IDIQ / multiple-award / ceiling language; a few
pool ceilings whose paragraphs say none of that land in the rule group's ≥20% band (n=10, +2.11).
Excluding that band leaves the rule group at ~+0.66 on 164 events: the finding does not rest on it.
**Hudson Technologies (2026-08-05, $0.21B at 96% of cap):** a ten-year DLA IDIQ for refrigerants —
a ceiling, correctly in group (iv); HDSN fell 9% the next day. **Rule for the live feed:** any award
above 100% of the awardee's point-in-time cap is tagged ceiling-suspect and never researches.

**Reading for the 2026-10-15 review:** the category average is zero, as the literature says; the
pre-registered subtype is positive and consistent but small and short-lived. Measurement-only is the
right state: the live rows accrue under the same rule with the open split, and the trading-path
ruling should ask for a per-year hit rate above 50% on the live flow before the first sized order,
with competed new awards to small and mid caps as the slice to watch.

### SHIPPING ORDER STEP 3 — the config bundle (built 2026-10-06; approved as config the same day)

- `risk_limits.yaml sizing`: floor 50 → **45**; bands **45–55 at 1%** (lower-inclusive; exactly 55 →
  1%, Constraint #6), 55–70 2%, 70–85 5%, 85+ 10%; hard cap 0.10 unchanged. Measured before
  shipping: zero long verdicts below 50 in the last ten sessions — this is the door, not the shots.
- `risk_limits.yaml equity_sleeve`: `max_daily_deployment` 0.25 → **0.35**, `max_sector_exposure`
  0.25 → **0.30**. Neither cap has bound an order since inception. **Consequence stated:** the sweep's
  liquidity buffer is both sleeves' daily caps plus the margin, so it rises 18,541 → ~24,000 on
  today's NAV (judged 0.35 × 55% = 19,250 + mechanical 2,250 + 2,500); ~5,500 less sits in SGOV and
  ~5,500 more in cash, by construction. The baseline sleeve's funding math moved with it (tests
  re-pinned: 512 → 458 SGOV on the first build, 811 → 756 parked lots, unsweep 299 → 298 shares).
- `orchestrator.yaml boundary_confirmation.band_width` 20 → **25**, so the band stays [45, 70) and
  every new admission still buys the second agreeing pass. `exits.fast_class_leash_bounds.weeks.floor`
  7 → **5** (Class 1 only; Class 2/3 untouched).
- **Health and weekly say deployment is verdict-limited** (`orchestrator.ops.deployment_line`,
  `verdict_funnel`): "judged deployment: N positions, X% of the judged sleeve — VERDICT-LIMITED (last
  10 sessions: P research passes, L long verdicts, A approved; long fates: …); caps, budget, sizing
  and the floor did not bind". Health renders it after the deployed-today line; the weekly right
  under the headline alpha line. Judged arm only; reads verdict records, never P&L.
- **The 1.3× deployment multiplier is NOT built** — see step 2: it conflicts with the
  adaptive-standards constraint as written; the human picks which ruling governs.
- Tests re-pinned to the ruled numbers: sizing bands and boundaries, recalibration caps, the risk
  gate's band test and the mechanical daily-budget split (four names at 8.75% each — a third of the
  0.35 cap would breach the 10% single-position cap), the boundary stamp (45, 20 — the harness keeps
  band 20), the fast-class leash floor, and the sweep/baseline buffer arithmetic above.

### SHIPPING ORDER STEP 2 — CLAUDE.md constraints, topology tests, the reward hurdle, demotions (built 2026-10-06)

- **CLAUDE.md § "Standards Move With the Opportunity Set, Never With the Scoreboard":** the two
  standing constraints as ruled. **Conflict surfaced, not resolved (Constraint #6):** the WEEKLY
  TARGET ruling's 1.3× deployment multiplier is a sizing input that is a function of distance from
  a return target, which the ADAPTIVE STANDARDS ruling's constraint 4 forbids in so many words. The
  two rulings landed the same day; the later one is the stricter and the multiplier can only ever
  enlarge a position, so the smaller-position reading wins until a human says which governs. **The
  multiplier is NOT built**; CLAUDE.md says so. (It was also measured inert: four 2%-band entries
  in ten sessions.) The health and weekly lines that state "deployment is verdict-limited" ship
  with step 3 regardless.
- **`tests/test_scoreboard_constraint.py`:** AST import fence — `orchestrator/hurdle.py`,
  `orchestrator/scalars.py`, (`orchestrator/deployment.py` if it ever exists), the research /
  review / triage prompt builders import nothing from `audit.attribution`, `audit.spend`,
  `audit.log`, `forward` or any target module; the hurdle's `Opportunity` carries exactly four
  fields (deployed fraction, positive candidates, open positions, target positions); the prompt
  sources never contain "weekly target", "shortfall", "behind target", "deployment multiplier",
  "realized P&L", "4-week return", "beta-adjusted return", "weeks without a trade"; CLAUDE.md states
  the constraint.
- **The reward hurdle (`orchestrator/hurdle.py`, wired in `pipeline._reward_risk_reason`):**
  annualized expected return `(target − entry)/entry × 365/days` must clear
  `base × (1 + k × u)` with base 0.40, k 0.5; the absolute reward:risk floor is 0.8 (was the flat
  1.3). Days: the report's `expected_resolution_date` clamped into the leash bounds for the
  horizon and class (`ExitsConfig.leash_bounds_for`), the horizon's time-stop fallback when the
  report named none. `u = max(judged deployed / judged sleeve NAV, min(1, positive-scored
  candidates in today's queue / (target_positions − open positions)))`, computed by the loop each
  tick from the gate's exposure and the dispatch scores and handed to the pipeline through
  `set_opportunity` — nothing about P&L or the calendar crosses that call. Today u ≈ 0.07 → the
  hurdle sits at ~41%. Config `orchestrator.yaml reward_risk` (min_ratio 0.8, annualized_hurdle
  0.40, opportunity_cost_k 0.5, target_positions 20); `annualized_hurdle: null` would restore the
  flat test exactly. Tests: `tests/test_hurdle.py` (9: the fast-vs-slow 2% move, the floor's veto,
  the cap at base × 1.5, the day clamps, the live config numbers, the pipeline's two-part test, the
  unchanged flat test, the bar moving with the opportunity set).
- **Demotions (`signals.yaml`):** congressional `daily_research_cap` 5 → 1; Form 4 5 → 2; 8-K stays
  6. Accepted consequence recorded: judged entries near zero until an event source earns in.
- No prompt changed in this step: no golden replay owed beyond step 1's.

### SHIPPING ORDER STEP 1 — government contract awards source, measurement-first (built 2026-10-06)

**Built (`gov_contract_awards`, Class 1, probation = measurement-first):**
- `signals/contracts.py`: ONE parser for the DoD daily digest (site/Wayback HTML and the reader
  proxy's markdown — paragraph → awardee(s), amount, ceiling language, kind new / modification /
  option / ceiling_increase, multiple-award and IDIQ flags, sole-source language, offers received,
  completion → term months, agency header, contract number, small-business `*`); the contractor
  map (`config/contractors.yaml`, ~440 human-edited prefixes with point-in-time renames `until` /
  `successor` / `from`, CIKs for delisted tickers, `null` for known private / JV / foreign /
  nonprofit / government-owned; EDGAR exact-name fallback); live sizing (Finnhub `profile2` cap,
  SEC companyfacts trailing-FY revenue); `DodDigestFetcher` (RSS → proxy → parse → resolve → size →
  one RawItem per award, drains the evening pending file first, self-throttles 15 min);
  `FpdsCivilianFetcher` (public ATOM, same-day civilian actions, DoD excluded — withheld 90 days
  there); `CombinedAwardsFetcher` behind the router. Tiers are RELATIVE: research = new + single
  awardee + award/cap ≥ 1%; measure (0.2–1%, or any modification / multi-award / unsized) and
  below_floor (<0.2%, the control) are measurement-only rows with their own codes
  (`award_measurement`, `award_below_floor`); unmapped / small-business / non-public awardees die as
  `no_instrument` with the resolution reason in the content. Every row carries the determinants as
  labelled lines the funnel parses back (`audit.records.snapshot_gov_award` → `FunnelEntry.gov_award`).
- Loop: a source that names its own `measurement_code` keeps it. Registry: family
  `government_awards`; the two codes are measurement codes (outside convergence). Scoring facet
  `tier|kind|mcap` with ruled priors (in-sample defaults, flagged ungrounded). Pooled dispatch.
- Forward engine OPEN SPLIT (ruling 1c): every row now carries the next-session open after the
  observation date and per-mark `open_return_pct` / `open_excess_pct`; rows computed before the
  split recompute once (`open_checked`). Weekly: "Government contract awards by subtype" — tier,
  kind, single vs multi/IDIQ, ceiling stated, sole-source, military/civilian, feed, award/cap band,
  awardee cap band, the PRE-REGISTERED RULE slice — each at 1/5/20/60d with close→close beside
  next-open→close, plus the pre-drift where the cache can say it and the research verdict codes.
- Research prompt: an award branch (buyer's announcement; timing per feed; the determinants are
  data; weigh against size and backlog, not headline dollars; priced_in MANDATORY; decline for
  demonstrated movement, never elapsed time). Two golden cases from the first live day (AMTM
  digest research-tier; MCK FPDS research-tier).
- Evening one-shot `python -m orchestrator contracts-evening` (+ `ops/vps/agentic-contracts.{service,timer}`,
  17:05 ET, install needs root): reads the fresh digest, writes the free rows the same evening,
  stashes research-tier awards for the open. The morning fetcher is self-sufficient without it.
- Backtest `python -m orchestrator contracts-backtest --digests DIR [--out report.json]`: the
  pre-registered groups, point-in-time caps (SEC shares on/before the day × that day's close),
  open split, pre-drift, gap, resolution rate by year, ceiling audit, top unresolved names.
- Tests: `tests/test_contracts.py` (13), pins updated in test_dispatch_scoring / test_prefilter /
  test_quiver / test_x; suite green on the droplet (scratch clone, scratch source first on the path —
  the venv's editable install points at the production checkout, so `python -m orchestrator` in a
  scratch clone needs `PYTHONPATH=<scratch>/src`; recorded here because the first golden launch
  silently ran the production code).

**Live validation (2026-10-06, droplet, production stack, no LLM):** RSS 30 digests; Oct 1 digest
54 awards → 33 items: research 1 (Amentum $79M sole-source task order, 1.77% of a $4.46B cap, 0.55%
of revenue), measure 8 (the $4.22B TRANSCOM multi-award IDIQ pool, one row per carrier — JBLU at
275% of cap is exactly the ceiling case the audit flags), below_floor 2, no_instrument 22 (20
unresolved, 2 non-public); FPDS live: 22 actions, 16 DoD excluded, research 1 (McKesson $1.16B VA
award, 1.09% of cap), measure 1 (SAIC State Dept modification). Finnhub and SEC lookups live.
**Live round trip (production ResearchPass, AMTM golden case, $0.37):** prompt rendered the award
frame and the 112h age; verdict `no_position/88`, horizon days, priced_in measured from real prints
(−1.84% first session, ~−1.3% net; "the 1.77% ratio is inflated by a market cap down ~37% since
September 2024; $33.8M/yr against $14.4B revenue is immaterial") — the model used the frame exactly
as intended and declined honestly. Golden grade PASS.
**Golden replay (29 cases, production path, ~$7):** **27 PASS, 0 FAIL, 2 DRIFT** — both drifts
are congressional cases the award prompt branch never touches, recorded for human review as the
ritual requires: `pelosi-uber-priced-in` long/62 (boundary second pass long/62, UPHELD) where the
case expects a decline, and `moskowitz-amat-max-lag` no_position/28 below the case's confidence
band. The two award cases pass (AMTM no_position/90, MCK no_position/90). Operational note: the
whole-set `golden` invocation hung idle (3 s CPU in 36 min, no sockets) in the scratch clone and
was killed; the per-case loop the 2026-09-18 launcher used ran clean, four workers in parallel.
**Headless Chromium (ruling 1a):** installed on the droplet without root; `chromium-headless-shell`
fails to LAUNCH for missing system libraries (`playwright install-deps` needs root) — it never
reached Akamai. Staying on the proxy as ruled; the test can be repeated after a root install.
**Partial backtest (588 digests 2019-01-02..2021-05-10, harvest still running):** rule group 62
events / 25 tickers: next-open→t+5 **+1.32 mean, +0.90 median, hit 61%**, ticker-weighted +1.41;
t+1 +0.55; t+20 +1.45 (54%); pre-drift +0.27 (no leakage), gap +0.11. Modifications −0.21 (t+5),
new-single-below-1% −0.36, multi/IDIQ/ceiling +0.16. By year 2019 +0.26 / 2020 +1.71 / 2021 +2.08;
small caps +3.37 (n=16); competed/unstated +2.28 vs sole-source −0.01; Army +3.00, Navy −0.02.
Resolution 28–36% of ≥$50M awards (the rest: 15% small-business, 10% known non-public, ~38%
unresolved — construction JVs, private contractors, a Bell-Boeing JV). Point-in-time cap resolved
for 993 of 1,127 mapped events. Not the criterion yet (n<100, <4 years); the full run follows.

### RULINGS ON THE FIVE REPORTS (2026-10-06) — accepted; build order; backtest PRE-REGISTRATION; the VST finding

**Ruled (human, 2026-10-06, after the reports below):** verdicts are the binding constraint; contract
awards is the bet; everything else is plumbing and is never reported as deployment.
1. Contract awards APPROVED with amendments: (a) reader proxy primary, Wayback history, one parser;
   test plain headless Chromium from the droplet (no stealth/fingerprint spoofing), stay on the proxy
   if it fails; (b) historical backtest in parallel, rule pre-registered here before running, through
   the same parser on Wayback digests back to 2019 or as far as the map supports — the 2026 slices
   were found on the data they are judged on; (c) every forward return split at the open: next-open →
   t+1/t+5/t+20 close as the tradeable numbers beside close-to-close; (d) ceiling audit: IDIQ,
   multiple-award and ceiling-value awards flagged apart; check Hudson Technologies (96% of cap) for a
   ceiling/mapping error; (e) find the VST ~$4B 16% mover; (f) everything else as proposed; (g) backtest
   market cap POINT-IN-TIME (SEC companyfacts shares outstanding × close on the award day), extend the
   parent map for historical awardees, report resolution rate by year.
2. ADAPTIVE STANDARDS approved as designed (40% annualized hurdle, absolute R:R floor 0.8, horizon from
   the resolution date clamped to leash bounds, base × (1 + 0.5 × u), calibration block at n ≥ 20, both
   CLAUDE.md constraints with topology tests).
3. Floor 45, boundary band [45,70), weeks leash floor 5, 1.3× multiplier, caps 35%/30%: approved as
   config; weekly and health must state plainly that deployment is verdict-limited.
4. Demotions now: congressional cap 5 → 1, Form 4 5 → 2, 8-K stays 6. Accepted consequence: judged
   entries near zero until an event source earns its way in.
5. Momentum adds DEFERRED (they only apply to the demoted source).
Shipping order: (1) parser, mapping table, live measurement-only feed + evening timer, FPDS civilian
feed, Wayback backtest with open split and ceiling audit — in parallel; (2) CLAUDE.md constraints,
topology tests, hurdle, demotions; (3) the config bundle; (4) 8-K widening and FDA calendar with their
own volume reports and pre-registered backtests. Golden replay + live round trip on every prompt-
touching step. Backtest and VST finding reported before 2026-10-15.

**BACKTEST PRE-REGISTRATION (written before any historical run; the rule may not move after the data
is seen):**
- Universe: every award in every DoD daily digest archived by the Wayback Machine, 2019-01-01 → 2026-10-02,
  parsed by the production parser (`signals/contracts.py`), one row per award paragraph, duplicate
  digests for one day collapsed to the fuller one.
- Rule under test: **new award (not a modification, option exercise or ceiling increase) AND single
  awardee (no "multiple award"/IDIQ pool language, one named awardee) AND award value ≥ 1% of the
  awardee's point-in-time market cap AND awardee resolves to a public US-listed parent through the
  mapping table.** Small-business (`*`) awardees, JVs, unresolved names: excluded (reported as the
  resolution rate by year, not graded).
- Market cap: SEC companyfacts `dei:EntityCommonStockSharesOutstanding` (latest value on or before the
  award date) × the awardee's close on the digest day. No current-cap lookups.
- Returns: event day t0 = digest publication day (17:00 ET, after the close). Tradeable path: **next
  session OPEN → t+1 close, t+5 close, t+20 close**, excess over SPY on the same path; alongside close
  (t0) → close (t+1/t+5/t+20) and pre-drift close(t−5) → close(t0). Marks absent if no bar within 4
  calendar days (the forward engine's rule).
- Comparison groups fixed in advance: (i) rule-passing events; (ii) modifications/options to the same
  parents; (iii) new single awards < 1% of cap; (iv) multiple-award / IDIQ / ceiling-stated awards;
  (v) all mapped awards. Slices reported but NOT used to pick a rule after the fact: agency, military
  vs civilian, award/revenue, cap band, term, sole-source language, year.
- Success criterion stated now: the rule-passing group's **next-open → t+5 close excess** is positive
  with hit rate > 50% on n ≥ 100 events across ≥ 4 calendar years, and beats group (ii) and (iii) at
  the same horizon. Anything less is "not demonstrated" and the source stays measurement-only.
- Ceiling audit: for every award with stated cumulative/ceiling language, record both the obligated
  figure and the ceiling; the ratio uses the obligated figure where both exist. Hudson Technologies
  (2026-08-05, $0.21B at 96% of cap) is checked by hand first.

**The VST finding (ruling 1e):** Vistra (VST) did not move 16% on a procurement award. The catalyst
was a **$4.2B federal LOAN from the Department of Energy's Loan Programs Office to add nuclear output**
(Bloomberg, Fri 2026-10-02 15:27 ET: "US to Offer $4 Billion Loan for Vistra to Boost Nuclear Output";
Benzinga 10-05 "Vistra Gets $4.2B Federal Loan"), amplified Monday 10-06 by Google's 20-year nuclear
deal with Constellation lifting every power name. Bars: 10-02 close 140.02 (+0.2% on 12.7M shares, the
leak landed mid-session), 10-05 144.89 (+3.5%), **10-06 open 151.47 (+4.5% gap), trading 159.55 at
14:40 ET (+10.1% on the day; +14% from Friday's close; the session high 162.69 is +16.2%).** FPDS has no
Vistra action (a loan is not a contract); the DoD digest never would. **What the design would have done
with it: nothing** — it is not a procurement award and neither feed carries it. What WOULD have caught
it: a DOE Loan Programs Office announcements source (press releases, conditional commitments and
closings; LPO publishes them) — a sixth event feed, same shape as 8-K items, and the only one of the
three event sources that would have seen this. Flagged for the ruling, not built. VST's only funnel
appearances were two 13F no-position passes on 2026-08-26.

### 2026-10-06 rulings bundle — REPORTS BEFORE SHIPPING (nothing built yet)

Five rulings landed in one session: REWORK (contract awards first, 8-K widening, FDA calendar,
demote congressional/Form 4), the design amendment (relative size, determinants, leakage,
measurement-first), AGGRESSION (deployment, floor 45, horizons, momentum adds), WEEKLY TARGET
(deployment multiplier, never standards), ADAPTIVE STANDARDS (horizon-adjusted R:R,
opportunity-cost hurdle, calibration feedback, the scoreboard constraint). Each asked for a report
before shipping. Everything below was measured live from the droplet; no code or config changed.

**A. Government contract awards — design, volume, latency, access**

- **Volume (DoD daily digest, 52 digests 2026-07-21..10-02 read from the Wayback Machine):** 19.9
  awards/digest (median 16.5). ≥$100M: **4.3/day** (max 22 on a fiscal-year-end day) — $100–250M
  1.87/day, $250–500M 0.94, $500M–1B 1.00, ≥$1B 0.50; of the ≥$100M, 176 new awards / 48
  modifications, 20 multiple-award, ~12% small-business (`*`, almost always private). Agencies
  ≥$100M: Navy 81, Army 76, Air Force 37, DLA 16, MDA 5.
- **Latency:** the digest publishes at **21:00 UTC (17:00 ET) ±5 min every business day — after
  the close.** The first tradeable print is the next open. Our session polls 13:30–20:00 UTC, so the
  design needs an evening poll (17:05 ET one-shot timer: fetch → parse → research inside caps →
  opening limit orders for 9:30, the pass told the post is ~16h old at the open) or it reads the
  digest at the 9:30 first-poll lookback and enters after the gap. Recommend the evening timer.
- **Access — the real cost:** war.gov sits behind Akamai Bot Manager. **HTTP 403 for curl/urllib
  from the droplet AND this box, browser headers or not, even robots.txt.** The RSS works (titles
  only, no body). Wayback has every digest but days late (backfill only; save-page-now returned 520).
  **The r.jina.ai reader proxy fetched today's digest in full, live** — a third-party dependency.
  Options, in order: (1) headless Chromium on the droplet (Playwright, ~300 MB install, self-
  contained), (2) r.jina.ai as fallback, (3) Wayback for history. All three feed one parser.
- **FPDS public ATOM feed (no key) is the structured second feed:** same-day for civilian agencies,
  with action type, reason-for-modification, extent competed, number of offers, signed/effective/
  completion dates, obligated vs base-and-all-options value, department — exactly the determinants.
  **But DoD is withheld 90 days**: 0 of 136 large actions signed in the last 10 days were DoD; 131 of
  200 in June. So FPDS = civilian live + DoD determinants 90 days late. USAspending inherits the lag.
  SAM.gov needs a key (none in `.env`) and carries award notices sparsely.
- **Awardee → ticker:** a curated parent map (primes, subsidiaries, known-private/foreign/nonprofit
  drops) resolved 225 of 512 awards ≥$100M (2015–2026 archive), EDGAR exact-name 8, two-word prefix
  5, 42 dropped as known non-public, **232 unresolved — nearly all JVs, small-business `*` names,
  private contractors (SRC, MTSI, Mortenson, Haskell).** The map is a human-editable config table
  like `sectors.yaml`; a `*` awardee drops at parse. Primes dominate: 171 of 218 mapped 2026 events
  were ≥$50B market cap.
- **Drift measurement (2026 live window, 218 mapped events ≥$100M on 42 tickers, excess vs SPY,
  points; t0 = digest day, announcement after that close):**

| slice | pre t-5→t0 | t0→t+1 | t0→t+5 | t0→t+20 |
|---|---|---|---|---|
| all mapped | −1.08 (hit 36%, n=78) | +0.17 (54%, n=79) | −0.22 (39%, n=71) | −4.78 (13%, n=45) |
| award / market cap 1–5% | −0.93 | **+0.88 (73%, n=15)** | +2.36 (med −0.34) | −0.97 |
| award / market cap ≥5% | −0.34 | +0.13 (64%, n=14) | −0.12 (med +0.98) | −6.38 |
| <0.2% of market cap | −0.81 | −0.01 (45%) | −1.24 | −7.44 (0%) |
| awardee market cap $5–50B | **−2.88 (17%)** | **+1.21 (85%, n=13)** | +2.01 | +0.60 |
| awardee ≥$50B | −1.18 | −0.01 (46%, n=57) | −0.79 | −5.64 |
| new award | −0.79 | +0.24 (58%) | +0.89 | −3.61 |
| modification / option | −1.47 | +0.07 (50%) | −1.74 | −7.14 (0%) |
| sole-source flagged | −2.07 (18%) | 0.00 (42%) | −0.77 | −6.06 |
| Air Force | −2.44 | **+0.99 (88%, n=16)** | −1.26 | −3.93 |
| new + single awardee + rel ≥1% | +0.19 | +0.32 (65%, n=17) | **+2.29 (med +1.47, 56%, n=16)** | −1.30 |

  Reading: the category average is the literature's ~0 (t+1 +0.17); **no positive pre-announcement
  leakage is visible — the five days before an award run −1.1 on average**, so the digest is not
  structurally late; the subtype with a pulse is **mid-cap awardee, new award, ≥1% of market cap**
  (+2.3 at five days on 16 events), and modifications to primes are noise at every horizon. The 20d
  column is the September defense tape, not the awards. **The 16% mover was not found:** last
  week's ≥$4B DoD awards were the $4.22B TRANSCOM team (FDX, t+1 +1.1%) and HII's $5.1B (t+1 +1.0%,
  +3.9% at t+2); the largest t+1 move in the mapped set was HDSN **−9.1%** (a $0.21B DLA award worth
  96% of its market cap). If the example was a civilian-agency award it is in FPDS live — name the
  ticker and it can be checked.
- **Proposed design (amended per the relative-size ruling):** source `gov_contract_awards`,
  Class 1, two fetchers behind one parser (DoD digest; FPDS civilian ≥$25M), measurement-only for
  30 days with a weekly subtype table. **Primary filter: award / market cap ≥ 1% researches
  (cap 6/day, Class 1 pool); 0.2–1% emits measurement-only; below 0.2% and all `*` small-business,
  JV and unresolved awardees drop free at the prefilter.** A $50M parser floor only. Market cap
  from Finnhub `profile2` (verified live: LMT $116.9B, LDOS $15.0B, KTOS $7.9B); trailing revenue
  from SEC companyfacts (verified: LMT FY2025 $75.05B) so award/revenue is stamped too. Stamped
  determinants: amount, award/mcap, award/revenue, awardee mcap band, new vs modification,
  multiple-award, sole-source flag + offers (FPDS gives both; the digest gives sole-source
  language only), agency and military/civilian, term in months from the completion date, feed,
  publish timestamp and age at observation. Forward report: pre-drift and post by every
  determinant from day one. Cost: $0 feed; ≤6 passes/day ≈ $1.3/day.

**B. The binding constraint on position count (last 10 sessions, 2026-09-23..10-06) — answers the
AGGRESSION report, the WEEKLY-TARGET question and the ADAPTIVE-STANDARDS replay together**

- 152 research passes, **11–20/day of the 40 cap — the budget is not binding.** 131 verdicts were
  `no_position` (86%), 122 of them at confidence ≥70. **21 long verdicts, every one from
  form4_insiders, every one in the 50–69 band:** 4 approved and bought (ADC, TPVG, DKS, BPRE),
  7 `insufficient_reward_risk`, 6 `unconfirmed_boundary` (5 reversed by the second pass, 1 upstream
  error), 4 `no_price` (SKIL, NYAX ×3 — unserved symbols). **Zero long verdicts below 50**, so the
  floor 50→45 admits nothing on this data and the [45,70) boundary band costs nothing. 8-K: 60 of
  60 passes no_position. Trump: 2–8/day, all no_position. Callers: ~0. Congressional: ~0 since the
  floor. **No concurrent-position cap exists for the judged sleeve** (only the mechanical arm's 30
  slots); the count — 2 open today, CELH and BPRE, ~4% of the sleeve — is limited by verdicts, not
  by any cap, not by sizing, not by the budget. A 1.3× deployment multiplier would have multiplied
  four 2%-band entries: **inert.** The honest answer to the weekly-target ruling's question 4 is
  that the target is unreachable until an event source produces long verdicts; the multiplier and
  the reporting lines are cheap to build and should not be reported as deployment.
- **Projected research spend under the whole bundle:** unchanged at ~$3–4/day (today $3.38, 17/40)
  plus ≤$1.3/day for the contract source; the FDA calendar would add its own cap later. The floor,
  the boundary band and the multiplier add $0 on the measured flow.
- **ADAPTIVE STANDARDS replay:** the last 30 declines (10-02..10-06) are 29 `no_position` and 1
  reversed boundary — **no R:R or floor variant changes any of them.** Over all 20 R:R rejections
  since 2026-09-02, annualized expected return on capital ((target−entry)/entry ÷ days/365, days from
  `expected_resolution_date`) runs −102% to +171%: **8 of 20 clear a 25% annualized hurdle with
  R:R ≥0.8, 7 clear 40%/0.8, 5 clear 60%/1.0** (RWT +121%, COO +159%, DMRA +162%, GME +112%, XENE
  +171%, INBX +79%, TFC +80%, BORR +80% among the clearers; the months-horizon ones — UBER +21%, GIII
  +30%, BCBP +11%, FTHY +6% — are what the flat 1.3 was wrongly treating the same as a 5-week move).
  **Proposal:** hurdle 40% annualized on expected move, floored by absolute R:R ≥ 0.8, days
  clamped into the leash bounds; **opportunity-cost scaling** `hurdle = base × (1 + 0.5 × u)`,
  `u = max(judged deployed / sleeve target, min(1, candidates-above-base in today's scored queue /
  free slots))`, computed in the orchestrator from deployment and dispatch scores only (today u≈0.07
  → the floor). **Calibration feedback:** a fenced `MEASURED RECORD` block per source × confidence
  band × horizon class — n, hit rate, mean 5d/20d excess from the forward engine — rendered only at
  n ≥ 20, framed as evidence. **Scoreboard constraint:** a topology test that the hurdle, multiplier
  and prompt builders import nothing from attribution, P&L or the target module.
- **Momentum adds:** `ThesisProgress.AHEAD` already exists in the review schema; the add is
  deterministic (intact + ahead → add up to the position's own band cap, mark above blended cost
  plus one ATR, never past hard_cap 0.10, `EntryReason.REVIEW_ADD`, same ATR/scalars). Note the
  only source producing longs today is the one the REWORK ruling demotes.
- **Demotion numbers proposed:** congressional research cap 5 → 1 (the $15,001 floor already starves
  it; the control arm's refill is untouched), Form 4 cap 5 → 2 (it is the only long-verdict source
  until the event source exists — cutting it to 0 means zero entries in the interim), 8-K stays 6
  until the widening is ruled with volume.

**Proposed shipping order after approval:** (1) contract-awards source, measurement-only, evening
timer, FPDS civilian feed, mapping table, subtype forward table; (2) CLAUDE.md standing constraints
(target never in prompts; nothing a function of P&L / target distance / elapsed time) with the
topology tests, the horizon-adjusted hurdle, the inert-today multiplier and its health/weekly
lines; (3) floor 45 + boundary [45,70) + leash floor 5 + deployment/sector caps (cheap, zero
projected spend); (4) momentum adds; (5) demotions; (6) 8-K widening and the FDA calendar with
their own volume reports. Golden replay + live round trip for each prompt-touching step.

### Are the declines selling strength? Run-up declines vs the rest, forward excess (2026-10-06, report only — nothing changed)

**Asked** on the VSTS decline 178299cfb0104e21 ("would be a pure momentum/narrative continuation
bet"). Two corrections first: that sentence is not in the audit log (no record contains "narrative
continuation"); the VSTS record's own grounds are "excerpt is XBRL cover metadata, no 5.02 content;
13.5h stale; volume 0.92x" — a content decline, not a momentum one, and the classifier below puts
it in "neither". The question stands regardless and was measured over every researched decline.

**Method (deterministic, read-only, droplet audit log + forward cache refreshed 2026-10-02):** 356
researched declines (324 `no_position`, 21 `insufficient_reward_risk`, 10 `unconfirmed_boundary`, 1
`already_held_no_add`). Each decline's thesis + priced_in_analysis (manipulation and invalidation
fields excluded — boilerplate and hypotheticals) parsed for the SIGNED facts the prose states:
"X% above/below its 200-DMA", "Y% off its 52-week high", "up/down Z%", plus markers (run-up,
already moved/played out, extended/stretched/overbought, rich valuation; momentum/narrative wording
tracked separately). **run-up** = above the 200-DMA, or within 10% of the high, or a strength marker,
or a stated gain ≥10%; **sold-off** = below the 200-DMA, or ≥20% off the high, or a stated fall ≥10%;
a conflict is settled by the 200-DMA sign. Result: run-up 81, sold-off 118, mixed 8, neither 149
(the "neither" bucket is mirror-verification failures, 13Fs, 8-K boilerplate — declines with no
price view at all). A first pass that keyed on the words "52-week high" / "200-DMA" alone tagged 212
as run-up because the prompt's market-context block puts those phrases in EVERY report — the
signed parse is the one to trust. Excess vs SPY, points; n rows / ev distinct ticker-days / tk
tickers / d observation days:

| slice | 5d | 20d | 60d |
|---|---|---|---|
| all researched declines | −2.18 (med −1.30, tw −2.65, hit 31%, n=160, 145 ev, 116 tk, 23 d) | +3.27 (med +0.38, tw −1.56, hit 53%, n=62, 53 ev, 40 tk, 13 d) | none yet |
| **run-up** | **−0.95** (med −1.23, tw −0.92, hit 35%, n=55, 48 ev, 35 tk, 19 d) | **+6.05 (med +1.84, tw +2.91, hit 59%, n=29, 25 ev, 19 tk, 10 d)** | none yet |
| **sold-off** | **−3.74** (med −1.97, tw −3.90, hit 28%, n=76, 74 ev, 69 tk, 19 d) | **−5.84 (med −4.85, tw −5.84, hit 29%, n=14, 14 ev, 14 tk, 9 d)** | none yet |
| neither | −0.20 (n=24) | +3.11 (med +2.09, tw +0.39, n=16) | — |
| run-up minus sold-off | +2.79 (SE 1.24, t +2.2) | +11.88 (SE 3.36, t +3.5) | — |
| above its 200-DMA (stated) | −0.96 (n=50, 32 tk, 19 d) | +5.42 (med +2.13, tw +3.20, hit 58%, n=26, 17 tk, 10 d) | — |
| below its 200-DMA (stated) | −4.07 (n=65, 59 tk, 16 d) | −6.87 (med −7.81, hit 30%, n=10) | — |

**The 200-DMA gradient at 5d is monotonic** (below 25%+ −6.92 n=21 → below 10–25% −3.45 n=19 → below
0–10% −2.13 n=25 → above 0–10% −1.26 n=10 → above 10–25% −1.13 n=26 → above 25%+ −0.42 n=14): over
115 rows and 16–19 observation days, the further above the 200-DMA a declined name sat, the better
it did the following week — relative to other declines. SPY over the same windows was flat (−0.3 to
+0.4), so excess ≈ raw: this is not beta.

**What carries it, and what cuts against it:**
- **Source:** congressional run-up declines 20d **+8.37 / med +3.14 / tw +4.25, hit 68%, n=25**
  (BE ×5 rows +34/+26, CRWD +23, INTC +22, AMD +20, PLTR +10 at the top; TOST −13, PRAX −19, ALKS
  −12 at the bottom) vs congressional non-run-up +0.92 / med −1.31 / tw −2.27. **Form 4 run-up
  declines run the OTHER way:** 5d −3.85 (hit 9%, n=11), 20d −8.45 (n=4; INBX, PRTS) — buying into
  strength on an insider signal looked wrong; declining it was right.
- **Who declined:** the research pass's own `no_position` run-up verdicts are the ones that went on
  to outperform (20d +7.07 / med +2.83 / tw +2.84, hit 63%, n=27). Run-up names the MODEL wanted
  (direction long, refused by the floor / reward:risk / boundary) did badly: 5d −3.10 (hit 17%,
  n=6), 20d −7.82 (n=2). The deterministic gates refusing run-up longs were right; the model's
  priced-in declines of run-up names are the question.
- **Markers:** "extended / stretched / overbought" 20d +11.13 / med +8.10 / tw +8.56, hit 70%, n=10
  (7 tickers). "Already moved / played out": 5d **−5.31, hit 19%, n=26** then 20d +1.28 — the names
  dip in the week after and recover. "Momentum/narrative" wording itself is unremarkable (20d
  +2.35 / tw −0.32).
- **Sample shape:** 20d run-up evidence is 19 tickers over 10 observation days (2026-08-27 →
  09-11), nearly all congressional, with AI/semis megacaps (AMD, CRWD, PLTR, NVDA, MSFT, AAPL) in
  the top half — one market path in which that factor ran. 60d marks for the late-August rows land
  ~2026-10-27; a second, independent observation window does not exist yet.

**Reading, stated not ruled.** Directionally the question's premise holds: the declines are not
random — the system correctly avoided the sold-off names (−3.7 / −5.8), and the names it declined
for having already run went on to beat both SPY and every other decline bucket at 20d, by median
and ticker-weighted as well as mean. At 5d even the run-up declines were slightly negative vs SPY,
so the system was not wrong on the week, only on the month. The mechanism is visible in the prose:
the priced-in frame reads "X% above the 200-DMA" as "no mean-reversion dislocation, nothing left to
capture" — a mean-reversion-only lens with no door for continuation, which is what the question
calls structural. Against calling it now: one source, one window, 19 tickers, no 60d, and Form 4
run-ups go the other way. **What would make it decidable:** (1) stamp a deterministic
`dma_distance_at_observation` (from the bars the forward engine already fetches) on every decision
record so this slice is mechanical, not prose-parsed; (2) a weekly forward line "declines by 200-DMA
band" at 5/20/60d; (3) rule after the 60d marks (~10-27) and a second window. If it holds, the
design question is a continuation door in the research frame with its own measurement tag — the
shape the options doors took — not a threshold tweak. No code or config changed.

### Citrini re-pointed to @citrini, trial clock reset — human ruling 2026-09-30

**Ruled:** `signals.yaml` citrini `handle` "@Citrini7" → **"@citrini"**; `start_date` 2026-08-25 →
**2026-09-30**, so feed billing and the fair trial begin at the fix. **Recorded as ruled: @Citrini7
(protected, 0 followers, "Citrinitas Research") was never the intended account, and the source has
had NO trial** — 100 polls from 2026-08-25 returned clean 200s with zero items because a protected
account's posts are invisible to search. **It must not be graded at 2026-10-15 and must not be
counted in the callers' 0-trades finding** (64 candidates / 8 passes / 0 trades are nolimitgains,
unusual_whales and optionshawk; citrini contributed nothing to any of the three numbers).

**Built so the report says the same:** `audit.spend.split_callers_by_trial` — a caller whose
`start_date` is later than the day the 10-15 table was requested (`FEED_SPEND_RULING_REQUESTED`
2026-09-28) is rendered on its own line, "re-wired after the ruling was requested — NO trial, NOT
graded 2026-10-15", never summed into the X-fed callers line. Deterministic from config: when
citrini's post-fix numbers accrue they appear, labelled, and stay out of the finding. Tests:
`tests/test_feed_spend.py` (+1; pins the handle and the reset date). First poll of @citrini: the
next scheduled session (fresh process each morning). Expect ~20 posts/day incl. replies, ~$3/month
of reads, most dropped by `require_instrument`.

### Citrini was never wired to Citrini; the real X bill is dollars (2026-09-30, report only — nothing changed)

**1. Citrini diagnosis: the handle is WRONG, the source has never had a trial.** Probed live from
the droplet (bodies only, never the credential):

- `signals.yaml` handle `@Citrini7` → `users/by/username` resolves to id 1677496020660305920,
  name **"Citrinitas Research"**, **protected**, 0 followers, 27 posts, created 2023-07-08, empty bio.
  A protected account's posts are invisible to `search/recent` (the production query
  `from:Citrini7 -is:retweet` → `result_count: 0`, HTTP 200) and `users/{id}/tweets` answers
  "not authorized to see the user". 100 polls (29 Aug, 71 Sep), 0 items, 0 errors — the fetcher was
  working; it was asking about a stranger.
- The real account is **@citrini** (id 1365809270034477069): "Citrini", bio "Thematic, Cross-Asset
  Investment Research", verified business, 290,933 followers, 43,998 posts, created 2021-02-27.
  `from:citrini -is:retweet` returns a full page (10 posts 09-28..09-29 with a next page); its
  09-29 bearish Truist (TFC) call was quoted across FinTwit the same day. `@CitriniResearch` is a
  protected placeholder whose bio reads "Follow Citrini Research on X @Citrini"; `@Citrini_7` is
  forbidden. Whether @Citrini7 was ever theirs cannot be read from the API; what is certain is that
  it has pointed at a protected zero-follower account since the source was wired 2026-08-25.
- **Not ruled on, by request.** The fix is one config line (`handle: "@citrini"`), a human ruling
  because it re-points a watchlist account; if made, restart the source's `start_date` so billing and
  the fair trial begin at the fix, and keep the 10-15 spend ruling from grading citrini as tried.
  Expect ~20 posts/day from the real account (replies included — `-is:retweet` does not exclude
  them), ~$3/month of reads, mostly dropped by `require_instrument`; `daily_read_warning` 200 holds.

**2. X API metered spend, reconciled (usage endpoint `GET /2/usage/tweets`, project 2089801343359741952,
cap 3,000,000 posts/month; price $0.005 per post read, $0.01 per user lookup — X pay-per-use, live since
the free tier closed 2026-02-06 and Basic was migrated 2026-06-01):**

| month | posts read (X meter) | of which production polls (run.log POLL items) | diagnostics/probes | metered $ | config "feed" budget |
|---|---|---|---|---|---|
| August (08-18..08-31) | 856 | 755 | ~100 (08-18 smoke, 08-19/20 mirror experiments) | **$4.28** | $50/mo across the four callers (prorated ~$12) |
| September (09-16..09-30; ZERO reads 09-01..09-15) | 2,041 | 1,619 | ~420 (09-16 ttox/tdp 7-day profiles 436; 09-28 connectivity; 09-30 citrini probes ~60) | **$10.21** | $50 |
| to date | 2,897 | 2,374 | ~520 | **$14.49** | $61.00 billed by the attribution |

Production reads by X source, Aug + Sep: trump_mirror_tdp 831 (**$4.16 — the dormant fallback is the
dearest line; every post it returns is billed and then dropped as commentary**), unusual_whales 673
($3.37), trump_mirror_ttox 413 ($2.07), optionshawk 274 ($1.37), nolimitgains 183 ($0.92), citrini 0.
Run-rate at full polling (09-16..09-30, 11 trading days): ~147 reads/day ≈ **$15/month for all six X
sources, ~$8/month for the four callers**. Config budgets $50/month for the callers ($25 + 10 + 10 +
5; trump_posts and both mirrors carry $0 — the config total is $50, not $75). **The metered bill is
3–4× below the budget lines and the callers' share is single-digit dollars a month.** Not verified:
the developer console's invoice (a prepaid-credit balance or minimum would show there, not on the
usage endpoint) — the human reads it before ruling; the usage counts above are the meter's own.

**Two facts that change the 10-15 framing:**
- **The X leg was dark 2026-09-01 → 09-16 13:15Z (11 trading days): the dead bearer token** of the
  09-16 diagnosis. Zero reads on the meter, zero X POLL lines in run.log, sessions started and logged
  LOOKBACK + MIRROR then polled nothing on X. The callers' "35–43 days" of feed billing cover ~20
  trading days of actual polling (9 in August, 11 in September); the attribution billed the outage.
- **The ruling is about noise and attention, not cost.** At ~$8/month metered the four callers are not
  a spend question; the question is whether 64 candidates → 8 passes → 0 trades earns its place in
  the funnel and the run log. Citrini is excluded from that question until it is pointed at Citrini.

### Two scheduling rulings for the review dates (2026-09-29) — feed spend 10-15, congressional quality 10-27

**Ruled (human, 2026-09-29):**

1. **Congressional signal-quality verdict: 2026-10-15 → 2026-10-27.** The 60-day marks for the
   August backfill bulk land ~2026-10-25 and belong in the package. The 20-day read is 852 of 960
   rows from ONE observation date (2026-08-26) — a single three-week market path — and must not
   carry a source-retirement decision on its own. The forward report's congressional band section
   and the weekly's band table now say 2026-10-27; the Form 4 / 8-K / 13D hard review stays
   2026-10-15 (unchanged by this ruling). **CLAUDE.md still reads "the 2026-10-15 review" in the
   Class 2 amount-floor bullet — a constitution edit, left for the human.**
2. **Feed spend rules SEPARATELY on 2026-10-15, on spend alone.** The X-fed callers (nolimitgains,
   unusual_whales, optionshawk, citrini) are decidable on candidates, passes and trades — not on
   forward returns they have never produced a position to measure.

**Built (this entry):** the weekly's feed-spend table (`audit/spend.py`, `orchestrator weekly`)
gains two columns and one line: **cands** (funnel candidates the source delivered at all —
the forward report's rule, first record per decision id, pre-filtered included because the feed
delivered and billed them), **next 90d $** (feed × 3 months + research at the source's observed
daily rate since its start_date — a projection of the rate, nothing more), and an **X-fed callers**
subtotal line (sources whose only platform is X and which are not a mirror). The paid-feeds line
now carries candidates, passes and the 90-day projection with its base. Tests:
`tests/test_feed_spend.py` (+1, the projection's edge cases; the pipeline test asserts the new
columns and the group line). Suite green on the droplet in a scratch clone (this box has no
Python).

**The per-source table for 2026-10-15 (droplet audit log, 2026-09-29; realised is gross; research
$ are estimates — the 09-02..09-15 window understates by ~one pass per floor-band entry; the
console bill is truth):**

| source | feed $/mo | feed to date | research $ | candidates | passes | trades | closed / won | realised $ | net $ | next 90d $ (feed + research) |
|---|---|---|---|---|---|---|---|---|---|---|
| congressional_disclosures | 30 | 43.00 | 11.28 | 3,221 | 87 | 1 | 1 / 1 | +102.06 | +47.78 | 113.61 (90.00 + 23.61) |
| unusual_whales | 25 | 29.17 | 0.37 | 34 | 3 | 0 | 0 | 0.00 | −29.54 | 75.95 (75.00 + 0.95) |
| nolimitgains | 10 | 14.33 | 0.50 | 17 | 1 | 0 | 0 | 0.00 | −14.83 | 31.05 (30.00 + 1.05) |
| optionshawk | 10 | 11.67 | 0.42 | 13 | 4 | 0 | 0 | 0.00 | −12.09 | 31.08 (30.00 + 1.08) |
| citrini | 5 | 5.83 | 0.00 | 0 | 0 | 0 | 0 | 0.00 | −5.83 | 15.00 (15.00 + 0.00) |
| form4_insiders | 0 | 0 | 32.15 | 536 | 57 | 8 | 4 / 2 | −51.26 | −83.41 | 107.17 |
| trump_posts | 0 | 0 | 23.68 | 259 | 77 | 0 | 0 | 0.00 | −23.68 | 49.56 |
| form_8k | 0 | 0 | 9.26 | 470 | 60 | 0 | 0 | 0.00 | −9.26 | 59.53 (14 days of base) |
| form_13f | 0 | 0 | 1.16 | 10 | 10 | 0 | 0 | 0.00 | −1.16 | 2.43 |
| form_13d | 0 | 0 | 0.48 | 3 | 3 | 0 | 0 | 0.00 | −0.48 | 1.60 |
| **X-fed callers** | **50** | **61.00** | **1.29** | **64** | **8** | **0** | **0** | **0.00** | **−62.29** | **153.08 (150.00 + 3.08)** |
| all sources | 80 | 104.00 | 79.30 | 4,563 | 302 | 9 | 5 / 3 | +50.80 | −132.50 | 486.98 |

Candidate → pass funnel for the callers: nolimitgains 17 → 1 (16 prefiltered: instrument-less),
unusual_whales 34 → 3 (31 prefiltered), optionshawk 13 → 4 (9 prefiltered), citrini 0 → 0 (87
polls, never an item; a direct `from:Citrini7` query returns nothing — quiet or wrong handle,
unresolved since 09-28). All 8 passes declined. Since the 09-28 read, realised moved +32.73 →
+50.80 (a Form 4 position closed as a win; Form 4 is now 4 closed, 2 won).

**Two facts the 10-15 spend ruling must carry:**

- **The callers' feed lines are BUDGET figures for the X pay-per-use meter, not invoices.**
  `signals.yaml` says so on each: reads cost ~$0.005 per post, requests with `since_id` are free,
  and $10 / $25 / $5 a month are "the conservative budget figure" the attribution bills by the
  2026-08-28 ruling. At the measured rate the callers' 64 delivered posts metered on the order of
  $0.30; the Trump mirrors' reads bill on the same meter (config carries it as the $25
  unusual_whales line). So cutting the four callers saves ~$150 of BUDGETED feed over 90 days and
  ~$3 of research, but the metered X bill barely moves while the mirrors stay — and the
  developer-console bill, not this table, is the number to read before ruling. The X meter
  itself is the Trump leg's delivery and is not on the table.
- **Zero trades is the finding, not the P&L.** 64 candidates in 35–43 days, 8 researched, 0 taken:
  the callers cannot be graded on forward returns because they have never produced a position.
  Keep-or-cut is a spend and attention decision (the 8 passes cost $1.29), and a cut source can
  be re-wired later — adding it back is a source approval like any other.

### The four defects fixed (2026-09-28, b41397b)

1. **Exit pricing:** `ExitEngine` and `MechanicalEngine` take `bids`; equity sells limit at the
   bid rounded down (mark rounded down without one); wired from `prices.bid` in bootstrap.
   A working exit no longer removes a position from `_review_queue` (only a review's own close
   verdict does); `_close_position` still refuses a second order while one works. TPVG's three
   cancelled attempts had already released their reservations; the next attempt prices at the
   bid. Tests: bid limit, no-bid fallback, review-while-pending.
2. **Unserved memo:** strikes counted once per day, `UNSERVED_STRIKES` 3 on distinct days
   blacklists, a 200 clears the strikes, legacy entries stand. INTC/BBD/RWT/SBLK purged on the
   droplet (`data/purge_memo.py`); AXIA3 kept.
3. **Overreaction timer:** `ops/vps/agentic-overreaction.{service,timer}` (weekdays 16:45 ET)
   were already in the repo, never installed. Install needs root — see the unit header. The
   missed sessions 09-04..09-28 were backfilled by hand (`overreaction --backfill`).
4. **Alerts:** `SENDGRID_API_KEY` (+ `ALERT_FROM` verified sender, `ALERT_TO`) adds an HTTPS
   transport tried before SMTP; outcomes land in `data/alerts_status.json`; health prints an
   `alerts:` line (never delivered / last failure / consecutive failures). Operator sets the key.

### Connectivity/integrity check, two verdict packages, and four open defects (2026-09-28)

**Connectivity (every credential exercised live, no values printed):** Alpaca trading + SIP
data, Anthropic Models API (all three pinned models present), Quiver (newest report 09-25), X
(449 reads left in window), Finnhub, EDGAR FTS + current feed, Robinhood refresh grant — all
PASS. **SMTP FAIL: outbound 465/587 time out and there is no IPv6 route (DigitalOcean's default
SMTP egress block). 28 alert send failures since alerts went live 09-04, zero successes ever;
the four "weekly report emailed" lines are the queue acknowledgement, not delivery.** Every
urgent alert (exits started, scanner errors) has been lost. Remedy: DO support ticket to
unblock SMTP, or an HTTPS mail API. Nothing changed.

**Sources:** all delivered items today except 13F (quiet by season, next window mid-Nov),
citrini (87 polls, never an item; a direct `from:Citrini7` query returns 0 posts/7d with no
error — quiet or wrong handle), and the overreaction screen (last record 09-03: it has NO
timer and runs by hand — the live 2026 slice is not accumulating). Truth Social has no fetcher
by design. **Timers:** five units (paper, earnings, weekly, backup, the elapsed one-shot
rh-refresh), all fired on schedule this week, exit 0. **Data:** audit 6,102 lines, credibility
1,505, forward cache refreshed 09-25 (weekly) and 09-28 (manual), session state clean, droplet
== origin. **Journal-only this week:** the SMTP failures; 354 "triggered review of BBD deferred"
warnings on 09-22 — the same-day dedupe attached each Form 4 echo as convergence and each echo
owed a review: BBD was reviewed FIVE times in two hours on day 0, all five concluding "same
filing" (spend the dedupe was meant to avoid); MSB stale quotes; 9 EDGAR 500s retried fine.

**Open defects found (none fixed — report-only run):**
1. **Exit pricing (TPVG 77e61df2):** guardrail/review sells limit at the LAST quote rounded
   down (`exits.py` ~1837; the mechanical time exit likewise). TPVG's trailing stop (HWM 5.60,
   stop 5.04) fired 09-24; three day orders at 4.89/4.88/4.84 rested unfilled on a thin $4.8
   BDC in a falling tape and were cancelled at the close. A pending exit skips the review queue,
   hence "reviewed never". Same class as the 2026-09-04 unsweep incident. Fix: sell at the bid
   rounded down (a cent under the ask without one); the bid source exists and feeds the sweep
   and baseline. The max-loss stop at 4.49 is still armed.
2. **Unserved memo poisoned:** INTC, BBD, RWT, SBLK memoised as "HTTP 400, never again" by
   the weekly at 21:01 UTC 09-25; the same request succeeds now. The Friday weekly will skip
   the forward marks of four judged names until the entries are deleted and 400 stops being
   permanent. (My manual refresh bypasses the memo, so their rows are current tonight.)
3. **Overreaction screen unscheduled** (above).
4. **This dev box has no Python** (venv points at a missing 3.12.10); suites ran on the
   droplet in a scratch clone this session. Bounce: not possible from the agentic account
   (sudo needs a password) and not needed — the paper unit is a fresh process each morning.

**BBD (3b12cf52):** opened 09-22 at 3.51, 314 sh, weeks, conf 60, five-insider $5.69M cluster;
five same-day echo reviews + one cadence hold; 09-24 review: close, `thesis_invalidated` —
3.38 below the thesis's own ~3.43 insider-price floor, the "meaningful volume" qualifier not
used to rescue it, confidence pushed ≤50; filled 3.38 same tick; realised **−40.86 (−3.7%)**.

**Built (b3b09b5):** Form 4 verdict package in the forward report (5/20/60d, row beside
ticker-weighted, caveats printed, 60d population dates: clusters first mark 2026-11-02, 20
tickers from 2026-11-16 — neither Oct 15 nor Oct 27 has a 60d Form 4 cell) and the feed-spend
table in the weekly (`audit/spend.py`). Today's read: clusters 5d −2.85 rows / −2.00
ticker-weighted (24 tickers, 25% hit) vs singles −1.57 / −1.60 (150 tickers, 34%); clusters
now WORSE than the control at 5d; the 20d cluster cell is one row (GROV +0.67; the 09-09..11
cohort's marks land Friday 10-02); singles 20d −7.21 / −7.12 on 50 tickers but 3 observation
days (one market path). Feed spend since inception: congressional $30/mo → 87 passes, 1 trade,
+102.06 realised, net +47.78 (one INTC trade carries the source); unusual_whales $25 → 3
passes, 0 trades, net −29.54; nolimitgains $10 → 1 pass, 0 trades; optionshawk $10 → 4 passes,
0 trades; citrini $5 → 0 passes; Form 4 (free) → 53 passes, 8 trades, 3 closed, −69.33;
trump_posts (free, X-metered) → 77 passes, 0 trades, $23.68 research. All sources: $104 feed +
$75.64 research = $179.64 spent, +32.73 realised, net −146.91. Note the X pay-per-use meter is
carried as the $25 line on unusual_whales by config; the mirrors' reads bill there.

### Oct-15 congressional verdict package and the overreaction core-tier check (2026-09-22)

**Built:** the forward report's congressional band section now renders 5d/20d/60d with row
means BESIDE ticker-weighted means, distinct-ticker and observation-day counts and the top-2
tickers' share of rows (`_weighted_line`), plus "all purchases" and "live flow only" lines; the
overreaction section adds a core-tier-by-year regime line. Every Friday weekly carries the
package from here.

**Congressional (repaired cache, excess vs SPY, points):**

| band | 5d rows / ticker-weighted (tickers) | 20d rows / ticker-weighted (tickers) | 60d |
|---|---|---|---|
| ≤15K | −1.23 / −1.18 (408), hit 29% | **−2.66 / −2.42 (364)**, hit 31% | not yet |
| 15–50K | −0.76 / −0.56 (67) | −0.69 / −0.75 (59), hit 46% | not yet |
| >50K | +0.02 / −0.25 (13) | +11.93 / **+2.86** (13); INTC+BE = 57% of rows; without them −0.85 / −0.53 (11) | not yet |
| all purchases | −1.16 / −1.17 (444) | −1.72 / −2.31 (395), hit 30% | not yet |
| live flow (Sep) | −1.27 / −1.04 (108) | 7 rows only | — |

Caveats the ruling must carry: (1) 852 of the 960 rows with 20d marks were observed on ONE day
(2026-08-26, the backfill drop) — the 20d congressional read is a single 3-week market path,
however many tickers it spans; the live Sep flow has 7 rows at 20d. (2) 60d marks for the
backfill bulk land ~2026-10-25 — Oct 15 rules on 5d/20d unless it waits ten days. (3) 648 sales
in the funnel are not graded here.

**Overreaction core tier — survives the repaired marks numerically, not the composition check.**
68 events, 16 distinct tickers; ticker-weighted 60d +4.56 (row +7.11), hit 67%. But: 27
covid_2020 (+16.4 rows / +20.2 ticker-weighted at 60d, 89% hit), 25 rates_2022 (+7.7 / +5.5),
15 q4_2018 (**−10.6 / −8.6**), 1 live. And the split that matters for the deferred LLM half:
market-day drops (SPY also down) +11.8 / +13.1 at 60d, 86% hit — idiosyncratic drops −4.5
ticker-weighted at 60d, 25% hit, negative at 20d too. The "core" universe is the 2026 judged
book (AMZN, BE, INTC, AAPL, AVGO, NVDA, AMD, IBP…) projected onto past windows: survivors with
beta > 1 rebounding harder than SPY after two V-shaped crashes. That is a high-beta regime
rebound in names known today to have survived, not idiosyncratic overreaction; the slice the
fundamental-vs-non-fundamental classifier would trade is the one that loses. Broad tier
(343 tickers) is flat at every horizon in every window. Stated, not ruled.

### Forward-return integrity incidents (2026-09-22) and the first longer-horizon read

Two defects surfaced while running the Form 4 cluster read; both fixed, shipped (348d5db,
e1c7847), cache repaired on the droplet.

1. **Mid-session refresh 403ed on every symbol.** Alpaca's free plan refuses a SIP bars query
   whose window reaches into the last 15 minutes. The Friday weekly runs after the close and
   always worked; a manual `priors --refresh` at 13:00 ET appended nothing (46 baseless rows).
   Fix: `AlpacaDailyBars.bars` clamps the query end with `completed_bars_end` — before today's
   bar until 16:16 New York (an intraday print must never become a mark), then now−16 min.
2. **Displaced bases/marks.** The overreaction screen's backfilled 2018/2020 events took their
   base from the first bar the fetch returned (months late: the client read only the first
   page of a multi-year history) and every horizon collapsed onto that bar — 66% of the slice
   read excess 0.00. 929 rows dropped (backup `forward_returns.jsonl.bak-2026-09-22`),
   recomputed. Fix: `MAX_MARK_GAP_DAYS = 4` (base or mark more than 4 calendar days after its
   day is ABSENT), and the bars client follows `next_page_token`. 2008 history is not served:
   those events stay absent.

**The read (2026-09-22, excess vs SPY in points; hit = share > 0; THIN = n<20):**

| slice | 5d | 20d | 60d |
|---|---|---|---|
| Form 4 clusters (all funnel rows) | −0.61, hit 50%, n=24 (~17 distinct ticker-days) | not yet (source began 09-03; first marks 09-23) | — |
| Form 4 singles control | −1.69, hit 38%, n=136 | not yet | — |
| Form 4 C-suite singles | n=1 | — | — |
| Form 4 sell clusters (bearish) | −1.29, hit 35%, n=162 | not yet | — |
| Congressional ≤15K | −1.23, 29%, n=1768 | **−2.66, 31%, n=816** (365 tickers; ticker-weighted −2.40) | not yet |
| Congressional 15–50K | −0.76, 31%, n=129 | −0.69, 46%, n=95 (59 tickers; only 3 observation days) | not yet |
| Congressional >50K | +0.02, 47%, n=51 | +11.93, 78%, n=49 — **13 tickers, INTC and BE are 28 of 49 rows**; ticker-weighted +2.86 | not yet |
| Congressional lag ≤7d / >35d | −1.81 / −1.01 | −2.04 / −2.66 | — |
| 13D | n=1 | — | — |
| Overreaction broad tier | −0.32, 48%, n=1318 | −0.14, 53%, n=1315 | +0.25, 51%, n=1302 |
| Overreaction core tier | +1.69, 54%, n=68 | +1.53, 53%, n=68 | +7.11, 63%, n=67 |
| 8-K (all) | +3.47 mean / −2.44 median, 38%, n=88 (AEMD +339 drives the mean; trimmed −0.09) | not yet | — |

Reading: the congressional pattern does NOT reverse at 20d — the ≤15K bulk deepens from −1.2
to −2.7 and the short-lag slice stays negative; the >50K band's +12 is two names (INTC, BE) in
the August backfill, not a band effect. Form 4 clusters at 5d are not positive (−0.61) but sit
1.1 points above the singles control on ~17 effective observations — inside one standard
error; the 20d cluster marks begin 2026-09-23 and reach n≈20 around 2026-10-06. The
overreaction screen is the one slice with three horizons: broad flat, core tier positive and
growing with horizon (n=67 at 60d) — the first longer-horizon signal in the book, measurement
only. 60d congressional and any Form 4 / 8-K 20d cells: not yet.

### Aggression step 3 — the baseline sleeve, SHIPPED 2026-09-21 at 55/30/15

**Ruled (2026-09-18, revised same day; weights FIXED 2026-09-21 at shipping — judged 55 /
baseline 30 / mechanical 15, because the draft's 45/40 shrank every judged position 40% against
lever 1; 55/30 keeps judged bands near current levels, 1,103 / 2,757 / 5,514 on today's NAV):**
30% of NAV in SPY, deterministic, weekly to target ±5pp, its own attribution bucket subtracted
from every alpha line, SGOV holds
the judged sleeve's undeployed cash; kill switch tripped = neither buys nor sells, frozen until
manual reset, inside NAV and drawdown. Report before shipping: the transition, and whether
25 → 15 forces a mechanical trim (human preference: freeze the 30 slices, no forced sells).

**Built:** `orchestrator/baseline.py` (`BaselineSleeve`: ISO-week check, band in NAV points
per Constraint #6, trade-to-target episodes, FIFO lots, one working order, freeze both ways),
`Sleeve.BASELINE` + gate branch (cash-secured, dust floor, own allocation ceiling 43%, alpha caps
waived; ceiling 33%), `SleeveWeights.baseline`, `BaselineSleeveLimits`, `record_baseline`,
`ExitReason.BASELINE_REBALANCE`, sweeper `liquidity_buffer()` + `extra_buffer` (the baseline's
`funding_need`) + `settle()` before the baseline runs, session `baseline_week_checked` /
`baseline_rebalancing`, `seed_account_state(baseline_open=)`, health line, stress book,
`BaselineAttribution` + `headline_alpha_line()` (judged return − judged book beta × SPY; the
beta weighting now uses JUDGED trails only — SGOV and the mechanical slices had been diluting
it), partition at every strategy filter. Tests: `tests/test_baseline.py` (15); the new weights re-pinned 56 existing asserts (judged
5% band 3,750 → 2,750, mechanical slice 833 → 500, sweep buffer 25,000 → 18,500).
Approved 2026-09-21: ±5 percentage points of NAV; the freeze skips the weekly check; judged-only
book beta; one rebalance, not staged; mechanical refill refused only until natural exits bring
the sleeve under the ceiling, never blocked permanently.

**(a) Transition on the 2026-09-21 book (NAV 100,256; cash 25,035; SGOV 47,292; mech 24,165;
judged 3,750):** target SPY 30,077. The liquidity buffer falls 25,058 → 18,541 (judged
25%×55% + mech 15%×15% + 2,500), so ~6,500 of today's cash is spendable at once; the remaining
~23,600 comes from ONE SGOV unsweep (the 2026-09-02 lot, ~235 of its 449 units). End state:
SPY 30,077 (30.0%), SGOV ~23,700 (23.6%), cash ~18,540 (18.5%), mechanical 24,165 (24.1%),
judged 3,750 (3.7%); cash-plus-SGOV falls from 72% of NAV to 42%. One rebalance, ~3 ticks (90 s) once SPY quotes, no staging — a beta
sleeve has no thesis to time, and Constraint #6 prefers the fewer trades. Runs at the first
tick after the bounce (the week has never been checked).

**(b) Mechanical 25 → 15:** NO trim is forced and none is built. The gate never touches held
positions; the 30 slices ride to their 2027-08-29+ time exits. Refill is blocked by the
allocation ceiling (15% + 3% = 18% of NAV vs 24.1% held; `sleeve_allocation_exceeded`) until
~7 exits land, then resumes at the new ~$500 slice. Breaker and ledger unaffected (value-based).
The floor ruling's "keep the control arm's refill alive" therefore matters from Aug 2027, not
now. **The flag that changed the weights:** at 45/40 the judged bands would have been 902 / 2,256 /
4,512 (were 1,504 / 3,760 / 7,519); at 55/30 they are 1,103 / 2,757 / 5,514.

**Status:** 71195d2 (built at 45/40, held) then the 55/30/15 shipping commit; pushed to origin
and vps with verified refs, droplet pulled and suite green there. Service bounce: human.

### FINAL floor ruling — $15,001 (2026-09-18); the $15K–$50K band tagged for 2026-10-15

**Ruled:** `prefilter.min_amount_max` → **15001**. Exclude the confident, measured-worst population
(≤$15K: −0.79%, 33% hit, n=1541). The $15K–$50K band's −0.67 is n=223 of August backfill — too
thin to cut the source to ~1 candidate/week, which would end the ability to measure it and starve
the mechanical control arm's refill. ~15/week keep flowing (48 of the last 16 days' 1,546
purchases: 44 in $15K–$50K, 4 above); both arms move together (funnel-identity rule 2026-08-27).

**Tagging, so the review reads live data:**
- Weekly attribution (`AttributionReport.congressional_bands`): judged congressional decisions by
  floor band — `<=15K (prefiltered since 2026-09-18)`, `15-50K (UNDER REVIEW)`, `>50K` — with
  decisions, traded, and the expectancy of the closed ones, since inception.
- Forward report: EVERY congressional purchase in the funnel, researched or capped, by the same
  three bands at 5d and 20d — the slice 2026-10-15 rules on.
- Tests: `tests/test_floor_band.py` (2); the prefilter floor tests pin 15,001 (`$1,001 – $15,000`
  skipped, `$15,001 – $50,000` researched). CLAUDE.md Class 2 bullet rewritten as final, with the
  two superseded same-day values recorded.

### Correction — congressional floor $100K → $50K (2026-09-18, same day), and what the live flow says

**Ruled:** `prefilter.min_amount_max` 100001 → **50001** (strictly-below): the "$15,001 – $50,000"
band (max 50,000) is excluded with the ≤$15K bulk; "$50,001 – $100,000" researches. Reasoning as
ruled: the confident finding is the sub-$15K bulk (−0.79, n=1541); the size effect above $50K is
thin (n=34/36, mostly August backfill) and should not drive a 99.7% cut; $50K excludes the
measured-worst population while preserving flow.

**Measured after the correction — the flow is not preserved.** The last 16 trading days
(08-28..09-18) delivered 1,546 congressional purchases to dispatch:

| band (range max) | candidates |
|---|---|
| ≤ $15,000 | 1,498 |
| $15,001 – $50,000 | 44 |
| $50,001 – $100,000 | **0** |
| $100,001 – $250,000 | **0** |
| $250,001 – $1,000,000 | 4 (2 distinct filings; 3 records researched, 1 lost to the cap) |

Nothing in the live flow sits between $50K and $250K, so the $50K floor admits the SAME four
records the $100K floor admitted: **0.25/day, ~1.2/week, both arms** (the mechanical arm's
qualification moves with it; it is at 30-position capacity, so the practical effect is slow
refill of time-exit slots). The only floor that keeps a measurable flow is one that excludes the
≤$15K bulk alone: 48 survivors in 16 days, **3.0/day, ~15/week**, of which 44 sit in the
$15K–$50K band that measured −0.67 (n=223) historically. Stated, not built — the floor is the
ruling's to set; implemented at $50K as ruled.

### Two rulings from the grounding pass — 2026-09-18 (congressional floor $100K; same-name-same-day de-dup)

**1. Congressional amount floor $15K → $100K (`signals.yaml prefilter.min_amount_max` 100001, strictly-below).**
The evidence: ≤$15K was 82% of graded candidates at −0.79% mean 5d excess (33% hit, n=1541);
$15K–$50K −0.67 (n=223); $50K–$100K −0.88 (n=34); only $100K–$250K (+0.58, n=34) and $250K–$1M
(+0.76, n=36) measured positive. The threshold is 100,001 so the "$50,001 – $100,000" band (max
exactly 100,000, measured −0.88) is excluded; "$100,001 – $250,000" researches.

**Two facts the ruling should know, measured after it was made:**
- **The live cut is ~99.7%, not ~80%.** Of the 1,546 congressional purchases that reached dispatch
  in the last 16 days, **4** topped out above $100,000 (0.25/day; all in the $250K–$1M band; 3 of
  the 4 were already researched). The positive bands' n (34/36) accrued mostly from the August
  roster backfill, not the live flow. Forward, the congressional source yields roughly one
  candidate a week. Its 5-pass cap and its class-pool share are now almost entirely headroom.
- **The mechanical arm moves with it.** Qualification is the same prefilter by the 2026-08-27 ruling
  (the funnels never diverge), so mechanical intake also falls to ~1/week. The sleeve holds 30
  positions at capacity (554 `mechanical_capacity` rejections in 16 days, none above $100K), so the
  practical change is that slices freed by 367-day time exits will refill slowly. Both flagged for
  the 2026-10-15 review; implemented as ruled, no divergence built.

**2. Same-name-same-day de-duplication (loop, before the caps).** The FIRST tradeable candidate for
a symbol on a day dispatches; every later same-day candidate on that symbol writes
`stage_rejection same_name_today` naming the first decision id, spends no pass, and attaches as
convergence: the registry already has it for the batch, and on a held name it is noted on the
position (`note_add_signal`, verdict `same_name_today`) and owes a review, which runs in the same
tick like a held add verdict's does. The ledger (`AuditLog.research_symbols_on`) is seeded from the
log at startup and rolls with the day. Keyed on the candidate's OWN instrument (structured tickers
field, then extraction), never on what research later returned; a signal naming nothing is not
de-duplicated. Consequence by design: a second same-day signal on a name opened today is no longer
an add decision — it attaches; the add path is for a later day. Origin: SBLK researched three
times on 2026-09-17 on three separate Form 4 filings. Tests: `tests/test_same_name_today.py` (4);
fixtures across the suite moved above the new floor, and same-symbol batches in the budget and cap
tests now use distinct names.

**Report (no build): Form 4 cluster sample vs the singles control.**
- Clusters: 51 candidates since the source went live 2026-09-02 (16 trading days), ~3.2/day, rising
  (8–10/day this week). 12 have 5d marks (mean −0.14, 50% hit — unmeasured at that n). A 5d mark
  needs five calendar days plus a forward refresh: with `priors --refresh` on or after 2026-09-22 the
  clusters observed through 09-15 (24) resolve → **n ≥ 20 at 5d by about 2026-09-22**, ~50 by the
  09-25 weekly report. At 20d, n ≥ 20 needs clusters observed by ~09-15 marked 20 days on → **about
  2026-10-06**, inside the 10-15 review window.
- Singles (the control): 186 candidates, 98 with 5d marks, **−1.80% mean, 43% hit** — measured
  negative, grounded. C-suite singles: 4 candidates in 16 days (0.25/day), 0 with marks; at that
  pace n ≥ 20 is ~80 trading days out, so the C-suite door cannot be graded on its own by 10-15 and
  should be judged against the singles control it was carved from.
- Flagged for 2026-10-15: if clusters are not positive at n ≥ 20 while singles stay negative, the
  cluster hypothesis fails its own test; if singles stay negative regardless, the C-suite door is
  a door into a measured-negative population.

## Standing reminders
- **Calibration block golden replay owed (2026-10-07):** the `MEASURED RECORD` block renders only
  once a source × voted-band × horizon × direction cell reaches n ≥ 20 at 5d; until then every
  prompt is byte-identical and no replay was spent. When the health/weekly first shows a cell
  (`calibration block: N cells` at startup), run the golden replay and a live round trip BEFORE
  the next session, per CLAUDE.md § LLM Request-Path Changes.
- **Vote stability read (2026-10-07):** the weekly's vote lines (held / overturned by source and
  direction, transcript hashes on the records) are the first measure of per-source noise; at
  ~20 votes a source, read whether overturn rates differ by source and band before touching k
  or the margin.

- **LLM-path changes need a live round trip (2026-08-24 ruling, now in
  CLAUDE.md § LLM Request-Path Changes):** elision/caching/tool-config/model
  changes are not "shipped" until the exact production request shape — full
  search→report — has run against the real API.
- **Source-wiring check (2026-09-30 ruling):** when wiring ANY X source, verify the
  handle resolves to the intended account BEFORE the first poll — `users/by/username`
  from the droplet: follower count, bio, post volume, `protected: false` — and
  confirm `search/recent from:<handle>` returns posts. A protected or wrong-handle
  account returns clean 200s with zero items forever, and the fetcher cannot tell
  that from a quiet account. Origin: citrini polled a protected zero-follower
  stranger (@Citrini7) for five weeks, 100 polls, 0 items, 0 errors.
- **Verified pushes (2026-08-21 ruling):** "pushed to both hosts" means CHECKED,
  not attempted — after every push, `git rev-parse HEAD` must match
  `git ls-remote vps refs/heads/main` (and origin). The droplet checkout at
  /home/agentic/Agentic still needs its `git pull` — the bare repo alone is not
  what the service runs.

- **Research cost estimates 2026-09-02 → 2026-09-15 UNDERSTATE** by about one
  full pass per floor-band entry: the boundary confirmation's second pass was
  not folded into the decision's estimate until 8cc2e4c. Month-to-date and
  weekly lines over that window are low; the console bill is the truth.
- `PAPER_MODE=true`. Live needs two variables, both set by a human, and the agent must
  never set, suggest setting, or write code that sets either.
- The kill switch resets manually or not at all.
- Adding a signal source needs explicit human approval, per source.
- `data/` is gitignored and holds the audit trail and session state. It is not backed up
  by the repo and belongs in a backup routine.
