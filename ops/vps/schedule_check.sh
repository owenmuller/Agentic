#!/bin/bash
# Schedule a one-shot check ON THE DROPLET (human ruling 2026-10-10), as an
# INSTALLED, PERSISTENT systemd user timer (human ruling 2026-10-14: a check
# must survive a reboot). Unit files go to ~/.config/systemd/user; the timer
# has Persistent=true, so a run missed while the droplet was down fires once
# at the next boot (lingering is on for the agentic user, so the user manager
# starts at boot without a login). The check runs ops/check_runner.py, which
# writes ~/checks/ and sends the result (DAILY on pass, URGENT on failure).
#
#   schedule_check.sh NAME "YYYY-MM-DD HH:MM" -- COMMAND [ARGS...]     (time in ET)
#   schedule_check.sh --remove NAME                                    (bare or check-NAME; an error if none)
#   e.g. schedule_check.sh ladder-first-run "2026-10-13 10:25" -- \
#          /home/agentic/Agentic/.venv/bin/python -m lab status ladder
#
# List: systemctl --user list-timers 'check-*'
# The COMMAND is written, shell-quoted, into ~/checks/bin/NAME.sh and the
# service runs that script, so no argument depends on systemd's quoting.
# A user unit is not production: the data guard confines a check to reads
# and scratch. This script sets no trading variable (Constraint #4).
set -eu
UNITS="$HOME/.config/systemd/user"
BIN="$HOME/checks/bin"

if [ "${1:-}" = "--remove" ]; then
  # NAME bare ("ladder-first-run") or as its units are named ("check-ladder-
  # first-run"). Nothing installed under either is an error (2026-11-10: a
  # prefixed name reported "removed" and removed nothing).
  GIVEN="${2:?usage: schedule_check.sh --remove NAME}"
  exists() { [ -e "$UNITS/check-$1.timer" ] || [ -e "$UNITS/check-$1.service" ] || [ -e "$BIN/$1.sh" ]; }
  MATCHES=()
  exists "$GIVEN" && MATCHES+=("$GIVEN")
  case "$GIVEN" in check-?*) exists "${GIVEN#check-}" && MATCHES+=("${GIVEN#check-}") ;; esac
  if [ "${#MATCHES[@]}" -eq 0 ]; then
    echo "no check named $GIVEN is installed; nothing removed" >&2
    echo "list them with: systemctl --user list-timers --all 'check-*'" >&2
    exit 1
  fi
  if [ "${#MATCHES[@]}" -gt 1 ]; then
    echo "$GIVEN is ambiguous: both check-${MATCHES[0]} and check-${MATCHES[1]} are installed; nothing removed" >&2
    exit 1
  fi
  NAME="${MATCHES[0]}"
  systemctl --user disable --now "check-$NAME.timer" 2>/dev/null || true
  rm -f "$UNITS/check-$NAME.timer" "$UNITS/check-$NAME.service" "$BIN/$NAME.sh"
  systemctl --user daemon-reload
  echo "removed check-$NAME"
  exit 0
fi

NAME="$1"; WHEN="$2"; shift 2
[ "${1:-}" = "--" ] && shift
[ "$#" -gt 0 ] || { echo "no command given" >&2; exit 2; }
mkdir -p "$UNITS" "$BIN"

{
  echo '#!/bin/bash'
  echo "# check $NAME, scheduled for $WHEN ET by ops/vps/schedule_check.sh"
  printf 'exec %q %q %q --' "$HOME/Agentic/.venv/bin/python" "$HOME/Agentic/ops/check_runner.py" "$NAME"
  printf ' %q' "$@"
  echo
} > "$BIN/$NAME.sh"
chmod +x "$BIN/$NAME.sh"

cat > "$UNITS/check-$NAME.service" <<EOF
[Unit]
Description=Agentic one-shot check $NAME ($WHEN ET)

[Service]
Type=oneshot
WorkingDirectory=$HOME/Agentic
MemoryMax=700M
Nice=10
ExecStart=/bin/bash $BIN/$NAME.sh
EOF

cat > "$UNITS/check-$NAME.timer" <<EOF
[Unit]
Description=Agentic one-shot check $NAME ($WHEN ET)

[Timer]
OnCalendar=$WHEN America/New_York
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now "check-$NAME.timer"
systemctl --user list-timers "check-$NAME.timer" --no-pager
