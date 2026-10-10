#!/usr/bin/env bash
# openclaw-health-restart.sh — periodic check of an OpenClaw gateway that is alive but degraded
# (openclaw-watchdog only covers one that is DOWN). It is the SCHEDULER and the NOTIFIER; the signal
# reading, the gates and the restart itself belong to the Python CLIs it calls (ADR-0004):
#
#   ai-resources openclaw pressure --json           classify (healthy|pressure|hard|frozen|refused-probe|health-fail)
#   ai-resources openclaw graceful-restart ...      the gates, the restart, the snapshots and the report
#
# It holds no copy of the cooldown, the daily cap or the window logic.
#
# Signals:
#   memory   confirmed in two samples one OPENCLAW_HEALTH_CONFIRM_S apart: cgroup >= PRESSURE_PCT of
#            MemoryHigh, swap >= 90 %, and `memory.events high` growing (see openclaw_pressure.py)
#   health   `openclaw health` fails or times out (with no memory pressure)
#   stalls   >= 2 * STALL_LIMIT "CLI produced no output" terminations in the last hour
#   frozen   main thread throttled by MemoryHigh, or "still starting" too long: a graceful restart is
#            impossible, so it only notifies (exit 2) and never restarts
#
# Contract (ADR-0004, supersedes ADR-0003 decision 7 for the memory trigger only):
#   - OPENCLAW_GRACEFUL_RESTART=off|notify|on. `notify` (the shipped default) tells the operator once
#     per episode what it WOULD do. Only `on` restarts, and only through the graceful-restart CLI, which
#     refuses outside the window (unless above the hard ceiling), inside the cooldown, past the daily cap,
#     under watchdog.off, from inside the gateway cgroup and while another run holds the lock.
#   - Confirmed memory pressure, and only that, restarts with runs in flight. The health and stalls
#     triggers keep honouring the busy probe: runs in flight or an unreadable probe mean "notify once".
#   - An idle gateway with a failed health check gets the kit's drained doctor, at most once per cooldown.
#   - A gateway that is not active is left to openclaw-watchdog; watchdog.off stands everything down.
#   - This script never restarts, stops or kills anything itself and never forces anything.
# `--dry-run` decides and logs; it sends nothing, writes no state and never touches the marker.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/health-restart.log"
STATE="$HOME/.openclaw/health-restart.state"        # key=value lines (the host copy's file, so the cooldown survives adoption)
NOTIFIED="$OPENCLAW_LOG_DIR/health-restart.notified" # the episode already reported to the operator
STALL_LIMIT=4
COOLDOWN_S="$OPENCLAW_RESTART_COOLDOWN_S"
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

mkdir -p "$OPENCLAW_LOG_DIR"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; [ -t 1 ] && echo "$*"; return 0; }
getv() { grep -s "^$1=" "$STATE" | tail -1 | cut -d= -f2; }
setv() { { grep -sv "^$1=" "$STATE"; echo "$1=$2"; } > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; }

# jget <json> <python expression over `d`> [default]: one field out of a JSON document, never a traceback.
jget() {
  printf '%s' "$1" | python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
    v = eval(sys.argv[1], {"__builtins__": {}}, {"d": d, "len": len, "str": str, "round": round})
except Exception:
    v = None
sys.stdout.write(sys.argv[2] if v is None else str(v))
' "$2" "${3:-}"
}

# notify_once <episode key> <message>: one Telegram per episode, not one per tick. The episode is marked only
# AFTER a send succeeded, so a notice that could not be delivered (a frozen gateway cannot deliver) is retried
# on the next tick. With no Telegram target there is nothing to retry: the episode is marked and logged.
notify_once() {
  if [ "$DRY" = 1 ]; then log "DRY RUN: would notify: $2"; return 0; fi
  [ "$(cat "$NOTIFIED" 2>/dev/null)" = "$1" ] && return 0
  if [ -z "${OPENCLAW_OWNER_TELEGRAM_ID:-}" ]; then
    echo "$1" > "$NOTIFIED"; log "note: no Telegram target configured; notice only in this log: $2"; return 0
  fi
  if notify "$2"; then echo "$1" > "$NOTIFIED"; else log "note: could not notify over Telegram; will retry next tick"; fi
}

# send_result <message>: the merged restart result. Retried a few times (the gateway has only just come back).
send_result() {
  [ "$DRY" = 1 ] && { log "DRY RUN: would notify: $1"; return 0; }
  local i
  for i in 1 2 3; do
    notify "$1" && return 0
    sleep 20
  done
  log "note: could not notify over Telegram"
}

if [ -e "$OPENCLAW_WATCHDOG_OFF" ]; then
  log "watchdog.off exists: a maintenance window is open, standing down"
  exit 0
fi

# Only act on a settled, active unit; transitions and downtime belong to openclaw-watchdog.
state="$(timeout 30 systemctl --user is-active "$OPENCLAW_UNIT" 2>/dev/null || true)"
if [ "$state" != active ]; then log "unit is '$state': skipping (the watchdog's job)"; exit 0; fi

# --- signals ---
stalls="$(timeout 30 journalctl --user -u "$OPENCLAW_UNIT" --since '1 hour ago' --no-pager 2>/dev/null | grep -c 'CLI produced no output')"
stalls="${stalls:-0}"

# `timeout 300`: part of the unit's time budget (openclaw_host.py, "Time budget"); a timed-out sample is
# an empty reading, which classifies refused-probe and fails closed.
pjson="$(timeout 300 ai-resources openclaw pressure --json --confirm-seconds "$OPENCLAW_HEALTH_CONFIRM_S" 2>/dev/null || true)"
class="$(jget "$pjson" 'd["classification"]' refused-probe)"
mem_pct="$(jget "$pjson" 'd["evidence"]["memory_pct"][-1]' '?')"
swap_pct="$(jget "$pjson" 'd["evidence"]["swap_pct"][-1]' '?')"
frozen_by="$(jget "$pjson" 'd["evidence"]["frozen_by"]' '')"
reason_txt="$(jget "$pjson" 'd["evidence"]["reason"]' '')"

log "class=$class mem=${mem_pct}% swap=${swap_pct}% stalls=$stalls mode=$OPENCLAW_GRACEFUL_RESTART${reason_txt:+ ($reason_txt)}"

# --- decision ---
reason=""; key=""
case "$class" in
  refused-probe)
    log "a required signal could not be read: fail closed, nothing acts"
    exit 0 ;;
  frozen)
    log "frozen (${frozen_by:-unknown}): a graceful restart is impossible; NOT restarting. Remedy: stop the heavy agent children by hand (docs/openclaw/pitfalls.md T41)"
    notify_once frozen "OpenClaw gateway is frozen under its memory limit (${frozen_by:-unknown}; cgroup ${mem_pct}% of MemoryHigh). A graceful restart would be refused, so none was attempted and no process was signalled. Free memory by stopping heavy agent children (Unity, tsc, Gradle) by hand; see pitfall T41."
    [ "$DRY" = 1 ] && exit 0
    exit 2 ;;
  pressure|hard)
    reason="memory pressure ($class: cgroup ${mem_pct}% of MemoryHigh, swap ${swap_pct}%)"; key=memory ;;
  health-fail)
    reason="openclaw health failing"; key=health ;;
  healthy)
    if [ "$stalls" -ge $((STALL_LIMIT * 2)) ]; then reason="$stalls CLI stalls in 1h"; key=stalls; fi ;;
esac
if [ -z "$reason" ]; then
  [ "$DRY" = 1 ] || rm -f "$NOTIFIED"      # a healthy tick ends the episode
  exit 0
fi

# --- the memory trigger: the graceful restart, through its own gates ---
if [ "$key" = memory ]; then
  case "$OPENCLAW_GRACEFUL_RESTART" in
    off)
      log "mode off: confirmed memory pressure only logged"; exit 0 ;;
    on) ;;
    *)
      # `notify`, and anything unrecognised: only `on` may restart, so a typo fails safe.
      log "mode notify: would restart gracefully ($reason)"
      notify_once memory "OpenClaw health check: $reason. Mode is 'notify', so nothing was restarted. A graceful restart aborts the runs in flight that outlast its 5 minute drain. Set OPENCLAW_GRACEFUL_RESTART=on to let the check do it, or look first with the graceful-restart dry run (docs/openclaw/runbook.md, Resource safety)."
      exit 0 ;;
  esac
  # ONE call site. A dry run passes --dry-run: the CLI evaluates every gate and mutates nothing.
  gr_args=(--reason memory --classification "$class" --json)
  [ "$DRY" = 1 ] && gr_args+=(--dry-run)
  log "mode on: running the graceful restart ($reason)${DRY:+ [dry run]}"
  out="$(ai-resources openclaw graceful-restart "${gr_args[@]}" 2>>"$LOG")"
  rrc=$?
  result="$(jget "$out" 'd["result"]' unknown)"
  log "graceful-restart rc=$rrc result=$result $(tr '\n' ' ' <<<"$out" | cut -c1-300)"
  [ "$DRY" = 1 ] && exit 0
  case "$rrc" in
    0)
      mb="$(jget "$out" 'round(d["memory_before"] / (1024 ** 3), 1)' '?')"
      ma="$(jget "$out" 'round(d["memory_after"] / (1024 ** 3), 1)' '?')"
      send_result "OpenClaw health check restarted the gateway gracefully: $reason. Cgroup memory ${mb} GiB -> ${ma} GiB. Runs: $(jget "$out" 'd["recovery"]["marked_interrupted"]' '?') marked interrupted, $(jget "$out" 'd["recovery"]["aborted_runs"]' '?') aborted, $(jget "$out" 'd["recovery"]["recovery_started"]' '?') main session(s) resumed, $(jget "$out" 'd["recovery"]["tombstoned"]' '?') tombstoned. Report: $(jget "$out" 'd["report"]' '-')"
      rm -f "$NOTIFIED"
      exit 0 ;;
    9)
      log "another graceful restart holds the lock; nothing to do"; exit 0 ;;
    10)
      gate="$(jget "$out" 'd["gate"]' unknown)"
      detail="$(jget "$out" '[g["detail"] for g in d["gates"] if not g["ok"]][0]' '')"
      log "graceful restart refused by gate '$gate': $detail"
      notify_once "gate:$gate" "OpenClaw health check: $reason, but the graceful restart was not attempted: gate '$gate' ($detail). It will be tried again on a later tick."
      exit 0 ;;
    8)
      log "refused: this process runs inside the gateway cgroup"
      notify_once self "OpenClaw health check: $reason, but the check itself runs inside the gateway cgroup, so it refused to restart it."
      exit 1 ;;
    *)
      send_result "OpenClaw health check: $reason; the graceful restart ended '$result' (exit $rrc). The watchdog marker was removed. Snapshot: $(jget "$out" 'd["snapshot_dir"]' '-'). Check \`ai-resources openclaw status\`."
      exit 1 ;;
  esac
fi

# --- guards for the legacy triggers (health failing, stalls) ---
now="$(date +%s)"; last="$(getv last_restart)"; last="${last:-0}"
if [ $((now - last)) -lt "$COOLDOWN_S" ]; then
  log "cooldown active (last action $((now - last))s ago): doing nothing"; exit 0
fi

# Strict probe: exit 0 idle, 1 busy, 2 unknown. Anything but 0 (a missing command included) means
# "never touch the gateway" for these triggers.
ai-resources openclaw busy >/dev/null 2>&1
busy_rc=$?
if [ "$busy_rc" -ne 0 ]; then
  log "agent runs in flight (or the busy probe could not tell, rc $busy_rc): not acting on '$reason'"
  notify_once "$key" "OpenClaw health check: $reason, but agent runs are in flight (or cannot be counted), so nothing was restarted. It will not force a restart over live runs; run \`ai-resources openclaw doctor\` yourself when idle."
  exit 0
fi

if [ "$DRY" = 1 ]; then log "DRY RUN: would run the drained doctor ($reason)"; exit 0; fi

# --- act: the drained doctor (it drains first, and refuses a busy gateway itself) ---
log "idle: running the drained doctor ($reason)"
setv last_restart "$now"
out="$(ai-resources openclaw doctor 2>&1)"
drc=$?
log "doctor rc=$drc $(tr '\n' ' ' <<<"$out" | cut -c1-300)"
if [ "$drc" -eq 0 ]; then
  notify "OpenClaw health check ran the drained doctor: $reason. The gateway answers again. Log: ~/.openclaw/logs/health-restart.log" || log "note: could not notify over Telegram"
  rm -f "$NOTIFIED"
else
  notify "OpenClaw health check: $reason; the drained doctor exited $drc. Check \`ai-resources openclaw status\`." || log "note: could not notify over Telegram"
  exit 1
fi
