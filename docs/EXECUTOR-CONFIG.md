# Executor Configuration (Global)

All CLIs (Claude, Cursor, Gemini) share a **single global executor configuration** that ensures cost-optimized, reliable model routing.

## Location

```
~/.config/ai-resources/executors.yaml
```

This file is **shared by all CLIs** and **project-specific directories**. No local overrides allowed.

## Profiles

The kit ships with several profiles in `/profiles/`:

- `cost-optimized.yaml` — Recommended. Smart retry + tier-based downgrade fallbacks
- `all-claude.yaml` — All requests use Claude (expensive)
- `all-gemini.yaml` — All requests use Gemini (cheaper)
- `quality-first.yaml` — Prioritize quality, higher cost

## Cost-Optimized Profile (Recommended)

**File:** `profiles/cost-optimized.yaml`

### Guarantees

- **max_retries: 3** — Not 1. Gives agents 3 attempts before fallback
- **Tier-based fallbacks ONLY**:
  - Flash (cheap) → Flash-lite (cheaper)
  - Pro (mid) → Flash (downgrade)
  - Sonnet (expensive) → Haiku (downgrade)
  - **NEVER upgrade to expensive models**

### Cost Impact

| Agent | Model | Fails → Fallback | Savings |
|-------|-------|-----------------|---------|
| Explore | flash | Retries 3×, downgrade to flash-lite | $0 |
| Planner | pro | Retries 3×, downgrade to flash | 4× cheaper |
| Architect | Sonnet | Retries 3×, downgrade to haiku | 3× cheaper |

### Example: Planner Agent Failure

```
Request: planner agent (gemini-2.5-pro, ~$0.30/M)
├─ Attempt 1: fails
├─ Attempt 2: fails
├─ Attempt 3: fails
└─ Fallback: gemini-2.5-flash (~$0.075/M, 4× cheaper)
```

## Validation

```bash
# Ensure all CLIs use global config
~/.config/ai-resources/validate-no-overrides.sh
✓ No executor overrides found
✓ All CLIs use: ~/.config/ai-resources/executors.yaml
```

## Updating Configuration

When updating `cost-optimized.yaml` in this repo:

1. Edit `profiles/cost-optimized.yaml`
2. Commit and tag a new release
3. Users get the update via `brew upgrade ai-resources`

Changes apply **immediately** to all CLIs and all projects using this profile.

## Project-Level Overrides

**NOT ALLOWED**. Projects must NOT have:

```
~/.claude/executors.yaml ❌
~/.cursor/executors.yaml ❌
~/.gemini/executors.yaml ❌
```

All use: `~/.config/ai-resources/executors.yaml` ✅

If a project requires custom routing, open an issue to discuss adding a new profile here.
