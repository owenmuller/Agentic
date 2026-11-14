"""``python -m lab`` - the separate paper accounts (PAPER PUSH, 2026-10-09 late).

  tick <account> [--dry-run]   one run of the account's engine. Real runs only
                               inside the account's own unit
                               (agentic-lab-<account>.service); --dry-run reads
                               the account and plans with live quotes, submits
                               nothing and writes only scratch.
  run <account>                the AI trader's session (its own unit only): wait
                               for the open, tick until the close.
  golden <account> [--with-brief]
                               the AI trader's golden replay: production prompt
                               and request path, no broker (costs ~$1);
                               --with-brief switches the research brief on.
  roundtrip <account>          the AI trader's order path, live, minimum size, in
                               session: one SPY share and one SPY call, bought and
                               sold through the gate; writes only scratch.
  status [<account>]           read-only: pin, kill switch, last snapshot, spend.
  scoreboard [--email]         read-only: the weekly table, every sleeve vs SPY and
                               the ladder, raw and slippage-adjusted.
  monthly-review [--month YYYY-MM] [--scheduled] [--post] [--facts-only]
                               the AI trader's monthly deep review (ruling
                               2026-11-13): facts by code, diagnosis and ranked
                               proposals by the model; --post sends it to #reports;
                               --scheduled acts only in a month's first sessions,
                               once a month (the agentic-ai-review unit).
  review-golden [--runs N]     the monthly review prompt's golden replay.
  halt <account> --reason R    operator: write the account's halt marker; its
                               next run trips its own kill switch (frozen).
  resume <account> --ack A     operator, A HUMAN ONLY: clear the halt marker and
                               reset the account's kill switch. The agent never
                               runs this.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import datahealth
import datasafety
from lab.account import LabAccount, LabRefused, LabState, Ledger, open_account
from lab.config import LabConfig
from execution import cards
from execution.alerts import Alerter, data_sink
from execution.market_data import BenchmarkMoves
from lab import reports
from lab.ladder import LadderEngine

OPERATOR_COMMANDS = frozenset({"halt", "resume", "retry-option-round-trip"})
#: How each account is named on a card (ruling 2026-10-12).
DISPLAY = {"ladder": "Leverage ladder", "ai-trader": "AI trader"}


def _snapshot(account: str) -> Path:
    """A scratch copy of the account's production data, for dry runs."""
    source = datasafety.lab_production_dir(account)
    target = datasafety.scratch_data_dir().parent / "lab-snapshot" / account
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if source.is_dir():
        for entry in source.iterdir():
            if entry.is_file() and entry.name != datasafety.MARKER:
                shutil.copy2(entry, target / entry.name)
    return target


def tick(name: str, dry_run: bool) -> int:
    config = LabConfig.load()
    if not dry_run and datasafety.current_unit() != datasafety.lab_unit(name):
        print(
            f"REFUSED: a real lab run trades the {name!r} paper account and writes its ledger; it runs only "
            f"inside {datasafety.lab_unit(name)}.service. Use --dry-run to plan without trading.",
            file=sys.stderr,
        )
        return 2
    try:
        account = open_account(name, config, data_dir=_snapshot(name) if dry_run else None)
    except LabRefused as refusal:
        print(f"LAB {name} REFUSED TO START: {refusal}", file=sys.stderr)
        return 3
    if account.config.sleeve != "leverage_ladder":
        print(f"LAB {name}: sleeve {account.config.sleeve!r} has no engine yet", file=sys.stderr)
        return 3
    # The urgent email tier (the drawdown alert, and - in the unit - the data
    # guard's refused writes and fail-open errors, ruling 2026-10-10 item 4).
    alerter = Alerter(status_path=account.data_dir / "alerts_status.json")
    label = DISPLAY.get(name, name)
    if not dry_run:
        _guard_cards(alerter, name, label)
    benchmark = BenchmarkMoves(api_key=account.api_key, api_secret=account.api_secret)
    holder: dict = {}

    def drawdown(subject: str, body: str) -> None:
        state = holder["engine"].gate.state
        start_day, start_value = reports.start_of(account.ledger.events(["snapshot"]))
        today = datetime.now(timezone.utc).astimezone(reports.NY).date()
        alerter.card(f"lab-{name}-drawdown", cards.drawdown_card(
            label, float(state.nav), float(state.high_water_mark),
            float(state.nav) / start_value - 1 if start_value else None,
            benchmark.between(start_day, today) if start_day else None))

    try:
        engine = LadderEngine(account, config.ladder, dry_run=dry_run, alert=drawdown)
        holder["engine"] = engine
        notes = engine.tick(datetime.now(timezone.utc))
        for note in notes:
            print(f"LAB {name}{' DRY RUN' if dry_run else ''}: {note}")
        if not dry_run:
            # The rebalance report (#reports) after a run that rebalanced or
            # traded; a snapshot-only run sends nothing (ruling 2026-10-12).
            today = datetime.now(timezone.utc).astimezone(reports.NY).date()
            card = reports.ladder_report(engine, account.ledger.events(),
                                         {rung.name: rung.label for rung in config.ladder.rungs},
                                         benchmark.between, today)
            if card is not None:
                alerter.card(f"lab-{name}-rebalance-{today}-{engine.rebalance}", card)
    finally:
        alerter.close()
    return 0


def _guard_cards(alerter: Alerter, name: str, label: str) -> None:
    """The data guard's production events for this unit, as red cards."""
    from lab.ai_trader import NY

    def sink(kind: str, message: str) -> None:
        local = datetime.now(timezone.utc).astimezone(NY)
        alerter.card(f"lab-{name}-datasafety-{kind}", cards.guard_card(
            label, "REFUSED" in kind, f"{local:%b} {local.day}, {local:%H:%M} ET", message))

    datasafety.set_alert_sink(sink)


def status(names: list[str]) -> int:
    config = LabConfig.load()
    for name in names or list(config.accounts):
        account = config.account(name)
        directory = datasafety.lab_production_dir(name)
        state = LabState.load(directory / "state.json")
        ledger = Ledger(directory / "ledger.jsonl", lambda: datetime.now(timezone.utc))
        snapshots = ledger.events(["snapshot"])
        faults = ledger.events(["fault"])
        print(f"{name}: {account.label} - {'ENABLED' if account.enabled else 'not enabled'}"
              f"{'' if account.enabled else ' (' + (account.status or '') + ')'}")
        print(f"  live eligible: {account.live_eligible}; pinned broker account: {state.account_number or '(not yet)'}")
        print(f"  kill switch: {'TRIPPED ' + (state.trip_reason or '') if state.kill_switch_tripped else 'clear'}; "
              f"halt marker: {'PRESENT' if (directory / 'HALT').exists() else 'none'}; "
              f"high-water mark: {state.high_water_mark}")
        passes = ledger.events(["llm_pass"])
        if passes:
            # Actual LLM spend, by day (spec: "report actual"): token estimate
            # from research.yaml pricing plus the per-search fee.
            by_day: dict[str, list] = {}
            for event in passes:
                by_day.setdefault(event["at"][:10], []).append(event)
            for day in sorted(by_day)[-5:]:
                rows = by_day[day]
                total = sum(float(e["cost_usd"]) for e in rows)
                print(f"  LLM spend {day}: ${total:.2f} over {len(rows)} pass(es), "
                      f"{sum(int(e.get('searches', 0)) for e in rows)} searches")
        fills = [e for e in ledger.events(["settled"]) if float(e.get("filled_qty") or 0) > 0]
        if fills:
            print(f"  fills: {len(fills)}; last {min(len(fills), 10)}:")
            for event in fills[-10:]:
                mid = event.get("modelled")
                print(f"    {event['at'][:19]} {event['side']:4} {event['symbol']} {event['filled_qty']} at "
                      f"{event['avg_price']} (limit {event.get('limit')}, mid {mid}, {event.get('status')}"
                      f"{', ' + event['reason'] if event.get('reason') else ''})")
        if snapshots:
            last = snapshots[-1]
            print(f"  last snapshot {last['at']}: equity {last.get('equity')}, rungs {json.dumps(last.get('rungs'))}")
            if last.get("gross_face") is not None:
                print(f"  gross exposure: face {float(last['gross_face']):.0%} of equity, leverage-adjusted "
                      f"{float(last['gross_leverage_adjusted']):.0%} (the cap enforces on {config.ai_trader.gross_basis if config.ai_trader else 'face'})")
        if faults:
            print(f"  faults: {len(faults)}, last: {faults[-1]['at']} {faults[-1]['message']}")
    return 0


def halt(name: str, reason: str) -> int:
    if not reason.strip():
        print("a halt needs --reason", file=sys.stderr)
        return 2
    directory = datasafety.resolve_lab_data_dir(name)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (directory / "HALT").write_text(f"{reason.strip()} (halted_at={now})\n", encoding="utf-8")
    print(f"LAB {name}: halt marker written; its next run trips its kill switch and freezes the account. "
          f"Resuming is a human's `python -m lab resume {name} --ack ...`.")
    return 0


def retry_option_round_trip(reason: str) -> int:
    """The live mirror's failed option round trip starts over at its next
    session start (a marker beside HALT; the running session never races it)."""
    from lab.live_mirror import RETRY_OPTION_RT

    if not reason.strip():
        print("a retry needs --reason", file=sys.stderr)
        return 2
    directory = datasafety.resolve_lab_data_dir("live-mirror")
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (directory / RETRY_OPTION_RT).write_text(f"{reason.strip()} (requested_at={now})\n", encoding="utf-8")
    print("LIVE mirror: retry marker written; the option round trip starts over at the next session start "
          "(from its window, with its full attempts).")
    return 0


def resume(name: str, ack: str) -> int:
    """FOR A HUMAN OPERATOR ONLY (CLAUDE.md: resume requires manual human
    reset). Clears the marker and the trip; the high-water mark re-baselines
    to the account's last snapshot equity, as the main gate's reset does."""
    if not ack.strip():
        print("a resume needs --ack naming the human who resets it", file=sys.stderr)
        return 2
    directory = datasafety.resolve_lab_data_dir(name)
    state = LabState.load(directory / "state.json")
    ledger = Ledger(directory / "ledger.jsonl", lambda: datetime.now(timezone.utc))
    snapshots = ledger.events(["snapshot"])
    (directory / "HALT").unlink(missing_ok=True)
    was = state.kill_switch_tripped
    state.kill_switch_tripped = False
    state.tripped_at = state.trip_reason = None
    if snapshots and snapshots[-1].get("equity"):
        state.high_water_mark = str(snapshots[-1]["equity"])
    state.save(directory / "state.json")
    ledger.append("kill_switch_reset", acknowledgement=ack.strip(), was_tripped=was,
                  high_water_mark=state.high_water_mark)
    print(f"LAB {name}: kill switch {'reset' if was else 'was clear'}; halt marker cleared; "
          f"high-water mark {state.high_water_mark}")
    return 0



# -- 2b, the AI trader (ruling 2026-10-10, item 5) ----------------------------------

def _ai_parts(account: LabAccount, config: LabConfig):
    from execution import AlpacaPriceSource
    from execution.options_data import AlpacaOptionsChain
    from lab.llm import LabLLM

    prices = AlpacaPriceSource(api_key=account.api_key, api_secret=account.api_secret)
    # (bid, ask) of ONE quote (2026-11-12: two reads could pair one quote's
    # ask with another's bid), for pricing the orders themselves
    quotes = prices.sides
    chain = AlpacaOptionsChain(api_key=account.api_key, api_secret=account.api_secret)
    return quotes, chain, LabLLM(config.ai_trader)


def _ai_reader(quotes, account: LabAccount):
    """The quote rule for stops, targets and levels (human ruling 2026-11-12,
    item 1): the main book's reader over one quote and the last trade."""
    from execution import AlpacaPriceSource
    from execution.quotes import QuoteReader

    prices = AlpacaPriceSource(api_key=account.api_key, api_secret=account.api_secret)
    return QuoteReader(lambda symbol: None, sides=quotes, last_trade=prices.last_trade, engine="AI trader")


def _catalyst_clock(account: LabAccount, config: LabConfig, calendar):
    """Move since public (ruling 2026-11-12, item 4), logged only: the news
    and bars on Alpaca's data host, with this account's own keys."""
    import httpx

    from execution.market_data import DATA_BASE_URL
    from lab.catalyst_clock import CatalystClock

    client = httpx.Client(base_url=DATA_BASE_URL, timeout=15.0, headers={
        "APCA-API-KEY-ID": account.api_key, "APCA-API-SECRET-KEY": account.api_secret})
    return CatalystClock(config.ai_trader.move_since_public, client, calendar=calendar)


def brief_renderer(ai_config):
    """The research brief (ruling 2026-10-15): the main book's momentum files
    (read only) and the committed evidence library, re-read at every scan."""
    from pathlib import Path

    from lab import brief

    evidence = Path(__file__).resolve().parents[2] / ai_config.brief_evidence_path
    return lambda: brief.render(datasafety.production_data_dir(), evidence)


def run_session(name: str) -> int:
    """The AI trader's session: wait for the open, tick until the close."""
    import time as _time

    from lab.ai_trader import AiTrader

    config = LabConfig.load()
    if datasafety.current_unit() != datasafety.lab_unit(name):
        print(f"REFUSED: the {name!r} session trades its paper account and writes its ledger; it runs only "
              f"inside {datasafety.lab_unit(name)}.service.", file=sys.stderr)
        return 2
    if config.ai_trader is None or config.account(name).sleeve != "ai_trader":
        print(f"LAB {name}: not an AI-trader account", file=sys.stderr)
        return 3
    try:
        account = open_account(name, config)
    except LabRefused as refusal:
        print(f"LAB {name} REFUSED TO START: {refusal}", file=sys.stderr)
        return 3
    alerter = Alerter(status_path=account.data_dir / "alerts_status.json")
    label = DISPLAY.get(name, name)
    _guard_cards(alerter, name, label)
    benchmark = BenchmarkMoves(api_key=account.api_key, api_secret=account.api_secret)
    quotes, chain, llm = _ai_parts(account, config)
    holder: dict = {}

    def kill_switch(subject: str, body: str) -> None:
        state = holder["trader"].gate.state
        snap = account.adapter.account_snapshot()
        prior = float(snap.get("last_equity") or 0)
        alerter.card(f"lab-{name}-kill-switch", cards.kill_switch_card(
            label, float(state.nav), float(state.high_water_mark),
            float(snap.get("equity") or 0) / prior - 1 if prior > 0 else None, benchmark.today(),
            f'python -m lab resume {name} --ack "your name"'))

    def leverage_watch(adjusted, face, contributors) -> None:
        # ruling 2026-10-14 (third): information to #urgent, never a block
        # one key per crossing: the 4-hour resend window must not swallow a second one
        alerter.card(f"lab-{name}-leverage-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}", cards.leverage_card(
            label, float(adjusted), float(face), [(s, float(f), float(share)) for s, f, share in contributors],
            float(config.ai_trader.leverage_alert_above), float(config.ai_trader.leverage_alert_rearm_below)))

    calendar = _calendar(account.api_key, account.api_secret, f"{label} calendar")
    trader = AiTrader(account, config.ai_trader, llm=llm, quotes=quotes, chain=chain, alert=kill_switch,
                      leverage_alert=leverage_watch, brief=brief_renderer(config.ai_trader),
                      calendar=calendar, reader=_ai_reader(quotes, account),
                      catalyst_clock=_catalyst_clock(account, config, calendar))
    holder["trader"] = trader
    try:
        clock = account.adapter.market_clock()
        now = datetime.now(timezone.utc)
        if not clock.get("is_open"):
            opens = datetime.fromisoformat(str(clock["next_open"]).replace("Z", "+00:00"))
            if (opens - now).total_seconds() > 8 * 3600:
                print(f"LAB {name}: no session today (next open {opens.isoformat()})")
                return 0
            print(f"LAB {name}: waiting for the open at {opens.isoformat()}")
            _time.sleep(max(0.0, (opens - now).total_seconds()))
            clock = account.adapter.market_clock()
        closes = datetime.fromisoformat(str(clock["next_close"]).replace("Z", "+00:00"))
        print(f"LAB {name}: session open; ticking every {config.ai_trader.tick_seconds}s until {closes.isoformat()}")
        last_snapshot = 0.0
        while datetime.now(timezone.utc) < closes:
            try:
                for note in trader.tick(datetime.now(timezone.utc)):
                    print(f"LAB {name}: {note}", flush=True)
            except Exception as error:  # noqa: BLE001 - one bad tick must not end the session
                logging.getLogger("lab").exception("tick failed")
                account.ledger.append("fault", message=f"tick failed: {type(error).__name__}: {error}")
                local = datetime.now(timezone.utc).astimezone(reports.NY)
                alerter.card(f"lab-{name}-tick", cards.crash_card(
                    label, f"{local:%b} {local.day}, {local:%H:%M} ET", f"{type(error).__name__}: {error}"))
            if _time.monotonic() - last_snapshot > 900:
                _ai_snapshot(account, trader)
                last_snapshot = _time.monotonic()
            _time.sleep(config.ai_trader.tick_seconds)
        _ai_snapshot(account, trader)
        print(f"LAB {name}: session closed")
        # End of day (#reports; red if the kill switch is on) - ruling 2026-10-12.
        today = datetime.now(timezone.utc).astimezone(reports.NY).date()
        alerter.card(f"lab-{name}-eod-{today}", reports.ai_day_report(
            account.ledger.events(), account.adapter.account_snapshot(), list(trader.positions().values()),
            benchmark.today(), float(config.ai_trader.daily_spend_cap_usd), account.state.kill_switch_tripped,
            today))
        return 0
    finally:
        alerter.close()


def _ai_snapshot(account: LabAccount, trader) -> None:
    from lab.ai_trader import NY

    snap = account.adapter.account_snapshot()
    today = datetime.now(timezone.utc).astimezone(NY).date()
    # Gross exposure, face and leverage-adjusted, as fractions of equity
    # (ruling 2026-10-14, second, item 2); None before the first tick's gate.
    gross_face = gross_adjusted = None
    equity = float(snap.get("equity") or 0)
    if trader.gate is not None and equity > 0:
        face, adjusted = trader.gross()
        gross_face, gross_adjusted = round(float(face) / equity, 4), round(float(adjusted) / equity, 4)
    account.ledger.append(
        "snapshot", equity=snap.get("equity"), cash=snap.get("cash"),
        positions=[{"symbol": p.trade_symbol, "instrument": p.instrument, "quantity": p.quantity}
                   for p in trader.positions().values()],
        spend_today=trader.spent_today(today), high_water_mark=account.state.high_water_mark,
        kill_switch=account.state.kill_switch_tripped,
        gross_face=gross_face, gross_leverage_adjusted=gross_adjusted,
    )


def golden(name: str, with_brief: bool = False) -> int:
    """The AI trader's golden replay: production prompt, production request
    path, no broker. Also the live search->report round trip. ``--with-brief``
    runs it with the research brief switched on (its pre-ship replay, ruling
    2026-10-15), whatever lab.yaml says."""
    from lab import golden as golden_replay
    from lab.ai_trader import NY
    from lab.llm import LabLLM

    config = LabConfig.load()
    if config.ai_trader is None:
        print("no ai_trader config", file=sys.stderr)
        return 3
    # the replay grades the brief arm: every case sends it
    ai = config.ai_trader.model_copy(update={"brief_mode": "always"}) if with_brief else config.ai_trader
    renderer = brief_renderer(ai) if ai.brief_mode != "off" else None
    if renderer is not None:
        sample = renderer()
        print(f"research brief: {sample.chars} chars, ~{sample.tokens_estimate} tokens (~2.6 chars a token), "
              f"sha {sample.sha}, sections {sample.sections}")
    today = datetime.now(timezone.utc).astimezone(NY).date()
    results = golden_replay.run(ai, LabLLM(ai), today, brief=renderer)
    print(golden_replay.render(results, golden_replay.next_session(today)))
    return 0 if all(r.passed for r in results) else 1


def roundtrip(name: str) -> int:
    """The order path, live, at minimum size, in the AI trader's paper
    account: buy then sell one SPY share and one SPY call (the rules engine's
    contract pick), each through the account's gate. Market hours only.
    Writes only scratch; the account ends flat in what it touched."""
    from datetime import timedelta
    from decimal import ROUND_UP, Decimal

    from lab.ai_trader import NY, AiTrader, Position, select_contract
    from risk_gate.schema import EquityBuyOrder, LimitExecution, OptionBuyToOpenOrder

    config = LabConfig.load()
    try:
        account = open_account(name, config, data_dir=_snapshot(name), allow_disabled=True)
    except LabRefused as refusal:
        print(f"LAB {name} REFUSED: {refusal}", file=sys.stderr)
        return 3
    if not account.adapter.market_clock().get("is_open"):
        print(f"LAB {name}: the market is closed; the round trip runs in session", file=sys.stderr)
        return 3
    quotes, chain, _ = _ai_parts(account, config)
    trader = AiTrader(account, config.ai_trader, llm=None, quotes=quotes, chain=chain)
    now = datetime.now(timezone.utc)
    today = now.astimezone(NY).date()
    trader.rebuild_gate(today)
    cents = Decimal("0.01")
    quote = quotes("SPY")
    if quote is None:
        print("no SPY quote", file=sys.stderr)
        return 3
    bid, ask = quote
    stock = Position(pid="rt-stock", symbol="SPY", instrument="etf", horizon="intraday", stop=Decimal("0"),
                     target=Decimal("0"), thesis="round trip", opened_at=now.isoformat())
    limit = (ask * Decimal("1.002")).quantize(cents, rounding=ROUND_UP)
    account.ledger.append("position_opened", pid=stock.pid, symbol="SPY", instrument="etf", horizon="intraday",
                          stop="0", target="0", thesis="round trip")
    stock.quantity = trader.work(EquityBuyOrder(symbol="SPY", quantity=Decimal("1"), sleeve="aggressive",
                                                execution=LimitExecution(limit_price=limit)),
                                 stock, "buy", limit, (bid + ask) / 2, reason="round_trip") or Decimal("0")
    print(f"stock leg: bought {stock.quantity} SPY at limit {limit} (mid {(bid + ask) / 2:.4f})")
    if stock.quantity:
        trader.close(stock, "round_trip", now)
    high = config.ai_trader.options.dte["intraday"][1]
    pick, why = select_contract(chain.chain_for("SPY", min_expiry=today, max_expiry=today + timedelta(days=high)),
                                "call", today, config.ai_trader.options)
    if pick is None:
        print(f"option leg: no contract ({why})")
    else:
        olimit = (pick.ask * Decimal("1.002")).quantize(cents, rounding=ROUND_UP)
        option = Position(pid="rt-option", symbol="SPY", instrument="call", horizon="intraday", stop=Decimal("0"),
                          target=Decimal("0"), thesis="round trip", opened_at=now.isoformat(), occ=pick.occ_symbol,
                          expiry=pick.expiration, right=pick.right, strike=pick.strike)
        account.ledger.append("position_opened", pid=option.pid, symbol="SPY", instrument="call", horizon="intraday",
                              stop="0", target="0", thesis="round trip", occ=pick.occ_symbol,
                              expiry=pick.expiration.isoformat(), right=pick.right, strike=str(pick.strike))
        order = OptionBuyToOpenOrder(symbol=pick.occ_symbol, underlying="SPY", right=pick.right,
                                     expiration=pick.expiration, strike=pick.strike, contracts=1,
                                     multiplier=pick.multiplier, execution=LimitExecution(limit_price=olimit))
        option.quantity = trader.work(order, option, "buy", olimit, pick.mid, reason="round_trip") or Decimal("0")
        print(f"option leg: bought {option.quantity} {pick.occ_symbol} at limit {olimit} (mid {pick.mid:.4f})")
        if option.quantity:
            trader.close(option, "round_trip", now)
    for event in account.ledger.events(["settled"]):
        print(f"  settled {event['side']} {event['symbol']}: {event['filled_qty']} at {event['avg_price']} "
              f"(limit {event['limit']}, modelled {event['modelled']}, {event['status']})")
    faults = account.ledger.events(["fault"])
    for event in faults:
        print(f"  FAULT {event['message']}")
    left = [p.symbol for p in account.adapter.get_positions() if p.symbol.startswith("SPY")]
    print(f"left holding after the round trip: {left or 'nothing in SPY'}")
    return 0 if not left and not faults else 1


# -- items 3 and 4: the weekly scoreboard ------------------------------------------

def scoreboard(email: bool) -> int:
    """Read-only: every lab sleeve, the execution test and the main book
    against SPY and the leverage ladder. ``--email`` sends it on the DAILY
    tier (the Friday weekly unit runs it)."""
    from datetime import timedelta

    from execution import AlpacaAdapter, AlpacaDailyBars
    from execution.market_data import UnservedSymbols
    from lab import scoreboard as board_mod
    from lab.ai_trader import NY

    config = LabConfig.load()

    def events(name: str) -> list[dict]:
        return board_mod.read_jsonl(datasafety.lab_production_dir(name) / "ledger.jsonl")

    ladder_events = events("ladder")
    ai_events = events("ai-trader") if "ai-trader" in config.accounts else []
    main_data = datasafety.production_data_dir()
    exec_lines = board_mod.read_jsonl(main_data / "execution_test_daily.jsonl") if main_data else []
    today = datetime.now(timezone.utc).astimezone(NY).date()
    stamps = [e["at"] for e in ladder_events + ai_events if e.get("event") == "snapshot"]
    start = min([datetime.fromisoformat(s).astimezone(NY).date() for s in stamps]
                + [date_from(line["day"]) for line in exec_lines] + [today]) - timedelta(days=7)
    spy: dict = {}
    for bar in AlpacaDailyBars(unserved=UnservedSymbols()).bars(
            # two weeks earlier, so a close sits on or before every row's first day
            "SPY", datetime.combine(start - timedelta(days=14), datetime.min.time(), timezone.utc),
            datetime.now(timezone.utc)):
        spy[date_from(str(bar["t"])[:10])] = float(bar["c"])
    # the control beside the AI trader (ruling 2026-10-15, item 4): the main
    # book's momentum sleeve (its daily line) and SPMO
    momentum = board_mod.momentum_series(
        board_mod.read_jsonl(main_data / "momentum" / "daily.jsonl") if main_data else [])
    spmo: dict = {}
    try:
        for bar in AlpacaDailyBars(unserved=UnservedSymbols()).bars(
                "SPMO", datetime.combine(start - timedelta(days=14), datetime.min.time(), timezone.utc),
                datetime.now(timezone.utc)):
            spmo[date_from(str(bar["t"])[:10])] = float(bar["c"])
    except Exception as error:  # noqa: BLE001 - the SPMO column is optional on the board
        print(f"(SPMO closes unavailable: {error})", file=sys.stderr)
    main_equity: dict = {}
    try:
        with AlpacaAdapter() as main:
            history = main.portfolio_history(start.isoformat())
        for stamp, equity in zip(history.get("timestamp") or [], history.get("equity") or []):
            if equity:
                main_equity[datetime.fromtimestamp(int(stamp), timezone.utc).astimezone(NY).date()] = float(equity)
    except Exception as error:  # noqa: BLE001 - the main line is optional on the board
        print(f"(main book history unavailable: {error})", file=sys.stderr)
        datahealth.report("lab scoreboard", f"the main book's history is unavailable ({type(error).__name__}); "
                                            f"its row reads n/a")
    # an option on a fund is a market bet, not a stock pick (ruling 2026-11-04,
    # item 7): the broker's asset name decides, read-only
    with AlpacaAdapter() as broker:
        board = board_mod.build(
            ladder_events=ladder_events, ai_events=ai_events, exec_lines=exec_lines, spy=spy,
            main_equity=dict(sorted(main_equity.items())),
            rung_labels={rung.name: rung.label for rung in config.ladder.rungs},
            stop_floor=float(config.ai_trader.min_stop_fraction) if config.ai_trader else None,
            leverage_table=dict(config.ai_trader.leverage_factors) if config.ai_trader else None,
            momentum=momentum, spmo=dict(sorted(spmo.items())),
            fund=board_mod.fund_lookup(broker.asset_name),
        )
    text = board_mod.render(board, today)
    print(text)
    if email:
        alerter = Alerter()
        # #reports: one plain line per account and whether it is too early to
        # judge; the full table attached (ruling 2026-10-12).
        alerter.card(f"lab-scoreboard-{today}", reports.scoreboard_report(
            board, today, text, board_mod.GRADE_AT, board_mod.VERDICT_AT))
        alerter.close()
    return 0


# -- the LIVE mirror (GO LIVE, human ruling 2026-11-04) --------------------------------

LIVE = "live-mirror"


def _live_parts(config):
    """The read-only client, and the order-capable adapter ONLY when this
    process resolves live under Constraint #4 (else None: the mirror runs
    dry). A half-set live flag raises here and the process stops."""
    import os

    from execution import require_paper_or_confirmed_live
    from execution.robinhood import RobinhoodAgenticAdapter, RobinhoodMcp, TokenStore

    store = TokenStore(Path(os.path.expanduser(config.token_store)))
    reader = RobinhoodMcp(store, config.account_number)
    if require_paper_or_confirmed_live():
        return reader, None
    return reader, RobinhoodAgenticAdapter(RobinhoodMcp(store, config.account_number, allow_orders=True))


def live_check() -> int:
    """Read-only: the Agentic account as the mirror sees it. Never sends an order."""
    from execution.robinhood import RobinhoodMcp, TokenStore, settled_cash
    import os

    config = LabConfig.load().live_mirror
    mcp = RobinhoodMcp(TokenStore(Path(os.path.expanduser(config.token_store))), config.account_number)
    row, portfolio = mcp.account_row(), mcp.portfolio()
    positions = mcp.equity_positions()
    print(f"LIVE {config.account_number}: type {row.get('type')}, state {row.get('state')}, "
          f"{row.get('option_level')}; cash {portfolio.get('cash')}, settled {settled_cash(portfolio, row)}, "
          f"unsettled {row.get('unsettled_funds')}, pending deposits {portfolio.get('pending_deposits')}, "
          f"total {portfolio.get('total_value')}; buying power {portfolio.get('buying_power')}; "
          f"positions {len(positions)}; trade approvals {'ON' if mcp.approvals_required() else 'off'}")
    return 0


def _calendar(api_key: str, api_secret: str, source: str):
    """The exchange calendar from Alpaca's paper trading API (sweep
    2026-11-10): holidays and early closes for every session rule."""
    from execution import PAPER_BASE_URL
    from execution.market_calendar import MarketCalendar, alpaca_fetch

    return MarketCalendar(alpaca_fetch(PAPER_BASE_URL, api_key, api_secret),
                          lambda: datetime.now(timezone.utc), source=source)


def live_session() -> int:
    """The live mirror's session, inside agentic-lab-live-mirror only."""
    import time as _time

    from lab.config import AccountConfig, account_limits
    from lab.live_mirror import LiveMirror, NY, _clock_time

    from execution import load_environment
    from execution.robinhood import RobinhoodAuthLost

    load_environment()   # refuses a .env that carries a live flag (ruling 2026-11-04)
    lab_config = LabConfig.load()
    config = lab_config.live_mirror
    if datasafety.current_unit() != datasafety.lab_unit(LIVE):
        print(f"REFUSED: the live mirror runs only inside {datasafety.lab_unit(LIVE)}.service.", file=sys.stderr)
        return 2
    if config is None or not config.enabled or lab_config.ai_trader is None:
        print("LIVE mirror: not enabled in config/lab.yaml", file=sys.stderr)
        return 3
    reader, broker = _live_parts(config)
    directory = datasafety.resolve_lab_data_dir(LIVE)
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    state = LabState.load(directory / "state.json")
    if state.account_number is None:
        state.account_number = config.account_number
    elif state.account_number != config.account_number:
        print(f"LIVE mirror pinned to {state.account_number}, config says {config.account_number}: refusing",
              file=sys.stderr)
        return 3
    alerter = Alerter(status_path=directory / "alerts_status.json")
    import os

    from lab.account import key_variables

    key_name, secret_name = key_variables(config.paper_account)   # read-only: the exchange calendar
    calendar = _calendar(os.environ.get(key_name, ""), os.environ.get(secret_name, ""), "Live mirror calendar")
    limits = account_limits(AccountConfig(enabled=True, label="LIVE MIRROR", sleeve="live_mirror",
                                          live_eligible=True, limits_overrides=config.limits_overrides))
    mirror = LiveMirror(
        config=config, ai_config=lab_config.ai_trader, reader=reader, broker=broker,
        paper_ledger=datasafety.lab_production_dir(config.paper_account) / "ledger.jsonl",
        ledger=Ledger(directory / "ledger.jsonl", clock), state=state, state_path=directory / "state.json",
        limits=limits, notify=lambda card: alerter.card(f"live-{card.happened}-{clock():%Y%m%dT%H%M%S%f}", card),
        clock=clock, halt_path=directory / "HALT", calendar=calendar)
    local = clock().astimezone(NY)
    session = calendar.session(local.date())
    if session is None:
        print(f"LIVE mirror: no session on {local.date()} (exchange calendar)")
        return 0
    start = datetime.combine(local.date(), _clock_time(config.session_start), NY)
    # an early close ends the session early; a calendar that cannot be read
    # (assumed session) keeps the full day, so exits stay watched
    end = (datetime.combine(local.date(), _clock_time(config.session_end), NY) if session.assumed
           else session.at(config.session_end))
    print(f"LIVE mirror: {'LIVE' if broker is not None else 'DRY (paper process: nothing is sent)'}; "
          f"session {start:%H:%M}-{end:%H:%M} ET")
    if clock() < start:
        _time.sleep((start - clock()).total_seconds())
    # the first tick starts the session (mirror.start), inside the loop's guard:
    # a broker error at the open is retried at the next tick, never fatal
    while clock() < end:
        try:
            mirror.tick()
        except RobinhoodAuthLost as error:   # the grant is gone: halt entries, one red card
            logging.getLogger("lab").error("live mirror: the Robinhood grant was refused: %s", error)
            mirror.ledger.append("fault", message=f"tick failed: {type(error).__name__}: {error}")
            mirror.grant_lost(error)
        except Exception as error:  # noqa: BLE001 - one bad tick must not end the session
            logging.getLogger("lab").exception("live mirror tick failed")
            mirror.ledger.append("fault", message=f"tick failed: {type(error).__name__}: {error}")
            when = clock().astimezone(NY)
            alerter.card("live-mirror-tick", cards.crash_card("Live mirror", f"{when:%b} {when.day}, {when:%H:%M} ET",
                                                              f"{type(error).__name__}: {error}"))
        _time.sleep(config.tick_seconds)
    if mirror.e.get("phase") == "mirroring":
        card = mirror.daily_card()
        if card is not None and broker is not None:
            alerter.card(f"live-mirror-day-{local.date()}", card)
    alerter.close()
    return 0


def monthly_review(month: Optional[str], scheduled: bool, post: bool, facts_only: bool,
                   golden_out: Optional[Path] = None) -> int:
    """The AI trader's monthly deep review (human ruling 2026-11-13, item 1).
    Read-only: the AI trader's and the live mirror's ledgers and the main
    book's learning files are read as files; the report goes to #reports
    (``--post``) and to ``<scratch>/reviews/``. Proposals wait for a ruling."""
    import os
    from datetime import timedelta

    from execution.market_data import AlpacaDailyBars, AlpacaMinuteBars
    from lab import monthly_review as review
    from lab import review_shadows as shadows
    from lab.ai_trader import NY
    from research.client import AnthropicResearchClient
    from research.config import ResearchConfig
    from risk_gate.sectors import SectorMap

    config = LabConfig.load()
    ai = config.ai_trader
    if ai is None:
        print("no AI trader configured", file=sys.stderr)
        return 1
    minute = AlpacaMinuteBars()       # loads the environment: the main book's data keys
    now = datetime.now(timezone.utc)
    today = now.astimezone(NY).date()
    reviews = datasafety.scratch_data_dir().parent / "reviews"
    if scheduled:
        calendar = _calendar(os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_API_SECRET", ""),
                             "monthly review calendar")
        # the first session, or the next four if the timer fired late (Persistent=true)
        if today not in review.first_sessions(calendar, today):
            print(f"{today} is not one of its month's first sessions; nothing to do")
            return 0
        month = review.previous_month(today)
        if (reviews / f"ai-trader-{month}.done").exists():
            print(f"the {month} review was already sent")
            return 0
    month = month or review.previous_month(today)
    start, end = review.month_bounds(month)
    ledger = datasafety.lab_production_dir("ai-trader") / "ledger.jsonl"
    if not ledger.exists():
        print(f"no AI trader ledger at {ledger}", file=sys.stderr)
        return 1
    events = review.read_jsonl(ledger)
    from lab.account import corrected

    live = corrected(review.read_jsonl(datasafety.lab_production_dir(LIVE) / "ledger.jsonl"))
    if not live:
        datahealth.report("AI trader monthly review", "the live mirror's ledger is missing or empty; its section "
                                                      "reads empty")
    main = datasafety.production_data_dir()
    grades = review.read_jsonl(main / "learning" / "idea_grades.jsonl") if main else []
    journal = review.read_jsonl(main / "learning" / "journal.jsonl") if main else []
    if not grades:
        datahealth.report("AI trader monthly review", "no idea grades from the learning run; the learning facts "
                                                      "read empty")
    symbols = sorted({e["symbol"] for e in events if e.get("event") == "position_opened"} | {"SPY"})
    raw = AlpacaDailyBars().bars_many(symbols, datetime.combine(start - timedelta(days=40), datetime.min.time(),
                                                                timezone.utc), now)
    daily = {s: {date_from(str(b["t"])): (float(b["o"]), float(b["c"])) for b in rows} for s, rows in raw.items()}
    if not daily.get("SPY"):
        print("no SPY bars: nothing can be measured against SPY", file=sys.stderr)
        return 1
    calendar = _calendar(os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_API_SECRET", ""),
                         "monthly review calendar")
    window = review.in_window(events, start, end)
    sim = shadows.Simulator(lambda symbol, a, b: minute.bars(symbol, a, b, "sip"), calendar,
                            intraday_close=ai.intraday_close, horizon_sessions=dict(ai.horizon_sessions), now=now,
                            report=datahealth.report)
    try:
        shadow_text = shadows.report(window, sim, today)
    finally:
        minute.close()
    sectors = SectorMap.load()
    facts, done = review.build_facts(events, live, start, end, lambda s: daily.get(s, {}),
                                     sector_of=sectors.sector_of, shadow_text=shadow_text, grades=grades,
                                     journal=journal)
    if golden_out is not None:
        # a golden case from a real month (the review prompt's replay set)
        import yaml

        golden_out.write_text(yaml.safe_dump({"id": f"month_{month}", "month": month,
                                              "facts": [[section, text] for _, section, text in facts.items],
                                              "trades": review.trade_table(done, dollars=False),
                                              "rules": review.rules_in_force(ai), "min": 1, "max": review.MAX_PROPOSALS},
                                             sort_keys=False, width=120, allow_unicode=True), encoding="utf-8")
        print(f"golden case written to {golden_out}")
    call = None
    if not facts_only:
        client = AnthropicResearchClient(ResearchConfig.load())

        def call(system: str, user: str, tool: dict):
            return client.single_call(system=system, user=user, tool=tool, model=ai.model, max_tokens=4000)

    diagnosis, proposals, dropped, cost, model = review.run(month, facts, done, review.rules_in_force(ai), call)
    text = review.render(month, facts, done, diagnosis, proposals, dropped, shadow_text, cost, model)
    label = date_from(month + "-01").strftime("%B %Y")
    won = sum(1 for t in done if t.pnl > 0)
    excess = [t.excess for t in done if t.excess is not None]
    trades_line = (f"Round trips closed: {len(done)}, {won} won, net {cards.usd(sum(t.pnl for t in done), signed=True)}"
                   + (f", {sum(excess) / len(excess) * 100:+.1f} points a trade against SPY" if excess else "") + ".")
    card = cards.monthly_review_card(label, trades_line, review.short(diagnosis), [p.title for p in proposals],
                                     cost, text, dropped=len([d for d in dropped if not d.startswith("facts only")]))
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"ai-trader-{month}.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n(saved to {reviews / f'ai-trader-{month}.txt'})")
    if post or scheduled:
        alerter = Alerter()
        alerter.card(f"ai-trader-review-{month}", card)
        alerter.close()
        (reviews / f"ai-trader-{month}.done").write_text(now.isoformat(), encoding="utf-8")
    return 0


def review_golden(runs: int) -> int:
    """The monthly review prompt's golden replay (a prompt on the request path:
    CLAUDE.md § LLM Request-Path Changes). Each case's facts, trades and rules
    are a real month's, rendered by ``monthly-review --facts-only``; a run
    passes when the report parses, every proposal cites existing facts, none is
    dropped, the count is within the case's bounds and every proposal names a
    shadow test."""
    import yaml

    from execution import load_environment
    from lab import monthly_review as review
    from research.client import AnthropicResearchClient
    from research.config import ResearchConfig

    config = LabConfig.load().ai_trader
    load_environment()
    client = AnthropicResearchClient(ResearchConfig.load())
    path = Path(__file__).resolve().parents[2] / "config" / "golden" / "lab_monthly_review.yaml"
    golden = yaml.safe_load(path.read_text(encoding="utf-8"))
    print(f"monthly review golden: {path} on {config.model}, {runs} run(s) a case; "
          f"prompt {review.__file__}")
    failed, spend = [], 0.0
    for item in golden["cases"]:
        facts = review.Facts()
        for section, text in item["facts"]:
            facts.add(section, text)
        passes = 0
        for _ in range(runs):
            result = client.single_call(system=review.SYSTEM, user=review.user_prompt(
                item["month"], facts, item["trades"], item["rules"]), tool=review.tool(), model=config.model,
                max_tokens=4000)
            spend += float(result.est_cost_usd or 0)
            diagnosis, proposals, dropped = review.parse(result.structured, facts)
            text = " ".join(f"{p.title} {p.change}" for p in proposals).lower()
            banned = [w for w in item.get("banned_words", []) if w in text]
            ok = (bool(diagnosis) and not dropped and item["min"] <= len(proposals) <= item["max"]
                  and all(p.shadow_test for p in proposals) and not banned)
            passes += ok
            print(f"  {item['id']:24} {'PASS' if ok else 'FAIL'}  {len(proposals)} proposal(s), dropped {len(dropped)}"
                  + (f", banned words {banned}" if banned else ""))
            print(f"      diagnosis: {diagnosis[:400]}")
            for p in proposals:
                print(f"      {p.rank}. {p.title} [{', '.join(p.evidence)}]: {p.change[:220]}")
                print(f"         shadow: {p.shadow_test[:220]}")
            for d in dropped:
                print(f"      dropped: {d}")
        if passes * 2 <= runs:
            failed.append(item["id"])
    print(f"monthly review golden: {len(golden['cases']) - len(failed)}/{len(golden['cases'])} cases pass; "
          f"spend ${spend:.2f}" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def review_shadows() -> int:
    """Read-only: the AI trader review's shadows scored (human rulings
    2026-11-12) - for the 10-session read after the exit shadow's last day."""
    import os

    from execution.market_data import AlpacaMinuteBars
    from lab import review_shadows as shadows
    from lab import scoreboard as board_mod

    config = LabConfig.load()
    if config.ai_trader is None:
        print("no AI trader configured", file=sys.stderr)
        return 1
    path = datasafety.lab_production_dir("ai-trader") / "ledger.jsonl"
    if not path.exists():
        # never an empty report from a missing ledger (rule 2026-10-15)
        print(f"no AI trader ledger at {path}", file=sys.stderr)
        return 1
    events = board_mod.read_jsonl(path)
    minute = AlpacaMinuteBars()       # loads the environment: the main book's data keys
    calendar = _calendar(os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_API_SECRET", ""),
                         "review shadows calendar")
    now = datetime.now(timezone.utc)
    sim = shadows.Simulator(lambda symbol, start, end: minute.bars(symbol, start, end, "sip"), calendar,
                            intraday_close=config.ai_trader.intraday_close,
                            horizon_sessions=dict(config.ai_trader.horizon_sessions), now=now,
                            report=datahealth.report)
    try:
        print(shadows.report(events, sim, now.astimezone(shadows.NY).date(),
                             read_after=config.ai_trader.exit_shadow_until), end="")
    finally:
        minute.close()
    return 0


def date_from(text: str):
    from datetime import date

    return date.fromisoformat(text[:10])

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # one INFO line per HTTP request (the mirror's MCP polling: ~45k a day) drowned
    # the journal (human ruling 2026-11-10)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # DATA HEALTH (global rule 2026-10-15): an expected fetch empty or failed is a yellow card
    datahealth.set_sink(data_sink("Paper lab"))
    parser = argparse.ArgumentParser(prog="python -m lab")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tick")
    p.add_argument("account")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("accounts", nargs="*")
    p = sub.add_parser("halt")
    p.add_argument("account")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("resume")
    p.add_argument("account")
    p.add_argument("--ack", required=True)
    p = sub.add_parser("scoreboard")
    p.add_argument("--email", action="store_true")
    for name in ("run", "golden", "roundtrip"):
        p = sub.add_parser(name)
        p.add_argument("account")
        if name == "golden":
            p.add_argument("--with-brief", action="store_true")
    p = sub.add_parser("retry-option-round-trip")
    p.add_argument("--reason", required=True)
    sub.add_parser("live-check")
    sub.add_parser("live-mirror")
    sub.add_parser("review-shadows")
    p = sub.add_parser("monthly-review")
    p.add_argument("--month")
    p.add_argument("--scheduled", action="store_true")
    p.add_argument("--post", action="store_true")
    p.add_argument("--facts-only", action="store_true")
    p.add_argument("--golden-out")
    p = sub.add_parser("review-golden")
    p.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "review-shadows":
        return review_shadows()
    if args.command == "monthly-review":
        return monthly_review(args.month, args.scheduled, args.post, args.facts_only,
                              Path(args.golden_out) if args.golden_out else None)
    if args.command == "review-golden":
        return review_golden(args.runs)
    if args.command == "live-check":
        return live_check()
    if args.command == "live-mirror":
        return live_session()
    command = args.command
    if command in OPERATOR_COMMANDS:
        # halt / resume write the account's production data by declaration
        # (a human at a shell must be able to stop an account); nothing else may.
        datasafety.declare_operator(command)
    if args.command == "tick":
        return tick(args.account, args.dry_run)
    if args.command == "status":
        return status(args.accounts)
    if args.command == "halt":
        return halt(args.account, args.reason)
    if args.command == "retry-option-round-trip":
        return retry_option_round_trip(args.reason)
    if args.command == "run":
        return run_session(args.account)
    if args.command == "scoreboard":
        return scoreboard(args.email)
    if args.command == "golden":
        return golden(args.account, args.with_brief)
    if args.command == "roundtrip":
        return roundtrip(args.account)
    return resume(args.account, args.ack)


if __name__ == "__main__":
    sys.exit(main())
