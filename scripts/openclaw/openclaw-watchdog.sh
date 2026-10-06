#!/usr/bin/env bash
# openclaw-watchdog.sh — keeps the gateway up and SAYS SO when it had to intervene.
#
# `Restart=always` does not cover an explicit stop, and `openclaw doctor --fix` stops the
# gateway and can abort before starting it again (it re-inspects the stopped unit and fails
# with "ownership or manager identity changed" if the cgroup is not drained yet). That is how
# the gateway sat dead for half an hour on 2026-09-18 without anyone noticing.
#
# Pause it with: touch ~/.openclaw/watchdog.off
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

UNIT="$OPENCLAW_UNIT"
LOG="$OPENCLAW_LOG_DIR/watchdog.log"
[ -e "$OPENCLAW_WATCHDOG_OFF" ] && exit 0
mkdir -p "$(dirname "$LOG")"

# T39: a stop that ran into its shutdown timeout leaves a stability bundle behind, and the bundle
# only appears AFTER the restart, so this runs before the early exit for an active gateway.
# Newest bundle by the timestamp in its name (not mtime). The first run only records a baseline,
# so bundles that already exist never page. The marker advances only once the send worked.
SEEN_MARK="$OPENCLAW_LOG_DIR/watchdog.seen-stability"
newest=""
for f in "$HOME"/.openclaw/logs/stability/*gateway.stop_shutdown_timeout.json; do
  [ -e "$f" ] || continue
  f="${f##*/}"
  [[ "$f" > "$newest" ]] && newest="$f"
done
if [ ! -e "$SEEN_MARK" ]; then
  printf '%s\n' "$newest" > "$SEEN_MARK"
elif [ -n "$newest" ] && [[ "$newest" > "$(cat "$SEEN_MARK")" ]]; then
  counts="$(python3 -I -c 'import json,sys
try:
    e=json.load(open(sys.argv[1]))["snapshot"]["events"]
    n=sum(1 for x in e if x.get("type")=="session.stalled")
    print(" (%d stalled sessions recorded, a lower bound)" % n if n else "")
except Exception:
    pass' "$HOME/.openclaw/logs/stability/$newest" 2>/dev/null || true)"
  if notify "OpenClaw gateway stop hit its shutdown timeout; systemd killed its children (bundle $newest)${counts}. Run: ai-resources openclaw status. See T39."; then
    printf '%s\n' "$newest" > "$SEEN_MARK"
    echo "$(date '+%F %T') notified: stop_shutdown_timeout bundle $newest" >> "$LOG"
  else
    echo "$(date '+%F %T') could not notify about bundle $newest (will retry)" >> "$LOG"
  fi
fi

state="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
DEACT_MARK="$OPENCLAW_LOG_DIR/watchdog.deactivating-alerted"
if [ "$state" = active ]; then
  rm -f "$DEACT_MARK"
  exit 0
fi

# MEASURED ON 2026-09-18: stopping this gateway takes minutes (TimeoutStopSec=330 plus the
# drain), so during a deliberate restart the unit sits in `deactivating` for a good while. The
# first version took that for a crash and called `start` in the middle, fighting the restart
# in progress: it happened at 18:42 and 19:00 and put two "interventions" in the log that were
# not.
#
# Only `inactive` and `failed` are a real death. In any transition state (`deactivating`,
# `activating`, `reloading`) this tick does nothing: if the gateway is still down when the
# transition ends, the next tick -- two minutes later -- will see it `inactive` and act then.
case "$state" in
  inactive|failed) : ;;
  *)
    echo "$(date '+%F %T') gateway in transition ('$state'): not intervening" >> "$LOG"
    if [ "$state" = deactivating ]; then
      # Age of the stop = monotonic now - monotonic moment the unit left `active` (microseconds).
      exit_us="$(systemctl --user show "$UNIT" -p ActiveExitTimestampMonotonic --value 2>/dev/null || true)"
      up_s="$(awk '{print int($1)}' "$OPENCLAW_UPTIME_FILE" 2>/dev/null || true)"
      if [[ "$exit_us" =~ ^[0-9]+$ && "$exit_us" -gt 0 && "$up_s" =~ ^[0-9]+$ ]]; then
        age=$(( up_s - exit_us / 1000000 ))
        if [ "$age" -ge "$OPENCLAW_WATCHDOG_DEACTIVATING_ALERT_SEC" ] \
           && [ "$(cat "$DEACT_MARK" 2>/dev/null)" != "$exit_us" ]; then
          if notify "OpenClaw gateway stuck in deactivating for $(( age / 60 )) min; blocked tool calls hold the drain. See T39."; then
            printf '%s\n' "$exit_us" > "$DEACT_MARK"
            echo "$(date '+%F %T') notified: deactivating for ${age}s" >> "$LOG"
          else
            echo "$(date '+%F %T') could not notify about deactivating (will retry)" >> "$LOG"
          fi
        fi
      fi
    fi
    exit 0 ;;
esac

since="$(systemctl --user show "$UNIT" -p ActiveExitTimestamp --value)"
echo "$(date '+%F %T') gateway was '$state' (exited: ${since:-?}) -- starting" >> "$LOG"
systemctl --user start "$UNIT"

# Notify only when there really was an intervention, and only once the gateway can send it.
for _ in $(seq 1 18); do
  sleep 5
  openclaw health >/dev/null 2>&1 && break
done
if openclaw health >/dev/null 2>&1; then
  echo "$(date '+%F %T') recovered" >> "$LOG"
  notify "The watchdog brought the OpenClaw gateway back up (it was '$state', exited ${since:-?}). Log: ~/.openclaw/logs/watchdog.log" \
    || echo "$(date '+%F %T') could not notify" >> "$LOG"
else
  echo "$(date '+%F %T') did NOT start: still down" >> "$LOG"
  exit 1
fi
