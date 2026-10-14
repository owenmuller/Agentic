#!/bin/bash
# Post-reboot checks (audit 2026-10-14; ops/vps/RUNBOOK-reboot.md, step 5).
# Run as the agentic user after the droplet is back:
#
#   bash ~/Agentic/ops/vps/post_reboot_check.sh
#   SKIP_CARDS=1 bash ...   (a dry run that sends no test card)
#
# Prints PASS/FAIL per line and exits non-zero when anything fails. Sends ONE
# test card to each Discord channel (#urgent and #reports). Reads only; it
# changes nothing and prints no secret.
set -u
fails=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails + 1)); }

echo "== host"
uname -r
if [ -e /var/run/reboot-required ]; then fail "reboot still required (/var/run/reboot-required present)"; else pass "no reboot pending"; fi
up=$(cut -d. -f1 /proc/uptime); [ "$up" -lt 86400 ] && pass "uptime ${up}s (rebooted today)" || fail "uptime ${up}s: this does not look like a fresh boot"
swapon --show | grep -q . && pass "swap active: $(swapon --show --noheadings | awk '{print $1, $3}')" || echo "INFO  no swap (only a FAIL if the runbook's optional swap step was done)"
free -m | sed -n 2p
df -h / | tail -1

echo "== system timers (every agentic-* timer enabled and armed)"
for t in $(systemctl list-unit-files 'agentic-*.timer' --no-legend | awk '{print $1}'); do
  en=$(systemctl is-enabled "$t"); act=$(systemctl is-active "$t")
  next=$(systemctl show "$t" -p NextElapseUSecRealtime --value)
  if [ "$en" = enabled ] && [ "$act" = active ] && [ -n "$next" ]; then pass "$t next $next"
  elif [ "$en" = enabled ] && [ "$act" = active ] && [ "$(systemctl is-active "${t%.timer}.service")" = active ]; then echo "INFO  $t armed, next not computed yet (its service is running)"
  elif [ "$en" = enabled ] && [ "$act" = active ]; then echo "INFO  $t has no future trigger (a one-shot that already fired)"
  else fail "$t enabled=$en active=$act"; fi
done

echo "== persistent user checks (installed, enabled, Persistent=yes)"
[ "$(loginctl show-user "$USER" -p Linger --value)" = yes ] && pass "lingering on (user timers run without a login)" || fail "lingering is OFF: user timers will not run"
found=0
for f in "$HOME"/.config/systemd/user/check-*.timer; do
  [ -e "$f" ] || continue; found=1
  t=$(basename "$f")
  en=$(systemctl --user is-enabled "$t"); p=$(systemctl --user show "$t" -p Persistent --value)
  next=$(systemctl --user show "$t" -p NextElapseUSecRealtime --value)
  last=$(systemctl --user show "$t" -p LastTriggerUSec --value)
  if [ "$en" = enabled ] && [ "$p" = yes ]; then pass "$t next ${next:-elapsed} last ${last:-never}"; else fail "$t enabled=$en persistent=$p"; fi
done
[ "$found" = 1 ] || echo "INFO  no check-* timers installed"
transient=$(systemctl --user list-timers --all --no-legend | awk '{for(i=1;i<=NF;i++) if ($i ~ /\.timer$/) print $i}' \
  | while read -r t; do [ "$(systemctl --user show "$t" -p Transient --value)" = yes ] && echo "$t"; done)
[ -z "$transient" ] && pass "no transient user timers" || fail "transient user timers (lost at the next reboot): $transient"

echo "== keys (names and status only)"
if "$HOME/Agentic/.venv/bin/python" "$HOME/Agentic/ops/vps/verify_keys.py"; then pass "every verifiable key"; else fail "a key failed verification (see above)"; fi

echo "== health"
if "$HOME/Agentic/.venv/bin/python" -m orchestrator health > /tmp/post-reboot-health.txt 2>&1; then
  grep -E "sleeves:|kill switch|alerts:" /tmp/post-reboot-health.txt | cut -c1-200; pass "orchestrator health ran"
else fail "orchestrator health failed: $(tail -3 /tmp/post-reboot-health.txt)"; fi

echo "== one test card to each Discord channel"
if [ "${SKIP_CARDS:-0}" = 1 ]; then echo "INFO  SKIP_CARDS=1: no test cards sent (dry run)"; else
cd "$HOME/Agentic" && if .venv/bin/python - <<'PY'
import sys
from execution.alerts import Alerter, DAILY, URGENT
a = Alerter()
urgent, reports = a.send_test(URGENT), a.send_test(DAILY)
print(f"#urgent sent={urgent}  #reports sent={reports}")
sys.exit(0 if urgent and reports else 1)
PY
then pass "test cards delivered (check both channels show one)"; else fail "a test card did not send"; fi
fi

echo
[ "$fails" -eq 0 ] && echo "ALL PASS" || echo "$fails FAILURE(S)"
exit "$fails"
