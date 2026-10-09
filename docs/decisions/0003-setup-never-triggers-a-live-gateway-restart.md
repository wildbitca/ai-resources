# ADR-0003: Setup never triggers a live gateway restart, and never overwrites a value the operator set

**Status**: accepted (unreleased, target 2.0.2)
**Date**: 2026-10-09
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

1. **A pinned restart-required table that fails closed.** `scripts/ai_resources/openclaw_reload_rules.py`
   mirrors OpenClaw's reload rules for one version (`PINNED_OPENCLAW_VERSION`). A different or unreadable
   version, `gateway.reload.mode: off` and a path no rule matches are all classified restart-required. A
   fixture copy keeps GitHub CI offline; a drift test compares the pin with the installed dist where there
   is one. (Q1)
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
6. **A bounded, backgrounded, cancellable post-setup watch reverts the run on failure.** A transient user
   unit (`systemd-run --user`, `RuntimeMaxSec=660`) polls `openclaw health` for 10 minutes. After three
   consecutive failures it reverts everything the run recorded (host profile keys and the engine section's
   keys): hot keys immediately and without a restart; restart-required keys only through the same drained
   window, never as a blind patch while the gateway is live (reverting such a key would make OpenClaw force
   a restart over live runs). It stands down on `watchdog.off`, a newer run, a cancel or `done`; states
   `activating`/`deactivating`/`reloading` count neither way; it notifies on Telegram with key paths only.
   If the window fails, the key stays pending. (Q6, as changed by the operator)
7. **The health-restart timer is adopted into the kit and cannot force anything.**
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
| Parse the installed dist at runtime | the dist files have hashed names and change shape between releases; a parse failure would fail open |
| Apply hot keys unattended and pend only the restart ones | an unattended run changing a live gateway's behaviour at all is what ADR-0001 rejects; unattended applies nothing |
| Wait up to N minutes for a drain, then apply | a bounded wait is still a forced restart in disguise |
| Patch the host copy of the health timer in place | the kit could not test, version or restore it |
| Make the engine section's primary model fill-only too | it is a wizard choice the operator just made |

## Consequences

### Positive
- No setup run, attended or not, can make OpenClaw restart a live gateway; operator values survive setup.
- A bad config write is detected and reverted by a watch the operator can follow and cancel.

### Negative
- Each OpenClaw upgrade makes every key restart-required until the pin is refreshed (safe but noisy:
  unattended hosts accumulate pending keys, shown in `status`). The drift test names the files to refresh.
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

## Related

- Pitfall T40 (`docs/openclaw/pitfalls.md`); runbook "How setup asks", "Operator overrides", "Pending
  restart-required keys" and "Post-setup watch" (`docs/openclaw/runbook.md`)
- Code: `scripts/ai_resources/openclaw_reload_rules.py`, `scripts/ai_resources/openclaw_host.py`
  (`drained_window`, `apply_drained`, `watch_config`), `scripts/ai_resources/setup/cockpits/_openclaw_host.py`
- Tests: `tests/test_openclaw_reload_rules.py`, `tests/test_openclaw_restart_gate.py`,
  `tests/test_openclaw_config_watch.py`
