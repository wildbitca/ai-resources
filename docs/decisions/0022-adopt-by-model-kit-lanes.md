# ADR-0022: Setup adopts the by-model kit-lane agents it can prove it wrote

**Status**: Accepted (2026-10-28, owner request, 3.2.8). Amends [ADR-0019](0019-roles-matrix-single-source.md) section 6 (the retirement of the by-model `kit-lane-*` agents) only for lanes in use.
**Date**: 2026-10-28
**Decision makers**: owner (requirements), implementer (record only). Release gate: the suite and the independent Verifier.
**Constraint, not amended**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md); nothing here restarts the gateway or spawns a session.

## Context

Every setup run on the owner's host printed `kit-lane agent kit-lane-agy-opus55: kept: not created by setup` for six agents
(`kit-lane-agy-gemini38flashhigh`, `-gemini4argonhigh`, `-agy-opus55`, `kit-lane-cli-haiku55`, `-opus55`, `-sonnet55`). They are
the by-model agents an earlier setup wrote. The retirement (`migrate_kit_lanes`) removes only an id that the lanes record
(`created_agent_ids`) names, and on this host that record is gone, so setup could neither retire nor manage them: it reported them
forever. They are not idle: every orchestrator lists them in `allowAgents` and the owner spawns them.

Two questions followed from "cross-agent `sessions_spawn` to a lane must run the lane's own model":

* OpenClaw resolves the model of a spawn as `<target>.subagents.model`, then `agents.defaults.subagents.model`, then the target's own
  model (`resolveSubagentConfiguredModelSelection`; a spawn that inherits its requester's model skips the target's own model too).
  Nothing in the requester's `subagents.model` takes part. Live on 2026-10-28 no layer is set, so the spawn lands on the lane's own
  model today; 3.2.7 had written the harmful `agents.defaults.subagents.model` and 3.2.8 retires it.
* The agy lanes run `agy-cli-lane/<slug>` against a catalog stub whose `baseUrl` is `http://127.0.0.1:9/` (the discard port). A call
  that reached the stub would be refused. It does not: OpenClaw treats a provider id that equals a registered CLI backend id as a CLI
  provider (`isCliProvider`), and the kit plugin registers the backend `agy-cli-lane` (`buildAgyLaneBackend`) when `agyBin` is an
  absolute path, which setup writes (`plugins.entries.ai-resources.config.agyBin`). The stub exists only so the catalog does not answer
  `Unknown model`.

## Decision

1. **Adopt, do not retire, a lane in the old writer's exact shape.** `is_legacy_lane`: id starts with `kit-lane-`; `name` and
   `identity.name` equal the id; `workspace` is `<...>/kit-lanes/<id>`; the entry has a primary model; the id is not in
   `created_agent_ids` (those keep the retirement path and its "nothing references it" proof). An agent that does not match (edited
   workspace, hand-made) stays unmanaged and is still reported `kept: not created by setup`.
2. **Fill-only.** For an adopted lane setup adds, when absent: `subagents.model` = the lane's own `model` (so a spawn lands on the
   lane whatever the defaults or the requester carry); the `claude-cli` `agentRuntime` pin of an Anthropic ref of the chain, only when
   a sibling ref already pins it; the `agy-cli-lane` catalog slug of an agy ref; the lane in each orchestrator's `allowAgents` (the
   existing wiring). The lane's model, fallbacks, tools, thinking and every other field are never written.
3. **Overrides are reported, not rewritten.** After the patch, `audit_spawn_models` resolves each adopted lane in OpenClaw's order. A
   lane whose spawn model is not its own (an operator's `subagents.model` on the lane, or a defaults value) is named once per run with
   the model and the layer; setup leaves operator values alone.
4. **State and teardown.** `lanes_applied.adopted_agent_ids` records the adoption (idempotent: a second run writes nothing and prints
   no `kept` line); `role_agents_applied.adopted_lanes` records exactly what was added. Teardown removes an added `subagents.model` or
   pin only where the live value is still the one setup wrote, and leaves the agents themselves in place.

## Consequences

* The six lane warnings disappear after the first 3.2.8 setup; the first run writes at most one validated, non-restarting
  `config patch`.
* A lane edited by hand out of the old shape drops out of management; its pins are left as they are.
* ADR-0019's retirement still applies to any lane the lanes record names and nothing references.

## Not verified live

No lane was spawned (an agy spawn costs quota). Proven from code and read-only config: the backend routing above, the plugin loaded from
the brew kit path with an absolute `agyBin` that exists, the stub catalog, and the unset override layers. A first real agy spawn remains
the end-to-end check.
