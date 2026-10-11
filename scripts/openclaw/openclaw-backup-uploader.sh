#!/usr/bin/env bash
# openclaw-backup-uploader.sh — off-box upload of the OpenClaw host backups, with NO JSON KEY.
#
# WHAT IT CLOSES
# The setup that runs the AI agents lives on this host's disk, the same disk the backups exist
# to survive. A local copy is not a backup.
#
# HOW IT AUTHENTICATES
# Whatever `gcloud` is already configured to use on this host (`gcloud auth list`). No key file
# is read or written by this script. By default the ACTIVE gcloud configuration and the process
# environment decide the identity, and on a host whose user manager exports an agent identity
# (CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE, GOOGLE_APPLICATION_CREDENTIALS) that identity is a
# read-only one and every write fails (pitfall T43). Two optional knobs in kit-host.env fix that
# without touching the active configuration:
#   OPENCLAW_OFFBOX_GCLOUD_CONFIG   a NAMED gcloud configuration to use for this script only
#                                   (CLOUDSDK_ACTIVE_CONFIG_NAME), with the three credential
#                                   variables above removed from gcloud's environment.
#   OPENCLAW_OFFBOX_IMPERSONATE_SA  a service account to impersonate on every gcloud call
#                                   (--impersonate-service-account). The caller needs
#                                   roles/iam.serviceAccountTokenCreator on it: an infra step.
# With both unset the behaviour is the one described above. OPENCLAW_OFFBOX_LIST_CMD (used by
# `openclaw status`) is a separate command of yours: it runs under whatever identity IT uses.
#
# EVERY RUN LEAVES A STATE FILE
# ~/.openclaw/state/offbox-last.json (override: OPENCLAW_OFFBOX_STATE) is written atomically on
# every exit: when, the identity used, ok|failed, the reason and the newest daily that is
# off-box. `openclaw status` and `ai-resources verify` read it; they never run gcloud.
#
# THE .sha256 IS THE WITNESS
# The producer writes the checksum LAST. A tarball without its sibling checksum is still being
# written, so this script skips it: uploading a half-written tarball is worse than uploading
# nothing, because it would look like a recovery point.
#
# IT ASSERTS, IT DOES NOT ONLY TRY
# At the end it checks that the newest local daily tarball is really in the bucket and fails
# otherwise. Without that, a silent upload failure leaves this run looking fine and everyone
# believing there is an off-box backup.
set -euo pipefail
# shellcheck source=_common.sh
. "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

: "${OPENCLAW_OFFBOX_BUCKET:?set OPENCLAW_OFFBOX_BUCKET (e.g. gs://my-bucket) in ~/.openclaw/kit-host.env}"
BUCKET="$OPENCLAW_OFFBOX_BUCKET"
GCFG="${OPENCLAW_OFFBOX_GCLOUD_CONFIG:-}"
GSA="${OPENCLAW_OFFBOX_IMPERSONATE_SA:-}"
STATE_FILE="${OPENCLAW_OFFBOX_STATE:-$HOME/.openclaw/state/offbox-last.json}"

# gc <gcloud args...>: gcloud under the identity this script is configured for. With a named
# configuration every CLOUDSDK_* property variable (project, account, token file, impersonation, ...)
# and the two GOOGLE_* credential variables are removed from gcloud's environment, so the configuration is
# the only thing that decides the identity. CLOUDSDK_CONFIG (where the configurations live) and
# CLOUDSDK_PYTHON* (how gcloud itself starts) are not identity and are kept.
gc() {
  local args=("$@") v
  local -a unset_args=(-u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES)
  if [ -n "$GSA" ]; then
    args+=("--impersonate-service-account=$GSA")
  fi
  if [ -n "$GCFG" ]; then
    while IFS= read -r v; do
      case "$v" in
        CLOUDSDK_ACTIVE_CONFIG_NAME|CLOUDSDK_CONFIG|CLOUDSDK_PYTHON|CLOUDSDK_PYTHON_SITEPACKAGES) ;;
        CLOUDSDK_*) unset_args+=(-u "$v") ;;
      esac
    done < <(compgen -e)
    env "${unset_args[@]}" CLOUDSDK_ACTIVE_CONFIG_NAME="$GCFG" gcloud "${args[@]}"
  else
    gcloud "${args[@]}"
  fi
}

json_esc() {
  printf '%s' "$1" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# The identity line: which configuration, which account, which impersonation, and (when nothing
# overrides it) the credential file the environment forces on gcloud.
identity() {
  local acct s
  acct=$(gc config get account 2>/dev/null | head -n 1 || true)
  s="account=${acct:-unknown}"
  [ -n "$GCFG" ] && s="config=$GCFG $s"
  [ -n "$GSA" ] && s="$s impersonating=$GSA"
  if [ -z "$GCFG" ] && [ -n "${CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE:-}" ]; then
    s="$s credential_file_override=$CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"
  fi
  printf '%s' "$s"
}

IDENTITY=""
NEWEST_NAME=""
NEWEST_EPOCH=""
LAST_OK=""
STATE_DONE=0

# Keep what the last successful run knew when this run fails before it can find out again.
if [ -r "$STATE_FILE" ]; then
  NEWEST_NAME=$(grep -o '"newest_offbox": *"[^"]*"' "$STATE_FILE" | head -n 1 | sed 's/.*: *"\(.*\)"/\1/' || true)
  NEWEST_EPOCH=$(grep -o '"newest_offbox_epoch": *[0-9]*' "$STATE_FILE" | head -n 1 | grep -o '[0-9]*$' || true)
  LAST_OK=$(grep -o '"last_ok_epoch": *[0-9]*' "$STATE_FILE" | head -n 1 | grep -o '[0-9]*$' || true)
fi

# write_state <ok|failed> <reason>: atomic (temp file in the same directory, then rename).
write_state() {
  local outcome="$1" reason="$2" dir tmp
  dir=$(dirname "$STATE_FILE")
  mkdir -p "$dir" 2>/dev/null || return 0
  tmp=$(mktemp "$dir/.offbox-last.XXXXXX" 2>/dev/null) || return 0
  {
    printf '{"at": "%s", "identity": "%s", "outcome": "%s", "reason": "%s", ' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(json_esc "$IDENTITY")" "$outcome" "$(json_esc "$reason")"
    printf '"bucket": "%s", "newest_offbox": "%s", "newest_offbox_epoch": %s, "last_ok_epoch": %s}\n' \
      "$(json_esc "$BUCKET")" "$(json_esc "$NEWEST_NAME")" "${NEWEST_EPOCH:-null}" "${LAST_OK:-null}"
  } > "$tmp" && mv -f "$tmp" "$STATE_FILE" || rm -f "$tmp"
  STATE_DONE=1
}

# Any exit that did not go through fail() or the success path still leaves a state file.
trap 'rc=$?; if [ "$rc" -ne 0 ] && [ "$STATE_DONE" != 1 ]; then write_state failed "the uploader exited with status $rc"; fi' EXIT

fail() {
  echo "ERROR: $1" >&2
  write_state failed "$1"
  notify "openclaw-backup-uploader failed on $(hostname): $1" || true
  exit 1
}

# hint_for <gcloud error text>: what the operator can do about the class of failure.
hint_for() {
  case "$1" in
    *"Executables need to be explicitly allowed"*|*GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES*)
      echo "the environment hands gcloud an executable-sourced credential: set OPENCLAW_OFFBOX_GCLOUD_CONFIG to a named configuration (see docs: off-box identity)" ;;
    *getAccessToken*|*serviceAccountTokenCreator*|*"Failed to impersonate"*|*"impersonat"*)
      echo "impersonation was refused: the caller needs roles/iam.serviceAccountTokenCreator on the service account (an infra step; see docs: off-box identity)" ;;
    *Reauthentication*|*invalid_grant*|*"refresh token"*|*"gcloud auth login"*)
      echo "the stored login expired or was revoked: sign in again with gcloud auth login (see docs: off-box identity)" ;;
    *Permission*|*"does not have"*|*403*|*"Access denied"*|*"AccessDenied"*)
      echo "this identity cannot write the bucket (see docs: off-box identity)" ;;
    *) echo "see docs: off-box identity" ;;
  esac
}

IDENTITY=$(identity)
echo "identity: $IDENTITY"

# Preflight: one cheap listing before the loop, so a wrong identity is one readable line and not
# a screen of gcloud noise per file. A tier that is merely empty is not an error.
pre=$(gc storage ls "$BUCKET/daily/" 2>&1 >/dev/null) || {
  case "$pre" in
    *"matched no objects"*|*"No URLs matched"*) ;;
    *)
      first=$(printf '%s\n' "$pre" | grep -m 1 -i 'error' || printf '%s\n' "$pre" | head -n 1)
      fail "$IDENTITY cannot access $BUCKET: ${first:-no output from gcloud}; $(hint_for "$pre")"
      ;;
  esac
}

uploaded=0
for tier in daily weekly monthly; do
  dir="$OPENCLAW_BACKUP_DIR/$tier"
  [ -d "$dir" ] || { echo "  $tier: directory does not exist"; continue; }
  for f in "$dir"/*.tar.gz; do
    [ -e "$f" ] || continue
    [ -f "$f.sha256" ] || { echo "  SKIP    $(basename "$f") (still being written)"; continue; }
    n=$(basename "$f")
    if gc storage ls "$BUCKET/$tier/$n" >/dev/null 2>&1; then
      echo "  ALREADY $tier/$n"
      continue
    fi
    echo "  UPLOAD  $tier/$n ($(du -h "$f" | cut -f1))"
    gc storage cp "$f" "$BUCKET/$tier/$n" || fail "upload of $tier/$n failed as $IDENTITY"
    gc storage cp "$f.sha256" "$BUCKET/$tier/$n.sha256" || fail "upload of $tier/$n.sha256 failed as $IDENTITY"
    uploaded=$((uploaded + 1))
  done
done
echo "uploaded in this pass: $uploaded"

newest=$(ls -1t "$OPENCLAW_BACKUP_DIR"/daily/*.tar.gz 2>/dev/null | head -1 || true)
[ -n "$newest" ] || fail "there is no daily backup on disk to upload (check openclaw-backup-daily.timer)"
gc storage ls "$BUCKET/daily/$(basename "$newest")" >/dev/null 2>&1 \
  || fail "the newest daily backup ($(basename "$newest")) is NOT in the bucket"
NEWEST_NAME=$(basename "$newest")
NEWEST_EPOCH=$(stat -c %Y "$newest" 2>/dev/null || date +%s)
LAST_OK=$(date +%s)
write_state ok "uploaded $uploaded; the newest daily backup is off-box"
echo "OK: the newest daily backup is off-box"
