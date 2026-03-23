#!/usr/bin/env bash
# Regenerate skills-registry.md + skills-index.json (discovery matrix).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
export AGENT_KIT="${AGENT_KIT:-$REPO}"
export AGENT_SKILLS_ROOT="${AGENT_SKILLS_ROOT:-$AGENT_KIT/cursor/skills}"
export SKILL_REGISTRY_OUT="${SKILL_REGISTRY_OUT:-$AGENT_KIT/cursor/skills-registry.md}"
export SKILLS_INDEX_OUT="${SKILLS_INDEX_OUT:-$AGENT_KIT/cursor/skills-index.json}"
bash "$AGENT_KIT/scripts/generate-skill-registry.sh"
python3 "$AGENT_KIT/scripts/generate_skills_index.py"
