# openclaw-maintenance.sh — weekly maintenance window for the OpenClaw setup.
#
# The risky part -- pause the watchdog, stop the gateway, wait for its cgroup to drain, run
# `openclaw doctor --fix`, clean the session stores, start the gateway again and wait for it to
# answer -- is `ai-resources openclaw doctor --cleanup-sessions`. It lives there, once, so the
# drain rule (T01) has exactly one place it can regress. This script keeps what is its own:
#   - turning the wrapper's exit code into a report line;
#   - the new-OpenClaw-version check;
#   - the backup-age check and the disk report;
#   - telling the operator ONLY when something is off or a new version is out.
#
# A week that finds agent runs in flight is NOT lost: the timer fires again every day at 04:30 and 08:30 (UTC, the
# operator's night) and this script runs the window on the first try whose probe finds the gateway idle. The state
# file `maintenance.week` records "<ISO week> <skipped tries> <done|open>": a week already done exits at once, only the
# first skip of a week is announced (and every sixth, so a week that never finds a quiet slot still escalates), and the
# report that finally runs says how many tries it took.
#
# It never deletes transcripts or job artifacts: it reports their size and lets the operator
# decide.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/maintenance.log"
REPORT="$(mktemp)"
rc=0
skipped=0

mkdir -p "$(dirname "$LOG")"
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }
say(){ echo "$*" >> "$REPORT"; }
finish(){ rm -f "$REPORT"; }
trap finish EXIT

# A maintenance week starts on Sunday (the first slot, after the Sunday backup): tomorrow's ISO week on a Sunday.
WEEK="$(date -d tomorrow +%G-W%V)"
WEEK_FILE="$OPENCLAW_LOG_DIR/maintenance.week"
read -r wk_id wk_skips wk_state < "$WEEK_FILE" 2>/dev/null || true
if [ "${wk_id:-}" != "$WEEK" ]; then wk_skips=0; wk_state=open; fi
wk_skips="${wk_skips:-0}"
if [ "${wk_state:-open}" = done ]; then
  log "=== maintenance already done for $WEEK; nothing to do ==="
  exit 0
fi
save_week(){ printf '%s %s %s\n' "$WEEK" "$1" "$2" > "$WEEK_FILE"; }

log "=== maintenance window ($WEEK, previous skipped tries: $wk_skips) ==="

out="$(ai-resources openclaw doctor --cleanup-sessions --yes 2>&1)"
drc=$?
echo "$out" >> "$LOG"
case "$drc" in
  0)
    after=""; [ "$wk_skips" -gt 0 ] && after="after $wk_skips skipped tries this week"
    rep="$(grep -oE "Repaired legacy bindings[^|]*" <<<"$out" | head -1 | tr -s ' ')"
    say "doctor: ok${rep:+ -- $rep}${after:+ ($after)}"
    say "gateway: active and healthy"
    save_week 0 done ;;
  2) say "WARNING: another maintenance window is open (watchdog.off exists): nothing was done"; rc=1 ;;
  3) say "WARNING: the cgroup did not drain in 90 s: doctor --fix skipped"; rc=1 ;;
  4) say "WARNING: doctor did not complete the maintenance (see maintenance.log)"; rc=1 ;;
  5) say "ALERT: the gateway does NOT answer after the maintenance"; rc=1 ;;
  # 6 = the drained doctor refused because agent runs are in flight (or the busy probe could not tell).
  # Nothing was stopped, so this is a skipped week and not a failure: the unit stays green and the report
  # says so once.
  6) wk_skips=$((wk_skips + 1)); save_week "$wk_skips" open
     say "WARNING: skipped: agent runs are in flight (or the busy probe could not tell); doctor was not run and nothing was stopped; the timer retries in the next quiet slot (try $wk_skips this week)"
     # Announce the first skip of the week, then every sixth: a retry that will succeed needs no extra noise.
     if [ "$wk_skips" -eq 1 ] || [ $((wk_skips % 6)) -eq 0 ]; then skipped=1; fi ;;
  *) say "ALERT: ai-resources openclaw doctor failed unexpectedly (exit $drc)"; rc=1 ;;
esac

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
if [ "$rc" -ne 0 ] || [ "$skipped" -ne 0 ] || grep -q "^UPDATE:" <<<"$body"; then
  notify "OpenClaw weekly maintenance:
$body" || log "note: could not notify"
fi
log "=== end (rc=$rc) ==="
exit "$rc"
