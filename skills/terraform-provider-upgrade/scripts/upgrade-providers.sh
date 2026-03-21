#!/usr/bin/env bash
# Terraform Provider Upgrade - runs the Python script or manual workflow.
# Usage: ./upgrade-providers.sh [project_root]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${1:-.}"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "${SCRIPT_DIR}/upgrade-providers.py" "$PROJECT_ROOT"
fi
echo "Python 3 required for automated upgrade. Run manually:" >&2
echo "  1. curl -s https://registry.terraform.io/v1/providers/<namespace>/<type>/versions | jq -r '.versions[].version' | sort -V | tail -1" >&2
echo "  2. Update version in required_providers" >&2
echo "  3. terraform init -upgrade && terraform validate" >&2
exit 1
