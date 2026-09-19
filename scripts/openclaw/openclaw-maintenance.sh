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
# It never deletes transcripts or job artifacts: it reports their size and lets the operator
# decide.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/maintenance.log"
REPORT="$(mktemp)"
rc=0

mkdir -p "$(dirname "$LOG")"
log(){ echo "$(date '+%F %T') $*" | tee -a "$LOG" >&2; }
say(){ echo "$*" >> "$REPORT"; }
finish(){ rm -f "$REPORT"; }
trap finish EXIT

log "=== maintenance window ==="

out="$(ai-resources openclaw doctor --cleanup-sessions 2>&1)"
drc=$?
echo "$out" >> "$LOG"
case "$drc" in
  0)
    rep="$(grep -oE "Repaired legacy bindings[^|]*" <<<"$out" | head -1 | tr -s ' ')"
    say "doctor: ok${rep:+ -- $rep}"
    say "gateway: active and healthy" ;;
  2) say "WARNING: another maintenance window is open (watchdog.off exists): nothing was done"; rc=1 ;;
  3) say "WARNING: the cgroup did not drain in 90 s: doctor --fix skipped"; rc=1 ;;
  4) say "WARNING: doctor did not complete the maintenance (see maintenance.log)"; rc=1 ;;
  5) say "ALERT: the gateway does NOT answer after the maintenance"; rc=1 ;;
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
if [ "$rc" -ne 0 ] || grep -q "^UPDATE:" <<<"$body"; then
  notify "OpenClaw weekly maintenance:
$body" || log "note: could not notify over Telegram"
fi
log "=== end (rc=$rc) ==="
exit "$rc"
