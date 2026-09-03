# Emergency path — halt and resume from a phone (ruling 2026-09-02)

The panic button. Everything here is deterministic Python; no LLM is anywhere
near it. The kill switch it trips is the same 12%-drawdown kill switch the gate
enforces — sticky, and cleared only by a human.

## Halt (from anywhere with SSH)

```
ssh agentic@137.184.59.200
cd /home/agentic/Agentic && .venv/bin/python -m orchestrator halt "why"
```

What it does, in order, and every step runs even if an earlier one fails:

1. Writes `data/HALT` (who, when, why). A **live** trading session reads this
   marker at the top of its next tick (≤ `tick_interval_seconds`, 30s): it
   trips its own kill switch, cancels every working order it holds, logs a
   `HALT` line, and persists the halt to `session_state.json` itself. A session
   started tomorrow trips again on its first tick — the marker stays until you
   resume.
2. Cancels **every open order at the broker directly** — immediate, not waiting
   for the loop. The live loop reconciles the cancels as terminal and releases
   its reservations, exactly as a shutdown does.
3. If **no** session is live (the instance lock says), writes
   `kill_switch_tripped: true` into `data/session_state.json` directly.
   (A live session owns that file and would overwrite an outside edit on its
   next tick — that is why the marker exists.)
4. Writes an `operator_action` record to the audit trail.
5. Sends the `[AGENTIC URGENT]` email.
6. Prints the reconstructed state (mode, NAV, drawdown, positions).

While halted: opening orders of every kind are refused (`kill_switch_active`),
both arms and the cash sweep included. **Risk-reducing closes keep working**:
stops, ratchet, leash, invalidation and review closes all still execute.
Positions are never trapped.

To also stop the process: `sudo systemctl stop agentic-paper.service`
(and `sudo systemctl disable agentic-paper.timer` to keep tomorrow's session
from starting). Neither is required for the halt to hold.

## Resume (a human decision, on purpose slow)

```
sudo systemctl stop agentic-paper.service      # if a session is running
cd /home/agentic/Agentic && .venv/bin/python -m orchestrator resume "Owen: I CONFIRM MANUAL RESET"
```

`resume` refuses while a session is live (a reset the running gate cannot see
is not a reset) and refuses any acknowledgement that is not your name plus the
exact, case-sensitive phrase `I CONFIRM MANUAL RESET`. It then calls the gate's
own operator reset — which re-bases the high-water mark to current NAV so the
reset is not inert — persists the session state, removes `data/HALT`, and
records the acknowledgement in the audit trail so every reset has a name on it.
Then `sudo systemctl start agentic-paper.service` (or wait for the timer).

## What this does NOT do

- It does not touch the mechanical sleeve's own circuit breaker
  (`mechanical_halted` in `session_state.json`): the kill switch already
  refuses mechanical opening orders, and the breaker has its own reset
  discipline.
- It does not close positions. If you want flat, that is a separate,
  deliberate decision — the review/stop machinery keeps running and will exit
  what its rules say to exit.
- It never changes `PAPER_MODE` or the live confirmation. Nothing does.

## Check state at any time

```
.venv/bin/python -m orchestrator health
```
