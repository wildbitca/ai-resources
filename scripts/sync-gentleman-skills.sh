#!/usr/bin/env bash
# Deprecated: use `agent-kit vendor-sync` + `agent-kit generate`, or `agent-kit self-update`.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CLI="${AGENT_KIT:-$REPO_ROOT}/bin/agent-kit"
[[ -x "$CLI" ]] || CLI="${HOME}/bin/agent-kit"
"$CLI" vendor-sync
"$CLI" generate
