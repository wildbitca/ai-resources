# ADR-0004: A bounded, notified graceful gateway restart under confirmed memory pressure

**Status**: accepted (unreleased; the release label is decided at the release step)
**Date**: 2026-10-10
**Decision makers**: planner, implementer; operator decisions D1-D15 recorded below
**Builds on**: [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md) (decision 7 is superseded for the memory trigger only)

## Context

On this host the gateway is permanently busy: `ai-resources openclaw busy` counts 6-12 runs in flight
at almost every tick. ADR-0003 decision 7 made the health-restart timer unable to act whenever any run
is in flight. The result on 2026-10-09/10 was a timer that logged `mem=pressure ... healthy` while the
gateway cgroup sat at 107 % of `MemoryHigh` with swap 100 % used, and then a frozen gateway.

## Evidence (measured, 2026-10-10, OpenClaw 2026.9.9)

- Root cause: the cgroup grows because per-session MCP runtimes of finished sessions are never evicted
  (`mcp.sessionIdleTtlMs` unset means "live until session cleanup or gateway shutdown"). The gateway
  process itself was ~2 GB RSS; 625 helper processes were the rest.
- `MemoryHigh=12G` with `MemoryMax=infinity` throttles instead of killing: the gateway main thread sat in
  state `D` (`wchan` `__mem_cgroup_handle_over_high`), the journal was silent for ~17 min, `openclaw
  health` printed "still starting", and `openclaw gateway restart` was refused with
  `GATEWAY_RESTART_PREPARATION_REFUSED ... database is locked ... Gateway was not signaled`.
- A graceful `openclaw gateway restart` with 6 runs in flight, from a process outside the gateway
  cgroup, returned rc 0 after 365 s, with about 35 s of real downtime; the cgroup went from 13.25 GB to
  5.56 GB and OpenClaw's native restart recovery resumed the interrupted main sessions (3 marked, 4 runs
  aborted, 3 `useResume=true` and 3 fresh sessions). The journal excerpt is the test fixture
  `tests/fixtures/journal/gateway-restart-recovery-2026-10-10.txt`.
- 00:54 the same day, a drained doctor stop used the whole `TimeoutStopSec=330` and ended in SIGKILL of
  ~260 helpers (`gateway.stop_close_failed`).
- NOT verified: whether any Slack or Telegram message was duplicated or lost after the 13:20 restart.
  This is an open risk, and it is why the shipped mode is `notify` (decision D5).

## Decision

### Allowed

A deliberate, bounded and notified graceful `openclaw gateway restart`, with runs in flight, triggered
only by **confirmed memory pressure**, from the health-restart timer or from the operator's CLI
(`ai-resources openclaw graceful-restart`). It relies on OpenClaw's native restart recovery.

### Limits

1. A mode knob `OPENCLAW_GRACEFUL_RESTART=off|notify|on`, shipped as `notify` (D5). `on` is enabled by the
   operator only after a supervised restart.
2. A window, evaluated in an explicit timezone knob (`OPENCLAW_RESTART_TZ`, default `America/Guayaquil`;
   the host clock is UTC) and a hard ceiling: window `02:00-05:00`, ceiling 105 % of `MemoryHigh`; the
   ceiling overrides the window (D1, D1b).
3. A cooldown of 3 h and at most 2 restarts per day (D2).
4. One notice per episode.
5. `watchdog.off` is held by a signal-safe marker guard on every exit path, SIGTERM included.
6. Never run from inside the gateway cgroup (T29).
7. Confirmed means two samples inside one run, a positive `memory.events high` delta and the existing
   cgroup-plus-swap predicate. An unreadable required signal fails closed: nothing acts.
8. Success means a new MainPID plus `openclaw health`; a `PREPARATION_REFUSED` is retried with a bounded
   backoff and is never classified as frozen.
9. **Time budget.** Every phase has a monotonic bound, so one run provably ends before the unit's
   `TimeoutStartSec=50min` (3000 s). Restart attempts plus backoffs share a 900 s phase budget: a retry
   starts only if its backoff plus a full attempt (600 s command + 30 s grace) still ends inside it. The
   health poll is a 180 s wall-clock budget that includes its probes (at most +5 s). Settle is clipped to
   180 s and every bookkeeping probe is capped at 30 s. Worst case: script probes 360 s + restart phase
   900 s + health 185 s + settle 180 s + at most 18 capped probes and the confirm sample 780 s + notices
   180 s = 2585 s (43 min), a margin of about 7 minutes. (The first revision budgeted 25 min and counted
   loop iterations; a hung probe stretched the poll to about 39 min.)

### Forbidden (unchanged)

- Setup never restarts a live gateway on its own initiative (ADR-0003 decisions 2-4).
- No `--force`, no killing of children, no edit of `openclaw.json`.
- No restart-required config write outside a drained window.
- A frozen gateway never triggers a restart: it is notify only (D3). There is no runtime `MemoryHigh`
  raise and agent children are not moved into their own slice (D4).

### Exceptions to ADR-0003's wording

ADR-0003 said nothing in the kit restarts the gateway. Two paths do, by the operator's explicit choice:
setup's and `models update`'s `restart_gateway` (`setup/cockpits/openclaw.py`, `models.py`). Per D11 the
unattended idle path of those goes through the same marker guard and logs; the interactive RESTART_NOW
choice stays. `ai-resources openclaw doctor` now refuses a busy gateway (exit 6) unless
`--even-if-busy` (D9): the valve never goes through the doctor.

### Also recorded

- `MemoryHigh` stays 12G; `_step_memory_high` re-asserts it on every setup run. It is measured, not
  raised (D4).
- Prevention comes first: `mcp.sessionIdleTtlMs=1800000` and `agents.defaults.timeoutSeconds=14400` are
  filled by the host profile, fill-only, hot keys, opt-out `OPENCLAW_RESOURCE_GUARDS=off` (D6, D14). The
  pinned dist shows an active lease prevents eviction, so a live run's runtime is not evicted.
- The orphan-MCP reaper is deferred (D12). Its orphanhood proof, kept here for later: the owning session
  is absent from `openclaw sessions list --active --json`; no live `claude` process is in its ancestry or
  process group; it has been idle longer than `sessionIdleTtlMs`; it holds on two re-checks 60 s apart;
  the reaper is default-off and dry-run first.
- Frozen notice channel: local log, the systemd failure and the gateway notice retried once it answers.
  No new direct Telegram Bot API credential path (D8).

## Alternatives Considered

| Option | Why not |
|---|---|
| Keep ADR-0003 decision 7 (never act with runs in flight) | on this host that means never acting; the gateway froze |
| Wait for a drain, then restart | there is no idle moment; a bounded wait is a forced restart in disguise |
| Raise `MemoryHigh` | hides the leak and makes the cgroup grow; the root cause is prevented instead |
| Runtime `set-property MemoryHigh` on frozen | untested reversibility and setup would silently revert it |
| Reaper now | needs the TTL measured first; the proof above is kept |
| Put the logic in bash only | the gate logic would exist twice; the Python CLI owns the gates |

## Consequences

### Positive
- Memory pressure is bounded without waiting for an idle moment that never comes.
- Every attempt is gated, snapshotted, reported and notified once.

### Negative
- A graceful restart aborts runs longer than the 5-minute drain; subagents, cron, ACP sessions and
  interrupted tool calls are not resumed, and work arriving during the drain is rejected (T29).
- Mode `on` is not validated against duplicate or lost messages yet (open risk).

## Links

- [ADR-0003](0003-setup-never-triggers-a-live-gateway-restart.md), pitfalls T29, T39, T40, T41
- Code: `scripts/ai_resources/openclaw_pressure.py`, `scripts/ai_resources/openclaw_host.py`
  (`graceful_restart`, `MarkerGuard`), `scripts/openclaw/openclaw-health-restart.sh`
