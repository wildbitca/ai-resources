#!/usr/bin/env bash
# openclaw-backup-uploader.sh — off-box upload of the OpenClaw host backups, with NO JSON KEY.
#
# WHAT IT CLOSES
# The setup that runs the AI agents lives on this host's disk, the same disk the backups exist
# to survive. A local copy is not a backup.
#
# HOW IT AUTHENTICATES
# Whatever `gcloud` is already configured to use on this host (`gcloud auth list`). No key file
# is read or written by this script. Impersonation, if the active account needs it to reach the
# target service account, is the active gcloud config's job, not this script's.
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

fail() {
  echo "ERROR: $1" >&2
  notify "openclaw-backup-uploader failed on $(hostname): $1" || true
  exit 1
}

uploaded=0
for tier in daily weekly monthly; do
  dir="$OPENCLAW_BACKUP_DIR/$tier"
  [ -d "$dir" ] || { echo "  $tier: directory does not exist"; continue; }
  for f in "$dir"/*.tar.gz; do
    [ -e "$f" ] || continue
    [ -f "$f.sha256" ] || { echo "  SKIP    $(basename "$f") (still being written)"; continue; }
    n=$(basename "$f")
    if gcloud storage ls "$BUCKET/$tier/$n" >/dev/null 2>&1; then
      echo "  ALREADY $tier/$n"
      continue
    fi
    echo "  UPLOAD  $tier/$n ($(du -h "$f" | cut -f1))"
    gcloud storage cp "$f" "$BUCKET/$tier/$n" || fail "upload of $tier/$n failed"
    gcloud storage cp "$f.sha256" "$BUCKET/$tier/$n.sha256" || fail "upload of $tier/$n.sha256 failed"
    uploaded=$((uploaded + 1))
  done
done
echo "uploaded in this pass: $uploaded"

newest=$(ls -1t "$OPENCLAW_BACKUP_DIR"/daily/*.tar.gz 2>/dev/null | head -1 || true)
[ -n "$newest" ] || fail "there is no daily backup on disk to upload (check openclaw-backup-daily.timer)"
gcloud storage ls "$BUCKET/daily/$(basename "$newest")" >/dev/null 2>&1 \
  || fail "the newest daily backup ($(basename "$newest")) is NOT in the bucket"
echo "OK: the newest daily backup is off-box"
