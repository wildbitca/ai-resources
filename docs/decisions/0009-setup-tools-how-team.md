# ADR-0009: Setup asks three questions (tools, how, team); every coding CLI is a tool

**Status**: Accepted (implemented on `main`, not released; the label is the operator's decision at release time)
**Date**: 2026-10-17
**Decision makers**: operator, planner, implementer. Operator decisions (answered by button and text on 2026-10-17) are cited inline as OD-1 to OD-4.
**Amends**: [ADR-0008](0008-setup-wizard-tui-on-rich-questionary.md) decision 6 (display only), its "one preset question" addendum ("Use this?") and its "where Gemini shows" addendum (the "enable it in step 6c" wording); [ADR-0007](0007-subscription-lanes.md) section 11 (where the Google terms check can open). ADR-0008 decisions 4 and 12 (installs stay on the first screen, Back refuses to cross them) are unchanged.

## Context

The operator asked (translated): "Claude Code should be treated the same as the others; they are ALL tools; everything depends on what to use, when and in which mode. Simplify the setup so everyone understands it."

An audit of the prompt call sites found: six always-asked questions on the subscription path (mode, preset, "Use this?", "Enable subscription lanes?", "Configure detected cockpits", "Apply this?") of which three describe one decision; four "Skipped" blocks on paths that never use a gateway; Claude Code named first in the mode text, the tool order and the completion banner; the cockpit list repeating the tool scan; and a documented behaviour that the code did not have (`--backend cli` was documented to skip the mode question and still asked it). Antigravity could not be enabled interactively at all: the lanes question set only `want`, `agy_on` came only from `--lanes-antigravity`, and the preset screen said "off until you enable it in step 6c", which was false.

## Decision

1. **One mental model, six words.** Every AI coding CLI is a **tool** (Antigravity, Aider, Claude Code, Codex, Continue, Copilot, Cursor, OpenClaw, OpenCode, Windsurf). Setup asks: which tools do you use, how should they work, who does what. The user-facing glossary is: tool, role, team, starting team (Balanced, Performance, Low cost, Custom), subscription, gateway. Plain phrases replace jargon on the default path ("Parallel runs" for subscription lanes, "Terms check" for acknowledgement, "Start at login" for lifecycle, "API keys" for credentials). The old words stay in code identifiers, state keys, CLI flags, ADR text, doctor and verify output.
2. **Four screens over ten internal steps (OD-1).** The screens are Your tools (n/4), How should they work? (n/4), Your team (n/4) and Review (n/4). `TOTAL_STEPS` stays 10 and the rail data stays; only the displayed header changes. This amends ADR-0008 decision 6 in display only.
3. **Answer-to-state mapping is unchanged.** "As a team" is `mode=single-model, cli_team=true`; "Each on its own" is `mode=single-model, cli_team=false`; "Advanced" asks which gateway and is `mode=multi-model, backend=litellm|openrouter`. The state schema does not change; an old state file keeps its meaning.
4. **No "Use this?".** Choosing the starting team is the confirmation. The role table is shown with the choice. This amends the ADR-0008 preset addendum (the confirm and the separate role-table step). Custom keeps the previous model and role questions unchanged.
5. **Silent skips.** Steps 3, 4, 5 and 8 print nothing on a path that does not use a gateway. Gateway paths keep their printed reasons.
6. **Claude Code is a tool in one list (OD-2).** The tool list is one order for every tool: installed first, then alphabetical. Claude Code carries a "recommended to start" label in that list, no special position and no special pre-tick. Installed tools are pre-ticked because they are installed. Safety rules stay and are explained as rules, not as a special tool: the writer stays on Claude Code (`roles_policy.DEFAULT_INTEGRATING_WRITER`).
7. **Claude-only parallel runs move to More options (OD-3).** The Claude-only lanes (backend `claude-cli`, off by default) and their Anthropic terms check leave the default path and sit under Review > More options.
8. **Antigravity is a tool like any other; ticking it is the opt-in (OD-4).** Amends ADR-0007 section 11 and its note "reaching the lanes from the setup mode question". The operator's decision (translated): "do not disable it as such, but let me SELECT not to use it; the steps are: verify/install, select which to use, use the recommended models according to Balanced / Performance / Low / Custom." Implementation: ticking Antigravity in the tool list and keeping it in the team is the opt-in, and there is no separate hidden "off" switch. What does NOT change (ADR-0007 section 11 safety, restated so this ADR cannot be read as weakening it):
   - Antigravity is never pre-ticked, even when installed (every other installed tool is). Its row says "sends repo content to Google; terms check next".
   - Ticking it opens the Google terms check with `lanes_render.ACK_TEXT['google']` verbatim. Accept writes `lane_acks['google']` through `make_ack`, exactly as `--lanes-antigravity` does. Decline, empty account, Ctrl-B and Ctrl-C return the tool to unticked and write nothing (fail closed).
   - `agy` never writes. The implementer and writer stay on Claude Code. `writable_backends()` and `WRITABLE_BACKENDS` are untouched, and gates G0-G7 (`agy_lane_gates`) stay NOT PROVEN, so admission still fails closed wherever a gate is unproven.
   - Codex is shown but not tickable while `CODEX_LANE_ADR_ACCEPTED` is False.
   - Slugs are labelled "unconfirmed slug" while `AGY_LANE_MODELS_CONFIRMED` is False.
   - `--non-interactive`, `--yes` and the flags behave as before: `--lanes-antigravity` without a valid stored acknowledgement still exits 2.
   - The team table lists Antigravity roles (second review, explore, tests, docs) only when it is ticked.
9. **`--backend` skips the mode screen.** `--backend cli|litellm|openrouter` prints one info line and does not ask the mode question. This makes the code match README and docs/multi-model.md.
10. **`--yes` scope is documented, not changed.** `--yes` installs only the missing members of Claude Code, Antigravity and OpenClaw; the interactive list still offers every missing tool. Gemini CLI (retired) is hidden from the wizard and still read from old state.
11. **Installs stay on the first screen.** ADR-0008 decisions 4 and 12 are unchanged: Back cannot rewind past an install.

## Consequences

- The default subscription path is four screens (five with the Antigravity terms check), and a run no longer prints skip blocks or asks about the same decision twice. The real gain is no duplicated decisions and no jargon, not just fewer prompts.
- Equal tools is a UI statement, not equal capability: Codex has no role profile (held), and only Claude Code and, with a terms check, Antigravity take roles. The team screen says "no roles yet" for the others.
- Antigravity becomes easier to turn on interactively. The mitigations are the ones above plus tests: decline fails closed, the accepted state equals the flag-path state, the writer is never agy, and the Codex row is never tickable.
- Scripts that grep wizard output break on wording; flags and state keys are unchanged.

## Rollback

Each step is one commit and one `git revert`. There is no state migration, so old and new state files keep their meaning both ways. This ADR can go back to Proposed or Superseded.

## Related

- [ADR-0007](0007-subscription-lanes.md), [ADR-0008](0008-setup-wizard-tui-on-rich-questionary.md)
- Plan: `.agent-output/planner/setup-simplification-plan.md`
