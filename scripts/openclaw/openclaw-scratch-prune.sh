#!/usr/bin/env bash
# openclaw-scratch-prune.sh - the daily prune (ADR-0020).
#
#   - `scratch prune`: expired run dirs under ~/.cache/ai-resources/runs and venvs idle for 30 days, decided by
#     .scratch-meta.json and owner-pid liveness, never by mtime; live runs are skipped; 2 GiB global cap (AI_RESOURCES_SCRATCH_CAP_GB), plus expired kit backups.
#   - `scratch prune-scans`: keep the newest 7 basic + 2 weekly runs of ~/.openclaw/security-scan-results.
#
# Both are best effort: a failure of one never skips the other, and neither touches anything outside its own root.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/scratch-prune.log"
mkdir -p "$OPENCLAW_LOG_DIR"
log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }

rc=0
for sub in prune prune-scans; do
  if out="$(_scratch_py "$sub" 2>&1)"; then
    log "$sub: $(tail -n1 <<<"$out")"
  else
    log "$sub FAILED: $(tr '\n' ' ' <<<"$out" | cut -c1-400)"
    rc=1
  fi
done
exit "$rc"
