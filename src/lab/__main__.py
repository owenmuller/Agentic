"""``python -m lab`` - the separate paper accounts (PAPER PUSH, 2026-10-09 late).

  tick <account> [--dry-run]   one run of the account's engine. Real runs only
                               inside the account's own unit
                               (agentic-lab-<account>.service); --dry-run reads
                               the account and plans with live quotes, submits
                               nothing and writes only scratch.
  run <account>                the AI trader's session (its own unit only): wait
                               for the open, tick until the close.
  golden <account>             the AI trader's golden replay: production prompt
                               and request path, no broker (costs ~$1).
  roundtrip <account>          the AI trader's order path, live, minimum size, in
                               session: one SPY share and one SPY call, bought and
                               sold through the gate; writes only scratch.
  status [<account>]           read-only: pin, kill switch, last snapshot, spend.
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

import datasafety
from lab.account import LabAccount, LabRefused, LabState, Ledger, open_account
from lab.config import LabConfig
from execution.alerts import Alerter
from lab.ladder import LadderEngine

OPERATOR_COMMANDS = frozenset({"halt", "resume"})


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
    if not dry_run:
        datasafety.set_alert_sink(
            lambda kind, message: alerter.urgent(f"lab-{name}-datasafety-{kind}", f"LAB {name} DATASAFETY {kind}", message)
        )
    try:
        engine = LadderEngine(
            account, config.ladder, dry_run=dry_run,
            alert=lambda subject, body: alerter.urgent(f"lab-{name}-drawdown", subject, body),
        )
        notes = engine.tick(datetime.now(timezone.utc))
        for note in notes:
            print(f"LAB {name}{' DRY RUN' if dry_run else ''}: {note}")
    finally:
        alerter.close()
    return 0


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
        if snapshots:
            last = snapshots[-1]
            print(f"  last snapshot {last['at']}: equity {last.get('equity')}, rungs {json.dumps(last.get('rungs'))}")
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

    def quotes(symbol: str):
        ask, bid = prices(symbol), prices.bid(symbol)
        if ask is None or bid is None or bid <= 0 or ask < bid:
            return None
        return bid, ask

    chain = AlpacaOptionsChain(api_key=account.api_key, api_secret=account.api_secret)
    return quotes, chain, LabLLM(config.ai_trader)


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
    datasafety.set_alert_sink(
        lambda kind, message: alerter.urgent(f"lab-{name}-datasafety-{kind}", f"LAB {name} DATASAFETY {kind}", message)
    )
    quotes, chain, llm = _ai_parts(account, config)
    trader = AiTrader(account, config.ai_trader, llm=llm, quotes=quotes, chain=chain,
                      alert=lambda subject, body: alerter.urgent(f"lab-{name}-{subject[:30]}", subject, body))
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
                alerter.urgent(f"lab-{name}-tick", f"LAB {name} tick failed", f"{type(error).__name__}: {error}")
            if _time.monotonic() - last_snapshot > 900:
                _ai_snapshot(account, trader)
                last_snapshot = _time.monotonic()
            _time.sleep(config.ai_trader.tick_seconds)
        _ai_snapshot(account, trader)
        print(f"LAB {name}: session closed")
        return 0
    finally:
        alerter.close()


def _ai_snapshot(account: LabAccount, trader) -> None:
    from lab.ai_trader import NY

    snap = account.adapter.account_snapshot()
    today = datetime.now(timezone.utc).astimezone(NY).date()
    account.ledger.append(
        "snapshot", equity=snap.get("equity"), cash=snap.get("cash"),
        positions=[{"symbol": p.trade_symbol, "instrument": p.instrument, "quantity": p.quantity}
                   for p in trader.positions().values()],
        spend_today=trader.spent_today(today), high_water_mark=account.state.high_water_mark,
        kill_switch=account.state.kill_switch_tripped,
    )


def golden(name: str) -> int:
    """The AI trader's golden replay: production prompt, production request
    path, no broker. Also the live search->report round trip."""
    from lab import golden as golden_replay
    from lab.ai_trader import NY
    from lab.llm import LabLLM

    config = LabConfig.load()
    if config.ai_trader is None:
        print("no ai_trader config", file=sys.stderr)
        return 3
    today = datetime.now(timezone.utc).astimezone(NY).date()
    results = golden_replay.run(config.ai_trader, LabLLM(config.ai_trader), today)
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

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    for name in ("run", "golden", "roundtrip"):
        p = sub.add_parser(name)
        p.add_argument("account")
    args = parser.parse_args(argv)
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
    if args.command == "run":
        return run_session(args.account)
    if args.command == "golden":
        return golden(args.account)
    if args.command == "roundtrip":
        return roundtrip(args.account)
    return resume(args.account, args.ack)


if __name__ == "__main__":
    sys.exit(main())
