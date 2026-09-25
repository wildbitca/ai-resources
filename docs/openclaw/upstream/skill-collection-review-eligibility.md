# Upstream report — the Skill Workshop review is scheduled for runtimes that can never run it

Filed against OpenClaw. Kept in this repo so the analysis survives whatever happens to the issue.

- **Observed on:** openclaw `2026.9.6` (`eb377ac`), Linux, npm install under a Homebrew prefix.
- **Job:** `skill-collection-review-claude`, id `8af32147-cb72-40c4-a9a1-68eeb104ce92`, `every 7d`,
  `sessionTarget: isolated`, system-owned (declaration `skill-collection-review:claude`).
- **Status:** `error (2x)`, last error:
  `CLI backend "claude-kit" does not declare instruction isolation with exact tools; collection review skipped`.

## Summary

The Skill Workshop monitor registers one weekly review job per configured agent. Whether the job is
*enabled* is decided by a projection that is strictly coarser than the check the run itself performs. For a
CLI backend that does not declare instruction isolation, the projection says "eligible", the run says "no",
and the result is a job that is scheduled forever, fails every time, and never auto-disables.

## The two layers disagree

**Enforcement** — `dist/prepare.runtime-EmRTe005.mjs`. When `params.rootedExecution` is set:

```js
if (backendResolved.isolatesInstructionsWithExactTools !== true)
  throw new Error(`CLI backend "${backendResolved.id}" does not declare instruction isolation with exact tools; collection review skipped`);
if (!canEnforceExactToolAvailability || !backendResolved.bundleMcp || nodeClaudePlacement || skipsTurnPreparation || params.disableTools)
  throw new Error("CLI runtime cannot enforce rooted execution with mediated tools; collection review skipped");
```

**Projection** — `dist/skill-collection-review-monitor-DnbqA4A4.mjs`:

```js
const enabled = workshopEnabled && hasEligibleRuntime !== false;
// …eligibility:
return policy.runtimeSource === "implicit"
    || policy.runtime === "auto"
    || supportsCronExecutionRoot(policy.runtime, isCliProvider(executionProvider, cfg));
```

**`supportsCronExecutionRoot`** — `dist/execution-root-runtime-Cn0UlMtn.mjs`:

```js
function supportsCronExecutionRoot(runtime, rootedCliExecution) {
  return runtime === "openclaw" || rootedCliExecution;   // rootedCliExecution = isCliProvider(...)
}
```

`isCliProvider` is true for **any** registered CLI backend, and `runtimeSource === "implicit"` short-circuits
to `true` before that whenever the agent does not pin a runtime. So eligibility cannot evaluate to `false`
for a CLI-backed agent, whatever the backend actually declares. The projection never consults
`isolatesInstructionsWithExactTools` or `bundleMcp` — the two things the run will demand.

## Why it cannot be worked around from outside

- The job is system-owned: `openclaw cron edit` and `openclaw cron disable` both answer
  `system-owned monitor jobs cannot be edited by cron clients`.
- There is no per-agent opt-out: `skills.workshop` is global with `additionalProperties: false`, and
  `agents.entries.<id>.skills` is only a skill-name allowlist.
- Deleting the job's cron session (so `hasStoredExecutionPreference` returns false) changes nothing,
  because eligibility was already `true` via `runtimeSource === "implicit"`.
- Pinning `agents.entries.<id>.runtime` changes nothing, because `isCliProvider` is still true.

The only levers left are repointing the agent's `model.primary` at a non-CLI provider — which changes the
operator's model and billing decision — or turning the Workshop's autonomous mode off globally, which also
removes the review for every agent whose runtime *can* run it.

## Reproducing

1. Register a CLI backend that leaves `isolatesInstructionsWithExactTools` undefined and sets
   `bundleMcp: false`. (A backend that exists to run a CLI unrestricted has to: `bundleMcp: true` makes core
   inject `--strict-mcp-config` / `--mcp-config` / `--disallowedTools`.)
2. Point an agent's `model.primary` at `<that-backend>/<model>`.
3. Leave `skills.workshop.autonomous.mode` at its default `auto`.
4. `openclaw cron list` → `skill-collection-review-<agent>` is registered and `enabled`.
5. Wait for the run, or inspect `openclaw cron show <id>` after it → `error`, with the message above.

## What would fix it

Either of these; the first is the smaller change:

1. **Make the projection mirror the enforcement.** Have the eligibility check consult the resolved backend's
   `isolatesInstructionsWithExactTools` and `bundleMcp` rather than `isCliProvider`. The monitor already has
   a `false` path that disables the job and labels it (`SKILL_COLLECTION_REVIEW_NO_ROOTED_RUNTIME_REASON`) —
   it is simply unreachable for CLI backends today.
2. **Offer a per-agent opt-out**, so an operator can say "this agent has no rooted runtime, do not schedule
   its review" without touching the global mode or the agent's model.

A third, independent of the above: let a client disable a system-owned monitor job, so an operator has some
recourse for a job that cannot succeed on their host.

## What this repo does meanwhile

`ai-resources` 1.9.4 has its setup author `skills.workshop.autonomous.mode = "propose"` when the host left it
unset, which stops the per-agent review job from being registered at all. It does not pretend to fix the
mismatch; it stops walking into it. See `docs/openclaw/pitfalls.md` T34.
