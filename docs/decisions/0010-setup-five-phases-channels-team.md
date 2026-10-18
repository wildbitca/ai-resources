# ADR-0010: Setup in five phases, unified team presets, disable-only channels (LiteLLM leaves the new-setup choice)

**Status**: Proposed. Only the N1 decision (LiteLLM) is implemented so far (plan step S5); the rest of the plan
(`.agent-output/planner/setup-five-steps-plan.md`, revision 3) is not implemented. The software-architect and
security-auditor sign-offs that plan step S0 requires are NOT recorded here and still block S2, S4, S6, S7 and S12.
**Date**: 2026-10-18
**Decision makers**: operator (N1 answered), implementer (record only).
**Amends**: [ADR-0008](0008-setup-wizard-tui-on-rich-questionary.md), gateway choice: LiteLLM is load-only.

## N1: LiteLLM readiness for Balanced with Gemini scores 4/10, so LiteLLM is removed from the wizard

The scale is readiness (10 = Balanced with Gemini works end to end today with recorded evidence). The operator's rule
was: 7 or more implements Gemini on LiteLLM; below 7 removes LiteLLM from the wizard, keeps old saved states loadable
and breaks no schema. The score is 4/10 because:

- The renderer maps `google/<m>` to `gemini/<m>` and the Providers and Credentials steps exist (+4 in total).
- Claude Code with Google on LiteLLM is UNVERIFIED (`compat.py`); nothing records that LiteLLM's translation of the
  Anthropic `/v1/messages` surface into Gemini (tool_use, thinking, cache_control, `drop_params=True`) is faithful.
- It needs a second, direct AI Studio key that account detection does not require.
- The per-model fallback union crosses checker and writer families.
- No test renders a full Gemini Balanced through LiteLLM, and no live check is allowed to lift the cell.

### Consequences (implemented)

- `GATEWAY_CHOICES` offers OpenRouter only. With one gateway left, "Advanced" picks it with one line and does not
  ask "Which gateway?". The Advanced text reads "gateway (OpenRouter) with an API key and budgets".
- A saved `mode=multi-model, backend=litellm` state is offered `LiteLLM (saved; no longer offered for new setups)` as
  the default, beside OpenRouter, and prints one notice line:
  `LiteLLM is no longer offered for new setups; your saved gateway keeps working. Choose OpenRouter to switch.`
- `--backend litellm` stays accepted and prints the same notice.
- `state.backend` still defaults to `litellm` for cli and single states, silently. No schema change.
- `setup/litellm.py` rendering, the service unit, `ai-resources litellm`, the doctor LiteLLM checks and the daemon are
  unchanged.
- S5-ALT (keep LiteLLM and implement Gemini on it) is not used.

## Not decided here

Decisions PD-1 to PD-7 of the plan (role split, disable-only channel allow-list, five phases, naming, evidence
record) are recorded in the plan only. They need the architect and security-auditor review before this ADR can be
Accepted.
