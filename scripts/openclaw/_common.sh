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
: "${OPENCLAW_EXTRA_PATH:=}"

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
  openclaw message send --channel telegram --target "$OPENCLAW_OWNER_TELEGRAM_ID" \
    --message "$1" >/dev/null 2>&1
}
