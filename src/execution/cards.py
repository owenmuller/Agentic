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


def pct(fraction: Optional[float], signed: bool = True) -> str:
    if fraction is None:
        return "n/a"
    value = float(fraction) * 100
    if abs(value) < 0.05:
        value = 0.0
    return f"{value:+.1f}%" if signed else f"{value:.1f}%"


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
    cleaned = re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)%", lambda m: f"{float(m.group(1)):.1f}%", cleaned)
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
    ), look_at=f"the benchmark has fallen {pct(drawdown, signed=False)} from its peak, past the 25.0% alert line",
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
    return Card("Main book", f"{len(holdings)} position(s) held with no record and no stop", ACTION, (
        f"Positions: {_joined([plain(h, 40) for h in holdings][:8])}.",
        "The broker holds them, but the system has no record of buying them, so no stop protects them.",
    ), what_to_do=_ask("Reconcile the unmanaged positions in the main book and tell me where they came from."),
        detail="\n".join(holdings), channel=URGENT, sample=sample)


def pending_card(count: int, detail: str, sample: bool = False) -> Card:
    return Card("Main book", f"{count} order(s) still unresolved after a restart", ACTION, (
        f"Orders in an unknown state: {count}.",
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
    return Card("Main book", f"{started} position(s) being sold by the exit rules", LOOK, (
        f"Sell orders started: {started}; positions fully closed so far: {closed}.",
        "These are the system's own stop, time and review exits working as designed.",
    ), look_at=f"{started} position(s) are being closed; the trade details are in the main book's log",
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
                     sample: bool = False) -> Card:
    return Card("Main book", "weekly report", GOOD, (
        f"Account value: {usd(value)}.",
        f"This week: {with_spy(week, spy_week)}.",
        "The full attribution and forward-return report is attached.",
    ), detail=report, detail_name="weekly-report.txt", sample=sample)


def ladder_card(month: str, complete: bool, bought: dict[str, float], sold: dict[str, float], value: float,
                since_start: Optional[float], spy_since: Optional[float], rungs: dict[str, float], faults: int,
                detail: str = "", sample: bool = False) -> Card:
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
    look = []
    if not complete:
        look.append("some orders did not fill, and the next run (10:00 ET) finishes the rebalance")
    if faults:
        look.append(f"{faults} problem(s) were recorded (in the attached detail)")
    return Card("Leverage ladder", f"{month} rebalance {'done' if complete else 'partly done'}", status,
                tuple(lines), look_at="; ".join(look) or None, detail=detail or None,
                detail_name=f"ladder-rebalance-{month}.txt", sample=sample)


def ai_day_card(today: Optional[float], today_usd: Optional[float], spy_today: Optional[float], opened: int,
                closed: int, wins: int, losses: int, holding: Sequence[str], ideas: int, taken: int, spend: float,
                cap: float, faults: int, halted: bool, detail: str = "", sample: bool = False) -> Card:
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
    look = []
    if faults:
        look.append(f"{faults} problem(s) were recorded today (in the attached detail)")
    if spend >= cap - 0.01:
        look.append("the model used its whole daily spend cap, so later scans were skipped")
    return Card("AI trader", "market closed", status, tuple(lines), look_at="; ".join(look) or None,
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
                look_at=(f"{len(faults)} problem(s) during the session, such as {plain(faults[0], 80)} "
                         f"(all in the attached detail)" if faults else None),
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
