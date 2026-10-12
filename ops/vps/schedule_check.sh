#!/bin/bash
# Schedule a one-shot check ON THE DROPLET (human ruling 2026-10-10): a
# transient systemd USER timer (lingering is on for the agentic user, so it
# survives logout and fires on its own clock, independent of any laptop
# session). The check runs ops/check_runner.py, which writes ~/checks/ and
# emails the result (DAILY tier on pass, URGENT on failure).
#
#   schedule_check.sh NAME "YYYY-MM-DD HH:MM" -- COMMAND [ARGS...]     (time in ET)
#   e.g. schedule_check.sh ladder-first-run "2026-10-13 10:25" -- \
#          /home/agentic/Agentic/.venv/bin/python -m lab status ladder
#
# List: systemctl --user list-timers 'check-*'   Cancel: systemctl --user stop check-NAME.timer
# A user unit is not production: the data guard confines a check to reads
# and scratch. This script sets no trading variable (Constraint #4).
set -eu
NAME="$1"; WHEN="$2"; shift 2
[ "${1:-}" = "--" ] && shift
exec systemd-run --user --unit="check-$NAME" --collect --quiet \
  --on-calendar="$WHEN America/New_York" --timer-property=AccuracySec=1s \
  -p WorkingDirectory="$HOME/Agentic" -p MemoryMax=700M -p Nice=10 \
  "$HOME/Agentic/.venv/bin/python" "$HOME/Agentic/ops/check_runner.py" "$NAME" -- "$@"
