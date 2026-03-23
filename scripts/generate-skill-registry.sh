#!/usr/bin/env bash
# Generate a markdown skill registry for orchestrators (Cursor, Claude, etc.).
# Respects AGENT_SKILLS_ROOT; defaults to $AGENT_KIT/cursor/skills.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
AK="${AGENT_KIT:-$REPO}"
ROOT="${AGENT_SKILLS_ROOT:-$AK/cursor/skills}"
OUT="${SKILL_REGISTRY_OUT:-$AK/cursor/skills-registry.md}"

if [[ ! -d "$ROOT" ]]; then
  echo "Skills root not found: $ROOT" >&2
  exit 1
fi

{
  echo "# Agent Skills Registry (generated)"
  echo ""
  echo "> Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ) — root: \`$ROOT\`"
  echo ""
  echo "| Skill path (under root) | SKILL.md |"
  echo "|-------------------------|----------|"
  find "$ROOT" -name 'SKILL.md' -print | sort | while read -r f; do
    rel="${f#"$ROOT"/}"
    echo "| \`${rel}\` | yes |"
  done
  echo ""
  echo "## Vendor: Gentleman-Programming"
  echo ""
  echo "| ID for prompts | Path |"
  echo "|----------------|------|"
  if [[ -d "$ROOT/vendor/gentleman-programming/curated" ]]; then
    find "$ROOT/vendor/gentleman-programming/curated" -name 'SKILL.md' -print | sort | while read -r f; do
      rel="${f#"$ROOT"/vendor/gentleman-programming/}"
      name="${rel%/SKILL.md}"
      echo "| \`vendor/gentleman-programming/${name}\` | \`$f\` |"
    done
  fi
  if [[ -d "$ROOT/vendor/gentleman-programming/community" ]]; then
    find "$ROOT/vendor/gentleman-programming/community" -name 'SKILL.md' -print | sort | while read -r f; do
      rel="${f#"$ROOT"/vendor/gentleman-programming/}"
      name="${rel%/SKILL.md}"
      echo "| \`vendor/gentleman-programming/${name}\` | \`$f\` |"
    done
  fi
} > "$OUT"

echo "Wrote $OUT"
cp "$OUT" "$AK/skills-registry.generated.md" 2>/dev/null || true
