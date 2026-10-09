#!/usr/bin/env bash
# openclaw-models-update.sh — the daily background upgrade of the Claude models OpenClaw runs on.
#
# All the work (discovery, policy, smoke probe, `openclaw config patch`, drained restart, health
# check, rollback) is `ai-resources models update --unattended`. This script keeps what is its own:
#   - standing down while a maintenance window is open (watchdog.off);
#   - turning the exit code into a report line in models-update.log;
#   - telling the operator ONLY when something changed or needs a decision.
#
# Exit codes of the command (one table, see docs/multi-model.md):
#   0 ok / no change   1 error   2 switched then rolled back   4 smoke or patch rejected before any write
#   6 rollback FAILED (critical)   10 approval pending   73 another run holds the lock   75 deferred
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/models-update.log"
NOTIFIED="$OPENCLAW_LOG_DIR/models-update.notified"
mkdir -p "$OPENCLAW_LOG_DIR"
log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }

if [ -e "$OPENCLAW_WATCHDOG_OFF" ]; then
  log "watchdog.off exists: a maintenance window is open, standing down"
  exit 0
fi

out="$(ai-resources models update --unattended --json 2>&1)"
rc=$?
log "rc=$rc $(tr '\n' ' ' <<<"$out" | cut -c1-600)"

# A field of the JSON report, "" when it cannot be read.
field(){ python3 -c 'import json,sys
try: d=json.loads(sys.stdin.read())
except Exception: sys.exit(0)
v=d.get(sys.argv[1], "")
print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1" <<<"$out" 2>/dev/null; }

# The exact commands that approve what is waiting, one per line.
approve_lines(){ python3 -c 'import json,sys
try: d=json.loads(sys.stdin.read())
except Exception: sys.exit(0)
for p in d.get("proposals", []):
    if p.get("decision") == "needs_approval":
        print("ai-resources models approve %s %s   # %s" % (p["cls"], p["new"], "; ".join(p.get("reasons", []))))' <<<"$out" 2>/dev/null; }

say(){ notify "$1" || log "note: could not notify over Telegram"; }

case "$rc" in
  0)
    if [ "$(field outcome)" = "switched" ]; then
      say "OpenClaw models updated: $(field message). Run \`ai-resources setup\` to re-render LiteLLM and the executors."
    fi ;;
  10)
    lines="$(approve_lines)"
    key="$(sha256sum <<<"$lines" | cut -d' ' -f1)"
    if [ -n "$lines" ] && [ "$(cat "$NOTIFIED" 2>/dev/null)" != "$key" ]; then
      echo "$key" > "$NOTIFIED"
      say "OpenClaw: newer Claude models are waiting for your approval. To allow one:
$lines"
    fi ;;
  1)  say "OpenClaw models update FAILED (error): $(field message)" ;;
  2)  say "OpenClaw models update was rolled back: $(field message)" ;;
  4)  say "OpenClaw models update stopped before any change: $(field message)" ;;
  6)  say "CRITICAL: OpenClaw models update could not roll back: $(field message). Check the gateway now (\`openclaw health\`, \`ai-resources models status\`)." ;;
  75)
    [ "$(field deferrals)" -ge 3 ] 2>/dev/null && say "OpenClaw models update has been deferred $(field deferrals) times (the gateway is busy). It resumes by itself." ;;
  73) : ;;
  *)  say "OpenClaw models update exited with an unexpected code $rc." ;;
esac
exit "$rc"
