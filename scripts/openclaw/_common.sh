# shellcheck shell=bash
# _common.sh — shared preamble of the OpenClaw host scripts. Source it; never execute it.
#
# The scripts run from systemd timers, whose environment is nearly empty: no login PATH, no
# XDG_RUNTIME_DIR and no DBUS_SESSION_BUS_ADDRESS (so `systemctl --user` fails). Every script
# used to repeat this block; it lives here so a fix lands in one place.
#
# Host-specific values are never written into a script. They come from
# ~/.openclaw/kit-host.env (see openclaw-host.env.example), which `ai-resources setup` writes.

OPENCLAW_HOST_ENV="${OPENCLAW_HOST_ENV:-$HOME/.openclaw/kit-host.env}"
if [ -r "$OPENCLAW_HOST_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$OPENCLAW_HOST_ENV"
  set +a
fi

: "${OPENCLAW_BACKUP_DIR:=/srv/openclaw-backups}"
# Off-box upload target for openclaw-backup-uploader.sh, e.g. gs://my-bucket. No default: the
# kit never chooses a destination, and the uploader refuses to run without one.
: "${OPENCLAW_OFFBOX_BUCKET:=}"
: "${OPENCLAW_EXTRA_PATH:=}"
# Seconds a gateway stop may sit in `deactivating` before the watchdog tells the operator. A stop
# that runs the whole TimeoutStopSec (330 s) is SIGKILLed right after, so keep this below that.
: "${OPENCLAW_WATCHDOG_DEACTIVATING_ALERT_SEC:=240}"
# Where the watchdog reads the monotonic clock; a test points it at a file.
: "${OPENCLAW_UPTIME_FILE:=/proc/uptime}"

# Resource-safety knobs (ADR-0004), read by openclaw-health-restart.sh and by the Python CLIs it calls
# (`ai-resources openclaw pressure|graceful-restart`, which have the same defaults). Exported so both
# sides see one value.
#   OPENCLAW_GRACEFUL_RESTART  off | notify | on. `notify` tells the operator what would be done; only `on`
#                              restarts, and only after a supervised restart proved it safe on this host.
#   OPENCLAW_RESTART_WINDOW    HH:MM-HH:MM in OPENCLAW_RESTART_TZ (the host clock is UTC, the operator's zone
#                              is America/Guayaquil). Empty: never inside a window, only the ceiling acts.
#   OPENCLAW_RESTART_HARD_PCT  % of MemoryHigh above which the window is overridden.
: "${OPENCLAW_GRACEFUL_RESTART:=notify}"
: "${OPENCLAW_RESTART_WINDOW=02:00-05:00}"
: "${OPENCLAW_RESTART_TZ:=America/Guayaquil}"
: "${OPENCLAW_RESTART_HARD_PCT:=105}"
: "${OPENCLAW_RESTART_PRESSURE_PCT:=90}"
: "${OPENCLAW_RESTART_COOLDOWN_S:=10800}"
: "${OPENCLAW_RESTART_DAILY_CAP:=2}"
: "${OPENCLAW_HEALTH_CONFIRM_S:=60}"
: "${OPENCLAW_RESTART_SNAPSHOTS_KEEP:=10}"
: "${OPENCLAW_FROZEN_MIN_S:=600}"
: "${OPENCLAW_RESTART_SETTLE_S:=120}"
export OPENCLAW_GRACEFUL_RESTART OPENCLAW_RESTART_WINDOW OPENCLAW_RESTART_TZ OPENCLAW_RESTART_HARD_PCT \
  OPENCLAW_RESTART_PRESSURE_PCT OPENCLAW_RESTART_COOLDOWN_S OPENCLAW_RESTART_DAILY_CAP \
  OPENCLAW_HEALTH_CONFIRM_S OPENCLAW_RESTART_SNAPSHOTS_KEEP OPENCLAW_FROZEN_MIN_S OPENCLAW_RESTART_SETTLE_S

# OPENCLAW_EXTRA_PATH goes FIRST: an operator who sets it wants that openclaw, not the default.
export PATH="${OPENCLAW_EXTRA_PATH:+$OPENCLAW_EXTRA_PATH:}/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin:$HOME/.local/bin"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"

OPENCLAW_UNIT=openclaw-gateway.service
OPENCLAW_LOG_DIR="$HOME/.openclaw/logs"
OPENCLAW_WATCHDOG_OFF="$HOME/.openclaw/watchdog.off"

# notify <message> — best effort. Returns non-zero when there is nowhere to send it or the send
# failed, so callers decide whether that matters (`notify "..." || log "could not notify"`).
notify() {
  [ -n "${OPENCLAW_OWNER_TELEGRAM_ID:-}" ] || return 1
  timeout 30 openclaw message send --channel telegram --target "$OPENCLAW_OWNER_TELEGRAM_ID" \
    --message "$1" >/dev/null 2>&1
}
