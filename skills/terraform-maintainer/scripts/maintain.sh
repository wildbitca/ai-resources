#!/usr/bin/env bash
# Terraform Maintainer: orchestrate existing tools only. Does NOT reimplement any logic.
# Invokes: terraform-version-commit (version-commit.py), optionally terraform-provider-upgrade (upgrade-providers.py).
# Usage: ./maintain.sh /path/to/org-iac [--dry-run-only]
#   --dry-run-only: run only dry-run (discover repos and order), do not release.
# The release step can take 15–45+ minutes for many repos (upgrade + init + release per repo).
# Optional: TIMEOUT_SEC=2700 (45 min) to cap runtime; unset = no limit.
set -e

PARENT_DIR=""
DRY_RUN_ONLY=false
for arg in "$@"; do
  if [[ "$arg" == "--dry-run-only" ]]; then
    DRY_RUN_ONLY=true
  elif [[ -d "$arg" ]]; then
    PARENT_DIR="$arg"
  fi
done

if [[ -z "$PARENT_DIR" ]] || [[ ! -d "$PARENT_DIR" ]]; then
  echo "Usage: $0 /path/to/org-iac [--dry-run-only]" >&2
  echo "  e.g. $0 /Volumes/Docker/Development/wildbit/org-iac" >&2
  echo "  Optional: DEPENDENT_PROJECTS='path/org-gitops,path/pacha/ops' (comma-separated)" >&2
  exit 1
fi

PARENT_DIR="$(cd "$PARENT_DIR" && pwd)"
VERSION_COMMIT_SCRIPT="${VERSION_COMMIT_SCRIPT:-$HOME/.cursor/skills/terraform-version-commit/scripts/version-commit.py}"
# Comma-separated list of dependent Terraform projects (e.g. org-gitops, pacha/ops).
# All get upgrade + init -upgrade; version-commit will commit and push their version bumps so next TFC/CI run uses fixed modules.
DEPENDENT_PROJECTS="${DEPENDENT_PROJECTS:-$PARENT_DIR/org-gitops}"

if [[ ! -f "$VERSION_COMMIT_SCRIPT" ]]; then
  echo "Error: version-commit.py not found at $VERSION_COMMIT_SCRIPT" >&2
  exit 1
fi

# Unbuffered Python so progress is visible
export PYTHONUNBUFFERED=1

echo "=============================================="
echo "Terraform Maintainer (orchestration only)"
echo "Parent dir: $PARENT_DIR"
echo "=============================================="
echo ""

echo "=== Step 1: Dry-run (discover repos and release order) ==="
python3 -u "$VERSION_COMMIT_SCRIPT" --parent-dir "$PARENT_DIR" --dry-run --dependent-projects "$DEPENDENT_PROJECTS"
echo ""

if [[ "$DRY_RUN_ONLY" == true ]]; then
  echo "Dry-run only. To release, run without --dry-run-only."
  exit 0
fi

echo "=== Step 2: Release (commit, CHANGELOG, tag, push) ==="
if [[ -n "${TIMEOUT_SEC:-}" ]] && [[ "$TIMEOUT_SEC" -gt 0 ]]; then
  echo "(timeout: ${TIMEOUT_SEC}s)"
  timeout "$TIMEOUT_SEC" python3 -u "$VERSION_COMMIT_SCRIPT" --parent-dir "$PARENT_DIR" --yes --dependent-projects "$DEPENDENT_PROJECTS"
else
  python3 -u "$VERSION_COMMIT_SCRIPT" --parent-dir "$PARENT_DIR" --yes --dependent-projects "$DEPENDENT_PROJECTS"
fi
echo ""
echo "=== Done ==="
