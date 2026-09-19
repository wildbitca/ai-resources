#!/usr/bin/env bash
# openclaw-maintenance.sh — weekly maintenance window for the OpenClaw setup.
#
# Sequence (order matters):
#   1. pause the watchdog, so it does not fight the maintenance stop;
#   2. stop the gateway and WAIT until its cgroup is drained -- `openclaw doctor --fix`
#      re-inspects the stopped unit and aborts with "ownership or manager identity changed"
#      if children (claude, engram, npx) are still alive, and when it aborts it leaves the
#      gateway down;
#   3. doctor --fix (migrates stale session model pins, refreshes the plugin registry);
#   4. session-store cleanup;
#   5. resume the watchdog and start the gateway explicitly -- never rely on the 2-minute
#      watchdog tick to end the outage;
#   6. assert health, report, and only bother the operator if something is off or a new
#      OpenClaw version is out.
#
# It never deletes transcripts or job artifacts: it reports their size and lets the operator
# decide.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/maintenance.log"
OFF="$OPENCLAW_WATCHDOG_OFF"
UNIT="$OPENCLAW_UNIT"
REPORT="$(mktemp)"
rc=0

mkdir -p "$(dirname "$LOG")"
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }
say(){ echo "$*" >> "$REPORT"; }
finish(){ rm -f "$OFF" "$REPORT"; }
trap finish EXIT

log "=== maintenance window ==="
touch "$OFF"

systemctl --user stop "$UNIT" || true
drained=no
for _ in $(seq 1 30); do
  [ "$(systemctl --user show "$UNIT" -p TasksCurrent --value)" = "[not set]" ] && { drained=yes; break; }
  sleep 3
done
log "drained=$drained"
[ "$drained" = yes ] || { say "WARNING: the cgroup did not drain in 90 s: doctor --fix skipped"; rc=1; }

if [ "$drained" = yes ]; then
  out="$(openclaw doctor --fix 2>&1)"
  echo "$out" >> "$LOG"
  if grep -q "Doctor complete" <<<"$out"; then
    rep="$(grep -oE "Repaired legacy bindings[^|]*" <<<"$out" | head -1 | tr -s ' ')"
    say "doctor: ok${rep:+ -- $rep}"
  else
    say "WARNING: doctor did not complete the maintenance (see maintenance.log)"; rc=1
  fi
  cl="$(openclaw sessions cleanup --all-agents 2>&1 | tail -2 | tr '\n' ' ')"
  log "sessions cleanup: $cl"
fi

rm -f "$OFF"
systemctl --user start "$UNIT"

# A cold start takes more than 12 s: poll for up to 2 minutes before calling it dead.
healthy=no
for _ in $(seq 1 24); do
  sleep 5
  if openclaw health >/dev/null 2>&1; then healthy=yes; break; fi
done
if [ "$healthy" = yes ]; then
  say "gateway: active and healthy"
else
  say "ALERT: the gateway does NOT answer after the maintenance"; rc=1
fi

upd="$(openclaw update status 2>&1 | grep -oE "npm latest [0-9][0-9.]*" | head -1)"
cur="$(openclaw --version 2>/dev/null | grep -oE "[0-9]{4}\.[0-9]+\.[0-9]+" | head -1)"
if [ -n "$upd" ] && [ -n "$cur" ] && [ "${upd##* }" != "$cur" ]; then
  say "UPDATE: a new OpenClaw version is out: $cur -> ${upd##* } (\`openclaw update\`)"
fi

bk_daily=$(ls -1 "$OPENCLAW_BACKUP_DIR"/daily/*.tar.gz 2>/dev/null | wc -l)
bk_last=$(ls -1t "$OPENCLAW_BACKUP_DIR"/*/*.tar.gz 2>/dev/null | head -1)
if [ -n "$bk_last" ]; then
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$bk_last") ) / 3600 ))
  say "backups: $bk_daily dailies on disk, the newest ${age_h} h old"
  [ "$age_h" -gt 36 ] && { say "WARNING: the newest backup is more than 36 h old"; rc=1; }
else
  say "ALERT: there is no local backup"; rc=1
fi

say "disk: ~/.claude/jobs $(du -sh "$HOME/.claude/jobs" 2>/dev/null | cut -f1), projects $(du -sh "$HOME/.claude/projects" 2>/dev/null | cut -f1), backups $(du -sh "$OPENCLAW_BACKUP_DIR" 2>/dev/null | cut -f1)"

body="$(cat "$REPORT")"
log "summary: $(tr '\n' ' ' <<<"$body")"
if [ "$rc" -ne 0 ] || grep -q "^UPDATE:" <<<"$body"; then
  notify "OpenClaw weekly maintenance:
$body" || log "note: could not notify over Telegram"
fi
log "=== end (rc=$rc) ==="
exit "$rc"
