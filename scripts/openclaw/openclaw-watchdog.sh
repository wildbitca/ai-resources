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

state="$(systemctl --user is-active "$UNIT" 2>/dev/null || true)"
[ "$state" = active ] && exit 0

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
  *) echo "$(date '+%F %T') gateway in transition ('$state'): not intervening" >> "$LOG"; exit 0 ;;
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
