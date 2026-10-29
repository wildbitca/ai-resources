#!/usr/bin/env bash
# openclaw-backup.sh — off-box-ready backup of the non-reproducible state of an OpenClaw host.
#
# Usage: openclaw-backup.sh <daily|weekly|monthly|manual>
#
# Mode (OPENCLAW_BACKUP_MODE, default minimal):
#   minimal  An allowlist of state that is not reproducible and not provided by ai-resources:
#              1. SQLite snapshots made by `openclaw backup sqlite create` (the shared database and one per
#                 agent that has its own database), plus engram through `VACUUM INTO`. A live .sqlite/-wal/-shm is
#                 never copied: the OpenClaw docs forbid it.
#              2. The files listed by _backup_lib.sh: config, secrets, credentials, extensions, sessions, workspace
#                 notes, the AI config that lives in no repo, the systemd units and the openclaw-* host scripts.
#              3. An inventory (versions, agents, models, restore steps).
#            Measured on the reference host: 323 MB and about 73 s, against 3.7-11 GB and 6-30 min for `full`.
#   full     The legacy path, kept as a rollback: `openclaw backup create` (all of ~/.openclaw, no exclude options)
#            plus engram plus the AI-config tar. Set OPENCLAW_BACKUP_MODE=full in kit-host.env to use it.
#
# It deliberately does NOT capture what `ai-resources setup` regenerates (kit roles and lanes, skills, subagents),
# ~/.claude/projects transcripts, project workspaces (git checkouts), or caches, venvs, worktrees and logs.
#
# Conventions copied from k3s-backup.sh, including the lessons its runbook records:
#   - stage on disk under $OPENCLAW_BACKUP_DIR, never in /tmp (tmpfs, sized in RAM), in a 0700 directory;
#   - the `.sha256` sibling is written LAST and is the only witness that a tarball is complete: the uploader
#     skips tarballs without it;
#   - no `|| true` on anything that matters -- a partial backup reported as good is worse than no backup;
#   - assert the result instead of assuming it;
#   - a run that did not produce a backup never exits 0: a held lock is waited for, then a failure.
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
# shellcheck source=_backup_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_backup_lib.sh"

TIER="${1:-manual}"
# Retention per tier, configurable in kit-host.env. A count under 1 would delete the backup just made.
case "$TIER" in
  daily)   KEEP="${OPENCLAW_BACKUP_KEEP_DAILY:-7}" ;;     # one week of dailies on disk
  weekly)  KEEP="${OPENCLAW_BACKUP_KEEP_WEEKLY:-4}" ;;    # about one month
  monthly) KEEP="${OPENCLAW_BACKUP_KEEP_MONTHLY:-3}" ;;   # one quarter
  manual)  KEEP="${OPENCLAW_BACKUP_KEEP_MANUAL:-3}" ;;
  *) echo "usage: $(basename "$0") <daily|weekly|monthly|manual>" >&2; exit 2 ;;
esac
case "$KEEP" in ''|*[!0-9]*|0) echo "$(basename "$0"): the retention for $TIER must be an integer >= 1 (got '$KEEP')" >&2; exit 2 ;; esac

MODE="${OPENCLAW_BACKUP_MODE:-minimal}"
case "$MODE" in minimal|full) ;; *) echo "$(basename "$0"): OPENCLAW_BACKUP_MODE must be minimal or full (got '$MODE')" >&2; exit 2 ;; esac

BASE="$OPENCLAW_BACKUP_DIR"
DEST="$BASE/$TIER"
LOG="$OPENCLAW_LOG_DIR/backup.log"
TS="$(date -u +%Y%m%d-%H%M%S)"
NAME="openclaw-$TS"
LOCK="$XDG_RUNTIME_DIR/openclaw-backup.lock"
LOCK_WAIT="${OPENCLAW_BACKUP_LOCK_WAIT:-3600}"       # seconds to wait for another run before failing
# The guard calls under 5 MB "not a backup of this setup"; the ceiling catches an allowlist that swallowed a tree.
MIN_BYTES="${OPENCLAW_BACKUP_MIN_BYTES:-5000000}"
MAX_BYTES="${OPENCLAW_BACKUP_MAX_BYTES:-4294967296}"
[ "$MODE" = full ] && MAX_BYTES="${OPENCLAW_BACKUP_MAX_BYTES:-34359738368}"
STAGE=""

umask 077                                              # the archive carries secrets
mkdir -p "$DEST" "$(dirname "$LOG")"

log(){ echo "$(date '+%F %T') [$TIER] $*" | tee -a "$LOG" >&2; }

# One EXIT trap does the cleanup and the failure notice, so a failure is announced exactly once however it
# happened (a failed command under set -e, or a `fail` call). Best effort: the notice never masks the error.
on_exit(){
  local rc=$?
  trap - EXIT
  [ -z "$STAGE" ] || rm -rf "$STAGE"
  if [ "$rc" -ne 0 ]; then
    rm -f "$DEST/$NAME.tar.gz.part"
    # A tarball without its checksum is not a backup: never leave one for the uploader or the guard to find.
    [ -f "$DEST/$NAME.tar.gz.sha256" ] || rm -f "$DEST/$NAME.tar.gz"
    log "FAILED (rc=$rc) -- see $LOG"
    notify "openclaw-backup ($TIER) failed on $(hostname) (rc=$rc). Log: ~/.openclaw/logs/backup.log" || true
  fi
  exit "$rc"
}
trap on_exit EXIT

fail(){ local rc="$1"; shift; log "ERROR: $*"; exit "$rc"; }

# The lock is shared by every tier so two runs never write the same disk at once. A tier that finds it held waits
# (bounded) instead of skipping: the old "another run in progress -- skipping" with exit 0 let the weekly backup go
# 17 days stale without a single alert.
exec 9>"$LOCK"
if ! flock -n 9; then
  log "another run holds the lock -- waiting up to ${LOCK_WAIT}s"
  flock -w "$LOCK_WAIT" 9 || fail 75 "could not take $LOCK within ${LOCK_WAIT}s; no backup was made"
fi

# The name is taken now, not before the wait: after an hour in the queue the timestamp would be an hour stale.
TS="$(date -u +%Y%m%d-%H%M%S)"
NAME="openclaw-$TS"
STAGE="$(scratch_tmp_beside -d "$BASE" "stage.XXXXXXXX")"
chmod 700 "$STAGE"
log "start ($MODE) -> $DEST/$NAME.tar.gz"

# ---- shared pieces -----------------------------------------------------------------------------------------

engram_snapshot(){ # $1 = destination file. VACUUM INTO gives a consistent snapshot of a live SQLite database.
  if [ -f "$HOME/.engram/engram.db" ]; then
    mkdir -p "$(dirname "$1")"
    sqlite3 "$HOME/.engram/engram.db" "VACUUM INTO '$1'"
    return 0
  fi
  log "note: ~/.engram/engram.db does not exist"
}

inventory(){ # $1 = restore steps file
  {
    echo "host:        $(hostname)"
    echo "taken:       $(date -Is)"
    echo "tier:        $TIER"
    echo "mode:        $MODE"
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
    cat "$1"
  }
}

# ---- minimal mode ------------------------------------------------------------------------------------------

EXPECTED_DBS=()      # snapshot repositories (names under db/) that must exist in the archive

build_minimal(){
  local id ws tsv
  mkdir -m 700 "$STAGE/db"

  # 1. SQLite state through OpenClaw's own online-backup command. The stage parent is not group-writable, which
  #    the command requires of a repository.
  openclaw backup sqlite create --global --repository "$STAGE/db/openclaw-global" >"$STAGE/sqlite-global.out" 2>&1 \
    || { cat "$STAGE/sqlite-global.out" >&2; fail 1 "openclaw backup sqlite create --global failed"; }
  EXPECTED_DBS+=("openclaw-global")

  tsv="$(backup_agents_tsv)" || fail 1 "could not read the agent list from 'openclaw agents list --json'"
  local ids=() wss=()
  while IFS=$'\t' read -r id ws; do
    [ -n "$id" ] || continue
    wss+=("$ws")
    if backup_agent_has_db "$HOME" "$id"; then ids+=("$id"); else log "note: agent $id has no database, skipped"; fi
  done <<<"$tsv"
  if [ "${#ids[@]}" -gt 0 ]; then
    printf '%s\n' "${ids[@]}" \
      | xargs -P 3 -I{} sh -c 'openclaw backup sqlite create --agent "$1" --repository "$2/db/agent-$1" >"$2/sqlite-agent-$1.out" 2>&1 || { cat "$2/sqlite-agent-$1.out" >&2; exit 1; }' _ {} "$STAGE" \
      || fail 1 "an 'openclaw backup sqlite create --agent' run failed"
    for id in "${ids[@]}"; do EXPECTED_DBS+=("agent-$id"); done
  fi
  engram_snapshot "$STAGE/engram/engram.db"

  # 2. The files, by allowlist. Workspaces come from the live agent list plus whatever the operator listed.
  # shellcheck disable=SC2086
  backup_allowlist "$HOME" "${wss[@]}" ${OPENCLAW_BACKUP_EXTRA:-} > "$STAGE/files.list"
  [ -s "$STAGE/files.list" ] || fail 1 "the allowlist matched no file under $HOME"
  local excl=() pat
  for pat in "${BACKUP_TAR_EXCLUDES[@]}"; do excl+=("--exclude=$pat"); done
  tar -C "$HOME" "${excl[@]}" -T "$STAGE/files.list" -cf "$STAGE/files.tar"

  # 3. Inventory with the restore steps of this layout.
  local steps="$STAGE/restore.txt"
  cat > "$steps" <<EOF
1. mkdir -p /srv/restore && tar xzf $NAME.tar.gz -C /srv/restore
2. for s in /srv/restore/db/*/*/; do openclaw backup sqlite verify "\$s"; done
3. tar xpf /srv/restore/files.tar -C \$HOME           (config, secrets, workspaces, units, host scripts)
4. openclaw backup sqlite restore <snapshot> --target <new path>   (global: the shared state database; agent-<id>: that agent's)
5. mkdir -p ~/.engram && cp /srv/restore/engram/engram.db ~/.engram/engram.db
6. brew install ai-resources && ai-resources setup   (regenerates the kit roles, lanes, skills and subagents)
7. openclaw gateway install --force && systemctl --user enable --now openclaw-gateway.service
EOF
  inventory "$steps" > "$STAGE/INVENTORY.txt"

  ARCHIVE_MEMBERS=(db files.tar INVENTORY.txt)
  [ -d "$STAGE/engram" ] && ARCHIVE_MEMBERS+=(engram)
  return 0
}

# ---- full mode (legacy rollback) ---------------------------------------------------------------------------

build_full(){
  # 1. openclaw's own backup (config, state, agent databases, secret store).
  openclaw backup create --no-include-workspace --verify --output "$STAGE" >"$STAGE/openclaw-backup.out" 2>&1
  local oc_archive
  oc_archive="$(find "$STAGE" -maxdepth 1 -name '*-openclaw-backup.tar.gz' -print -quit)"
  [ -n "$oc_archive" ] || { cat "$STAGE/openclaw-backup.out" >&2; fail 1 "openclaw backup create left no archive"; }
  grep -q "Archive verification: passed" "$STAGE/openclaw-backup.out" || {
    cat "$STAGE/openclaw-backup.out" >&2; fail 1 "openclaw backup create did not verify the archive"; }
  mv "$oc_archive" "$STAGE/openclaw-state.tar.gz"

  # 2. engram.
  engram_snapshot "$STAGE/engram.db"

  # 3. The config that lives in no repo, plus the legacy per-workspace notes.
  local tsv ws id wss=()
  tsv="$(backup_agents_tsv 2>/dev/null || true)"
  while IFS=$'\t' read -r id ws; do [ -z "$ws" ] || wss+=("$ws"); done <<<"$tsv"
  # shellcheck disable=SC2086
  backup_allowlist "$HOME" "${wss[@]}" ${OPENCLAW_BACKUP_EXTRA:-} > "$STAGE/ai-config.list"
  local excl=() pat
  for pat in "${BACKUP_TAR_EXCLUDES[@]}"; do excl+=("--exclude=$pat"); done
  tar czf "$STAGE/ai-config.tar.gz" "${excl[@]}" -C "$HOME" -T "$STAGE/ai-config.list"

  # 4. Inventory.
  local steps="$STAGE/restore.txt"
  cat > "$steps" <<EOF
1. tar xzf $NAME.tar.gz
2. openclaw backup restore openclaw-state.tar.gz   (or unpack it over ~/.openclaw)
3. cp engram.db ~/.engram/engram.db
4. tar xzf ai-config.tar.gz -C \$HOME
5. brew install ai-resources && ai-resources setup   (rebuilds ~/.claude skills + subagents)
6. openclaw gateway install --force && systemctl --user enable --now openclaw-gateway.service
EOF
  inventory "$steps" > "$STAGE/INVENTORY.txt"

  ARCHIVE_MEMBERS=(openclaw-state.tar.gz ai-config.tar.gz INVENTORY.txt)
  [ -f "$STAGE/engram.db" ] && ARCHIVE_MEMBERS+=(engram.db)
  return 0
}

# ---- build, assert, publish --------------------------------------------------------------------------------

ARCHIVE_MEMBERS=()
if [ "$MODE" = minimal ]; then build_minimal; else build_full; fi

PART="$DEST/$NAME.tar.gz.part"
tar -C "$STAGE" -cf - "${ARCHIVE_MEMBERS[@]}" | backup_gzip > "$PART"

# Assertions on the unpublished file. Any failure leaves nothing behind for the uploader or the guard to find.
BYTES=$(stat -c %s "$PART")
[ "$BYTES" -ge "$MIN_BYTES" ] || fail 1 "$NAME.tar.gz weighs only $BYTES bytes (minimum $MIN_BYTES)"
[ "$BYTES" -le "$MAX_BYTES" ] || fail 1 "$NAME.tar.gz weighs $BYTES bytes, over the $MAX_BYTES ceiling: the allowlist swallowed something"
tar -tzf "$PART" > "$STAGE/archive.list" || fail 1 "$NAME.tar.gz is not listable"
[ -s "$STAGE/archive.list" ] || fail 1 "$NAME.tar.gz lists no member"

if [ "$MODE" = minimal ]; then
  # Every snapshot that was created must be in the archive, with its database and manifest.
  sed -n 's#^\(db/[^/]*\)/[^/]*/database\.sqlite$#\1#p' "$STAGE/archive.list" > "$STAGE/archive.dbs"
  sed -n 's#^\(db/[^/]*\)/[^/]*/manifest\.json$#\1#p' "$STAGE/archive.list" > "$STAGE/archive.manifests"
  for r in "${EXPECTED_DBS[@]}"; do
    grep -qxF "db/$r" "$STAGE/archive.dbs" || fail 1 "the archive has no database snapshot for $r"
    grep -qxF "db/$r" "$STAGE/archive.manifests" || fail 1 "the archive has no manifest for the $r snapshot"
  done
  [ -f "$HOME/.engram/engram.db" ] && { grep -qxF "engram/engram.db" "$STAGE/archive.list" || fail 1 "the archive has no engram.db"; }
  tar -tf "$STAGE/files.tar" > "$STAGE/files.list.archived"
  if [ -f "$HOME/.openclaw/openclaw.json" ]; then
    grep -qxF ".openclaw/openclaw.json" "$STAGE/files.list.archived" || fail 1 "files.tar has no .openclaw/openclaw.json"
  fi
else
  grep -qxF "openclaw-state.tar.gz" "$STAGE/archive.list" || fail 1 "the archive has no openclaw-state.tar.gz"
fi

mv "$PART" "$DEST/$NAME.tar.gz"
# The checksum LAST: it is the completeness witness. It is then checked against the file it names.
( cd "$DEST" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
( cd "$DEST" && sha256sum -c "$NAME.tar.gz.sha256" >/dev/null ) || {
  rm -f "$DEST/$NAME.tar.gz.sha256"; fail 1 "checksum does not match"; }

# Rotation: keep the newest $KEEP complete pairs.
mapfile -t OLD < <(ls -1t "$DEST"/*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) || true)
for f in "${OLD[@]:-}"; do [ -n "$f" ] || continue; rm -f "$f" "$f.sha256"; log "rotated out $(basename "$f")"; done

log "OK $NAME.tar.gz ($MODE, $(du -h "$DEST/$NAME.tar.gz" | cut -f1)) -- $(ls -1 "$DEST"/*.tar.gz 2>/dev/null | wc -l) left in $TIER"
