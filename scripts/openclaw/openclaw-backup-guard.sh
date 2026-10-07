#!/usr/bin/env bash
# openclaw-backup-guard.sh — freshness guard for the OpenClaw host backups.
#
# WHY IT EXISTS
# A backup that stopped happening and a backup nobody knows stopped happening are the same thing
# until the day it is needed. The producer is a systemd USER timer (openclaw-backup-{daily,
# weekly,monthly}.timer), and a user timer is easy to lose quietly: a disabled unit, a full disk,
# or `linger` turned off.
#
# WHAT IT CHECKS, per tier: the newest tarball exists, is no older than its limit, weighs at
# least 5 MB, and has its `.sha256` sibling. It does NOT check that the tarball restores: only a
# real restore proves that.
#
# WHY monthly IS SOFT: it is empty until the first run after install, or for the first weeks of
# a fresh host. Reported, never fails the run, or this would cry wolf for weeks.
#
# HOW IT ALERTS: on a hard failure it notifies the operator (the same `notify` every other host
# script uses) and exits 1, which `openclaw-verify.sh`'s timer count also watches for silence.
set -uo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

MIN_BYTES=5000000   # 5 MB: under this it is not a backup of this setup.

declare -A LIMIT_HOURS=([daily]=36 [weekly]=216 [monthly]=780)
SOFT_TIERS="monthly"

failures=()
soft_failures=()

is_soft() { [[ " $SOFT_TIERS " == *" $1 "* ]]; }

for tier in daily weekly monthly; do
  dir="$OPENCLAW_BACKUP_DIR/$tier"
  limit="${LIMIT_HOURS[$tier]}"
  if [ ! -d "$dir" ]; then
    echo "  MISSING $tier: $dir does not exist"
    if is_soft "$tier"; then soft_failures+=("$tier: no directory"); else failures+=("$tier: $dir does not exist"); fi
    continue
  fi
  newest=$(ls -1t "$dir"/*.tar.gz 2>/dev/null | head -1 || true)
  if [ -z "$newest" ]; then
    echo "  EMPTY   $tier: no backup at all"
    if is_soft "$tier"; then soft_failures+=("$tier: no tarball"); else failures+=("$tier: no tarball"); fi
    continue
  fi
  age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
  size=$(stat -c %s "$newest")
  state="OK"; [ "$age_h" -le "$limit" ] || state="STALE"
  printf "  %-7s %s: %s  %sh  %s bytes  (limit %sh)\n" "$state" "$tier" "$(basename "$newest")" "$age_h" "$size" "$limit"
  tier_failures=()
  if [ "$age_h" -gt "$limit" ]; then tier_failures+=("$tier: ${age_h}h old, limit ${limit}h"); fi
  if [ "$size" -lt "$MIN_BYTES" ]; then
    echo "  SMALL   $tier: $size bytes"
    tier_failures+=("$tier: the newest tarball is $size bytes, suspiciously small")
  fi
  if [ ! -f "$newest.sha256" ]; then
    echo "  NOSHA   $tier: $(basename "$newest").sha256 is missing"
    tier_failures+=("$tier: the newest tarball has no .sha256")
  fi
  if [ "${#tier_failures[@]}" -gt 0 ]; then
    if is_soft "$tier"; then soft_failures+=("${tier_failures[@]}"); else failures+=("${tier_failures[@]}"); fi
  fi
done

if [ "${#soft_failures[@]}" -gt 0 ]; then
  echo "SOFT: $(IFS='; '; echo "${soft_failures[*]}")"
fi

if [ "${#failures[@]}" -gt 0 ]; then
  msg="$(IFS='; '; echo "${failures[*]}")"
  echo "FAIL: $msg"
  notify "openclaw-backup-guard failed on $(hostname): $msg" || true
  exit 1
fi
echo "daily and weekly are up to date"
