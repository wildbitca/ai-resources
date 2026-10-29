#!/usr/bin/env bash
# openclaw-backup.sh — off-box-ready backup of the non-reproducible state of an OpenClaw host.
#
# Usage: openclaw-backup.sh <daily|weekly|monthly|manual>
#
# What it captures: an allowlist of state that is not reproducible and not provided by ai-resources:
#   1. SQLite snapshots made by `openclaw backup sqlite create` (the shared database and one per agent that has its
#      own database), plus engram through `VACUUM INTO`. A live .sqlite/-wal/-shm is never copied: the OpenClaw docs
#      forbid it.
#   2. The files listed by _backup_lib.sh: config, secrets, credentials, extensions, sessions, workspace notes, the AI
#      config that lives in no repo, the systemd units and the openclaw-* host scripts.
#   3. An inventory (versions, agents, models, restore steps).
# Measured on the reference host: 323 MB and about 73 s. There is one backup layout; it has no mode switch.
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
    rm -f "$DEST/$NAME.tar.gz.part" "$DEST/$NAME.tar.gz.sha256.part"
    # An empty checksum is no witness; a tarball without a valid one is not a backup: never leave either for the
    # uploader or the guard to find.
    [ -s "$DEST/$NAME.tar.gz.sha256" ] || rm -f "$DEST/$NAME.tar.gz.sha256" "$DEST/$NAME.tar.gz"
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

# A SIGKILL or reboot mid-run leaves a .part behind; nothing else would ever remove it.
find "$DEST" -maxdepth 1 \( -name '*.tar.gz.part' -o -name '*.sha256.part' \) -mmin +60 -delete

# The same kill leaves a stage.* directory (a full copy of the state) under $BASE; the EXIT trap never ran. The lock
# is held, so no other run owns a stage: anything older than an hour is orphaned. No symlink is followed.
# One pass: the listing that is counted is the listing that is removed (a dir crossing the 60 minute mark between two
# finds skewed the count). find -type d never matches a symlink and rm -rf on a path never follows one. The trailing
# slash in "$BASE/" makes find enter $BASE when it is itself a symlink (a backup disk linked into place).
stale_stages=0
while IFS= read -r -d '' stale; do
  if rm -rf -- "$stale"; then stale_stages=$((stale_stages + 1)); else log "could not remove stale stage $stale"; fi
done < <(find "$BASE/" -maxdepth 1 -type d -name 'stage.*' -mmin +60 -print0)
[ "$stale_stages" -eq 0 ] || log "removed $stale_stages stale stage dir(s)"

# The name is taken now, not before the wait: after an hour in the queue the timestamp would be an hour stale.
TS="$(date -u +%Y%m%d-%H%M%S)"
NAME="openclaw-$TS"
STAGE="$(scratch_tmp_beside -d "$BASE" "stage.XXXXXXXX")"
chmod 700 "$STAGE"
log "start -> $DEST/$NAME.tar.gz"

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
  # Sessions and memory files are appended to while the gateway runs: tar exits 1 for "file changed as we read it",
  # which is not a failed backup. Exit 2 or more (a real error) still fails the run.
  local tar_rc=0
  tar -C "$HOME" "${excl[@]}" --warning=no-file-changed --verbatim-files-from -T "$STAGE/files.list" \
    -cf "$STAGE/files.tar" || tar_rc=$?
  [ "$tar_rc" -le 1 ] || fail 1 "tar of the allowlist failed (rc=$tar_rc)"

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

# ---- build, assert, publish --------------------------------------------------------------------------------

ARCHIVE_MEMBERS=()
build_minimal

PART="$DEST/$NAME.tar.gz.part"
tar -C "$STAGE" -cf - "${ARCHIVE_MEMBERS[@]}" | backup_gzip > "$PART"

# Assertions on the unpublished file. Any failure leaves nothing behind for the uploader or the guard to find.
BYTES=$(stat -c %s "$PART")
[ "$BYTES" -ge "$MIN_BYTES" ] || fail 1 "$NAME.tar.gz weighs only $BYTES bytes (minimum $MIN_BYTES)"
[ "$BYTES" -le "$MAX_BYTES" ] || fail 1 "$NAME.tar.gz weighs $BYTES bytes, over the $MAX_BYTES ceiling: the allowlist swallowed something"
tar -tzf "$PART" > "$STAGE/archive.list" || fail 1 "$NAME.tar.gz is not listable"
[ -s "$STAGE/archive.list" ] || fail 1 "$NAME.tar.gz lists no member"

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

mv "$PART" "$DEST/$NAME.tar.gz"
# The checksum LAST: it is the completeness witness. It is then checked against the file it names.
# Written beside, checked, then renamed: the final name never exists empty or wrong.
( cd "$DEST" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256.part" )
( cd "$DEST" && sha256sum -c "$NAME.tar.gz.sha256.part" >/dev/null ) || fail 1 "checksum does not match"
mv "$DEST/$NAME.tar.gz.sha256.part" "$DEST/$NAME.tar.gz.sha256"

# Rotation: keep the newest $KEEP complete pairs.
# Ordered by name (the timestamp is in it), so a skewed mtime cannot push the fresh archive out.
mapfile -t OLD < <(ls -1r "$DEST"/*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) || true)
for f in "${OLD[@]:-}"; do [ -n "$f" ] || continue; [ "$f" != "$DEST/$NAME.tar.gz" ] || continue; rm -f "$f" "$f.sha256"; log "rotated out $(basename "$f")"; done

log "OK $NAME.tar.gz ($(du -h "$DEST/$NAME.tar.gz" | cut -f1)) -- $(ls -1 "$DEST"/*.tar.gz 2>/dev/null | wc -l) left in $TIER"
