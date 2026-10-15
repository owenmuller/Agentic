"""Plain-English alert cards (human ruling 2026-10-12, the Discord redesign).

Every Discord message is a CARD, rendered as an embed:

  1. colour = status: green (all good), yellow (worth a look), red (action
     needed);
  2. title = the verdict: "[Account]: [what happened], [nothing to do |
     worth a look | action needed]" (green / yellow / red); a yellow body
     opens with one "Worth a look: ..." line (card review, 2026-10-12);
  3. body = 3-6 short lines, sentences or "label: value" with human labels -
     no variable names, raw JSON, tables or code blocks;
  4. trading P&L in whole dollars (model spend in dollars and cents against
     its cap - card review 2026-10-12), percents to one decimal, and every
     return beside SPY's;
  5. a red card ends with "What to do:" - exactly what to tell Claude Code;
  6. anything more goes in an attached .txt, never in the message;
  7. nobody is ever pinged (the transport disables mentions).

The builders below are pure: they take plain numbers and words and return a
Card, so every message type is tested for the rules above
(``tests/test_cards.py``). ntfy and email receive the same card as text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

GOOD, LOOK, ACTION = "good", "look", "action"
COLORS = {GOOD: 0x2ECC71, LOOK: 0xF1C40F, ACTION: 0xE74C3C}
#: The title verdict for each colour (ruling 2026-10-12, card review).
VERDICTS = {GOOD: "nothing to do", LOOK: "worth a look", ACTION: "action needed"}
MAX_LINES = 6
URGENT, DAILY = "URGENT", "DAILY"


@dataclass(frozen=True)
class Card:
    account: str
    happened: str
    status: str
    lines: tuple[str, ...]
    what_to_do: Optional[str] = None
    detail: Optional[str] = None
    detail_name: str = "details.txt"
    #: URGENT -> #urgent, DAILY -> #reports.
    channel: str = DAILY
    sample: bool = False
    #: Yellow only (ruling 2026-10-12, card review): one line saying what is
    #: worth looking at, shown first as "Worth a look: ...".
    look_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in COLORS:
            raise ValueError(f"unknown card status {self.status!r}")
        if self.status == ACTION and not self.what_to_do:
            raise ValueError("a red card must say what to do")
        if self.status == LOOK and not self.look_at:
            raise ValueError("a yellow card must say what is worth a look")
        if self.status != LOOK and self.look_at:
            raise ValueError("only a yellow card carries a 'worth a look' line")
        if not self.lines:
            raise ValueError("a card needs at least one line")

    @property
    def title(self) -> str:
        prefix = "SAMPLE - " if self.sample else ""
        return f"{prefix}{self.account}: {self.happened}, {VERDICTS[self.status]}"[:256]

    @property
    def color(self) -> int:
        return COLORS[self.status]

    @property
    def all_lines(self) -> tuple[str, ...]:
        head = (f"Worth a look: {self.look_at.rstrip('.')}.",) if self.look_at else ()
        return head + self.lines

    @property
    def shown_lines(self) -> tuple[str, ...]:
        return self.all_lines[:MAX_LINES]

    @property
    def description(self) -> str:
        text = "\n".join(self.shown_lines)
        if self.what_to_do:
            text += f"\n\nWhat to do: {self.what_to_do}"
        return text[:4000]

    @property
    def attachment(self) -> Optional[tuple[str, str]]:
        """The detail file: the card's own detail plus any line beyond six."""
        overflow = self.all_lines[MAX_LINES:]
        parts = [self.title, ""]
        if overflow:
            parts += list(overflow) + [""]
        if self.detail:
            parts.append(self.detail)
        if not overflow and not self.detail:
            return None
        return self.detail_name, "\n".join(parts).rstrip() + "\n"

    def text(self) -> str:
        """The same card for text-only transports (ntfy, email)."""
        tail = "\n(details attached in Discord)" if self.attachment else ""
        return f"{self.description}{tail}"


# -- numbers -------------------------------------------------------------------------

def usd(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "n/a"
    rounded = round(float(value))
    if signed:
        return f"{'+' if rounded >= 0 else '-'}${abs(rounded):,}"
    return f"{'-' if rounded < 0 else ''}${abs(rounded):,}"


def cents(value: Optional[float]) -> str:
    """Model spend and its cap (card review 2026-10-12: the whole-dollar rule
    is for trading P&L only): "$1.85", and a whole cap as "$3"."""
    if value is None:
        return "n/a"
    value = float(value)
    return f"${value:,.0f}" if value == int(value) else f"${value:,.2f}"


def _trim(number: str) -> str:
    """'25.0' -> '25', '+1.0' -> '+1' (card polish 2026-10-12)."""
    return number[:-2] if number.endswith(".0") else number


def pct(fraction: Optional[float], signed: bool = True) -> str:
    """One decimal at most, never a trailing '.0': '+1.2%', '25%', '0%'."""
    if fraction is None:
        return "n/a"
    value = float(fraction) * 100
    if abs(value) < 0.05:
        return "0%"
    return _trim(f"{value:+.1f}" if signed else f"{value:.1f}") + "%"


def count(n: int, noun: str, plural: Optional[str] = None) -> str:
    """'1 problem', '2 problems' - never 'problem(s)'."""
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


# -- what a problem means, in plain words ----------------------------------------------

#: The gate's refusal codes as reasons a person can read.
_GATE_REASONS = {
    "kill_switch_active": "the kill switch is on",
    "insufficient_buying_power": "there wasn't enough cash",
    "unsettled_funds": "today's sale proceeds hadn't settled yet",
    "below_min_notional": "the order was too small",
    "position_not_held": "there was no position to sell",
    "close_exceeds_held_quantity": "it tried to sell more than was held",
    "max_single_position_exceeded": "it would have made one position too large",
    "max_daily_deployment_exceeded": "today's spending limit was reached",
    "max_options_premium_exceeded": "the options budget was full",
    "max_short_dated_premium_exceeded": "the short-dated options budget was full",
    "sector_concentration": "too much would have sat in one sector",
    "sleeve_allocation_exceeded": "the account's allocation limit was reached",
    "pdt_limit_reached": "the day-trading limit was reached",
    "illiquid_position": "the stock trades too thinly",
    "max_positions_exceeded": "all position slots were full",
}


def _side(word: Optional[str]) -> str:
    return {"buy": "a purchase", "sell": "a sale", "entry": "a purchase", "exit": "a sale"}.get(
        (word or "").lower(), "an order")


def explain(message: str) -> tuple[int, str]:
    """(importance, plain sentence) for one logged problem; 0 is the most
    important: a book that no longer matches the broker; then anything that
    blocks a SALE or a crash (risk stays open); then refused purchases;
    then unfilled orders, missing prices and model hiccups. Unknown messages
    fall back to a cleaned copy of the text."""
    text = str(message)
    low = text.lower()
    symbol = (re.search(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\b(?! \d)", text) or [None, None])[1]
    if "broker holds" in low and "rung books" in low:
        sym = text.split(":", 1)[0].strip()
        return 0, f"the ladder's records don't match the broker for {sym}, so trading is paused until it's reconciled"
    if "does not trade" in low and "frozen" in low:
        return 0, "the account holds a stock the ladder doesn't trade, so trading is paused"
    if "not flat" in low:
        return 0, "the account wasn't empty at its first run, so trading is paused"
    if "tick failed" in low:
        return 1, "a trading cycle crashed and was retried"
    if "holds no such position" in low:
        sym = (re.search(r"exit (\S+)", text) or [None, symbol])[1]
        return 2, f"an exit of {sym} found no position to sell, so the broker's record was taken as correct"
    if ("gate rejected" in low or "gate refused" in low):
        side = (re.search(r"gate (?:rejected|refused) (buy|sell|entry|exit)", low) or [None, None])[1]
        sym = (re.search(r"gate (?:rejected|refused) (?:buy |sell |entry |exit )?([A-Za-z0-9.]+)", text) or
               [None, symbol])[1]
        reason = next((why for code, why in _GATE_REASONS.items() if code in low), "it broke a risk limit")
        return (1 if side in ("sell", "exit") else 2), f"the risk gate refused {_side(side)} of {sym} because {reason}"
    if "broker refused" in low:
        side = (re.search(r"broker refused (buy|sell|entry|exit)", low) or [None, None])[1]
        sym = (re.search(r"broker refused (?:buy |sell |entry |exit )?([A-Za-z0-9.]+)", text) or [None, symbol])[1]
        return (1 if side in ("sell", "exit") else 2), f"the broker refused {_side(side)} of {sym}"
    if "decision at" in low and "missed" in low:
        when = (re.search(r"decision at (\d\d:\d\d)", text) or [None, "one"])[1]
        return 2, f"the {when} decision was missed because the check ran late"
    if "no cap table or no nav" in low:
        return 2, "an entry was skipped because the account had no limits or value set"
    if "unfilled after" in low:
        side = (re.search(r"\b(buy|sell)\b", low) or [None, None])[1]
        sym = (re.search(r"\b(?:buy|sell) ([A-Z]{1,5})", text) or [None, symbol])[1]
        seconds = int((re.search(r"after (\d+)s", low) or [None, "0"])[1])
        wait = count(round(seconds / 60), "minute") if seconds >= 60 else count(seconds, "second")
        tail = "; the next run finishes it" if "next run" in low else " and was cancelled"
        return 3, f"an order to {side or 'trade'} {sym} didn't fill within {wait}{tail}"
    if "left unfilled" in low:
        sym = (re.search(r"exit of (\S+?):", text) or [None, symbol])[1]
        return 1, f"an exit of {sym} only partly filled, so it keeps retrying"
    if "exit of" in low and "no quote" in low or ("exit" in low and "delayed: no quote" in low):
        sym = (re.search(r"exit (?:of )?([A-Za-z0-9.]+)", text) or [None, symbol])[1]
        return 1, f"a planned exit of {sym} waited because no live price was available"
    if "model pass failed" in low:
        return 3, "one model research pass failed, so that scan placed no trades"
    if "returned no report" in low:
        return 4, "the model gave no usable answer on one scan"
    if "no usable quote" in low or "skipped: no quote" in low:
        sym = (re.search(r"(?:quote for|entry) ([A-Z]{1,5})", text) or [None, symbol])[1]
        return 4, f"there was no live price for {sym}, so it waited"
    if "no live quote or vwap" in low:
        when = (re.search(r"decision (\d\d:\d\d)", text) or [None, "one"])[1]
        return 4, f"the {when} decision was skipped because no live price was available"
    if "inputs unavailable" in low:
        when = (re.search(r"decision (\d\d:\d\d)", text) or [None, "one"])[1]
        return 4, f"the {when} decision was skipped because the price history was missing"
    if "reversal entry" in low and "dropped" in low:
        return 4, f"a switch into {symbol} was dropped because the exit filled too late"
    if "sized to zero" in low:
        return 5, f"an entry in {symbol} was skipped because its size came out to zero"
    return 6, plain(text, 120)


def worth_a_look(problems: Sequence[str], extra: Sequence[tuple[int, str]] = ()) -> Optional[str]:
    """The yellow line: the most important issue in plain words, then
    'plus N more in the attachment' when there are others."""
    issues = sorted([explain(p) for p in problems] + list(extra), key=lambda item: item[0])
    if not issues:
        return None
    head = issues[0][1]
    more = len(issues) - 1
    return head + (f", plus {more} more in the attachment" if more else "")


def with_spy(fraction: Optional[float], spy: Optional[float]) -> str:
    """A return with SPY's beside it, always."""
    return f"{pct(fraction)} (SPY {pct(spy)})"


def plain(text: str, limit: int = 160) -> str:
    """One line of technical text made fit for a card: no code fences or
    braces, variable_names as words, dollars whole, percents to one decimal,
    single line, cut at a word. (The raw text goes in the attachment.)"""
    cleaned = " ".join(str(text).replace("`", "'").replace("{", "(").replace("}", ")").split())
    cleaned = re.sub(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b", lambda m: m.group(0).replace("_", " ").lower(), cleaned)
    cleaned = re.sub(r"\$(\d[\d,]*\.\d+)", lambda m: f"${round(float(m.group(1).replace(',', ''))):,}", cleaned)
    cleaned = re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)%", lambda m: _trim(f"{float(m.group(1)):.1f}") + "%", cleaned)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rsplit(" ", 1)[0] + "..."


def _joined(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _ask(request: str) -> str:
    return f'Tell Claude Code: "{request}"'


# -- urgent alerts -------------------------------------------------------------------

def error_card(account: str, when: str, what: str, detail: str = "", sample: bool = False) -> Card:
    return Card(account, "an error was logged", ACTION, (
        f"When: {when}.",
        f"What: {plain(what)}",
        "The session keeps running; the full log line is attached.",
    ), what_to_do=_ask(f"Investigate the {account} error logged at {when} and tell me the cause and the fix."),
        detail=detail or what, channel=URGENT, sample=sample)


def cost_card(account: str, when: str, what: str, sample: bool = False) -> Card:
    return Card(account, "research spend tripwire hit", ACTION, (
        f"When: {when}.",
        f"What: {plain(what)}",
        "Model spend is above the expected daily range.",
    ), what_to_do=_ask(f"Explain the {account} research spend tripwire at {when} and whether to change anything."),
        detail=what, channel=URGENT, sample=sample)


def billing_card(account: str, when: str, what: str, sample: bool = False) -> Card:
    return Card(account, "unusual API usage detected", ACTION, (
        f"When: {when}.",
        f"What: {plain(what)}",
        "Usage reported by the provider does not match what the system recorded.",
    ), what_to_do=_ask(f"Reconcile the {account} API usage warning from {when}."),
        detail=what, channel=URGENT, sample=sample)


def kill_switch_card(account: str, value: float, peak: float, today: Optional[float], spy_today: Optional[float],
                     resume_command: str, sample: bool = False) -> Card:
    drawdown = 1 - value / peak if peak else 0.0
    return Card(account, f"kill switch tripped after a {pct(drawdown, signed=False)} drop", ACTION, (
        f"Account value: {usd(value)}, down {pct(drawdown, signed=False)} from its peak of {usd(peak)}.",
        f"Today: {with_spy(today, spy_today)}.",
        "New positions are blocked. Open positions can still be sold, and their stops keep working.",
        "Only you can lift the block.",
    ), what_to_do=_ask(f"Explain why the {account} fell {pct(drawdown, signed=False)} and whether to reset its kill "
                       f"switch.") + f" To reset, run: {resume_command}",
        channel=URGENT, sample=sample)


def drawdown_card(account: str, value: float, peak: float, since_start: Optional[float], spy_since: Optional[float],
                  sample: bool = False) -> Card:
    drawdown = 1 - value / peak if peak else 0.0
    return Card(account, f"down {pct(drawdown, signed=False)} from its peak", LOOK, (
        f"Account value: {usd(value)}; peak {usd(peak)}.",
        f"Since start: {with_spy(since_start, spy_since)}.",
        "By your ruling the ladder has no kill switch and keeps rebalancing. This is information, not a halt.",
    ), look_at=f"the benchmark has fallen {pct(drawdown, signed=False)} from its peak, past the 25% alert line",
        channel=URGENT, sample=sample)


def leverage_card(account: str, adjusted: float, face: float, contributors: Sequence[tuple[str, float, float]],
                  above: float, rearm: float, sample: bool = False) -> Card:
    """Ruling 2026-10-14 (third): leverage-adjusted gross past the alert line.
    ``contributors``: (symbol, signed leverage factor, leverage-adjusted
    share of the account), largest first."""
    top = [f"{plain(symbol, 22)} {pct(share, signed=False)} ({_trim(f'{factor:g}')}x)"
           for symbol, factor, share in contributors[:4]]
    return Card(account, f"leverage-adjusted exposure at {pct(adjusted, signed=False)}", LOOK, (
        f"Exposure counting each fund's leverage: {pct(adjusted, signed=False)} of the account; "
        f"at face value {pct(face, signed=False)}.",
        f"Largest: {_joined(top)}.",
        "Nothing is blocked: by your ruling the 100% cap counts face value in this account.",
        f"The next alert comes only after it falls below {pct(rearm, signed=False)} and passes "
        f"{pct(above, signed=False)} again.",
    ), look_at=f"the AI trader's leverage-adjusted exposure passed the {pct(above, signed=False)} alert line",
        channel=URGENT, sample=sample)


def guard_card(account: str, refused: bool, when: str, what: str, sample: bool = False) -> Card:
    happened = ("a write to data it does not own was blocked" if refused
                else "the data guard failed and let a write through")
    return Card(account, happened, ACTION, (
        f"When: {when}.",
        ("The protected file was not touched." if refused
         else "The write went ahead; the guard's own error is in the attached detail."),
        f"What: {plain(what)}",
    ), what_to_do=_ask(f"Investigate the {account} data-guard alert at {when}."),
        detail=what, channel=URGENT, sample=sample)


def crash_card(account: str, when: str, what: str, sample: bool = False) -> Card:
    return Card(account, "a trading cycle failed", ACTION, (
        f"When: {when}.",
        f"What: {plain(what)}",
        "The session keeps running and tries again on the next cycle.",
    ), what_to_do=_ask(f"Investigate the {account} cycle failure at {when}."),
        detail=what, channel=URGENT, sample=sample)


def unmanaged_card(holdings: Sequence[str], sample: bool = False) -> Card:
    return Card("Main book", f"{count(len(holdings), 'position')} held with no record and no stop", ACTION, (
        f"Positions: {_joined([plain(h, 40) for h in holdings][:8])}.",
        "The broker holds them, but the system has no record of buying them, so no stop protects them.",
    ), what_to_do=_ask("Reconcile the unmanaged positions in the main book and tell me where they came from."),
        detail="\n".join(holdings), channel=URGENT, sample=sample)


def pending_card(orders: int, detail: str, sample: bool = False) -> Card:
    return Card("Main book", f"{count(orders, 'order')} still unresolved after a restart", ACTION, (
        f"Orders in an unknown state: {orders}.",
        "A crash may have left money committed to orders the system cannot see.",
    ), what_to_do=_ask("Resolve the main book's orders still pending settlement after recovery."),
        detail=detail, channel=URGENT, sample=sample)


def breaker_card(what: str, sample: bool = False) -> Card:
    return Card("Main book", "the mechanical copy-trading arm hit its circuit breaker", ACTION, (
        "The congressional copy-trading arm fell by more than a quarter from its own peak.",
        "It has stopped opening new positions; its existing positions ride on.",
        f"Detail: {plain(what)}",
    ), what_to_do=_ask("Review the mechanical arm's drawdown and tell me whether to reset its breaker."),
        detail=what, channel=URGENT, sample=sample)


def halt_card(account: str, what: str, sample: bool = False) -> Card:
    return Card(account, "halted by an operator", LOOK, (
        "Someone ran the halt command and the live session obeyed it.",
        "New positions are blocked; sales and stops keep working.",
        f"Note: {plain(what)}",
    ), look_at="trading stays halted until someone resumes it", channel=URGENT, sample=sample)


def exits_card(started: int, closed: int, sample: bool = False) -> Card:
    return Card("Main book", f"{count(started, 'position')} being sold by the exit rules", LOOK, (
        f"Sell orders started: {started}; positions fully closed so far: {closed}.",
        "These are the system's own stop, time and review exits working as designed.",
    ), look_at=(f"{count(started, 'position')} {'is' if started == 1 else 'are'} being closed by the stop, time or "
                f"review rules; which ones and why are in the main book's log"),
        channel=URGENT, sample=sample)


# -- reports -------------------------------------------------------------------------

def first_entry_card(arm: str, count: int, sample: bool = False) -> Card:
    return Card("Main book", f"first {arm} trade of the day", GOOD, (
        f"Orders approved: {count}.",
        "Every order passed the risk gate before reaching the broker.",
    ), sample=sample)


def close_card(value: float, today: Optional[float], spy_today: Optional[float], drawdown: float, positions: int,
               spend: Optional[float], halted: bool, detail: str = "", sample: bool = False) -> Card:
    status = LOOK if drawdown >= 0.06 else GOOD
    return Card("Main book", "market closed", status, (
        f"Account value: {usd(value)}.",
        f"Today: {with_spy(today, spy_today)}.",
        f"Positions held overnight: {positions}.",
        f"Down from peak: {pct(drawdown, signed=False)}.",
        f"Research spend today: {cents(spend)}." + (" The kill switch is ON." if halted else ""),
    ), look_at=(f"the account is {pct(drawdown, signed=False)} below its peak, so new positions are being sized "
                f"down" if status == LOOK else None), detail=detail or None, sample=sample)


def weekly_main_card(value: Optional[float], week: Optional[float], spy_week: Optional[float], report: str,
                     sample: bool = False, judged: Optional[tuple[float, float]] = None) -> Card:
    lines = [f"Account value: {usd(value)}.", f"This week: {with_spy(week, spy_week)}."]
    if judged is not None:
        allocation, deployed = judged
        idle = max(0.0, allocation - deployed)
        lines.append(f"Judged sleeve: {usd(deployed)} deployed of {usd(allocation)}; {usd(idle)} idle "
                     f"({pct(idle / allocation, signed=False) if allocation else 'n/a'}). Research is off for "
                     f"Form 4, congressional and 8-K since Oct 14.")
    lines.append("The full attribution and forward-return report is attached.")
    return Card("Main book", "weekly report", GOOD, tuple(lines), detail=report, detail_name="weekly-report.txt",
                sample=sample)


def ladder_card(month: str, complete: bool, bought: dict[str, float], sold: dict[str, float], value: float,
                since_start: Optional[float], spy_since: Optional[float], rungs: dict[str, float],
                faults: Sequence[str], detail: str = "", sample: bool = False) -> Card:
    status = GOOD if complete and not faults else LOOK
    trades = []
    if bought:
        trades.append("bought " + _joined([f"{s} {usd(v)}" for s, v in bought.items()]))
    if sold:
        trades.append("sold " + _joined([f"{s} {usd(v)}" for s, v in sold.items()]))
    lines = [
        f"Trades: {'; '.join(trades) or 'none needed'}.",
        f"Account value: {usd(value)}.",
        f"Since start: {with_spy(since_start, spy_since)}.",
        "Rungs: " + _joined([f"{name} {usd(v)}" for name, v in rungs.items()]) + ".",
    ]
    # an unfilled order explains itself; say so only when no fault does
    extra = [] if complete or any("unfilled" in f for f in faults) else [
        (3, "some orders didn't fill; the next run (10:00 ET) finishes the rebalance")]
    return Card("Leverage ladder", f"{month} rebalance {'done' if complete else 'partly done'}", status,
                tuple(lines), look_at=worth_a_look(faults, extra) if status == LOOK else None,
                detail=detail or None, detail_name=f"ladder-rebalance-{month}.txt", sample=sample)


def ai_day_card(today: Optional[float], today_usd: Optional[float], spy_today: Optional[float], opened: int,
                closed: int, wins: int, losses: int, holding: Sequence[str], ideas: int, taken: int, spend: float,
                cap: float, faults: Sequence[str], halted: bool, detail: str = "", sample: bool = False) -> Card:
    status = LOOK if faults or spend >= cap - 0.01 else GOOD
    lines = [
        f"Today: {with_spy(today, spy_today)}, {usd(today_usd, signed=True)}.",
        f"Trades: {opened} opened, {closed} closed ({wins} won, {losses} lost).",
        f"Holding overnight: {len(holding)}" + (f" ({_joined(list(holding)[:5])})." if holding else "."),
        f"Ideas from the model: {ideas}; the rules took {taken} and turned down {ideas - taken}.",
        f"Model spend: {cents(spend)} of {cents(cap)} daily cap.",
    ]
    if halted:
        return Card("AI trader", "market closed with the kill switch on", ACTION, tuple(lines),
                    what_to_do=_ask("Explain the AI trader's kill-switch trip and whether to reset it."),
                    detail=detail or None, detail_name="ai-trader-day.txt", sample=sample)
    extra = [(5, "the model used its whole daily budget, so later scans were skipped")] if spend >= cap - 0.01 else []
    return Card("AI trader", "market closed", status, tuple(lines),
                look_at=worth_a_look(faults, extra) if status == LOOK else None,
                detail=detail or None, detail_name="ai-trader-day.txt", sample=sample)


def exec_test_card(day: str, session: int, total: int, live: float, modelled: float, spy_day: Optional[float],
                   agree: str, fills: int, seconds: Optional[float], faults: Sequence[str], detail: str = "",
                   sample: bool = False) -> Card:
    status = LOOK if faults else GOOD
    lines = [
        f"Session {session} of {total} ({day}).",
        f"Result: {usd(live, signed=True)} live vs {usd(modelled, signed=True)} in the backtest model "
        f"(gap {usd(live - modelled, signed=True)}).",
        f"SPY that day: {pct(spy_day)}.",
        f"Decisions matching the model: {agree}.",
        f"Fills: {fills}" + (f", about {round(seconds)} seconds each." if seconds is not None else "."),
    ]
    if not faults:
        lines.append("Problems: none.")
    return Card("Execution test", f"session {session} of {total} checked", status, tuple(lines),
                look_at=worth_a_look(faults) if faults else None,
                detail=detail or None, detail_name=f"execution-test-{day}.txt", sample=sample)


def check_card(name: str, account: str, purpose: str, ok: bool, when: str, exit_code: int, output: str,
               file_hint: str, sample: bool = False) -> Card:
    if ok:
        return Card(account, f"scheduled check '{name}' ran", GOOD, (
            f"When: {when}.",
            f"What it checked: {purpose}.",
            "Result: passed. The full output is attached.",
        ), detail=output, detail_name=f"check-{name}.txt", sample=sample)
    return Card(account, f"scheduled check '{name}' failed", ACTION, (
        f"When: {when}.",
        f"What it checked: {purpose}.",
        f"Result: failed (exit code {exit_code}). The full output is attached.",
    ), what_to_do=_ask(f"The scheduled check '{name}' failed at {when}; read {file_hint} and fix it."),
        detail=output, detail_name=f"check-{name}.txt", channel=URGENT, sample=sample)


def momentum_plan_card(month_name: str, rebalance_day: str, names: int, bought: Sequence[str],
                       sold: Sequence[str], vetoed: Sequence[tuple[str, str]], exposure: float,
                       buys_allowed: bool, problems: Sequence[str], spend: Optional[float], cap: float,
                       detail: str, sample: bool = False, scores: Optional[dict] = None,
                       sleeve_value: Optional[float] = None, sleeve_weight: Optional[float] = None,
                       off_cycle: bool = False) -> Card:
    """The momentum plan (ruling 2026-10-13; scores, vetoes with reasons and
    the sleeve's size from 2026-10-14): what the live spec will do on the
    rebalance day, what the AI vetoed and why, and any data problems."""
    size = ""
    if sleeve_value is not None:
        size = f"; sleeve {usd(sleeve_value)}" + (f" ({pct(sleeve_weight, signed=False)} of the book)"
                                                  if sleeve_weight is not None else "")
    scored = [f"{plain(s, 12)} {scores[s]:+.2f}" if scores and scores.get(s) is not None else plain(s, 12)
              for s in bought]
    lines = [
        f"Trades: {rebalance_day} from 10:00 ET, {count(names, 'name')} at {pct(exposure, signed=False)} invested{size}.",
        f"Buying (score): {_joined(scored[:15]) or 'nothing new'}.",
        f"Selling: {_joined([plain(s, 12) for s in sold][:10]) or 'nothing'}.",
    ]
    if vetoed:
        lines.append(f"AI vetoed: {_joined([f'{plain(s, 12)} ({plain(why, 60)})' for s, why in vetoed][:4])}; "
                     f"the next names in the ranking took the slots.")
    else:
        lines.append("AI vetoed: nothing; every new name was reviewed.")
    if not buys_allowed:
        lines.append("SPY is below its 200-day average, so no new names this time (the rule).")
    if spend is not None:
        lines.append(f"AI spend: {cents(spend)} of {usd(cap)} cap.")
    look = worth_a_look(list(problems))
    happened = "first plan ready (off-cycle start)" if off_cycle else f"{month_name} plan ready"
    return Card("Momentum", happened, LOOK if look else GOOD, tuple(lines[:6]),
                look_at=look, detail=detail, detail_name=f"momentum-plan-{rebalance_day}.txt", sample=sample)


def momentum_prep_failed_card(when: str, what: str, detail: str = "", sample: bool = False) -> Card:
    return Card("Momentum", "the monthly plan was not made", ACTION, (
        f"When: {when}.",
        f"What: {plain(what)}",
        "Without a plan the sleeve trades nothing this month; held names stay as they are.",
    ), what_to_do=_ask(f"The momentum prep failed at {when}: find the cause, fix it and rerun momentum-prep."),
        detail=detail or what, detail_name="momentum-prep.txt", channel=URGENT, sample=sample)


@dataclass(frozen=True)
class BoardLine:
    """One account's line on the weekly scoreboard card."""

    name: str
    ret: Optional[float] = None
    spy: Optional[float] = None
    since: Optional[str] = None
    extra: str = ""


def scoreboard_card(as_of: str, lines: Iterable[BoardLine], sessions: int, grade_at: int, verdict_at: int,
                    table: str, sample: bool = False, file_stamp: Optional[str] = None) -> Card:
    rendered = []
    for line in lines:
        head = f"{line.name}: "
        if line.ret is not None:
            head += f"{with_spy(line.ret, line.spy)}" + (f" since {line.since}" if line.since else "")
            head += f"; {line.extra}" if line.extra else ""
        else:
            head += line.extra or "no result yet"
        rendered.append(head + ".")
    if sessions < grade_at:
        note = (f"Too early to judge: {sessions} of {grade_at} sessions before the first grading "
                f"(the real verdict comes at {verdict_at}).")
    elif sessions < verdict_at:
        note = f"First grading is due ({sessions} sessions); the real verdict comes at {verdict_at}."
    else:
        note = f"{sessions} sessions: the verdict is due."
    return Card("Paper lab", f"weekly scoreboard for {as_of}", GOOD, tuple(rendered[:5]) + (note,),
                detail=table, detail_name=f"scoreboard-{file_stamp or as_of}.txt", sample=sample)


def momentum_week_card(report, sample: bool = False) -> Card:
    """The momentum sleeve's weekly line (ruling 2026-10-13; v1.1 and SPMO as
    the binding comparison, 2026-10-14). ``report``: momentum_report.Report."""
    if report.started is None:
        return Card("Momentum", "weekly report", GOOD, (
            "The sleeve has not traded yet; its first rebalance is the first trading day of the month.",
        ), detail=report.table(), detail_name="momentum-weekly.txt", sample=sample)
    live_w, live_s = report.week.get("v1.1 live"), report.since.get("v1.1 live")
    spy_w, spmo_w = report.week.get("SPY"), report.week.get("SPMO")
    spy_s, spmo_s = report.since.get("SPY"), report.since.get("SPMO")
    shadows = {n: v for n, v in report.since.items()
               if n not in ("v1.1 live", "SPY", "SPMO", "v1.1 modelled (plan at closes)") and v is not None}
    ahead = sum(1 for v in shadows.values() if live_s is not None and v > live_s)
    lines = [
        f"Week to date: {pct(live_w)} (SPY {pct(spy_w)}, SPMO {pct(spmo_w)}).",
        f"Since start ({report.started}): {pct(live_s)} (SPY {pct(spy_s)}, SPMO {pct(spmo_s)}).",
        f"Holding {count(report.holdings, 'name')}, {usd(report.invested)} invested.",
        f"Shadows: {ahead} of {len(shadows)} ahead of the live sleeve since start; the table is attached.",
    ]
    look = None
    if live_s is None or spmo_s is None:
        look = "a return could not be computed (missing prices); the attachment shows which"
    return Card("Momentum", "weekly report", LOOK if look else GOOD, tuple(lines), look_at=look,
                detail=report.table(), detail_name="momentum-weekly.txt", sample=sample)


def momentum_daily_card(report, sample: bool = False) -> Card:
    """The momentum sleeve's daily line (standing rule 2026-10-14): today,
    week to date and since start, beside SPY and SPMO; every shadow in the
    attached table. ``report``: momentum_report.Report."""
    if report.started is None:
        return Card("Momentum", "daily report", GOOD, ("The sleeve has not traded yet.",),
                    detail=report.table(), detail_name="momentum-daily.txt", sample=sample)

    def trio(window: dict) -> str:
        return f"{pct(window.get('v1.1 live'))} (SPY {pct(window.get('SPY'))}, SPMO {pct(window.get('SPMO'))})"

    shadows = {n: v for n, v in report.since.items()
               if n not in ("v1.1 live", "SPY", "SPMO", "v1.1 modelled (plan at closes)") and v is not None}
    live_s = report.since.get("v1.1 live")
    ahead = sum(1 for v in shadows.values() if live_s is not None and v > live_s)
    lines = (
        f"Today: {trio(report.day)}.",
        f"Week to date: {trio(report.week)}.",
        f"Since start ({report.started}): {trio(report.since)}.",
        f"Holding {count(report.holdings, 'name')}, {usd(report.invested)} invested.",
        f"Shadows: {ahead} of {len(shadows)} ahead of the live sleeve since start; the table is attached.",
    )
    look = None
    if report.day.get("v1.1 live") is None or report.day.get("SPMO") is None:
        look = "a return could not be computed (missing prices); the attachment shows which"
    return Card("Momentum", "daily report", LOOK if look else GOOD, lines, look_at=look,
                detail=report.table(), detail_name="momentum-daily.txt", sample=sample)
