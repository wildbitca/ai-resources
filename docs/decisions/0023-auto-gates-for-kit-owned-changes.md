# ADR-0023: The recommended run applies kit-owned, reversible, non-restarting changes; the rest is listed, never silent

**Status**: Accepted (2026-10-28). Implemented in 3.2.8.
**Date**: 2026-10-28
**Decision makers**: owner (requirements from the 3.2.7 live runs), implementer (record only). Release gate: the full suite and the independent Verifier.
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md). Nothing here restarts a live gateway or answers a restart question for the operator.

## Context

The 3.2.7 recommended run (`ai-resources setup`, `--auto` by default since 2.8.0) answered every consent gate of the OpenClaw host section
with "no" and said nothing about it. `_gate` called `ui.confirm(message, default=False)`, and in auto mode `ui.confirm` returns the default.
So, on the owner's host, three required fixes were skipped without a word:

- installing the `openclaw-*` systemd units, including the scratch-prune and worktrees-gc timers (a later verify step then warned
  "timer ... is not enabled" about units setup itself had declined to install);
- removing `agents.defaults.subagents.model`, a value an earlier kit release wrote and that is now harmful (it beats the target agent's own
  model on a cross-agent `sessions_spawn`);
- the cost migrations for values the kit itself added (the `kit-role-verifier` Opus fallback).

The gates exist for a reason (an operator's gateway is live; ADR-0003). But a gate that fails closed *and silent* hides required work, and
"ask the operator" is not available in the default run. The rule has to say which changes the recommended run may make by itself, and what
it does with the rest.

## Decision

1. **A change is applied by default in the recommended run (`--auto`) if and only if all three hold:**
   - **kit-owned**: the kit wrote it earlier (recorded in the setup state: `host_config_changes`, `host_units_previous`,
     `role_agents_applied`) or it is a kit artifact (the `openclaw-*` units and timers, `openclaw-scratch-prune`, `openclaw-worktrees-gc`),
     or it removes a value the kit shipped in a past release and that is now harmful (`RETIRED_KIT_VALUES`, matched only while the live
     value is still byte-equal to what the kit wrote);
   - **reversible**: teardown or the state records what was there before;
   - **non-restarting**: the existing reload-rules classification (`openclaw_reload_rules`, fail closed: an unclassifiable key is
     restart-required) says the keys hot-reload. A restart-required key is never applied unattended.

   Anything else stays gated: operator-owned values (a patch that *replaces* a value the operator holds, an Opus fallback the operator added,
   a cost value the operator chose), the group bindings (who may talk to which agent), the workboard plugin, the off-box gcloud identity, and
   everything that needs a restart.

2. **What a gate does, by mode** (`_gate(message, detail, *, kit_owned=False, restart=False)` in `setup/cockpits/_openclaw_host.py`):

   | mode | kit-owned, hot | anything else |
   |---|---|---|
   | `--auto` (default) | applied, one line says so | not applied, recorded |
   | `--ask` | asked, default **yes** | asked, default no |
   | `--non-interactive` | not applied, recorded | not applied, recorded |
   | any mode + `--accept-gates` | applied | applied **unless it restarts** |

   A mixed patch is split: the retired-value removals are their own kit-owned patch, applied first; the part that replaces operator values
   is gated on its own. A gated part therefore never keeps a harmful kit value in place.

3. **`--accept-gates`** (CLI and wizard) answers yes to every *non-restarting* gate, for CI and `--non-interactive` runs. It never answers a
   restart-required gate (`restart=True`: the workboard plugin enable, which may reload the gateway), an operator-owned gate
   (`operator_owned=True`: turning `image_generate` off for ALL agents, replacing the operator's image model), a drained-window
   confirm, or a credential prompt. It does answer the other operator-owned, non-restarting gates: the group bindings and the
   off-box identity (documented in `setup --help`). Restart-required keys stay in `host_restart_pending` for
   `ai-resources openclaw apply-pending` (ADR-0003 stays).

   The wait for OpenClaw's offline maintenance is ONE run-wide budget (default 180 s, maximum 900 s, never negative) drawn on by
   every patch; once spent a patch makes a single attempt and is recorded. A dry run refused for the whole budget is recorded too
   (the real write behind it never happens), de-duplicated against the real write. Recorded patches are replayed through an
   allowlist (`slack_cost`, `retired`, booleans only; both set = refused); a replay that would restart the gateway stays pending.

4. **Cost migrations** (`_offer_cost_migrations`): the recommended run applies the kit-owned ones and keeps the operator's; `kit_owned()` in
   `openclaw_cost_migrations.py` decides. A value at a path the kit recorded in `host_config_changes` is kit-owned only while it still equals the value the kit
   recorded writing (`written` in the record); an operator edit since, or an older record without `written`, makes it theirs
   (unless the live value is in `KNOWN_KIT_VALUES`). The Opus-fallback
   removal is kit-owned only for an agent in `role_agents_applied.created_agent_ids` whose Opus refs are all in the fallbacks the kit
   recorded for it (item 5(g): it removes an escalation the kit itself added). Operator-set values remain proposals with their snippet.

5. **No silent skip.** Every change not made calls `ui.record_skipped(what, why, how_to_finish)`. The wizard prints one block at the end of
   the run: what, why, and the exact command or flag that finishes it. The block is printed also when the run succeeded.

6. **Ask once.** Informational decisions that do not change on their own ("channel X is already routed to agent Y") are remembered per key in
   `OpenClawState.decisions` (`ui.remember` / `ui.remembered`), printed the first time, then counted in one line; `--verbose` lists them.
   Informational "not sent" keys are one count line unless `--verbose`.

## Config patch refused during OpenClaw maintenance (item 2)

`openclaw config patch` reads and writes the state database. While no gateway holds it (stopped, restarting, a drained window of an earlier
run) the CLI refuses with "OpenClaw state at ... is undergoing offline maintenance; retry when it finishes". The 3.2.7 run reported the error
and still said "Setup complete".

- `setup/patch_retry.py` retries the refusal with a short backoff (2, 4, 8, 15, 30, 30 ... s, no sleep above 30 s) inside a total budget
  (default 180 s; `AI_RESOURCES_PATCH_WAIT_S` or `--patch-wait SECONDS`). A `watchdog.off` window held by someone else is waited out first.
  The kit's own drained window (`allow_restart`) is never waited on: the gateway is down there on purpose.
- A patch still refused is stored in `OpenClawState.config_patch_pending`, listed in the skipped block with its command, and the run is
  **INCOMPLETE**: no completion banner, exit code **75** (`EX_TEMPFAIL`; 1 stays "error-level finding"). `ai-resources openclaw apply-pending`
  and the next setup run apply the stored patches through the same writer.
- Order: the post-setup watch starts **after** the drained reconcile window closes. Started before it, the watch's first tick found
  `watchdog.off` and stood down (no safety net for the run), and the next run's first patch met a gateway that was still restarting.

## Alternatives considered

- **Apply every gate in auto.** Rejected: bindings, the workboard plugin and replaced operator values are not the kit's to decide.
- **Keep all gates and only print the skips.** Rejected: the owner's evidence is that a required fix is then never made on the default path.
- **Make `--non-interactive` apply kit-owned changes.** Rejected: that mode means "saved answers, no surprises"; CI opts in with `--accept-gates`.
- **Retry inside `apply_drained`.** Rejected: it would stretch a window in which the gateway is down.

## Consequences

- A host that ran 3.2.7 gets the units, the retired-value removal and the kit-owned cost fixes on the next `ai-resources setup`, and "timer ... is not enabled" disappears.
- A run that cannot apply a patch says so, exits 75, and leaves the finishing command in the output and the state.
- The skipped-block text and `--help` are part of the contract; a new gate must choose `kit_owned` and `restart` deliberately.
