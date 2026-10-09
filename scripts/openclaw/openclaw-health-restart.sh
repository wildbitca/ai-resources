#!/usr/bin/env bash
# openclaw-health-restart.sh — hourly check of an OpenClaw gateway that is alive but degraded
# (openclaw-watchdog only covers one that is DOWN).
#
# Signals:
#   health   `openclaw health` fails or times out
#   memory   gateway cgroup at >= 90% of MemoryHigh AND system swap >= 90% used
#   stalls   >= STALL_LIMIT "CLI produced no output" terminations in the last hour
# Acts when: health fails, OR memory AND stalls >= 1, OR stalls >= 2 * STALL_LIMIT.
#
# Safety (ADR-0003): this script NEVER restarts, stops or starts anything itself, and it never
# forces anything over live agent runs.
#   - Runs in flight, or a busy probe that cannot tell (`ai-resources openclaw busy` exit 1 or 2),
#     mean it does nothing but tell the operator once per episode, then waits for the next tick.
#   - An idle gateway gets the kit's drained doctor (`ai-resources openclaw doctor`: watchdog.off,
#     stop, drain, fix, start, health), at most once per COOLDOWN_S.
#   - A gateway that is not active is left to openclaw-watchdog; watchdog.off stands it down.
# `--dry-run` decides and logs without acting.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

LOG="$OPENCLAW_LOG_DIR/health-restart.log"
STATE="$HOME/.openclaw/health-restart.state"        # line: last_restart=<epoch> (the host copy's file, so the cooldown survives adoption)
NOTIFIED="$OPENCLAW_LOG_DIR/health-restart.notified" # the episode already reported to the operator
STALL_LIMIT=4
COOLDOWN_S=$((3 * 3600))
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1

mkdir -p "$OPENCLAW_LOG_DIR"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; [ -t 1 ] && echo "$*"; return 0; }
getv() { grep -s "^$1=" "$STATE" | tail -1 | cut -d= -f2; }
setv() { { grep -sv "^$1=" "$STATE"; echo "$1=$2"; } > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; }
# notify_once <episode key> <message>: one Telegram per episode, not one per tick.
notify_once() {
  [ "$(cat "$NOTIFIED" 2>/dev/null)" = "$1" ] && return 0
  echo "$1" > "$NOTIFIED"
  notify "$2" || log "note: could not notify over Telegram"
}

if [ -e "$OPENCLAW_WATCHDOG_OFF" ]; then
  log "watchdog.off exists: a maintenance window is open, standing down"
  exit 0
fi

# Only act on a settled, active unit; transitions and downtime belong to openclaw-watchdog.
state="$(systemctl --user is-active "$OPENCLAW_UNIT" 2>/dev/null || true)"
if [ "$state" != active ]; then log "unit is '$state': skipping (the watchdog's job)"; exit 0; fi

# --- signals ---
health=ok
timeout 45 openclaw health >/dev/null 2>&1 || health=fail

mem=ok
cur="$(systemctl --user show "$OPENCLAW_UNIT" -p MemoryCurrent --value 2>/dev/null)"
high="$(systemctl --user show "$OPENCLAW_UNIT" -p MemoryHigh --value 2>/dev/null)"
swap_total="$(free -b 2>/dev/null | awk '/^Swap:/{print $2}')"
swap_used="$(free -b 2>/dev/null | awk '/^Swap:/{print $3}')"
if [[ "$cur" =~ ^[0-9]+$ && "$high" =~ ^[0-9]+$ && "$high" -gt 0 && "${swap_total:-0}" -gt 0 ]]; then
  if [ $((cur * 100 / high)) -ge 90 ] && [ $((swap_used * 100 / swap_total)) -ge 90 ]; then mem=pressure; fi
fi

stalls="$(journalctl --user -u "$OPENCLAW_UNIT" --since '1 hour ago' --no-pager 2>/dev/null | grep -c 'CLI produced no output')"
stalls="${stalls:-0}"

# --- decision ---
reason=""; key=""
if   [ "$health" = fail ]; then reason="openclaw health failing"; key=health
elif [ "$mem" = pressure ] && [ "$stalls" -ge 1 ]; then reason="memory+swap pressure with $stalls CLI stalls"; key=memory
elif [ "$stalls" -ge $((STALL_LIMIT * 2)) ]; then reason="$stalls CLI stalls in 1h"; key=stalls
fi
log "health=$health mem=$mem stalls=$stalls -> ${reason:-healthy}"
if [ -z "$reason" ]; then rm -f "$NOTIFIED"; exit 0; fi

# --- guards ---
now="$(date +%s)"; last="$(getv last_restart)"; last="${last:-0}"
if [ $((now - last)) -lt "$COOLDOWN_S" ]; then
  log "cooldown active (last action $((now - last))s ago): doing nothing"; exit 0
fi

# Strict probe: exit 0 idle, 1 busy, 2 unknown. Anything but 0 (a missing command included) means
# "never touch the gateway", for every trigger, health failure included. There is no deferral
# counter and no forced path.
ai-resources openclaw busy >/dev/null 2>&1
busy_rc=$?
if [ "$busy_rc" -ne 0 ]; then
  log "agent runs in flight (or the busy probe could not tell, rc $busy_rc): not acting on '$reason'"
  notify_once "$key" "OpenClaw health check: $reason, but agent runs are in flight (or cannot be counted), so nothing was restarted. It will not force a restart over live runs; run \`ai-resources openclaw doctor\` yourself when idle."
  exit 0
fi

if [ "$DRY" = 1 ]; then log "DRY RUN: would run the drained doctor ($reason)"; exit 0; fi

# --- act: the drained doctor (the only thing that stops the gateway, and it drains first) ---
log "idle: running the drained doctor ($reason)"
setv last_restart "$now"
out="$(ai-resources openclaw doctor 2>&1)"
drc=$?
log "doctor rc=$drc $(tr '\n' ' ' <<<"$out" | cut -c1-400)"
if [ "$drc" -eq 0 ]; then
  notify "OpenClaw health check ran the drained doctor: $reason. The gateway answers again. Log: ~/.openclaw/logs/health-restart.log" || log "note: could not notify over Telegram"
  rm -f "$NOTIFIED"
else
  notify "OpenClaw health check: $reason; the drained doctor exited $drc. Check \`ai-resources openclaw status\`." || log "note: could not notify over Telegram"
  exit 1
fi
