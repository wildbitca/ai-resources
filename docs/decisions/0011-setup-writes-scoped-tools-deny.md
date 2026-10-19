# ADR-0011: Setup writes a scoped, reversible global `tools.deny` entry (`image_generate`)

**Status**: Proposed (2026-10-19). Not signed. See "Operator waiver" and "Sign-offs": the architect and security-auditor
reviews have not run yet.
**Date**: 2026-10-19
**Decision makers**: operator (Q1 and Q2 answered, one-run waiver below), implementer (record only).
**Plan**: `.agent-output/planner/setup-image-generation-plan.md`, revision 2 (decisions 8 to 17).
**Related**: [ADR-0001](0001-model-pins-and-auto-update.md) (`config patch` stays the only writer),
[ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (decision 5, no restart; the narrow exception below),
[ADR-0007](0007-subscription-lanes.md) (decision 6 and V7: the lanes writer is no longer the only `tools.deny` writer),
[ADR-0010](0010-setup-five-phases-channels-team.md) (phase 2.1 is where the question lives; ADR-0010 is not edited here).

## Operator waiver

The plan expects an architect validation (P0), this ADR signed by the software-architect and a BLOCKING
security-auditor review before any `tools.deny` code is written (plan AC-D15). The implementing session cannot spawn
reviewers. OPERATOR WAIVER: the operator chose to land all steps in one run on 2026-10-19; architect and security-auditor
reviews of ADR-0011 and of I1/I3/I4/I8/I9 run immediately AFTER the commits and gate Acceptance; the plan AC that
requires the ADR sign-off commit before the deny code is waived for that reason.

Consequences of the waiver, stated so nobody reads more into it: this ADR stays **Proposed**; the sign-off table below
is empty on purpose and the implementer does not fill it; nothing in this series is Accepted or verified until those
reviews return and any conditions they raise are applied.

## Context

`ai-resources setup` can configure OpenClaw's image generation. The operator wants two things: no image route is
recommended (every documented route is listed with its billing and proof label), and answering **No** must turn the
`image_generate` tool off. OpenClaw has no "off" for `agents.defaults.mediaModels.image`: unset means auto-detect from any
credentialed provider, so the only way to switch the tool off is tool policy, a write to the global `tools.deny`.

That is a new policy-write surface. Until now the kit wrote `tools.deny` only per agent (`agents.entries.<id>.tools.deny`,
the lanes writer), and no kit code wrote the global key. `config patch` replaces arrays and scalars and merges objects,
so a naive write of `["image_generate"]` would erase every operator entry (for example `bundle-mcp`).

## Decision

1. **Scope: global top-level `tools.deny`.** `disable` adds the exact token `image_generate`. Deny wins over a per-agent
   allow, so the switch is host-wide and also applies to kit lane agents and operator agents. The prompt, Review and
   the runbook say "affects all agents". Per-agent `tools.deny` is not used for this.
2. **Never a group token.** Not `group:media` (it also removes `view_image`, `tts`, `pdf`, `music_generate`,
   `video_generate`), not `group:openclaw`, not `*`, never `tools.allow`, `tools.alsoAllow`, `tools.profile`,
   `tools.byProvider`, never any `agents.entries.*` path.
3. **Merge, not overwrite.** Because arrays replace, every deny write sends the full list with
   `--replace-path tools.deny`: the live operator entries byte for byte, in their order, duplicates kept, with the kit
   token appended last. The write is idempotent under list equality.
4. **Covering tokens.** If a live entry already denies the tool (case-insensitive `image_generate`, `group:media`,
   `group:openclaw`, `*`, or an `*` wildcard pattern that matches it), the kit writes nothing, records nothing and
   Review names the entry. Wildcard grammar pinned from the installed docs (tool-policy.md, "case-insensitive, supports
   `*` wildcards"): `*` is the only metacharacter, it matches any run of characters, the match is anchored. Any other
   spelling is not treated as covering, so the kit appends its token (redundant, fail safe).
5. **Tracked, kit-token-only reversal.** State holds `image_deny_added` (True only after the kit's token write landed)
   and `image_deny_key_absent` (the key did not exist before the first write). Removal re-reads the live list and removes
   exactly one occurrence of the exact token. Result empty and key absent before: send null. Otherwise send the rest
   (possibly `[]`) with `--replace-path tools.deny`. Token already gone: no write, flags cleared. **Exception to
   ADR-0003 decision 5 ("add-only for operator values"):** the kit removes an entry it added itself, never an operator
   one, and never restarts anything.
6. **Fail-closed sequence** for every add or remove: cockpit is openclaw; fresh read of the config; shape check (absent,
   or a list of strings; anything else is one warning and no write); build the new list; `assert_single_token_delta`
   (the new list is the old one plus the token at the end, or minus one exact occurrence, nothing else); a `--dry-run`
   patch whose non-zero exit means no write and no state change; under `--dry-run` stop and print before/after; re-read
   immediately before the live write and abort without writing if the list changed; classify `tools.deny` with the kit
   classifier; state is recorded only after the live patch succeeds.
7. **Hot only, never deferred.** If `tools.deny` classifies as restart, `gateway.reload.mode` is off, or the rules are
   unreadable, the deny is **not applied and not deferred** (a policy write never lands later in a drained window),
   state is unchanged and a loud line says: "Image generation was NOT disabled: OpenClaw would need a restart to reload
   tools.deny. Run `openclaw config get tools.deny` and add `image_generate` by hand, or re-run setup after enabling hot
   reload."
8. **Race window accepted.** No lock exists between the read and the write; re-read-and-abort narrows the window and
   cannot close it. It is the same window the lanes writer accepts.
9. **New guard on the single writer.** `assert_tools_deny_safe(patch, replace_paths)` in `openclaw_host.py`, a sibling of
   `assert_channels_safe`, called next to it on every code path that invokes `config patch` through the kit's validated
   writers. Shape only (the writer alone has the live list). It fires only when the patch carries top-level
   `tools.deny`: the value is null or a list of strings; a list comes with `tools.deny` in `replace_paths`; the patch
   carries no `tools.allow`, `tools.alsoAllow`, `tools.profile`, `tools.byProvider` and no `agents` key at all; no
   `replace_paths` entry is `tools` or `tools.allow`. It does NOT reject group tokens or `*`: operator entries are re-sent
   as they are. Lanes, host-profile and voice patches never trigger it.
10. **The host profile never carries `tools.deny`.** A test enforces it.
11. **No credential in this surface.** Deny patches carry tool names only. The image credential is never held by the kit
    (OpenClaw's auth store through `openclaw models auth ...` on the operator's terminal).
12. **Live effect NOT PROVEN.** A hot classification does not prove that a running session drops a denied tool (the same
    status as `bundle-mcp`, ADR-0007 FU-1). Review, `verify()` and the runbook say "live effect on running sessions:
    NOT PROVEN". The operator probes it after the series lands.
13. **Patch-mode schema check (F13).** Whether `config patch` validates a non-string `tools.deny` element is pinned by a
    skip-if-absent integration test against a scratch config copy. If the check turned out to be vacuous, the shape check
    in `openclaw_tool_policy` would be the only validation.

## Facts read for this ADR (read only, 2026-10-19, OpenClaw 2026.10.5)

- `openclaw config get tools.deny` and `agents.defaults.mediaModels`: both unset on this host; no per-agent `tools.deny`
  on the seven configured agents. So no global `bundle-mcp` entry exists today (F16); the lanes writer is per agent.
- `openclaw models auth list`: one profile, `openrouter:default` (F8).
- The installed `config-reload-plan` lists `tools`, `auth.profiles` and `agents.defaults` as hot prefixes (F3, F15). The
  kit baseline fixture classifies `tools.deny` and `agents.defaults.mediaModels.image` hot and, with `reload_mode` off,
  restart.
- `models auth login --provider <id>` (OAuth) and `models auth paste-api-key --provider <p>` take no secret argument (F9
  for the stdin behaviour stays NOT PROVEN). For OpenAI, `--provider openai` defaults to the ChatGPT/Codex account login
  (cli/models.md:392), so the Codex provider id is `openai` (F10, docs only).
- Every call site that sends `config patch` through a kit validated writer: `openclaw.py` `apply_patch`,
  `openclaw_host.py` (two host writers), `models.py` (the models apply path). The guard is called at each.

## Alternatives considered

- **Per-agent `tools.deny` for the main agent only.** Would not stop operator agents or lane agents, and leaves `main`
  able to call the tool through delegation. Rejected by the operator's wording ("No means off").
- **`group:media` or `*`.** Removes unrelated tools. Rejected.
- **Overwrite `tools.deny` with the one token.** Erases operator entries. Rejected.
- **Defer the write to a drained window when not hot.** A policy write that lands later than the operator saw is worse
  than a loud refusal. Rejected.
- **No "No" option, only "keep".** Leaves the tool on through auto-detect. Rejected by the operator.

## Consequences

- Positive: the operator's "No" has a real effect; every write is reversible by the kit alone; operator entries are never
  touched.
- Negative: `disable` is host-wide; on a host with hot reload off setup cannot apply it (manual command printed); the live
  effect on a running session is unproven; a read-modify-write race remains.
- Risk: the guard sits on the writer used by lanes, host profile and voice. It fires only on top-level `tools.deny`.

## Rollback

Per commit `git revert`. At runtime, teardown removes only the kit token (null when the key was absent before). Old saved
states load because every new field is optional.

## Sign-offs

| Reviewer | Scope | Verdict |
|---|---|---|
| software-architect | this ADR; P0 decisions (ADR placement, global scope, guard placement, hot-only, re-read-and-abort) | pending (run after implementation) |
| security-auditor | this ADR; I1, I3 (guard), I4 (deny writer), I8 (answer mapping), I9 (auth hand-off) | pending (run after implementation) |
| verifier | plan acceptance criteria | pending (run after the two reviews) |
