# ADR-0001: Model pins in one place and unattended model updates through `config patch`

**Status**: accepted
**Date**: 2026-10-09
**Decision makers**: planner, implementer (confirmation of the write path requested from the operator)

## Context

The Claude model ids that the OpenClaw path uses were spelled in a dozen places (audit aliases, the
`claude-code` engine, the worker default, the host profile, the profiles, LiteLLM, providers, aider).
Moving to a newer model meant a kit release and a hand edit of `openclaw.json`. The operator wanted new
models picked up in the background, with the gateway restarted safely and a way back if anything breaks.

## Decision

1. `scripts/ai_resources/model_pins.py` is the single declaration of the four class pins. The OpenClaw-path
   consumers are built from it. A CI drift test keeps the hand-written ids elsewhere aligned with it.
2. A host overlay (`~/.config/ai-resources/model-pins.json`) can move a class forward. The effective pin is the
   **higher** of the kit default and the overlay; only an explicit freeze holds a class down.
3. `ai-resources models update` is the only writer. It changes `openclaw.json` **exclusively through
   `openclaw config patch`** (the documented kit rule): the schema is validated, `channels` is never touched,
   and the rollback is an inverse patch. The file copy in `~/.openclaw/backups/models-update/` is forensics.
4. The restart is **defer, never force**: a busy gateway postpones the update, a deferred restart is
   remembered (`pending_restart`) and resumed by the next run. The command holds `watchdog.off` while it
   restarts, polls `openclaw health` and requires a new gateway PID.
5. Policy: a minor bump with a known, not-higher price applies by itself; everything else waits for
   `ai-resources models approve`. Prices are never invented.

## Alternatives Considered

| Option | Pros | Cons |
|--------|------|------|
| Edit `openclaw.json` directly | simple, no CLI dependency | bypasses schema validation and the journal/fingerprint OpenClaw keeps; breaks the kit rule |
| Re-render everything from the overlay (LiteLLM, OpenRouter, aider) | one source for all backends | far larger blast radius; those follow kit releases |
| Token refactor of the profile files (`pin:`) | removes the hand-written ids | changes the profile format; the drift test gives the same guarantee |

## Consequences

### Positive
- A new Claude model reaches the host the day it is in the catalog, after a smoke test and a health check.
- Every switch is reversible by one command.

### Negative
- The overlay moves only the OpenClaw path; LiteLLM, OpenRouter and aider follow kit releases.
- Until `audit.PRICES` knows a new id, its bump needs an operator approval.

### Risks
- `claude -p` is a real call on the operator's account (one cheap call per applicable class, per run).
- The catalog or the CLI output shape may change: discovery and the smoke probe fail closed and notify.

## Related

- Feature specs: `docs/multi-model.md` (Automatic model updates), `docs/openclaw/runbook.md`
- Other ADRs: none
