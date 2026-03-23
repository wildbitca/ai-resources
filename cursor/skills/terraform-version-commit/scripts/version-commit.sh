#!/usr/bin/env bash
# Terraform Version Commit - runs the Python script
# Usage: ./version-commit.sh [--bump patch|minor|major] [--dry-run] [project_root]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/version-commit.py" "$@"
