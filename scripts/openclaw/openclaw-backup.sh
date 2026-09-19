#!/usr/bin/env bash
# openclaw-backup.sh — off-box-ready backup of the whole AI setup of an OpenClaw host.
#
# Usage: openclaw-backup.sh <daily|weekly|monthly|manual>
#
# What it captures (everything here is hand-written or irreplaceable state):
#   1. openclaw's own verified backup: ~/.openclaw config + state + per-agent DBs.
#   2. engram: a consistent SQLite snapshot of ~/.engram/engram.db.
#   3. The AI config that lives in no repo: ~/.claude/CLAUDE.md, settings.json, hooks/,
#      ~/.config/shell/, the systemd units and host scripts, ~/.openclaw/kit-host.env, and the
#      AGENTS.md / MEMORY.md / USER.md / memory/ of every agent workspace.
#   4. An inventory (versions, agents, models) so a restore knows what it is.
#
# It deliberately does NOT capture: the ~/.claude skills and subagents (regenerable with
# `ai-resources setup`), ~/.claude/projects transcripts (about 1.2 GB of history), or the
# project workspaces (they are git checkouts). Before ai-resources 1.9.0 it also left out the
# watchdog, verify and team-watch scripts and eight of the ten units -- by accident, not by
# design; the list below is the fix.
#
# Conventions copied from k3s-backup.sh, including the lessons its runbook records:
#   - stage on disk under $OPENCLAW_BACKUP_DIR, never in /tmp (tmpfs, sized in RAM);
#   - the `.sha256` sibling is written LAST and is the only witness that a tarball is
#     complete: the uploader skips tarballs without it;
#   - no `|| true` on anything that matters -- a partial backup reported as good is worse
#     than no backup;
#   - assert the result (size + listable + checksum) instead of assuming it.
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

TIER="${1:-manual}"
case "$TIER" in
  daily)   KEEP=7 ;;   # one week of dailies on disk
  weekly)  KEEP=4 ;;   # about one month
  monthly) KEEP=3 ;;   # one quarter
  manual)  KEEP=3 ;;
  *) echo "usage: $(basename "$0") <daily|weekly|monthly|manual>" >&2; exit 2 ;;
esac

BASE="$OPENCLAW_BACKUP_DIR"
DEST="$BASE/$TIER"
LOG="$OPENCLAW_LOG_DIR/backup.log"
TS="$(date +%Y%m%d-%H%M%S)"
NAME="openclaw-$TS"
LOCK="$XDG_RUNTIME_DIR/openclaw-backup.lock"
MIN_BYTES=1000000            # under 1 MB it is not a backup of this setup

mkdir -p "$DEST" "$(dirname "$LOG")"
STAGE="$(mktemp -d -p "$BASE" "stage.XXXXXXXX")"

log(){ echo "$(date '+%F %T') [$TIER] $*" | tee -a "$LOG" >&2; }
cleanup(){ rm -rf "$STAGE"; }
trap cleanup EXIT

# Tell the operator when this fails. Best effort: never let the notice mask the error.
notify_failure(){
  local rc=$?
  [ "$rc" -eq 0 ] && return 0
  log "FAILED (rc=$rc) -- see $LOG"
  notify "openclaw-backup ($TIER) failed on $(hostname) (rc=$rc). Log: ~/.openclaw/logs/backup.log" || true
  return "$rc"
}
trap notify_failure ERR

exec 9>"$LOCK"
if ! flock -n 9; then log "another run in progress -- skipping"; exit 0; fi

log "start -> $DEST/$NAME.tar.gz"

# 1. openclaw's own backup (config, state, agent databases, secret store).
openclaw backup create --no-include-workspace --verify --output "$STAGE" >"$STAGE/openclaw-backup.out" 2>&1
OC_ARCHIVE="$(find "$STAGE" -maxdepth 1 -name '*-openclaw-backup.tar.gz' -print -quit)"
[ -n "$OC_ARCHIVE" ] || { log "ERROR: openclaw backup create left no archive"; cat "$STAGE/openclaw-backup.out" >&2; exit 1; }
grep -q "Archive verification: passed" "$STAGE/openclaw-backup.out" || {
  log "ERROR: openclaw backup create did not verify the archive"; cat "$STAGE/openclaw-backup.out" >&2; exit 1; }
mv "$OC_ARCHIVE" "$STAGE/openclaw-state.tar.gz"

# 2. engram -- VACUUM INTO gives a consistent snapshot of a live SQLite database.
if [ -f "$HOME/.engram/engram.db" ]; then
  sqlite3 "$HOME/.engram/engram.db" "VACUUM INTO '$STAGE/engram.db'"
else
  log "note: ~/.engram/engram.db does not exist"
fi

# 3. The config that lives in no repo. Paths are relative to $HOME and only listed when they
#    exist, so a host that never had one of them is not an error.
: > "$STAGE/ai-config.list"
for p in \
  ".claude/CLAUDE.md" ".claude/settings.json" ".claude/hooks" \
  ".config/shell" \
  ".openclaw/openclaw.json" ".openclaw/telegram.token" ".openclaw/gateway.systemd.env" \
  ".openclaw/kit-host.env" ".openclaw/bin" \
  ".config/systemd/user/openclaw-gateway.service" \
  ".config/systemd/user/openclaw-backup@.service" \
  ".config/systemd/user/openclaw-backup-daily.timer" \
  ".config/systemd/user/openclaw-backup-weekly.timer" \
  ".config/systemd/user/openclaw-backup-monthly.timer" \
  ".config/systemd/user/openclaw-maintenance.service" \
  ".config/systemd/user/openclaw-maintenance.timer" \
  ".config/systemd/user/openclaw-watchdog.service" \
  ".config/systemd/user/openclaw-watchdog.timer" \
  ".config/systemd/user/openclaw-verify.service" \
  ".config/systemd/user/openclaw-verify.timer" \
  ".local/bin/openclaw-backup.sh" ".local/bin/openclaw-maintenance.sh" \
  ".local/bin/openclaw-watchdog.sh" ".local/bin/openclaw-verify.sh" \
  ".local/bin/openclaw-team-watch.py"
do
  [ -e "$HOME/$p" ] && echo "$p" >> "$STAGE/ai-config.list"
done

# Agent workspaces: read from openclaw.json (relative to $HOME), plus whatever the operator
# listed in OPENCLAW_BACKUP_EXTRA.
workspaces() {
  python3 - <<'PY' 2>/dev/null || true
import json, os
home = os.path.expanduser("~")
try:
    doc = json.load(open(os.path.join(home, ".openclaw", "openclaw.json")))
except Exception:
    doc = {}
seen = set()
for entry in (doc.get("agents", {}).get("entries") or {}).values():
    ws = entry.get("workspace")
    if not ws:
        continue
    ws = os.path.realpath(os.path.expanduser(ws))
    if ws.startswith(home + os.sep) and ws not in seen:
        seen.add(ws)
        print(os.path.relpath(ws, home))
PY
}
for ws in $(workspaces) ${OPENCLAW_BACKUP_EXTRA:-}; do
  for f in AGENTS.md SOUL.md IDENTITY.md USER.md MEMORY.md DREAMS.md DOCS.md memory; do
    [ -e "$HOME/$ws/$f" ] && echo "$ws/$f" >> "$STAGE/ai-config.list"
  done
done
tar czf "$STAGE/ai-config.tar.gz" -C "$HOME" -T "$STAGE/ai-config.list"

# 4. Inventory: what this snapshot is, so a restore is not archaeology.
{
  echo "host:        $(hostname)"
  echo "taken:       $(date -Is)"
  echo "tier:        $TIER"
  echo "openclaw:    $(openclaw --version 2>/dev/null | head -1)"
  echo "node:        $(node -v 2>/dev/null)"
  echo "claude:      $(readlink -f "$HOME/.local/bin/claude" 2>/dev/null | xargs -r basename)"
  echo "ai-resources:$(brew list --versions ai-resources 2>/dev/null | head -1)"
  echo
  echo "--- agents (id | model | workspace) ---"
  python3 - <<'PY' 2>/dev/null || true
import json, os
d = json.load(open(os.path.expanduser("~/.openclaw/openclaw.json")))
dm = (d["agents"]["defaults"].get("model") or {}).get("primary", "?")
for k, v in d["agents"]["entries"].items():
    print(f'{k} | {(v.get("model") or {}).get("primary", dm + " (default)")} | {v.get("workspace", "-")}')
PY
  echo
  echo "--- restore ---"
  echo "1. tar xzf $NAME.tar.gz"
  echo "2. openclaw backup restore openclaw-state.tar.gz   (or unpack it over ~/.openclaw)"
  echo "3. cp engram.db ~/.engram/engram.db"
  echo "4. tar xzf ai-config.tar.gz -C \$HOME"
  echo "5. brew install ai-resources && ai-resources setup   (rebuilds ~/.claude skills + subagents)"
  echo "6. openclaw gateway install --force && systemctl --user enable --now openclaw-gateway.service"
} > "$STAGE/INVENTORY.txt"

# Single tarball per run, then the checksum LAST as the completeness witness.
tar czf "$DEST/$NAME.tar.gz" -C "$STAGE" \
  openclaw-state.tar.gz ai-config.tar.gz INVENTORY.txt \
  $( [ -f "$STAGE/engram.db" ] && echo engram.db )

BYTES=$(stat -c %s "$DEST/$NAME.tar.gz")
[ "$BYTES" -ge "$MIN_BYTES" ] || { log "ERROR: $NAME.tar.gz weighs only $BYTES bytes"; rm -f "$DEST/$NAME.tar.gz"; exit 1; }
tar tzf "$DEST/$NAME.tar.gz" >/dev/null || { log "ERROR: $NAME.tar.gz is not listable"; rm -f "$DEST/$NAME.tar.gz"; exit 1; }
( cd "$DEST" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
( cd "$DEST" && sha256sum -c "$NAME.tar.gz.sha256" >/dev/null ) || {
  log "ERROR: checksum does not match"; rm -f "$DEST/$NAME.tar.gz" "$DEST/$NAME.tar.gz.sha256"; exit 1; }

# Rotation: keep the newest $KEEP complete pairs.
mapfile -t OLD < <(ls -1t "$DEST"/*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) || true)
for f in "${OLD[@]:-}"; do [ -n "$f" ] || continue; rm -f "$f" "$f.sha256"; log "rotated out $(basename "$f")"; done

log "OK $NAME.tar.gz ($(du -h "$DEST/$NAME.tar.gz" | cut -f1)) -- $(ls -1 "$DEST"/*.tar.gz 2>/dev/null | wc -l) left in $TIER"
