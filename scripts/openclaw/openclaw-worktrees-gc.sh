#!/usr/bin/env bash
# openclaw-worktrees-gc.sh - the weekly `openclaw worktrees gc` (ADR-0021).
#
# OpenClaw decides what is removable (snapshotted, clean, published); the kit only schedules it. The job is queued in the
# background and its id is logged; this script never removes a worktree itself.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/worktrees-gc.log"
mkdir -p "$OPENCLAW_LOG_DIR"
log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }

if out="$(openclaw worktrees gc --json 2>&1)"; then
  log "queued: $(tr '\n' ' ' <<<"$out" | cut -c1-400)"
else
  log "FAILED: $(tr '\n' ' ' <<<"$out" | cut -c1-400)"
  exit 1
fi
