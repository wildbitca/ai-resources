# ADR-0003: Setup never triggers a live gateway restart, and never overwrites a value the operator set

**Status**: accepted; decision 7 superseded by [ADR-0004](0004-bounded-graceful-restart-under-memory-pressure.md) for the memory trigger (unreleased, target 2.0.2)
**Date**: 2026-10-09
**Amended by**: [ADR-0007](0007-subscription-lanes.md) (watch record for lane keys; set-difference teardown; classify before `agents add`; plugin backend and tool through the drained path)
**Decision makers**: planner, implementer; open questions answered by the operator (Q1-Q9 below)
**Builds on**: [ADR-0001](0001-model-pins-and-auto-update.md) (defer, never force; `config patch` is the only writer)

## Context

On 2026-10-09 an `ai-resources setup` run restarted a live OpenClaw gateway and cut the agent runs in
flight (about 8.5 min of 503s; the gateway was also at 13.3 GB RSS with swap full and 120+ stalled
sessions, T39, which made the forced restart slow). The root cause, in order:

1. `build_host_patch` skipped a leaf only when the live value equalled the profile value. Any other value
   the operator had set, here `gateway.bind: loopback`, was overwritten with the profile's `tailnet`.
2. The host config step asked one generic "Apply this patch?" question. Nothing in the kit knew which keys
   make OpenClaw restart, so the prompt did not say a restart would follow, or how many runs were in flight.
3. OpenClaw's config watcher classifies every `gateway.*` key as restart-required unless it is a listed hot
   exception. It defers a restart for a hard-coded 300 s (there is no setting) and then forces it. The
   kit's own code never ran a restart, so "setup never restarts the gateway" was true of the code and
   false in practice (pitfall T40).
4. `agents.defaults.model.primary` came from the engine section. It is a hot key and did not cause the outage.
5. `openclaw config patch --dry-run --json` has no restart field (checked on 2026.9.9), so the CLI cannot
   tell the kit in advance which keys restart the gateway.

## Decision

1. **A restart-required table that fails closed.** `scripts/ai_resources/openclaw_reload_rules.py`
   classifies keys from OpenClaw's reload rules. *Amended (unreleased, see "Amendment: the rules follow the
   installed OpenClaw" below): the rules are read from the installed package, never pinned to a version.*
   Unreadable rules, `gateway.reload.mode: off` and a path no rule matches are all classified
   restart-required. A checked-in dist excerpt keeps GitHub CI offline. (Q1)
2. **The gate lives at the single writer.** `apply_patch` raises `RestartRequired` before any CLI call when
   a non-dry patch carries a restart-required key and the caller did not pass `allow_restart`. Only
   `apply_drained` passes it.
3. **Restart-required keys are applied only inside a drained window, after an explicit confirm, while
   idle, and never unattended.** The window is the one extracted from `doctor` (`drained_window`:
   `watchdog.off`, stop, drain, patch, start, poll health, marker removed on every exit path), after a dry
   run and a strict busy probe that treats "cannot tell" as busy. `openclaw config patch` writes the file
   without a live gateway (spike on a copy, 2026-10-09), so the write happens while nothing is running for
   OpenClaw's own watcher to force-restart. (Q3, Q4)
4. **Pending keys are recorded, shown and applied later.** Unattended runs, busy gateways and declined
   confirms record the key in `host_restart_pending`; `status` and `verify` list them with
   `ai-resources openclaw apply-pending`, which re-derives values from the profile and the overrides (or
   uses the recorded inverse) and refuses while busy. No secret value is ever stored.
5. **The host profile fills; it does not overwrite.** A key the operator set to a different value is kept
   and listed ("kept your value"). `~/.openclaw/kit-host-overrides.json5` (`keep` and `force` lists, parsed
   as JSON5 and never shell-sourced; a lone `"*"` in `force` restores the 2.0.x behaviour) opts a key back
   in. Array keys are still unioned (add-only). An agent on a haiku primary is still replaced (T29). The
   engine section remains a deliberate wizard choice, but the keys it changed are now listed. (Q2, Q8)
   See ADR-0011 for the one exception: setup removes only the `image_generate` token it added to the global
   `tools.deny` itself, tracked in state; operator values stay add-only and nothing restarts.
6. **A bounded, backgrounded, cancellable post-setup watch reverts the run on failure.** A transient user
   unit (`systemd-run --user`, `RuntimeMaxSec=1800`, `TimeoutStopSec=600`) polls `openclaw health` for 10 minutes. The unit outlives that poll
   because a revert's drained window alone can take up to ~5.5 min, and a SIGTERM needs time to unwind it. After three
   consecutive failures it reverts everything the run recorded (host profile keys and the engine section's
   keys): hot keys immediately and without a restart; restart-required keys only through the same drained
   window, never as a blind patch while the gateway is live (reverting such a key would make OpenClaw force
   a restart over live runs). It stands down on `watchdog.off`, a newer run, a cancel or `done`; states
   `activating`/`deactivating`/`reloading` count neither way; it notifies on Telegram with key paths only.
   If the window fails, the key stays pending. (Q6, as changed by the operator)
7. **The health-restart timer is adopted into the kit and cannot force anything.** (Superseded by
   [ADR-0004](0004-bounded-graceful-restart-under-memory-pressure.md) for confirmed memory pressure only.)
   `scripts/openclaw/openclaw-health-restart.sh` keeps the triggers, the 3 h cooldown (same state file)
   and `watchdog.off`; with runs in flight or an unreadable probe it only notifies once per episode; an
   idle gateway gets the drained doctor; it never stops, starts or restarts anything itself. Setup takes a
   hand-installed copy over only interactively: the old timer is disabled and its files are moved (never
   deleted) to `~/.openclaw/backup/hand-units/<timestamp>/`. (Q7)
8. **`gateway.bind: tailnet` left by 2.0.0** is offered back on the next interactive run, as a gated,
   drained revert to the recorded previous value, default No. (Q5)

## Alternatives Considered

| Option | Why not |
|---|---|
| Ask the CLI which keys restart | `config patch --dry-run --json` has no such field |
| Parse the installed dist at runtime | *Rejected originally, adopted by the amendment below:* the objection was that a parse failure would fail open. The parse is now strict and a failure is fail closed, so the objection no longer holds |
| Apply hot keys unattended and pend only the restart ones | an unattended run changing a live gateway's behaviour at all is what ADR-0001 rejects; unattended applies nothing |
| Wait up to N minutes for a drain, then apply | a bounded wait is still a forced restart in disguise (for setup; the memory valve of [ADR-0004](0004-bounded-graceful-restart-under-memory-pressure.md) is a different, gated mechanism) |
| Patch the host copy of the health timer in place | the kit could not test, version or restore it |
| Make the engine section's primary model fill-only too | it is a wizard choice the operator just made |

## Consequences

### Positive
- No setup run, attended or not, can make OpenClaw restart a live gateway; operator values survive setup.
- A bad config write is detected and reverted by a watch the operator can follow and cancel.

### Negative
- *(Superseded by the amendment below.)* The original pin made every key restart-required on each OpenClaw
  upgrade until the pin was refreshed. Now only a format change the parser does not understand does, and
  `status` and `verify` say so.
- Behaviour change (called out in the release notes): hosts customised under 2.0.0 are no longer made
  canonical. This host keeps the `bind: tailnet` 2.0.0 wrote, because it now looks like an operator value
  (decision 8 offers the way back).
- Hand edits stay outside the kit: a hand-run `openclaw config set gateway.*` still restarts a live gateway
  after 300 s. That is OpenClaw's behaviour and there is no setting; the runbook says to use
  `apply-pending`.
- The engine teardown, the MCP mirror and `models update` write through their own paths with hot keys only
  (asserted by tests for the engine and bindings patches); folding them into the drained helper is a
  follow-up.

### Risks
- A flapping `openclaw health` for unrelated reasons (the memory pressure of the incident) can trigger a
  revert. Three consecutive failures limit this, and the revert is notified.
- `systemd-run --user` unavailable: the watch is skipped with a message, never replaced by a foreground loop.

## Release notes (prepared, not applied as a release)

Target label **2.0.2**, with the fill-only behaviour change called out; see `CHANGELOG.md`, "Unreleased".

## Amendment (kit 2.1.1): the watch's outcome model

The 2026-10-10 run showed the watch reverting a healthy gateway because its own probes could not run
(pitfall T42). The revert rule above stands; what changed is when the watch is allowed to conclude "failed":

- **Probe classes.** answered (rc 0), no-answer, slow (rc 124) and cannot-probe (rc 126/127, a missing
  interpreter, `openclaw --version` failing). A cannot-probe ends the watch INCONCLUSIVE at once.
- **Settle, sustain, corroborate.** Probes are ignored for 120 s after the last reload line that names a
  recorded key (the journal read degrades to time-only); the failure must last 300 s; and one of MainPID
  changed, NRestarts grew, unit failed/inactive, port not listening, a stop in the journal must hold. Without
  corroboration the watch ends INCONCLUSIVE; the health timer (T41) remains the backstop for a wedged gateway.
- **Recorded kind.** Setup records `kind` (hot|restart|none) and `openclaw_version` per change. The watch
  routes by the recorded kind when the live version is unreadable or equal; a different readable version goes
  through the live reload rules (the amendment below; fail closed when they cannot be read); a record from
  before 2.1.1 is classified the same way.
- **Confirmed keys.** A key whose `config hot reload applied` line was followed by an answered probe is
  reported as `kept_confirmed` and never reverted or queued.
- **Exit 7.** A rejected dry run of the drained window (`EXIT_APPLY_REJECTED`) never queues a restore entry.
- **Outcomes** recorded in `config_watch.result.outcome`: healthy, reverted, inconclusive, revert-failed,
  baseline-unhealthy. Only revert-failed is an ERROR in `verify`.

## Amendment (unreleased): the rules follow the installed OpenClaw

Decision 1 pinned the table to one OpenClaw version and treated every other version as all-restart. The
2026.10.3 dist table was identical to the pinned 2026.10.2 one, so the pin cost a forced "everything is
restart-required" mode on every release and bought nothing. The kit no longer pins or compares a version.

- **Source.** The rules are read from the installed package: `dist/config-reload-plan-*.mjs` next to the
  `openclaw` the PATH resolves, a file read (no `node`, no subprocess, no gateway). The version is shown in
  `status` as information and is compared with nothing.
- **Strict parse.** The reader returns "unavailable" unless the three policy tables are each found once,
  every policy object has the exact `{ prefixes: [...], kind: "..." }` shape, every prefix is a string
  literal or the spread of `AUTH_CREDENTIAL_PATHS`, every kind is restart, hot or none, and the table is
  plausible (at least 40 rules, `gateway` and `discovery` are restart). Anything else is a parse failure.
- **Restart dominance.** `BASELINE_RULES` is the table the kit was written against (`BASELINE_READ_FROM`,
  documentation only). Its restart entries always win over the installed table: a key the installed OpenClaw
  now calls hot stays restart-required until a developer moves the baseline, and `status` lists those
  prefixes. A key that became restart-required in the installed OpenClaw is restart at once.
- **Fail closed.** Unreadable or unparseable rules make every key restart-required: setup completes, the keys
  are recorded as pending exactly as before, `status` names the reason and `verify` warns (never errors).
- **Unchanged.** The write gate in `apply_patch`, the drained window, the busy probe ("cannot tell" is busy),
  the watch's outcome model and recorded-kind routing (the watch re-classifies with the live rules when the
  version differs, fail closed when they cannot be read), the health timer and the ADR-0004 memory valve.
- **Not modelled, always conservative.** `canHotReloadGatewayAuthCredentials`, the `gateway.roles` noop and
  channel plugins' runtime prefixes make OpenClaw less restart-happy than the static table; the kit may defer
  a key that would have applied live.

## Related

- Pitfall T40 (`docs/openclaw/pitfalls.md`); runbook "How setup asks", "Operator overrides", "Pending
  restart-required keys" and "Post-setup watch" (`docs/openclaw/runbook.md`)
- Code: `scripts/ai_resources/openclaw_reload_rules.py`, `scripts/ai_resources/openclaw_host.py`
  (`drained_window`, `apply_drained`, `watch_config`), `scripts/ai_resources/setup/cockpits/_openclaw_host.py`
- Tests: `tests/test_openclaw_reload_rules.py`, `tests/test_openclaw_restart_gate.py`,
  `tests/test_openclaw_config_watch.py`

## Links

- [ADR-0007](0007-subscription-lanes.md): amends this ADR for lane agents (watch record, teardown, classify-before-add, drained path).
- [ADR-0004](0004-bounded-graceful-restart-under-memory-pressure.md): supersedes decision 7 for the memory trigger.

## Amendment (ADR-0010, proposed)

Carve-out: a confirmed channel disable (`openclaw channels remove --channel C --account A`) may ask the Gateway to stop that account and restart its listener. It never restarts the Gateway itself, and the other restart rules are unchanged. Approvers: operator (approved, N5); security-auditor APPROVE (2026-10-19, re-review at 5953c22). See [ADR-0010](0010-setup-five-phases-channels-team.md).
