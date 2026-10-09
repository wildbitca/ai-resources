# ADR-0002: Provider-agnostic model selection, honest cockpit gating and interactive updates

**Status**: accepted
**Date**: 2026-10-09
**Decision makers**: planner, implementer, operator (Addendum A of plan v2)
**Supersedes in part**: [ADR-0001](0001-model-pins-and-auto-update.md) (alternative 2, "re-render everything from the overlay", is adopted; the pins, the higher-of rule, `config patch` as the only writer and the defer-never-force restart stay)

## Context

ADR-0001 moved four Claude classes. The wizard and `models update` knew nothing about a user who runs
Gemini, OpenAI, DeepSeek or Moonshot models, and three artifacts said different things about routing:
the generated instruction text told every cockpit that "model calls go through" the gateway while also
saying Anthropic does not route Claude Code to non-Claude models; rule 017 drew Cursor and Gemini CLI
behind LiteLLM; the code wired a gateway only for Claude Code and Aider. A single non-Claude pick silently
became opus/haiku/inherit in Claude Code, and Aider under OpenRouter was given a base URL OpenRouter's
OpenAI surface does not serve.

## Decision

1. **Slots.** A slot is `<provider>:<family>`. The four Claude classes are the anthropic slots; a host with
   no recorded selection derives them, so existing setups produce byte-identical output.
2. **Adapters.** `scripts/ai_resources/model_providers/` holds one pure adapter per provider (naming,
   ordering, channels `stable|preview|alias|snapshot`, catalog ids, credential gate, discovery sources, smoke
   kinds, OpenClaw runtime, kit defaults). Aliases, previews, snapshots and unknown families are reported,
   never applied. Parity tests tie the registry to the legacy provider tables.
3. **Ownership.** `setup-state.yaml` (`selection`) owns WHAT is wanted and is written by the wizard; the
   overlay `model-pins.json` schema 2 owns HOW each slot moves and is written only by `models ...`.
   `profiles/*.yaml` stay kit templates; the user's rendered `executors.yaml`, `litellm.yaml` and the
   cockpit files follow the effective slots through the fan-out (decision 8).
4. **Credential gate.** A provider is asked only if enabled AND credentialed, from the kit's `.env` merged with
   OpenClaw's auth status through a field allowlist (names, kinds and counts; key material is never read).
5. **The compatibility matrix is code** (`setup/compat.py`, every cell cited to file:line). One table drives
   the wizard summary, the apply gate, the instruction text, `docs/multi-model.md`, rule 017, the update
   fan-out and the drift guard. A cell that is not verified end to end behaves as `skip` ("not verified yet")
   unless `--allow-unverified` opts in, and the summary labels it. The spike (S1) verified nothing it could not
   probe read-only on the live host, so every Claude-to-non-Claude gateway cell, OpenClaw's native Google
   runtime and the other unverified cells stay unverified; the one confirmed defect is Aider's OpenRouter base
   (`/api/v1`), fixed. Aider under OpenRouter is the one exception to "unverified": the base URL
   (`/api/v1`) and the model id format come from OpenRouter's public documentation, not from a live probe, so
   that cell is wired (`via gateway`) as trusted from documentation, not live-verified. A single non-Claude model keeps `mode: single-model` and needs no backend.
6. **Wizard order (Addendum A1)**: tools (detect, offer to install), then accounts (only the providers
   without credentials are asked about), then models from the enabled accounts, then a summary per cockpit,
   then one confirmation (Apply / Change models / Cancel), then the write. Non-interactive runs take the same
   choices from flags and fail with zero writes on a contradiction. **Addendum A2**: kit content is written for
   every detected cockpit; only a model setting the cockpit can never use is withheld, and the summary says so.
7. **Update interaction.** `models.py` never prompts. On a terminal, `models update` discovers once, shows the
   table, asks (Update now / Not now / Always / Never, or the batch question), releases the lock while the user
   reads, re-acquires it (exit 73, never waiting), aborts with no write if the policy changed, and applies the
   SAME discovery. Unattended it never asks. Remembered answers are per slot (`always|ask|never`, `max_bump`,
   `never_ids`, family `never`); precedence is exclude/frozen > never > approved > always > ask. **Addendum A3**:
   the timer applies an `always` answer only with a known, not-higher price and a passing smoke test. v1
   `auto-minor` migrates to `always/minor`, `approve` to `ask`, `frozen` stays.
8. **Fan-out.** `model_fanout.py` is the one apply/rollback path: an ordered registry of artifacts, each with
   `affected`, `render` (returns its restore payload) and `restore`; a failure restores the earlier artifacts in
   reverse. openclaw.json is touched only through `openclaw config patch`.
9. **Discovery failures**: exit 1 only when every enabled provider fails (there is no dedicated code); partial
   failure is a `discovery failed` row. **Egress**: vendor `/models` for OpenAI, DeepSeek and Moonshot is opt-in
   per provider, hard-coded hosts, key in the Authorization header only. Price sources: exact `audit.PRICES`,
   the operator's `prices`, OpenRouter's public list (cached 24 h); never invented.
10. **Re-render (S12) is the last, gated step**: it re-renders `executors.yaml`, `litellm.yaml`, the Claude
    subagent frontmatter and settings, and `~/.aider.conf.yml` from the effective slots, only for cockpits the
    matrix configures, with per-file backups, one health-checked LiteLLM restart and reverse-order restore.

## Alternatives Considered

| Option | Pros | Cons |
|--------|------|------|
| Switch to multi-model whenever a non-Claude model is picked (v1 decision a) | simple mapping | forces a gateway on cockpits that need none; promises routes the code does not set up |
| Per-cockpit if/else in the wizard | no new module | the instruction text, docs and rule 017 drift from the code (they did) |
| Keep class-keyed overlay and add a parallel table for other providers | smaller diff | two policy shapes, two migrations, no single "answer" per slot |
| Let the timer prompt on a TTY | one code path | a human can block the timer; the unattended path must be provably prompt-free |

## Consequences

### Positive
- Every cockpit gets exactly what the code can do for it, and the summary shows it before the write.
- One path updates every artifact, with a rollback that covers all of them.
- Existing setups (no selection) are unchanged; the Claude-only golden fixtures are untouched.

### Negative
- A larger surface: adapters, the matrix, the registry and overlay schema 2 (downgrade is unsupported; the v1
  backup is kept once).
- Most non-Claude bumps need a human until a price source covers them.

### Risks
- A matrix cell is wrong. Mitigation: one data source, a test against the real `configure()` output, a test that
  the docs equal the generated table, and unverified cells that behave as skip.
- The re-render touches `~/.claude`, aider and gateway files. Mitigation: compat-gated `affected()`, per-file
  backups, reverse-order restore, a last and separable step.

## References

- Plan v2 and Addendum A: `.agent-output/planner/models-provider-agnostic-plan-v2.md`
- `docs/multi-model.md` (matrix, answers, exit codes), `rules/017-multimodel-routing.mdc`
