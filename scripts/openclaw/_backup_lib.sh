# shellcheck shell=bash
# _backup_lib.sh — the allowlist and the exclude rules of the minimal OpenClaw backup. Source it; never execute it.
#
# Kept apart from openclaw-backup.sh so the tests can build a list against a fixture home without running a backup.
# The minimal backup captures only state that is NOT reproducible and is NOT provided by ai-resources: everything
# `ai-resources setup` regenerates (kit roles and lanes, skills, subagents, rendered units' templates) stays out.

# tar exclude patterns applied to the whole allowlist: editor and tool backups, reconcile leftovers, lock files and
# the live SQLite sidecars (a -wal/-shm copied next to a hot database is corrupt; the snapshots cover the databases).
BACKUP_TAR_EXCLUDES=(
  '*.bak' '*.bak-*' '*.bak.*' '*.reconcile-*' '*.lock' '*-wal' '*-shm'
)

# Paths relative to $HOME, copied whole when they exist.
BACKUP_HOME_PATHS=(
  .openclaw/openclaw.json .openclaw/.env .openclaw/credentials
  .openclaw/gateway.systemd.env .openclaw/kit-host.env .openclaw/kit-host-overrides.json5
  .openclaw/config-journal-fingerprint.key .openclaw/telegram.token
  .openclaw/extensions .openclaw/bin
  .openclaw/agents/main/agent/workshop-skills
  .claude/CLAUDE.md .claude/settings.json .claude/hooks
  .config/shell
  .config/ai-resources/model-pins.json .config/ai-resources/operator-rules.md .config/ai-resources/lanes.yaml
  .config/ai-resources/offbox-wif-cred.json .config/ai-resources/setup-state.yaml
  .config/systemd/user
)

# Files and directories of an agent workspace that hold hand-written state. DREAMS.md and DOCS.md predate the
# minimal mode and stay on the list.
BACKUP_WORKSPACE_ITEMS=(
  AGENTS.md MEMORY.md USER.md SOUL.md IDENTITY.md TOOLS.md DREAMS.md DOCS.md memory monitors
)

# backup_allowlist <home> [workspace ...]
# Prints, one per line and relative to <home>, every allowlisted path that exists. A workspace may be absolute or
# relative to <home>; one outside <home> cannot be archived relative to it and is skipped. Duplicates are dropped.
backup_allowlist() {
  local home="$1"; shift
  local p ws f s rel
  {
    for p in "${BACKUP_HOME_PATHS[@]}"; do
      [ -e "$home/$p" ] && printf '%s\n' "$p"
    done
    # Only openclaw-* from ~/.local/bin: the directory holds hundreds of MB of binaries that are not ours.
    for f in "$home"/.local/bin/openclaw-*; do
      [ -f "$f" ] && printf '.local/bin/%s\n' "$(basename "$f")"
    done
    for s in "$home"/.openclaw/agents/*/sessions; do
      [ -d "$s" ] && printf '%s\n' "${s#"$home"/}"
    done
    for ws in "$@"; do
      [ -n "$ws" ] || continue
      case "$ws" in /*) rel="${ws#"$home"/}"; [ "$rel" != "$ws" ] || continue ;; *) rel="$ws" ;; esac
      # Workspaces of the kit's own role and lane agents are rendered by `ai-resources setup`: not ours to keep.
      case "$rel" in .openclaw/kit-roles|.openclaw/kit-roles/*|.openclaw/kit-lanes|.openclaw/kit-lanes/*) continue ;; esac
      for f in "${BACKUP_WORKSPACE_ITEMS[@]}"; do
        [ -e "$home/$rel/$f" ] && printf '%s/%s\n' "$rel" "$f"
      done
    done
    :   # the last test above may be false; a pipeline under pipefail would report it as a failure
  } | awk '!seen[$0]++'
}

# backup_agents_tsv — `id<TAB>workspace` per configured agent, from `openclaw agents list --json`. Fails (non-zero)
# when the command or its output is unusable: the snapshot list must come from the live config, never be guessed.
backup_agents_tsv() {
  local json
  json="$(openclaw agents list --json)" || return 1
  printf '%s' "$json" | python3 -I -c '
import json, sys
data = json.load(sys.stdin)
rows = data if isinstance(data, list) else data.get("agents", data)
if not isinstance(rows, list) or not rows:
    sys.exit("no agents in the list output")
for row in rows:
    if isinstance(row, dict):
        print("%s\t%s" % (row["id"], row.get("workspace") or ""))
    else:
        print("%s\t" % row)
'
}

# backup_agent_has_db <home> <id> — an agent without its own database (an unconfigured directory such as
# agents/openclaw) is skipped: `openclaw backup sqlite create --agent` rejects it as an unknown agent id.
backup_agent_has_db() { [ -f "$1/.openclaw/agents/$2/agent/openclaw-agent.sqlite" ]; }

# backup_gzip — the compressor command: pigz on two threads when present, plain gzip otherwise.
backup_gzip() {
  if command -v pigz >/dev/null 2>&1; then pigz -p 2; else gzip; fi
}
