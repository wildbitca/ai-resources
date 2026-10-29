# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

## [Unreleased]

## [3.2.10] — 2026-10-29 — Minimal allowlist backup

See ADR-0025.

- feat(backup): `openclaw-backup.sh` takes a minimal allowlist backup: `openclaw backup sqlite create` snapshots of the shared database and every agent database, engram through `VACUUM INTO`, and only non-reproducible files (`_backup_lib.sh`). 311 MB and 78 s on the reference host, against 3.7-11 GB and 6-30 min. The legacy whole-`~/.openclaw` archive (`OPENCLAW_BACKUP_MODE=full`) is removed: there is one backup layout. The global snapshot sanitizes the delivery queue.
- fix(backup): a run that finds the lock held waits (`OPENCLAW_BACKUP_LOCK_WAIT`, 3600 s) and then fails with exit 75 and a notice; it no longer logs "skipping" and exits 0, which let the weekly backup go 17 days stale. The archive is built as `.part`, asserted (size bounds, listable, every snapshot, engram, checksum) and only then published; the `.sha256` is still written last.
- feat(backup): retention per tier (`OPENCLAW_BACKUP_KEEP_{DAILY,WEEKLY,MONTHLY,MANUAL}`, defaults 7/4/3/3) and `OPENCLAW_BACKUP_SOFT_TIERS` for the guard.
- fix(units): the weekly backup timer moves to Sun 05:00 and the monthly one to the 1st at 05:30, clear of the daily 03:30 run and of each other. After `ai-resources setup`, the local `openclaw-backup-weekly.timer.d/override.conf` (Sun 05:00) is redundant and can be removed.
- test: an `integration` marker (`tests/conftest.py`) labels the tests that drive real processes, signals, locks, `/proc` or other tooling instead of the files the kit generates. They are skipped by default and run with `pytest --integration` or `INTEGRATION=1 scripts/run-tests.sh`; 18 test files carry it, kept for history. This takes the flaky `test_the_pool_counts_live_watchers_only_and_a_slot_frees_when_one_dies` (red CI on `main` since 2026-10-28) out of the default run.

## [3.2.9] — 2026-10-28 — Spawn acceptance judges the right session

See ADR-0024.

- fix(acceptance): `spawn.landed-on-role-agent` took "the newest session" by `updatedAt`. An old spawn (13:21, before the 3.2.4 pin of `subagents.model`) had been touched later (17:03) and outranked the Haiku spawn (14:41) that proved the fix, so the case reported `kit-role-explore ran claude-sonnet-5-5` on a host whose roles all resolve correctly. It now orders by `sessionStartedAt`, treats a session that began before the live config last changed as unproven (not a failure), and proves the config directly: OpenClaw's spawn order (`<target>.subagents.model`, `agents.defaults.subagents.model`, the target's primary) must give a model of the role's own chain, with the exact `openclaw config set` command when it does not.
- fix(setup): the "your model or thinking setting is kept" warning is silent when the live values already equal what the kit would write, and otherwise names the live and matrix values, the model a spawn of that agent runs on, and the commands to follow the matrix.

## [3.2.8] — 2026-10-28 — One setup run leaves the host correct

See ADR-0022 (kit-lane adoption), ADR-0023 (auto gates) and docs/runbooks/setup-incomplete-and-skipped.md.

- feat(setup): in `--auto` a change that is kit-owned, reversible and non-restarting is applied by default (the `openclaw-*` units and timers, the retired `agents.defaults.subagents.model`, kit-owned cost values, the `kit-role-verifier` Opus escalation the kit added). `--accept-gates` answers yes to every non-restarting gate for CI; it never answers a restart-required one (ADR-0003). Every skipped gate is listed at the end of the run with its reason and the command to finish.
- fix(setup): a config patch rejected for "offline maintenance" is retried with short backoff (2, 4, 8, 15, 30 s; 180 s total, `--patch-wait` / `AI_RESOURCES_PATCH_WAIT_S`), then stored as a pending patch (`apply-pending` and the next setup apply it). An unapplied patch ends the run INCOMPLETE with exit code 75 and the one-line command to finish; "Setup complete" is no longer printed. The post-setup watch now starts after the drained reconcile, so it no longer opens a window the next patch meets.
- fix(auth): the OpenClaw sign-in reader parses stdout only, extracts the JSON object that has the expected keys, retries a maintenance rejection, tells unreadable from empty, and corroborates with the config's `auth.profiles`. A failed read no longer persists "no usable provider", which had blocked kit-role agents, routing, fallbacks and image changes. `--auto` reads the live auth too.
- fix(lanes): setup adopts the `kit-lane-*` agents whose config has the legacy writer's shape (fill-only: the lane's own `subagents.model`, runtime pin, catalog slug, orchestrator `allowAgents`), records it and tears it down safely. A spawn override that still beats a lane's own model is named once.
- fix(warnings): no stale Telegram routing warning when Telegram is retired (an `--ask` cleanup of the stale lines is offered once); "Restore the engine it replaced?" and each "already routed to agent X" decision are asked once and remembered; `Not sent:` lines only with `--verbose`; the utility-model probe parses the real reply and names its failure cause.
- fix(doctor): every issue has a stable name and a `fixed by: setup | operator (command)` label; the banner counts open issues only and says "re-run setup" only when one is setup-fixable; the litellm key check runs only in multi-model mode.
- fix(setup): setup never writes into the kit repo checkout (an agent workspace that is the checkout is skipped), so a run no longer dirties the repo's `AGENTS.md`.

## [3.2.7] — 2026-10-28 — Lane agents keep their own model

- fix(host): the profile no longer writes `agents.defaults.subagents.model`. OpenClaw resolves a spawn's model as the target's `subagents.model`, then `defaults.subagents.model`, then the target's primary, so the Sonnet default overrode the target on every cross-agent `sessions_spawn` (`kit-lane-agy-*` and `kit-lane-cli-*` all ran `claude-cli/claude-sonnet-5-5`).
- fix(host): `setup` removes that key from a live config when it still holds the value the kit wrote (the current Sonnet pin); any other value is the operator's and stays, and `keep` in `kit-host-overrides.json5` blocks the removal. The change is recorded so a restore puts it back.

## [3.2.6] — 2026-10-28 — No residue in ~/.cache or ~/.openclaw

See ADR-0021 and docs/runbooks/scratch-and-residue.md.

- feat(scratch): `keep-on-failure` is explicit (`--keep-on-fail`); the default failure removes the run. The root cap is 2 GiB by default (`AI_RESOURCES_SCRATCH_CAP_GB`) and every removal is logged to `~/.cache/ai-resources/prune.log`.
- feat(caches): GOCACHE, GOMODCACHE, npm, pnpm, pip, pub and gradle share `~/.cache/ai-resources/shared/<tool>` (`scratch shared-env`, exported by `scratch new --shell`, `scan-env` and dispatch); `scratch prune --shared` trims them to 6 GiB.
- feat(scans): `scratch scan-env` and `scratch scan-checkout` for scans that read existing checkouts; security-scan-results keeps 7 basic + 2 weekly.
- feat(backups): `scratch backup-dir` puts kit backups under `~/.cache/ai-resources/backups/<name>-<date>` with a 14-day expiry pruned by the daily timer.
- feat(worktrees): `scratch worktree-done` removes a managed worktree only when clean and pushed; weekly `openclaw-worktrees-gc.timer` runs `openclaw worktrees gc`.
- feat(doctor): sizes of `~/.cache/ai-resources` and the legacy `~/.openclaw` dirs; `doctor --fix` is a dry run, `--fix --apply` deletes (legacy `~/.openclaw` children after 7 days).

## [3.2.5] — 2026-10-28 — One scratch root

Runs no longer litter `~/.cache`: 336 stray top-level entries (about 10 GB) came from verifier, implementer, reviewer and security-auditor runs that each invented their own scratch path. See ADR-0020.

- feat(scratch): `ai-resources scratch new|done|rm|ls|prune|prune-scans|venv`. One directory per run, `~/.cache/ai-resources/runs/<role>-<slug>-<UTCstamp>-<pid>-<rand>/` (`tmp/ pytest/ logs/ out/ clone/` and `.scratch-meta.json`), exported as `AI_RESOURCES_SCRATCH` and `TMPDIR`. Success removes it; failure keeps only `logs/` for 3 days. Python `scratch.run()` and bash `scratch_begin` (trap on EXIT INT TERM) in `scripts/scratch.sh`.
- feat(scratch): pruning decides by meta `created_at` and owner-pid liveness (start ticks), never by mtime; live runs are skipped, `flock` serialises pruners, a 5 GiB global cap removes the oldest first. Runs on every `scratch new` and daily from the new `openclaw-scratch-prune.timer` user unit that `ai-resources setup` installs with the other host units.
- feat(scratch): shared virtualenvs under `~/.cache/ai-resources/venvs/<python>-<lockhash>/`, pruned after 30 idle days (`tests/requirements.txt` is the suite's lockfile).
- feat(doctor): warns about legacy top-level `~/.cache` scratch names and about `security-scan-results` above 500 MB; `doctor --fix` removes a legacy entry only when it is older than 24 h and no process has its cwd or an open file inside. Real tool caches are allowlisted.
- feat(scratch): `scratch prune-scans` keeps the newest 7 `security-scan-results` runs, ordered by the timestamp in the directory name; the host writer calls it at the end of every run.
- feat(orchestration): every dispatched child gets a scratch directory and the rule "never write directly under ~/.cache or /tmp" in its prompt; the engine finishes it when the step is judged. Role prompts, `kit-implement.js`, `WORKFLOW_CONTRACT.md`, the generated AGENTS block and the `kit-orchestration` skill name `$AI_RESOURCES_SCRATCH`; `validate_kit.py` rejects `/tmp`, `mktemp`, `--basetemp` and bare `~/.cache` paths in role, persona and workflow prompts.
- fix(tests): a bare pytest run keeps its basetemp in a scratch run and removes it on green (the old `ai-resources-pytest-runs` pruning by mtime is gone); the host shell scripts and every library temp directory use the scratch helpers.

## [3.2.4] — 2026-10-28 — Spawn pin for operator-owned roles

- fix(orchestration): a kit-role agent whose model the operator owns still gets `subagents.model` (its own model) when the host sets a default subagent model, so a `sessions_spawn` lands on the role's model instead of the default.

## [3.2.3] — 2026-10-28 — Retired-components follow-ups

- fix(setup): the retired-provider auth logout passes `--agent main` (OpenClaw refuses it with several agents configured).
- fix(host): the host profile no longer fills `channels.telegram.streaming` for a Telegram that is not configured, so setup stops recreating the stub the retired-components reconciler removes.

## [3.2.2] — 2026-10-28 — Retired-components dry run and auth logout

Patch release: the 3.2.1 tag predated these two commits, so an installed 3.2.1 did not list or log out retired components.

- fix(setup): the retired-components reconciler also logs out the retired provider's saved auth profile (`openclaw models auth logout openrouter:default --yes`): that profile lives in OpenClaw's auth store, not in openclaw.json, and its missing env key (`Secret store entry "OPENROUTER_API_KEY" was not found`) failed every Gemini agent turn.
- feat(setup): `setup --dry-run` lists what the retired-components reconciler would remove (`retired components: would remove ...`).

## [3.2.1] — 2026-10-28 — Self-maintaining OpenClaw host, Slack alerts, cross-provider proof

Fixes from the 2026-10-28 audit of a live OpenClaw host. `ai-resources setup` on a fresh or drifted host now leaves a clean setup and keeps it clean.

### Fixed

- fix(tests): the suite deleted the operator's real `~/.config/ai-resources/offbox-wif-cred.json` on every full run (the teardown tests unlink the kit-derived credential and `tests/conftest.py` did not redirect `OFFBOX_WIF_CRED_PATH`); this was the root cause of the uploader failures "not readable" on 2026-10-27 and 10-28. The path is now redirected, and a test pins it.
- fix(offbox): `ai-resources openclaw offbox-cred [--repair]`; `verify`, the timer-run `openclaw-verify.sh` and the `setup.offbox-cred` acceptance case regenerate the kit-derived credential when it is missing (a path the operator named is only checked).
- fix(host): `lanes.yaml` said `mode: single-model` on a host whose team spans Claude Code, Antigravity, the Anthropic API and the Gemini API. `single-model` only ever meant "no LiteLLM/OpenRouter gateway". A CLI/API team host now renders `mode: multi-provider`; `multi-model` stays for a real gateway.
- fix(orchestration): a `sessions_spawn` of a `kit-role-*` agent ran on `agents.defaults.subagents.model` (Sonnet) whatever the role's matrix row says: OpenClaw resolves `<target>.subagents.model`, then the default, and only then the agent's own `model` (measured: `kit-role-explore` ran Sonnet, its chain is Haiku; the verifier would have run Sonnet instead of Opus). Setup now pins each kit-role entry's `subagents.model` to its own chain (fill-only; an operator's value stays), and `spawn.landed-on-role-agent` checks the spawned session's model against the chain.
- fix(setup): a bare `channels.telegram: {enabled: true}` stub no longer counts as an active Telegram channel.

### Added

- feat(setup): retired components. `scripts/ai_resources/retired.py` is a declarative list (OpenRouter, LiteLLM, Telegram) with in-use guards and an idempotent reconciler that runs in `setup` (and honours `--dry-run`): it removes the auth profile, plugin entries, `channels.telegram`, telegram `bindings[]` and `commands.ownerAllowFrom` entries, the matching env keys and the Telegram token file, backs up what it removed under `~/.config/ai-resources/backups/retired-<stamp>/` (0600, secret values never copied) and keeps catch-all `bindings[]` entries last. The setup state's `backend: litellm` becomes `cli` when no gateway is deployed.
- feat(alerts): host alerts (watchdog, maintenance, verify, backup guard, health restart, db-fault) go to Slack: `OPENCLAW_NOTIFY_CHANNEL` / `OPENCLAW_NOTIFY_TARGET` in `kit-host.env` (`user:<id>` or `channel:<id>`), filled by setup from `commands.ownerAllowFrom`; Telegram is used only when it is the only configured route. The team narration hook, sender and watcher deliver to Slack (`channel:<id>` targets, `OPENCLAW_NARRATION_TARGET` override).
- feat(host): `ai-resources openclaw db-fault [--json] [--notify]` detects stuck agent SQLite databases ("reclamation commit checkpoint expired" in the journal) and ingress pressure / dead letters in `openclaw health --json`; the watchdog alerts once per fault and `openclaw-verify.sh` fails on it. It never runs `doctor`.
- feat(host): the weekly maintenance window retries instead of silently skipping: the timer fires daily at 04:30 and 08:30 UTC, `maintenance.week` records the tries, the first skip of a week is announced, a finished week exits at once.
- feat(host): setup raises the gateway `MemoryHigh` itself (live `set-property`, never lowers a higher value). Sizing: node `--max-old-space-size=8192` + about 95 MiB per MCP child + the claude/agy workers; 16 GiB is the floor for a team host, 20 GiB (`OPENCLAW_MEMORY_HIGH=20G`) for a heavy one.
- feat(acceptance): `N/A` status for a component the host does not use; group `team` with `team.main-assumes-21-roles`, `team.cross-provider-run` (the real engine over `feature-cross-provider`, agy + claude-code roles, checker not the implementer's family), `team.cli-and-api-one-run` (agy, claude-code, anthropic-api and gemini-api answer under one run id); `spawn.landed-on-role-agent` reads the gateway's own sessions; `spawn.explore-run` no longer calls `openclaw agent` inside an agent session (OpenClaw refuses it) and proves the spawn from the recorded session; `gemini.tool-call` is proven on the Gemini API or Antigravity route actually used and is N/A when neither is configured.

## [3.2.0] — 2026-10-27 — Roles matrix, OpenClaw orchestrator, acceptance

Major internal rewrite of routing, with BREAKING changes (see below). Roles are the unit: one table, `scoring/routing.ROLES`, generates `lanes.yaml`, the Claude Code agent definitions, the OpenClaw `kit-role-*` agents and the setup tables, and `tests/test_matrix_equality.py` pins them. Decisions: [ADR-0019](docs/decisions/0019-roles-matrix-single-source.md).

### Added

- feat(routing): `routing.ROLES` (21 roles: 15 team, 6 chat) with ONE primary and ONE fallback chain per role, `classify_quota` for every pool, `ai-resources route <role> [--run RUN] [--writers] [--usage-file] [--unavailable b=cause] [--record --step S] [--refresh] [--json]`; the fallback fires only on pool exhaustion (quota text or `pool_usage >= spill_at` 0.85) or no response, with the cause recorded; a checker with no independent rung is `blocked` (`no-independent-checker`), never weaker.
- feat(routing): dynamic checker independence from the dispatch ledger (Sonnet checks Gemini/Haiku-written work, Opus only as the ceiling; INFERRED from Sonnet 59.6 vs Opus 55.4 on untagged security).
- feat(agy): `ai-resources agy run --role R` calls `agy -p` from a Claude Code session and returns the report contract; quota, timeout, empty answer and a missing binary exit 75 with the cause and the next matrix cell. agy has the same privileges as claude and writes in the checkout.
- feat(dispatch): `dispatches.jsonl` ledger (role, pool, backend, model, tokens, duration, outcome, fallback cause, run id, writer families) with `ai-resources dispatch record|list [--summary]`; `runs record --from-dispatches RUN`; `doctor` compares actual against the matrix per pool and role and prints a weekly summary.
- feat(orchestration): `main` and every channel agent may `sessions_spawn` every `kit-role-*` agent (`allowAgents` wired and recorded); spawn limits are raised, never lowered; per-run `.agent-output/<run>/<role>/`, one handoff per team and a write-scope claim (`scope_guard`) keep concurrent teams apart.
- feat(acceptance): `ai-resources acceptance [--group] [--case] [--json] [--list] [--operator] [--dry-run] [--offline]`: 52 cases in 7 groups (route, fault, checker, spawn, gemini, agy, setup-live) that make one real minimal call per matrix cell and assert the model and pool that answered; operator-run cases are listed with their exact command. `verify --live` is the setup-live subset of the same registry; doctor prints the Gemini tool-calling verdict.
- feat(setup): one Review table `Role | Primary (backend:model, pool) | Fallback` from the matrix; chat plane on Anthropic API (sonnet, fallback haiku) and Gemini API (flash); heartbeat stays on Haiku until `gemini.tool-call` passes; Codex is an optional pool, off unless detected and authenticated.
- docs: ADR-0019; ADR-0006/0007/0009/0010/0015/0016/0017/0018 marked superseded where it applies; routing, orchestration and kit-orchestration docs describe routed dispatch.

### Changed

- BREAKING: `lanes.yaml` is `{mode, generated_from, routing, digest}`; the `agents`, `candidates`, `second`, `resolved`, `gates` and `acks` keys are gone. Old state files load and are migrated on the next save (`lane_acks` and `gates` are dropped).
- BREAKING: the by-model `kit-lane-*` agents are retired and replaced by the per-role `kit-role-*` agents (model = matrix primary plus native `model.fallbacks`). Setup deletes only agents it created that nothing references; any other is kept with `kept: <reason>`.
- Workflow steps default to `backend: route`; overrides are `claude-code`, `openclaw-agent` (+`agentId`) and `agy`.
- `explore` on the chat plane is now `explore-bulk`. A role whose cell is skipped (`agy-missing`, `codex-missing`) shows the cell that runs and the cause.

### Removed

- BREAKING: the worktree `lanes` ledger (`lanes` subcommand, `orchestrator/worktree.py`, `backend: lane`, `workflows/feature-lanes`), replaced by `scope_guard.py`.
- BREAKING: the scoring engine (`compose`, `flow`, `explain`, `detect`, `gate`, `headroom`, `headroom_sources`, `profile_out`, `scoring/lanes.py`) and the flags `--compose`, `--headroom`, `--headroom-live`, `--why-json`, `--allow-fable`.
- BREAKING: gates G0-G7 and their evidence guard hook, the Google and Anthropic acknowledgements and the operator waiver, `--lanes-antigravity` / `--no-lanes-antigravity`, `--lanes-orchestrator`, `--lanes-dry-run`, the held-lane machinery, `lane_member: second`, display-only rows, static role-count weights, the Gemini write tripwire and the agy report-only role list.
- Unused leftovers: `model_accounts.render_text` / `to_json`, `_openclaw_providers.preselected` / `profile_providers`, `openclaw_agy.stub_models`, `compat.claude_google_openrouter_verified`, `wizard._untick`, `tests/lane_symmetry.py` and the tests of every removed concept (about 2,900 tests). Net across the release: +6,741 / -19,854 lines in `scripts/` and `tests/` since 3.1.0 (`git diff --shortstat 912edad..HEAD -- scripts tests`).

### Fixed

- fix(setup): the acceptance run found the pre-setup defects it exists for: `allowAgents` missing for the kit roles, a gateway plugin path that differs from the disk, 384 MCP children under the gateway, an off-box credential file that does not exist. `ai-resources setup` clears them.
- fix(acceptance): the pool of an `anthropic-api` / `gemini-api` cell is read from the transport's wire API line, so a CLI runtime cannot pass as API credits.

- fix(setup): Claude Code agents are installed with the routing-matrix model, so a profile `routing:` override reaches them and a legacy `by_role` pin does not (tests updated to the matrix).
- fix(orchestrator): every checker is gated for independence and gets its per-run model; the model actually used is recorded and read-only scope violations are logged on the engine path.
- fix(scope-guard): an ad-hoc run keeps its claim while its holder process lives.
- fix(agy-run): `--run` is validated, a symlinked output is refused, agy errors are scrubbed and the secret patterns widened.
- fix(ledger): private rows are ASCII-only, readers are tolerant, run ids are validated and an unknown writer fails closed.
- fix(route): `--writers` given as matrix cells (`agy:gemini-4-argon-high`, `claude-code:sonnet`) resolve their family by stripping the backend.
- fix(acceptance): `setup.plugin-fresh` states which rule tripped (path, version or time) with the running and the disk path or version.

### Migration notes

1. `brew upgrade ai-resources`
2. `ai-resources setup` (recommended run; shows the dry-run diff, applies the `allowAgents` wiring, retires unused `kit-lane-*` agents, performs ONE drained restart for the stale plugin and the MCP-children overflow).
3. `ai-resources acceptance` (add `--operator` to list the cases that kill turns: `setup.sigterm-window`, `setup.real-restart`, `setup.watchdog`, `spawn.from-main`, and run them yourself).
4. Rename any profile `routing:` override to the new shape `routing: {role: {primary, fallback, class}}`; an API-credit pool is rejected on a team role.

### Unverified (only a real run on the host proves)

- `ai-resources setup` applied on the live gateway, and the `allowAgents` patch applying through `openclaw config patch`.
- A kit-role spawn from `main` (`spawn.explore-run`, `spawn.from-main`).
- Native `model.fallbacks` firing under the `claude-cli` runtime pin (needs a forced provider failure).
- SIGTERM and watchdog behaviour (operator-run cases).
- Gemini tool-calling through OpenClaw: `gemini.tool-call` is SKIP on this host (`Secret store entry "OPENROUTER_API_KEY" was not found`), so no verdict is written and the heartbeat stays on Haiku.
- MCP load under many concurrent teams (the memory sample is a measurement, not a cap).

## [3.1.0] — 2026-10-27 — one routing matrix across four pools, a setup that survives restarts

### Added

- feat(routing): one routing matrix (role -> primary -> secondary) in `scoring/routing.py`, rendered into `lanes.yaml` under `routing:`; Claude Code (Max) and Antigravity (AI Pro) are primary routes, chat agents use Anthropic credits and light work uses Gemini credits (ADR-0018).
- feat(routing): consumption-balancing policy (`routing.choose_pool`, spill at 85%) so Balanced distributes work across the four pools.
- feat(doctor): `pool_usage.py` and doctor sub-check 4g2 show usage per pool and where each class of work runs now; read-only, `unknown` instead of zero, never prints a key.
- feat(setup): the recommended run turns the Antigravity lane on by default when `agy` is installed and records the Google acknowledgement under the operator waiver of gates G0-G7; `--no-lanes-antigravity` still opts out.
- docs: ADR-0018; ADR-0007 section 11, ADR-0009 decision 8, ADR-0016 and ADR-0017 marked superseded where it applies; `docs/routing.md`.
- fix(openclaw): the drained window (`doctor`, `apply-pending`, setup's restart) turns SIGTERM and SIGINT into exits, so `watchdog.off` is removed and the gateway started again on every exit path, a signal mid-action included; the create-to-try gap is closed by holding the signals from the marker create until the `try` is entered.
- feat(openclaw): `ai-resources openclaw reconcile [--dry-run]`, run at the end of `ai-resources setup`: ONE drained restart (lock, marker guard, busy probe, T29 refusal) when the gateway runs older kit plugin code than the disk (new `version` rule next to `path` and `time`) or its cgroup holds more than 24 MCP children; a refusal is recorded as pending; a clean gateway and a second run are no-ops.
- feat(verify): `ai-resources verify --live` reads the RUNNING system: gateway health, plugin version against the disk, kit hooks (StopFailure quota signal, narration hook), lane agents and backend, MCP children, off-box credential file mode.
- fix(openclaw): the reload rules are read through the `~/.openclaw/tmp/agent-cli/openclaw` shim (followed to its package, then other PATH hits, then the Homebrew node_modules); units are rendered with the real binary, never the shim; if nothing is readable the reason names the shim.
- fix(setup): the off-box uploader credential is regenerated (0600) from the host federation and `openclaw-backup-uploader.service` is started (`--no-block`) and its report read.
- feat(setup): stdio MCP servers are denied (fill-only, recorded) to agents matching `OPENCLAW_MCP_DENY_AGENTS` (default `kit-role-*`) in the same run that creates them.
- feat(openclaw): MemoryHigh is sized to the host (55 % of RAM, 12G..24G; `OPENCLAW_MEMORY_HIGH` overrides) instead of a fixed 12G.
- feat(hooks): `kit_sleep_guard` also refuses sleeps that add up above 30 s in one command and `while`/`until` wait loops; the OpenClaw AGENTS.md polling rules say one blocking wait per job (`awaitResults`).
- feat(setup): `--dry-run` shows the hooks to register, the off-box credential, MemoryHigh and the owed restart.
- feat(credits): credit ledger in `~/.config/ai-resources/credits.json` (0600, atomic): per prepaid pool (`anthropic-api`, `gemini-api`) the declared opening credit and date (recorded once by setup from `--credit-anthropic-usd` / `--credit-gemini-usd`, `AI_RESOURCES_CREDIT_ANTHROPIC_USD` / `_GEMINI_USD` or the amount already in the file; the recommended run never asks and leaves `declared: unknown`; `ai-resources credits set <pool> <usd>`), the measured spend (OpenClaw per-agent transcripts for provider `anthropic` / `google`, priced with the audit's list prices, an estimate) and `remaining = declared - measured`. The Max pool is tokens only, no USD. Neither provider exposes a balance API; the ledger says so. `ai-resources credits [show|collect|set]`; doctor refreshes it; the daily verify timer runs `credits collect --check`; an 80 % alert reaches `doctor`, `verify` and the verify timer.
- feat(keys): `ai-resources keys` and doctor sub-check 4k probe the Gemini and Anthropic keys in `gateway.systemd.env` AND in the OpenClaw auth profile with one free `models.list` call each and report `gemini key: env ok / profile ok / mismatch fixed`, never a value. A rejected env key beside a working profile key is rewritten from the profile (backup first, 0600, atomic); the restart that applies it is the existing drained reconcile, which now also owes a restart whenever `gateway.systemd.env` is newer than the running gateway (`env-changed`). `verify --live` reports a rejected key.
- fix(setup): `gateway.systemd.env` is not rewritten (mtime kept) when its content is unchanged.
- test: the stale-plugin doctor tests ignore the new pool-usage lines; the sizing golden no longer depends on the CPython wording of a `TypeError`; the setup-idempotence run allows the read-only live probes; the recommended-run Google acknowledgement test follows ADR-0018.
- chore: remove unused `locate_reload_plan_dist`, `utc_iso`, `apply_defaults`.

### Removed

- Unused `locate_reload_plan_dist`, `utc_iso`, `apply_defaults`; ADR-0007 section 11, ADR-0009 decision 8, ADR-0016 and ADR-0017 routing text marked superseded by ADR-0018.

### Operator notes

- Run `ai-resources setup` yourself after `brew upgrade`; it performs one drained restart for the stale plugin code and the MCP-children overflow.
- `GEMINI_API_KEY` in `gateway.systemd.env` was rejected by Google (HTTP 401) while the `google:default` profile key works: `ai-resources keys` (also doctor and setup) rewrites the env entry from the profile key, keeping a backup, and the next idle `ai-resources openclaw reconcile` restarts the gateway to load it.
- Declare the opening credit once per pool: `ai-resources credits set anthropic-api <usd>` and `... gemini-api <usd>`; until then the ledger shows `declared: unknown` and measured spend only.

## [3.0.0] — 2026-10-27 — delegation has to pay for itself, inter-agent text is untrusted, the ledger does not move

Major version: `plan-check` now rejects a plan that delegates without a named reason, and the scorecard rules file, the handoff template and the hook set grew (migration notes at the end).

### Added

- **Unified delegation rule** (`skills/kit-orchestration` "Delegation triggers", `references/scorecard.md` "Delegation"). Every orchestration step must pay its token cost with a named reason and the default is not to delegate. Three ordered layers, cheapest first, the first that decides stops the rest: (1) resumability (a LITE card works inline); (2) divisibility, a new `divisible` field: the work splits into units that share no state and wait on none. A code heuristic decides (disjoint files, no `depends_on`); only when it is doubtful (a unit lists no files) may one small `explore` call answer, with strict JSON and only the unit list and its files as input; not divisible, or in doubt, means one agent even when the work is large; (3) a named `reason`, required to delegate: `explore-without-polluting-main-context`, `parallel-writers-on-disjoint-units` (needs `divisible: yes`) or `independent-verification` (never skipped for security- or merge-touching work, also on a LITE card). The decision (layer, evidence, `divisible`, `reason`, and the real token cost of the divisibility call, `null` when the runtime does not report tokens) is recorded on the scorecard and in the handoff `scorecard` line. One shared block (`workflows/scripts/sizing-core.js.inc`, embedded in `/kit-plan` and `/kit-implement`) with a Python twin in `scripts/ai_resources/scorecard.py` (`decide_delegation`, `divisibility`); `tests/test_delegation.py` runs both over the same cases. New CLI flags `ai-resources scorecard ... --units JSON --delegate-reason R --security-or-merge --model-answer JSON`. Existing size flags still win and record no decision; a card without a decision keeps its 2.9.0 form, and a bare `lite` still means inline.
- `plan-check`: `reason` is required when the route is not `inline` (or `spawn_budget` > 0) and must be one of the three; `route: writers` needs `divisible: yes`; `resume_test` may not contradict a recorded `resumability` score (pass with 2+, fail with 0). The template and the planner's Sizing block carry the new `divisible` and `reason` fields.
- **UNTRUSTED markers** for inter-agent text. Free text one agent writes and another reads (review findings handed to the verifier and the fixer, a `Block_reason` that reaches the next step through the handoff, the reason quoted when a run fails) travels between the literal markers `<<<UNTRUSTED from=...` and `UNTRUSTED>>>`, capped at 600 characters, with any marker inside it neutralised. Defined once (`untrusted` in `scorecard.json`), applied by `orchestrator/untrusted.py` and by the embedded block; covers same-provider reports, not only another provider's.
- **A step reports once.** The engine keeps a digest of the accepted report: an identical repeat stays idempotent (`already reported`); a second report that differs is refused and flagged (`refused: true`, a `report-refused` event, `refused_reports` on the step) and its text is not stored.
- **Ledger by reference.** `ai-resources ledger-check <tasks.md>` fails unless the `## Log` is an append-only continuation of the committed one and no `S#` was rewritten or removed (a change of scope appends an `L#` line and a new `S#` that supersedes the old one; `task-ledger.md` and the template no longer say to rewrite `S#` in place). `ai-resources covered-check`, the engine and the SubagentStop hook validate that `Covered:` names only ids that were handed out (a handed `T#` hands out the `S#` it covers); nothing handed out, nothing checked.
- **Real-agent tests and UNVERIFIED** (`tests/real_agent_gate.py`). A test that needs a live provider is marked `real_agent`; it is skipped unless `--real-agent` (`REAL_AGENT=1 scripts/run-tests.sh`), `provider_unavailable(reason)` skips with `UNVERIFIED: ...`, every such skip is printed as `UNVERIFIED` instead of counting as green, and a run that asked for real agents (or `AI_RESOURCES_REQUIRE_REAL_AGENT=1`) fails while any is UNVERIFIED. Rule 017 states that a provider outage is never committed into the shared default team (profiles, the `balanced` default): it is handled at runtime or by a local override.
- **Provider quota-block signal for the headroom reader.** Claude Code 2.1.310 fires `StopFailure` when a turn ends on an API error, with a structured `error` (`rate_limit`, `billing_error`, `overloaded`, ...), optional `error_details` and the rendered error text; it carries **no structured reset time**. `hooks/kit_quota_signal.py` (StopFailure, `rate_limit|billing_error`) records the block in `~/.cache/ai-resources/quota-blocks.json`; `headroom_sources.read_quota_block` turns an unexpired block into a known reading (100% used until `resets_at`) that the Claude pool, lane weights and the scoring flow prefer over an older status-line percentage. A reset is used only when the error text states one; otherwise a 15-minute window is assumed and recorded as `assumed-ttl`, never as a measured reset. The signal is positive-only: no file, no hook or no active block is `unknown` (`quota-block:signal-unavailable` / `none-active`), never evidence of headroom. Not proven on a live quota event: no real StopFailure payload existed on this host, the shape is the one in the Claude Code hooks reference.
- **Read-only enforcement.** `ai-resources readonly on|off|status` writes `.agent-output/readonly.lock`; while it exists (or `AI_RESOURCES_READONLY` is set) `hooks/kit_handoff_guard.py` refuses, for the main session and subagents, file-writing tools outside `.agent-output/` and plainly writing Bash commands (redirects, `tee`, `sed -i`, `rm`/`mv`/`cp`/`mkdir`/`touch`, mutating `git` subcommands, package installs). The tool side is exact; the Bash side is a best-effort deny-list, not a sandbox. A lock older than 12 hours is ignored.
- **Per-spec verdicts.** A verifier handed spec ids ends with `Spec_verdicts: S1=pass, S2=fail, S3=not_run`; the engine rejects a reply with no entry for a handed spec id, an invalid value, or a PASS beside a `fail`, and keeps the verdicts on the step.

### Changed

- Verification follow-ups: the readonly deny-list ignores quoted text and catches more write forms, `Covered:` reads only its leading id list and expands a handed `T#` through `refs.spec`, an agent-written reason is wrapped in status and events too, and an inline security- or merge-touching `/kit-implement` run is told to get an independent verifier.
- `skills/kit-orchestration/SKILL.md` stays under its 12,700-byte ceiling (the detail lives in `references/scorecard.md` and `references/handoff.md`).
- `tests/golden/sizing_snapshot.json` and `tests/fixtures/role_render/claude_subagents.json` regenerated deliberately: the golden moves only for the `write-plan` prompt (the Sizing block now asks for `divisible` and `reason`), the scorecard usage text, the handoff template content and the verify/apply-fixes prompts of the review cases (UNTRUSTED markers); the render fixture moves only for the `planner` and `verifier` roles.

### Migration notes (2.9.x -> 3.0.0)

- Plans and `tasks.md` that delegate (route `explorer(...)` or `writers(...)`, or `spawn_budget` > 0) must now carry `reason: <one of the three>`, and writers also `divisible: yes`; `ai-resources plan-check` reports the missing fields. Inline plans are unaffected.
- `skills/kit-orchestration/scorecard.json` gains two top-level keys, `delegation` (`reasons`, `layers`, `min_units`) and `untrusted` (`begin`, `end`, `limit`); the six dimensions, thresholds, rosters and size spellings are unchanged, so existing scores map to the same modes. The `scorecard` handoff line is unchanged unless a delegation decision was recorded.
- `handoff.md.template` gains the optional keys `reason` and `divisible`. A handoff `block_reason` written by an agent is now wrapped in the UNTRUSTED markers.
- Existing ledgers: `ai-resources ledger-check` compares with the committed version, so a ledger that rewrote an `S#` before upgrading is reported on its next change. Run `ai-resources setup` after upgrading to register the new `StopFailure` hook in `~/.claude/settings.json` (the plugin route registers it from `hooks/hooks.json`).
- Test suites: `scripts/run-tests.sh` is unchanged; `real_agent` tests (none ship yet) are skipped and reported UNVERIFIED unless `REAL_AGENT=1`.

## [2.9.0] — 2026-10-27 — automatic team sizing, one sizing block, a suite that does not depend on where it runs

### Added

- Automatic team sizing: a six-dimension scorecard (resumability, blast radius, risk, uncertainty, verifiability, spread; 0-3 each) maps deterministically to LITE / STANDARD / FULL, a role roster with model tiers, and escalation thresholds (never a silent downgrade). Rules in `skills/kit-orchestration/scorecard.json`, logic in `ai_resources.scorecard` and the `ai-resources scorecard` command, embedded in `/kit-plan` and `/kit-implement` (which no longer need a mode; `--inline|--lite`, `--light|--standard`, `--full` still override). `handoff.md.template` gains the optional `scorecard` key. Regression fixtures in `evals/scorecard/cases.json`, tests in `tests/test_scorecard.py`.

### Fixed

- Scorecard follow-ups from the independent verification. The handoff `scorecard` key is now written by code (`ai-resources scorecard ... --handoff PATH`, also `escalate` and the new `gate` command, which measures the diff itself); a risk-item bump records `scored_mode`, the effective `mode` and `bump_reason` on the card; `/kit-implement` re-enters an inline run on `{"size":"inline","failures":N,"diff_lines":N}` so LITE escalates to STANDARD or FULL in code, and recomputes the test-round limit the moment it escalates (a light run that fails twice now gets full's second round); invalid or out-of-range scores mean FULL in the Python module, the CLI and both scripts, also beside a bare legacy `mode`, while no scores at all because the classifier failed or returned nothing stays light; a lone `verifiability` 3 is STANDARD, not LITE (`max_verifiability` 2); every generated `workflow-*` skill scores first, and `/kit-implement` re-scores from the scores recorded in the plan's `## Sizing` block (the folded `workflow-odd` intent); fixtures are 12 (3 LITE, 5 STANDARD, 4 FULL) and the handoff abbreviations are defined. `references/scorecard.md` lists what is code-enforced and what is instruction. Tests: `tests/test_scorecard_fixes.py`.
- `kit-implement.js`: a mid-run reassess `mode_change` now retargets the scorecard card (mode, roster, handoff, scorecard line, escalations) together with the run size, so the recorded card and the ledger follow the new mode in both directions.
- A bare legacy `mode` of `lite` (a response or Sizing block with no scores) now means LITE/inline in `/kit-plan` and `/kit-implement`, as it already did in the Python scorecard and as `--lite` does; the scripts used to read it as light. The resolver reads `size_flags` in `scorecard.json` (generated into `sizing-core.js.inc`), so there is one table; every other spelling (`inline`, `light`, `standard`, `full`, unknown, none) resolves as before. Pinned by `tests/test_bare_lite_mode.py`; the golden snapshot moves for the three `lite` cases only.
- `ai-resources openclaw doctor`, the drained window and the restart snapshot defaulted `self_cgroup_path` to the literal `/proc/self/cgroup`, so the `SELF_CGROUP_PATH` constant never applied and a run from inside the gateway cgroup was refused with exit 8 even where the caller meant otherwise. The default is now resolved at call time (`None` falls back to `SELF_CGROUP_PATH`).
- Test suite independent of the host it runs on: `OPENCLAW_CLI` is unset for every test (it made setup headless and skipped its interactive steps), the `parent_alive` test uses a script named `claude` instead of a symlink the host-binary guard resolves to the interpreter, and the process-group timeout test counts a killed, not yet reaped process as dead. Run from inside an OpenClaw session the suite went from 52 failures to 0 with no test removed or relaxed.

### Changed

- Sizing consolidation, no behaviour change (pinned by `tests/test_sizing_characterization.py`, a snapshot of the scorecard CLI, the Python helpers and both scripts over every accepted input spelling). `skills/kit-orchestration/scorecard.json` now also holds the mode-to-size map (`sizes`), every accepted flag and `size` spelling (`size_flags`) and the handoff abbreviations (`abbrev`); `ai_resources.scorecard` builds every card through one helper (`_card`, `_roster`) and resolves a bare legacy mode with `mode_of_spelling`; `escalate` and `gate` share one code path. `/kit-plan` and `/kit-implement` carry one generated sizing block (`workflows/scripts/sizing-core.js.inc`, pasted by `ai-resources generate`; a test fails when it is stale) with a single `resolveSize` (valid scores decide, invalid scores mean full, a bare mode only without scores, no answer means light), `bumpForRisk` and `parseArgs`. Dead code removed: the overridden first `escalations` key in the `/kit-implement` result and `/kit-plan`'s unused `escalateSize` (now local to `/kit-implement`). Prose stated once in `references/scorecard.md`: `docs/orchestration.md` no longer repeats the mode table, triggers and escalation numbers, and the generated `workflow-*` wrappers point at the skill for the fallbacks. Known difference, kept as it was: a bare legacy `mode` of `lite` means LITE in the Python helper but light in the scripts, whose classifier schema only allows `inline`, `light` and `full` (`standard` agrees: STANDARD and light).
- `workflow-odd` is folded into `/kit-plan` and `/kit-implement` and removed (`workflows/odd.workflow.yaml`, `skills/workflow-odd/`, its index entry, `tests/test_workflow_odd.py`): one classifier, no duplicated logic. Plan is always the first step and classification happens once, in `/kit-plan`, which now also asks `ai-resources runs suggest` for a hint (never a decision) and passes `start`, `estimated`, `forced` and the forecast on to `/kit-implement`. `/kit-implement` no longer classifies: it takes the size from its arguments or reads the plan's `## Sizing` block (an unreadable block means `light`; scores in the block that are present but invalid mean `full`), and with neither a plan nor a size it stops and points at `/kit-plan`, because workflow scripts cannot import a shared module. It also runs mid-run `runs reassess` (files over twice the estimate, a sensitive area, a trivial task), counts verifier gaps as rework (`runs reassess <mode> --verifier-fail`) and appends the run with `runs record`; each degrades silently when `ai-resources runs` is missing. The managed-block pointer now reads `/kit-plan --inline`. Counts: 141 skills, 19 workflows.
- Task-size gating (adoption plan wave 1, `docs/research/gentle-ai/04-gap-analysis-and-adoption-plan.md`). `kit-orchestration` gains "Authorize first", "Task size" (`inline` / `light` / `full`, risk items, `--inline` / `--light` / `--full`), "Delegation triggers" (no subagent without a named reason, handoff by reference) and "Tasks live with the spec" (temporal `specs/features/<feature>/tasks.md`, folded by `knowledge-audit` at closure; template `templates/tasks.template.md`). `/kit-plan` and `/kit-implement` classify once (or take `size`) and run fewer phases for `inline` and `light`; `--full` keeps the old loop. Workflow steps may carry `execution_hints.inline: true`, so the agent holding the plan does them itself and delegates only when a delegation trigger fires. The planner role gains the Sizing rubric; the verifier may run authorized spec examples in a `mktemp -d` scratch copy, matching what `/kit-implement` already asked of it.
- `skills/kit-orchestration/SKILL.md` drops from 21,758 to about 12 KB: backend and lane procedure moved to `references/backends-and-lanes.md`, read only for `openclaw-agent` and `lane` steps. Its size ceiling test is 12,700 bytes; the sizing prose now lives once in `references/scorecard.md` (risk items, size spellings, the fallback rule), which leaves the core at 12,100 bytes.

- By-reference handoff (adoption plan wave 2). `handoff.md.template` gains **optional** keys `refs.spec`, `task_ids`, `spec_ids`, `class` and `route`; the orchestrator engine and reply parser carry them (an unknown `Class` is ignored, a plain run renders none), its step prompt adds one "Work by reference" line only when a run has them, and the SubagentStop hint names the optional `Covered: T#/S#` line. No hook or check requires the new keys (`tests/test_handoff_optional_keys.py`).
- Implementer and tester roles gain the RED-per-requested-rule, PRESERVE and differential-check rules (scratch copy under `mktemp -d`, outside the workspace), a commit rule (one unit per commit, a risk item gets its own, never push or merge) and, for the tester, gate checks instead of new broad suites.
- Task ledger and Engram mirror convention (`skills/kit-orchestration/references/task-ledger.md`, one line in `rules/300-engram-memory.mdc`): the temporal `tasks.md` is primary; an optional best-effort mirror under topic key `kit/<feature>/tasks`, two writes, ids and paths only, core `mem_*` tools only. `mem_current_project` resolves the repository's project inside a managed worktree.
- `workflow-odd` (adoption plan wave 3): a four-step size-gated workflow (`classify`, `implement`, `verify`, `close`) whose steps carry the new `execution_hints.inline: true`, so the agent holding the plan does them itself and delegates only when a delegation trigger fires. The generated skill and `skills-index.json` entry are checked in; `workflow-*` skills are already linked by prefix, so `skill_scope.py` needs no entry. `ai-resources setup` adds one line (about 210 bytes) to the managed OpenClaw workspace block pointing at "Task size" and `workflow-odd`. (Later folded into `/kit-plan` and `/kit-implement`, see above.)
- Plan sizing check (adoption plan wave 4): `ai-resources plan-check <plan-or-tasks.md>` and a `validate_kit.py` pass reject a missing or self-contradicting `## Sizing` block (unknown mode, inline with a risk item or a spawn budget, a risk item without a verifier, writers in `light`). `/kit-plan` asks the plan writer to run it and to return `task_ids` and `spec_ids`; `/kit-implement` takes `tasks_path`, `task_ids` and `spec_ids`, hands work over by reference (or the request verbatim with no plan), adds RED, PRESERVE, differential and commit rules to the writer prompt, makes the sign-off verifier run authorized examples in a scratch copy with one verdict per `S#`, and gives `light` one correction round (`full` keeps two).
- `skills/kit-orchestration/SKILL.md` core stays about 12.4 KB with the new pointers; its size ceiling test moves from 12,500 to 12,700 bytes.

### Added (adaptive sizing)

- Run ledger and adaptive sizing. `ai-resources runs record|suggest|reassess|list|report` (`scripts/ai_resources/run_ledger.py`): one append-only JSON line per finished run in `~/.local/state/ai-resources/run-ledger.jsonl` (size chosen and recommended, forced flag, agents, steps, verifier rework, outcome, lines, wall time, and tokens/USD read from the `audit` transcripts for the run window). `suggest` is heuristic until 5 similar runs exist, then history may move it one step; the owner's `--inline`/`--light`/`--full` always wins. `reassess` gives mid-run advice from signals (verifier gaps, files over twice the estimate, a sensitive area, a trivial full task) in either direction with no floor or ceiling. `report` prints cost against quality per mode with the sample size beside each number and threshold proposals that are text only. `/kit-plan` and `/kit-implement` call them when the command exists; `skills/kit-orchestration/references/run-ledger.md` holds the rules. Nothing in OpenClaw core changes.

### Added

- Balanced credit policy (ADR-0017). Setup now offers, one confirm per path, the SAFE subset on a host that already routes to a `google/*` model: `agents.defaults.utilityModel` -> `google/gemini-3.8-flash`, a `flash` alias entry, and the Gemini API credit as the first fallback of `kit-lane-agy-gemini38flashhigh` (then Haiku). The heartbeat is not moved. `openclaw_cost_migrations.balanced_credit_proposals`, `setup/lanes_render.LANE_API_FALLBACKS`.
- `templates/ai-usage/credit_alert.py`: the 80% credit alert of the usage report (`AI_USAGE_CREDIT_ALERT_PCT` overrides the threshold), copied next to the host's `ai-usage-report.sh`.

## [2.8.4] — 2026-10-25 — setup no longer signs agy out

### Fixed

- Setup no longer signs agy out. Every apply deleted `~/.gemini/antigravity-cli/antigravity-oauth-token`, which is where agy keeps its session over SSH or without a secret service, so the agy lanes failed until the next manual login. Each cockpit owns its sign-in; setup leaves this one alone.

## [2.8.3] — 2026-10-25 — setup reads the OpenClaw config again when another node is first on PATH

### Fixed
- Setup took the last output line of `openclaw config file` as the config path. With an older node first on PATH,
  openclaw re-runs itself under a newer one and prints a notice after the path, so setup read an empty config and
  acted as if every OpenClaw setting were unset. It now takes the last line that is a path.
- The Skill Workshop mode the kit authors on an unset host is `"off"`: OpenClaw removed the proposal flow and its
  schema rejects `"propose"`, so the patch was declined.

## [2.8.2] — 2026-10-25 — planning and exploring roles run without a shell

### Changed
- The OpenClaw `kit-role-planner`, `kit-role-architect` and `kit-role-explore` agents now also deny `exec` and
  `process`: the claude-cli runtime turns `tools.deny` into Claude Code's disallowed tools, so they lose Bash. The
  reviewer, verifier and security agents keep their shell (they run git and tests) and still cannot edit files.
- Setup moves an entry that still carries the 2.8.1 deny list to today's one; a deny list the operator changed is kept.

## [2.8.1] — 2026-10-25 — the off-box upload works under workload identity federation

### Fixed

- **Off-box backup uploader:** with a federated credential file (`OPENCLAW_OFFBOX_CRED_FILE`), gcloud no longer
  inherits the quota project of the operator's active configuration. That quota project made Google ask the backup
  service account for `serviceusage.services.use`, so every upload failed with HTTP 403. gcloud now runs with
  `billing/quota_project=LEGACY` for that identity.

## [2.8.0] — 2026-10-25 — setup runs on its own, and Sentry and Figma share one token

### Changed

- **`ai-resources setup` applies the recommended answer to every question by default.** There is no "How should setup run?" question any more. A first run, or `--fresh`, still walks every step. `--ask` asks every question as before. Credentials and the Google acknowledgement are still asked.
- **Off-box backups use the host's workload identity federation.** When `OPENCLAW_OFFBOX_BUCKET` is set and the host has an agent WIF credential config (`~/.config/gcloud/agent-wif`), setup writes `~/.config/ai-resources/offbox-wif-cred.json` (0600). That file impersonates the backup service account (`OPENCLAW_OFFBOX_IMPERSONATE_SA`, default `k3s-backup`), and setup points `OPENCLAW_OFFBOX_CRED_FILE` at it. No question is asked. The uploader prefers that credential file over the other identity knobs. An unreadable file fails with a state file. The `roles/iam.workloadIdentityUser` grant goes through GitOps.
- `ai-resources openclaw status` reuses a clean `openclaw doctor` result if it is under 15 minutes old and `openclaw.json` has not changed since. It prints the result's age. `--fresh-doctor` forces a new run. `openclaw doctor` itself takes over a minute on a busy host.

### Added

- **Shared-token MCP servers (Sentry, Figma).** Each token is stored once in the kit credential file (`~/.config/ai-resources/.env`, 0600), and Claude Code, OpenClaw and agy all get the same `ai-resources mcp-run <server>` stdio entry. No cockpit config holds the secret.
  - **When setup asks:** setup imports a token already on the host (the shell env or `~/.openclaw/.env`) without a question. It asks for a token only when some cockpit already defines that server, and it asks only once.
  - **OpenClaw:** the shared entry replaces the hosted OAuth entry (`mcp.sentry.dev`, `mcp.figma.com`) and is restored on teardown. A server the operator defined is left alone.
  - **agy:** the shared entries are added fill-only, next to engram.
  - **Packages:** `mcp-run` runs pinned packages through `npx`: `@sentry/mcp-server@0.42.0` and `figma-developer-mcp@0.13.2`.

### Fixed

- Kit role agents in OpenClaw keep their `model` when the shell entry is rewritten, and a same-model role records its primary.
- Skill links into a vanished Cellar directory, or into an ai-resources checkout, move to the installed kit. A plain `skills` directory elsewhere is still treated as foreign.

## [2.7.3] — 2026-10-24 — a Slack cost key no longer aborts the OpenClaw step

### Fixed

- Accepting "Stop the Slack bot answering every message of a thread" sent the patch through the host channels guard, which only admits `channels.telegram.streaming`. Its error aborted the rest of the OpenClaw step, so the lanes, kit roles, orchestrator plugin and agy drift migration never ran (the drifted agy lanes were never offered their repoint). The Slack cost writer and its teardown now run their own guard, which admits exactly their two keys, and a refused Slack patch is reported without stopping the step.

## [2.7.2] — 2026-10-24 — agy drift repoints to the lane's own model

### Fixed

- The agy lane drift migration derived its target from the drifted Anthropic primary, so `kit-lane-agy-gemini38flashhigh` (on `anthropic/claude-sonnet-5-5`) was offered only removal and `kit-lane-agy-gemini4argonhigh` would have been repointed to `claude-opus-5-5`. The target is now the lane's own model in the lanes table (`agy-cli-lane/gemini-3.8-flash-high`, `agy-cli-lane/gemini-4-argon-high`).

## [2.7.1] — 2026-10-24 — re-signing the Google acknowledgement keeps its account

### Fixed

- The Google acknowledgement prompt proposed an empty account when the stored acknowledgement was stale (its text changed), so pressing Enter closed the Antigravity lane with "the Google account must be named". It now proposes the account the stale acknowledgement named.

## [2.7.0] — 2026-10-24 — the Antigravity lane opens on the operator's signature

### Changed

- **Gates G0-G7 no longer hold the Antigravity lane (operator waiver, ADR-0007 section 11).**
  - `agy_lane_gates.WAIVED_BY_OPERATOR` lists every gate, so `agy-cli-lane` becomes writable.
  - An agy candidate is admitted when the flag is on, a valid Google acknowledgement for the current text lists its
    role, and the plugin has an absolute `agyBin`. No probe window is needed.
  - The acknowledgement is still signed only by the operator.
  - A waived gate is not proven. G5 stays off, so agy keeps the operator's `~/.gemini` login, and slugs stay
    "unconfirmed" (display only).
  - Doctor and `lanes gates` report "waived by the operator". The next step they print is the signature.

## [2.6.6] — 2026-10-24 — the registration shell includes its identity

### Fixed

- **The bare entry `openclaw agents add` leaves is recognized with its `identity`.** That command also writes
  `identity: {name: <id>}`, so the 2.6.5 shell check never matched a live entry and the 13 unfinished kit roles stayed
  on the Anthropic API. An identity other than the bare name is still the operator's.

## [2.6.5] — 2026-10-24 — every kit role runs through Claude Code

### Fixed

- **A kit role spelled as a bare model string is read as the kit's.** OpenClaw accepts `"model": "<ref>"` as well as
  `{"primary": "<ref>"}`; the role and lane steps read only the second form, so such a role looked operator-changed,
  was kept, and never got its claude-cli pin (it stayed on the Anthropic API).
- **The bare entry `openclaw agents add` leaves when a run stops early is finished.** A run interrupted during the
  agent registrations left 13 roles with only name, workspace, agentDir and model. A rerun now recognizes that shell
  (only those keys, the kit's workspace and model) and fills the kit's fields: description, skills, thinking, the
  report roles' tool deny and the claude-cli pin. An entry with any other shape is still the operator's.

## [2.6.4] — 2026-10-24 — the haiku lane runs haiku again

### Fixed

- **A claude-cli lane whose primary drifted goes back to the model its id names.** Before 2.6.3 the B2.1 "no
  orchestrator on haiku" rule rewrote `kit-lane-cli-haiku55` to sonnet, and a rerun kept it there (the lane writes are
  fill-only). The worker question that moves the kit's agents to Claude Code now also restores such a lane's primary to
  its `anthropic/claude-<family>-<version>` ref and pins that ref on claude-cli. A primary off the Anthropic route is
  the operator's own and is left alone.

## [2.6.3] — 2026-10-24 — one question, then the recommended setup

### Changed

- **Setup asks one question, then takes the recommended answers.** "How should setup run?" offers the recommended setup
  (OpenClaw's agents on the Anthropic API, the kit team on Claude Code), every question, or every question from empty
  answers; it replaces "Use previous answers as defaults?". In the recommended run each prompt with an answer takes it and
  prints one line; credentials, hand-overs and prompts with no answer are still asked. `setup --auto` skips the question.
  ADR-0016 has an addendum.
- **The worker step also moves orphan kit lanes.** A `kit-lane-*` agent the current lanes mode no longer lists kept running
  on the API key, billed per token; it now moves to Claude Code with the `claude` worker, in the same question.

### Fixed

- **Setup no longer looks hung while it adds the kit-role agents.** Each `openclaw agents add` reloads the gateway (about
  20 s) and fifteen ran with no output; setup now says how many it adds and prints one line per agent.
- **The haiku lane stays on haiku.** The "no orchestrator on haiku" rule moved `kit-lane-cli-haiku55` to Sonnet; kit lanes
  are now exempt, as kit-role agents already were.
- **CLAUDE.md no longer loads the operator rules twice.** After the rules file was seeded, the rules rendered inside the
  managed block while the original blocks stayed below it. Blocks the rendered rules carry word for word are removed after a
  backup; an edited block stays.
- **Review no longer labels every OpenClaw provider "API key (ANTHROPIC_API_KEY)".** That variable is Claude Code's; an
  OpenClaw provider's key reads "API key".
- **The recommended run never signs the Google acknowledgement** for the operator: without a valid one Antigravity stays off.

## [2.6.2] — 2026-10-24 — kit agents run through Claude Code without moving the operator's agents

### Fixed

- **claude-cli lanes and kit-role agents no longer run on the Anthropic API by accident.** The kit put the `claude-cli` runtime
  in the shared `agents.defaults.models`, and only for a ref not listed there yet. On a host whose catalog already listed the
  ref (`anthropic/*` on the API) nothing was written and the lanes billed per token; on another host the pin would have moved
  every agent on that ref. The runtime is now pinned in the kit agent's own `models` map (OpenClaw reads it first), fill-only,
  and the lanes restore removes exactly the pins it added.

### Changed

- **A kit role on a Claude model runs through Claude Code** when Claude Code is installed and signed in, even when OpenClaw
  itself signs in to Anthropic with an API key. Review's "Billed to" shows the Claude Code sign-in for those rows.
- **Setup offers to run the `claude` worker through Claude Code** (default No; never in a dry run or a non-interactive run).
  Your own agents keep their route, so they can use an API key whose organization receives a Max plan's monthly API credits
  while the work they hand off runs on the subscription. ADR-0016 has an addendum.

## [2.6.1] — 2026-10-23 — the dry run shows rc files as a real apply leaves them

### Fixed

- **`setup --dry-run` no longer shows ~/.zshrc and ~/.bashrc as replaced by one line.** The preview diffed the whole file against a
  placeholder, so every operator line looked deleted; a real apply has always kept them and only written the managed env block.
  The preview now renders the file a real apply leaves (same variables, values masked) and lists no rc file when there is nothing
  to export.

## [2.6.0] — 2026-10-23 — delegated cockpit auth, live state over saved state, one operator-rules file (ADR-0016)

### Changed (setup is a coordinator; ADR-0016 supersedes the auth parts of ADR-0015)

- **Sign-in is delegated.** Each tool shows what it reports LIVE and asks keep or change. Change hands the terminal to the tool's
  own flow (`claude auth login`, `openclaw configure --section model`, `agy`) only on an explicit choice and never in
  `--non-interactive`, `--dry-run` or a test; the kit re-reads the live state and records that, not the answer. "API key" is shown
  only when `ANTHROPIC_API_KEY` or an `apiKeyHelper` is live. Claude Code's only kit extra is the `apiKeyHelper` setting (a
  command or a 0600 key-file path; a readable key file is refused; the key is never read or stored). `claude auth status` fields
  other than the method (email, org id) are dropped at the parse.
- **Live config beats saved state.** The OpenClaw engine and model defaults come from `openclaw.json`; the saved answer only fills
  where live is empty. A difference is shown "old -> new" and needs an explicit confirm (default No); without it no
  `agents.*.model` or runtime is patched. A stale saved `antigravity` engine is asked again, never applied. The engine no longer
  adds "google (CLI backend)" on its own. The confirmed `kit-lane-agy-*` drift migration is unchanged.
- **All writes wait for Apply.** Hygiene, the agy MCP file, the CLAUDE.md migration and the operator-rules file queue their
  changes (`setup/deferred.py`); Review lists them under *Will write*, `--dry-run` lists them, Ctrl-C before Apply leaves the disk as
  it was.
- **Review shows who runs and who is billed**, in separate columns (Runs in, Billed to) with the model and effort of every role;
  one live sign-in row per tool; "unknown" is shown, never guessed. The progress rail follows the five phases.
- **Channel re-enable** takes the token with a masked prompt and passes it through the child environment (`--use-env`, Telegram);
  other channels get OpenClaw's own interactive command. A token is never an argument.

### Added

- **One operator-rules file** (`~/.config/ai-resources/operator-rules.md`, seeded once from your CLAUDE.md after a confirm,
  secret-scanned) rendered as a managed block into `~/.claude/CLAUDE.md`, every OpenClaw agent workspace `AGENTS.md` and
  `~/.gemini/GEMINI.md`. A rule moved to a skill by the CLAUDE.md migration is offered to the file first.
- Antigravity shows whether a Google account state is present (presence only) and offers keep or change.

### Fixed

- **agy lanes read "held" without gate evidence.** The host overlay that made absent evidence count as proven is gone; the Review
  no longer prints a literal "(on)". The Google acknowledgement now states that the Antigravity terms prohibit third-party tools
  using Antigravity OAuth and that you accept the suspension risk; its text hash changed, so a stored Google acknowledgement is
  rejected once and setup asks you to re-confirm it (it never re-signs for you).
- **OpenClaw routing follows the live reading.** When OpenClaw reports no usable provider, setup changes no routing, fallback,
  kit-role or image entry and points to `openclaw configure --section model`; usability is read per agent, not across all of them.
  A provider profile of unknown type is billed "unknown", never "API key".
- **No silent billing change in Claude Code.** A saved `apiKeyHelper` is no longer written back from saved state; it is applied only
  when chosen in this run, cleared when another sign-in is chosen, and every form (file, `cat <file>`, command with arguments) is
  checked for owner and 0600 mode at Apply. Choosing Vertex records the live kind after a re-read.
- The live model is the default of the OpenClaw engine model question, so keeping it raises no "old -> new" confirm.
- The operator-rules file is secret-scanned again at every render; a flagged file is not copied into any cockpit. A discarded
  seed no longer leaves planned headings behind.
- **Tests never touch the host.** An unstubbed configure path ran `npm install -g openclaw@latest` and `systemctl --user enable --now`
  on every full run. `tests/conftest.py` now refuses host binaries (also through shells, wrappers and `os.system`/`exec*`).
  `scripts/run-tests.sh` runs the suite in parallel with pytest-xdist and the wall-clock `timing` tests serially: about 3 minutes
  instead of 28. CI uses the same script. kit-plan asks each step to verify with its own tests, and kit-implement's reviewers read
  the branch diff instead of an empty working tree.

### Removed

- The kit's own key prompt for OpenClaw providers (`paste-api-key`), the image gate's own sign-in and its `<provider>:manual`
  profile, the dead Providers and Credentials wizard steps, the unused lifecycle step and the stale ten-step rail labels.

## [2.5.0] — 2026-10-23 — per-cockpit setup, OpenClaw providers and kit roles, native orchestrator (ADR-0015)

### Added (per-cockpit setup, OpenClaw providers and roles, native orchestrator; ADR-0015)

- **Each tool asks its own sign-in.** The global "how should they work" question is gone. Claude Code: subscription, an API
  key (read from your environment, never stored) or Vertex AI. OpenClaw: pick the providers from its own catalog and, per
  provider, an API key (saved by OpenClaw's own store) or a CLI backend. Antigravity: your Google account. A saved answer is
  migrated once, fill-only, and shown old -> new. The gateway (LiteLLM, OpenRouter) is no longer offered; an installed one is
  left alone. `--backend` is deprecated and pre-fills the subscription sign-in.
- **One role renderer for every cockpit** (`setup/role_render.py`): Claude subagents are byte-identical to before; OpenClaw gets
  one `kit-role-<role>` agent per role with the profile's model limited to the enabled providers, `thinkingDefault` and a scoped
  skills list; agy gets a lean role list in `GEMINI.md`.
- **Effort is applied**: `role_effort` is complete in `balanced` and `performance`; Claude subagents carry `effort:` (an
  operator edit is kept) and OpenClaw agents the mapped `thinkingDefault`.
- **Skill scoping for every cockpit** (the Claude Code include list, `AI_RESOURCES_SKILLS` to widen): `~/.agents/skills` and
  OpenClaw (`skills.load.extraDirs` with a managed directory). `doctor` warns when a vendor pin is behind upstream.
- **OpenClaw**: `maxSpawnDepth` and `runTimeoutSeconds` beside the existing subagent limits (fill-only, hot); a confirmed
  migration for `kit-lane-agy-*` agents billed on the Anthropic API (repoint at the Antigravity catalog or remove, one agent at a
  time, never silent); the waiting rules in every cockpit's instruction file.
- **Native team orchestrator**: `ai-resources orchestrate ...` (a resumable state machine) and the OpenClaw plugin commands
  `/equipo` and `/workflow run|status|approve|cancel|resume|cost`. Plan approval before the first writer, per-role timeouts,
  `maxTeams` with a queue, a worktree and branch per team, `Blocked`/`Return_to_step` at most 3 times, verifier independence,
  no spend cap. A new plugin version is recorded as ONE pending reload for `apply-pending`; setup never restarts the gateway.
- **Audit**: spend by origin, provider and role, an effort validation table and `orchestrate cost`. Older snapshots compare as
  `n/a`. See [docs/validating-cost.md](docs/validating-cost.md) and [docs/cockpit-recommendations.md](docs/cockpit-recommendations.md).

### Changed

- `tests/test_openclaw_gateway_guard_words.py` timing bound 3 s -> 10 s (a slow host, not a regression).
- The wizard step ids: `cockpit_auth` replaces `mode`; `gateway`, `providers`, `credentials` and `lifecycle` are no longer steps.


### Fixed

- **Ticking Antigravity now writes its files.** The wizard ticked `agy` but the apply step only knew the module that
  writes `~/.gemini/GEMINI.md`, the MCP file and the skills, so agy got none of the kit content. Its settings no longer
  force `selectedAuthType: gemini-api-key`, since agy signs in with a Google account.
- **A second `ai-resources setup` is a zero diff again.** The kit-role agents get their kit block on the first run, the
  host profile's `*` model rule no longer moves them off their profile model, and the kit's own block in a role's
  `AGENTS.md` is not mistaken for an operator edit.

## [2.4.0] — 2026-10-22 — token-cost lean defaults and round 2: retuned balanced, billing awareness, hygiene and lean OpenClaw (ADR-0013, ADR-0014)

### Added (token-cost round 2, ADR-0014)

- **`balanced` is retuned** (no new profile): Haiku for explore, tester, doc-writer and package-upgrade, Sonnet for the planner,
  architect and writers, Opus for the verifier and security auditor only. A saved `balanced` shows the old -> new table once.
  `validate_kit.py` fails a Haiku role whose estimated static prompt reaches the 100k Haiku price tier; `audit` reports the share
  of Haiku calls above 100k and the global CLAUDE.md weight (estimate).
- **Billing awareness.** `doctor`, `audit` (`--json`, `--billing-only`) and the daily Slack section show the auth mode, the 5 h and
  weekly quota and the credit left from a manual `~/.config/ai-resources/credits.json`; alerts for prepaid billing, an
  exhausted quota, an expiring promo and a low balance.
- **Polling guard.** `hooks/kit_sleep_guard.py` refuses a Bash `sleep` over 30 s; `audit` and `doctor` alert when poll turns
  exceed 5% of the cost.
- **Hygiene.** Setup offers (default No, one by one) to disable user-level MCP servers and plugins with no call in 30 days, and to
  move heavy blocks of the global CLAUDE.md to a skill or a rule; both are backed up, recorded and restored with
  `setup --restore-hygiene` and `--restore-claude-md`. Persona subagents are rendered only for active domains.
- **OpenClaw.** Confirmed workboard and heartbeat migrations (restart-class keys wait for `apply-pending`), an opt-in
  utility-model probe, a guarded Haiku route for Slack notification channels, and a doctor note that completion announcements
  cannot be silenced.
- **agy.** Setup can add `engram` and servers you pick to the agy MCP file (fill-only, backup, `--restore-agy-mcp`); Flash-low in a
  fallback chain needs a valid Google acknowledgement and proven gates.

### Added (token-cost lean defaults, ADR-0013)

- **`ai-resources audit snapshot <label>` and `audit compare <a> <b>`.** Cost per active hour, per session and per call,
  first-call and p50/p90 context, share of calls above 200k, poll-turn cost share and cache hit ratio, by origin and agent,
  plus the config inventory of the keys this release changes. The operator's `bench.py` `before-*.json` files are readable.
  `--probe` is opt-in and prints its cost first. Figures are API-equivalent estimates.
- **`audit --all-projects`, `--subagents`, `--openclaw`.** Subagent transcripts, every project, and OpenClaw native-runtime usage
  per agent and model (read-only; zstd rows are decoded when a decoder exists, otherwise counted as "undecoded rows").
- **Prices** for Opus, Sonnet and Haiku 5.5 (Haiku with a >100k prompt tier), Fable 5.1 and Gemini 4 Argon, with date-bounded rows
  for the Gemini price changes; source and date cited in `audit.py`.
- **Claude Code.** `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` (fill-only); only the core skills are linked natively
  (`AI_RESOURCES_SKILLS=all` restores every one); `workflow-*` skills are not listed to the model; role subagents preload their
  skills; the Engram plugin's first-message ToolSearch directive is trimmed (hash-guarded, backed up, re-applied by setup).
- **OpenClaw host profile.** Idle session reset, `cache-ttl` context pruning, Anthropic server-side compaction at 150k, lower
  thinking defaults, capped subagent fan-out, an explicit utility model and an isolated, light main heartbeat. All keys are hot.
- **Confirmed cost migrations** for values a host already sets (thinking level, Opus fallbacks behind a Sonnet primary): per path,
  recorded by id, nothing applied unattended.
- **A narrow Slack writer** (`threadParticipation=false`, `allowBots="mentions"` on channels you pick), confirmed per path;
  `assert_channels_safe` is unchanged.
- **MCP mirror is opt-in for new installs** (`OPENCLAW_MCP_SERVERS` in `kit-host.env`).
- A drift report for `kit-lane-agy-*` agents running a Claude model on the API, a printed workboard and agy MCP hint, and a
  polling guidance block in the kit's AGENTS.md and CLAUDE.md blocks.
- The OpenClaw plugin tests (`npm test`) now run in CI.

### Changed

- The audit counts each streamed message once (the last record), so earlier totals were over-counted.
- The `claude-kit` backend is unchanged: `--disable-slash-commands` would strip `/kit-plan`, `/kit-implement` and all skills from OpenClaw-spawned Claude workers, so the saving comes from skill scoping. `--strict-mcp-config` stays forbidden.
- Automatic pin bumps to the 5.5 models (sonnet 5 -> 5.5 equal price, opus 5 -> 5.5 lower price) are accepted.
- Role-preloaded skills are linked (hidden from the main listing); `audit --openclaw` with no data exits 0; `audit snapshot` records its counting method and `compare` warns on a mismatch with a `bench.py` baseline (re-take it with `--since/--until`).
- In MCP opt-in mode, mirrored servers that are not listed are denied per agent by name (never the global `tools.deny`).
- agy fallback chains use `gemini-3.8-flash-low` as their routine safety net.
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is no longer written, and the kit-written value is removed once.

## [2.3.0] — 2026-10-21 — OpenClaw native cockpit and lifecycle orchestrator plugin (ADR-0012)

### Added (OpenClaw Cockpit & Lifecycle Orchestrator)

- **ADR-0012: OpenClaw Native Cockpit & Lifecycle Orchestrator.** Architecture formalizing OpenClaw as the primary cockpit and event dispatcher, while keeping the Python engine as the offline setup and auditing authority.
- **Cockpit `/lanes` command in `ai-resources` plugin.** Displays subscription lanes status, mode (`single-model` vs `multi-model`), resolved role mappings across Claude Code and Gemini (Antigravity), and active fallback chains directly inside chat (Telegram/Discord/Web UI).
- **Cockpit `/workflow` command family.** Native discovery and execution of declarative workflows:
  - `/workflow list`: Formatted table of all 19 kit workflows and pipeline stages.
  - `/workflow info <id>`: Step-by-step breakdown of criteria, roles, and transitions.
  - `/workflow run <id> [goal]`: Dispatches workflows to worker subagents safely without blocking gateway event loop.
- **Enhanced `/equipo` command.** Elevated to full lifecycle team dispatcher enforcing the sequential role protocol (`planner` ➔ `implementer` ➔ `verifier`) with an immutable non-self-approval barrier on the implementer.
- **Expanded plugin manifest schema.** Adds `kitRoot`, `lanesConfigPath`, and `enableOrchestrator` to `configSchema`.
- **Automated setup plugin configuration.** `ai-resources setup` automatically writes `kitRoot`, `lanesConfigPath`, and `enableOrchestrator=True` into `plugins.entries["ai-resources"].config` in `openclaw.json`.

### Fixed

- **Skip Telegram prompts when Telegram is disabled in setup.** When the operator disables or unticks Telegram in the
  channels step, the OpenClaw host section no longer prompts for team narration into Telegram topics, group chat IDs,
  or the operator Telegram user ID.
- **Exclude `kit-lane-*` agents from Telegram group binding prompts.** Internal lane agents created for subscription
  routing are no longer treated as human-routed agents requiring Telegram group chat IDs.

## [2.2.0] — 2026-10-20 — subscription lanes, multi-provider fallbacks, OpenClaw concurrency and guided setup

### Added (OpenClaw concurrency and multi-provider fallbacks)

- **Configurable `serializeAgy` in `ai-resources` OpenClaw plugin.** `buildAgyBackend` and `buildAgyLaneBackend` default
  `serialize: false`, enabling concurrent per-channel/per-run execution in OpenClaw instead of locking all channels into a
  single global FIFO queue (`agy-cli`). Manifest declares `serializeAgy: { type: "boolean" }` under `configSchema`.
- **Cross-provider 3-layer fallbacks.** Automatic fallback routing between Claude Code and Gemini (via Antigravity agy)
  when quotas are exhausted or a provider backend fails.
- **Interactive channel credential auth in setup.** Enabling channel accounts in `ai-resources setup` interactively
  prompts for tokens/credentials when required by the underlying channel runner.


- **One image question, no recommendation.** With OpenClaw as a tool, phase 2 asks about image generation: `keep` (default,
  writes nothing), `disable`, or any documented route, each with a billing label and a proof label. Choosing a route writes
  only `agents.defaults.mediaModels.image` after a dry run, and offers (default No) to sign in through
  `openclaw models auth ...` on your own terminal; the kit never holds an image key.
- **`disable` is a scoped, reversible `tools.deny` entry.** Setup merges the single token `image_generate` into the global
  `tools.deny` without touching your entries (full list, `--replace-path`, dry run, re-read before the write, hot only, never
  deferred), tracks it in state and removes only that token on teardown. A new shape guard, `assert_tools_deny_safe`, sits on
  the single writer next to `assert_channels_safe`. Effect on running sessions is NOT PROVEN. ADR-0011 is Accepted (2026-10-19,
  implemented on main, not released) after the post-commit architect, security-auditor and verifier reviews the operator
  waived up front.
- New state fields `image_*` (optional; saved states load unchanged), Review rows, `--dry-run` lines, a `verify` line and the
  runbook section "Image generation".

### Fixed (low-severity advisories from the ADR-0010 re-review)

- Evidence-record hook: `git am` is read as a patch command; cp, mv, rsync, ln, install, tar/unzip extraction, `sed -i`, tee
  and `dd of=` that land in `scripts/ai_resources/data/` are denied without the file name; `git commit -a`, `git add
  -A/-u/.`, `git reset --hard`, `git stash pop/apply`, `git pull` and `git merge` are denied only while the record is
  modified or untracked. The control stays operator review.
- Channel disable: a capped output re-reads the channel list before reporting failed or turned off. `redact()` also masks
  Cookie/Set-Cookie values, URL credentials, bare `sk-` keys and `auth=`, and no longer backtracks on a huge single word. The
  capped runner closes the child's stdout pipe when it abandons the reader.

### Changed (setup in five phases, OpenClaw questions by phase, channel intent; ADR-0010, proposed)

- **Five phases.** Step headers read `[n/5]`: Tools, Cockpit, Team, Audio, More. The ten internal steps, their ids and
  the saved state are unchanged; a saved v1.0 state dry-runs to the same outputs. Cockpit and Audio ask nothing
  unless OpenClaw is one of your tools (one line says why).
- **Main tool before the team.** A new step names the main tool among the ticked, installed ones (Claude Code,
  OpenClaw) and, for OpenClaw, asks the engine and which channel accounts stay on before the team is picked. The
  model, MCP servers, voice notes (Audio) and the host section (More) are asked in their own steps; More options >
  OpenClaw still asks everything in one go. Nothing is written before Apply.
- **Channel accounts, disable only.** Unticking a listed channel account (default: everything that is on now; nothing is
  remembered between runs) makes Apply run `openclaw channels remove --channel C --account A` after a per-change confirm
  (default No; Telegram asks twice), only on a terminal, never under `--dry-run` or `--yes`, with the OpenClaw cockpit,
  with reload rules that call `channels.<C>` hot, and after a fresh listing still shows the account on. It re-lists
  afterwards and reports any difference; the undo says to re-add the account with credentials, yourself. An enable is
  only printed. The kit still never patches `channels.*` (`assert_channels_safe` is unchanged).
- **Sign-in for installed tools.** Ticked, already installed tools with a login command (for example `codex login`)
  are listed unticked in the sign-in step. The kit never starts `agy`: its sign-in command is only named.
- **Start at login** keeps a saved answer without asking; More options asks it again.
- New module `openclaw_channels` (inventory, a pure disable planner and the guarded executor) and a shrink-only
  checker-independence test over every shipped profile. `KNOWN_VIOLATIONS` now lists only `claude-inherit` and
  `all-gemini` (open question Q-1).

### Fixed (checkers and writers never share a model family; ADR-0010)

- In `claude-native`, `balanced-claude-55`, `openrouter-balanced`, `measured-best`, `quality-first`, `all-claude`,
  `cost-optimized` and `openrouter-cheap` the verifier and the security-auditor now run on Opus with no fallback onto a
  writer's family; cross-family default fallbacks are gone and `performance-claude-55` lost its Sonnet fallbacks on Opus
  roles. Saved setups that load those profiles get the new checkers on their next run.

### Changed (starting-team names, Balanced with Gemini, per-row Google routing; ADR-0010)

- **One set of names.** The starting teams are `balanced`, `performance` and `low-cost` on every row (`gateway/...` on
  a gateway). `subscription-claude-55`, `subscription-performance`, `subscription-economy`, `performance-claude-55` and
  `economy-claude-55` are aliases that load the same team forever; `claude-native`, `balanced-claude-55` and
  `openrouter-balanced` keep their own file and only open the question on Balanced. A saved old name is saved under the
  new one on the next real run, with one line saying so. `--profile` and the `low-pricing` key still work.
- **Balanced is multi-provider.** The CLI Balanced names Gemini over Antigravity on explore and on the second opinions
  of the code-reviewer, planner and software-architect (held until the lane gates are proven; planner and architect
  seconds are display-only). `gateway/balanced` runs Gemini Flash on explore, tester and doc-writer; Review asks once
  (default No) whether to use Gemini through OpenRouter. No preset lists an agy candidate on a writer role. A role list
  that adds the planner and architect re-signs the Google acknowledgement (text unchanged).
- **Team table with real providers and lanes.** One set of lines says what is held, on or not active per row (Antigravity
  not selected, Each on its own, LiteLLM legacy). A new user's Antigravity tick starts off and a saved tick is restored
  only with a valid stored Google acknowledgement. On the legacy LiteLLM row a `google/*` role without a Google provider
  runs on its Claude fallback.

### Added (Antigravity gate evidence record; ADR-0010, no gate is flipped)

- `ai-resources lanes gates` (read-only status) and `ai-resources lanes gates record --run-dir <window>` (terminal
  only, typed attestation per stage, never runs `agy` or `openclaw`, never commits). The record
  `scripts/ai_resources/data/agy_gate_evidence.json` is validated by a lazy, strict, fail-closed loader (seal, manifest
  listing, G0 anchor, G1 slug coverage, G7 text hash; tracked and equal to HEAD in a git kit root) and lifts the gates
  and the OpenRouter Google cell only when valid. No record is shipped; the gate constants are untouched. A PreToolUse hook
  (`agy_evidence_guard.py`) denies agent writes to the record, the file is forbidden to lane children and watched as a
  tripwire, and `doctor` gains an Antigravity lane check.

### Changed (LiteLLM leaves the new-setup gateway choice; ADR-0010)

- **Advanced offers OpenRouter only.** Readiness of Balanced with Gemini on LiteLLM was scored 4/10, so a fresh setup
  no longer sees LiteLLM and is not asked "Which gateway?". A saved LiteLLM setup keeps loading and running, is offered
  as `LiteLLM (saved; no longer offered for new setups)`, and prints one notice line; `--backend litellm` is still
  accepted with the same notice. The state schema, LiteLLM rendering, service and daemon are unchanged
  ([ADR-0010](docs/decisions/0010-setup-five-phases-channels-team.md), amending ADR-0008).

### Changed (setup asks three things: tools, how, team; ADR-0009)

- **Every coding CLI is a tool, and the default path is four screens.** `ai-resources setup` now asks *Your tools*,
  *How should they work?*, *Your team* and *Review* (ten internal steps and the progress rail stay; the headers count
  screens). The six words it uses are tool, role, team, starting team, subscription and gateway; the glossary is at the
  top of `docs/multi-model.md`. State keys, flags and files keep their names; an old state file keeps its meaning and
  the schema does not change ([ADR-0009](docs/decisions/0009-setup-tools-how-team.md), amending ADR-0008 decision 6
  in display only, its preset addendum, and ADR-0007 section 11 on where the Google terms check can open).
- **Your tools** is one list for every tool, one order (installed first, then alphabetical), and it also holds the
  tools the kit should use: installed tools are tickable, a missing one is ticked to install, unticking never
  uninstalls. Claude Code carries a "recommended to start" label in the same list, no special position or tick. The
  second "Configure detected cockpits" checkbox of step 7 is gone; step 7 only asks the OpenClaw questions, and only on
  the first configure. Gemini CLI (retired) is no longer listed; an old state entry still loads.
- **How should they work?** replaces the four-way mode question with *As a team*, *Each on its own* and *Advanced*
  (which asks LiteLLM or OpenRouter). Each of the four old answers writes the same state as before. `--backend` now
  skips the question, as the README and the guide always said.
- **Your team** merges the preset question, the role table and "Use this?": choosing a starting team (Balanced,
  Performance, **Low cost**, Custom; "Low pricing" was renamed, same profiles) is the confirmation. As a team it also
  shows who does what (Claude Code, Antigravity when ticked, Codex "no roles yet"); writing code stays with Claude
  Code. A re-run no longer drops a saved customized or composed team, the lanes or the acknowledgements when the
  profile is unchanged (the single-model profile step used to rebuild the profile).
- **Silent skips.** The gateway steps (3, 4, 5, 8) print nothing on a path with no gateway; gateway paths still say
  why a step was skipped.
- **Review** offers Apply, Change tools, Change how, Change team, More options and Cancel (replacing "Edit a
  section..." and "Change models"). The Claude parallel runs (lanes, off by default) and their terms check, Gateway and
  API keys, Start at login and the OpenClaw questions are entries of *More options*; OpenClaw's questions are asked on
  the first configure only and a saved configuration re-applies silently. `--lanes`, `--no-lanes`, `--lanes-dry-run`
  and `--lanes-orchestrator` are unchanged.
- **Antigravity is a tool like any other, and ticking it is the opt-in.** It is never ticked on its own, even when
  installed (its row says it sends repo content to Google and a terms check comes next). Ticking it opens the Google
  terms check (`ACK_TEXT['google']` verbatim) after *Your team*; accept stores the same acknowledgement as
  `--lanes-antigravity`, decline, an empty account, Ctrl-B and Ctrl-C untick it and store nothing, and unticking a tool
  that was on closes the gate. The gates G0-G7 are still not proven, so nothing is admitted; `agy` never writes, the
  implementer stays on Claude Code, and the Codex row is never a choice. The check is also a plain function
  (`wizard.agy_terms_check`) shared with the flag. Unattended runs, `--yes` and the flags behave as before.
- Neutral wording on the default path (no cockpit, lane, preset, backend or engine), the banner and the completion
  "Try" lines name every configured tool, and `--yes`/`--backend` are documented in `setup --help` (`--yes` installs
  only the missing ones of Claude Code, Antigravity and OpenClaw).

### Added

- **Setup shows where Gemini (Antigravity) takes part.** The Custom "pick models from my accounts" list never showed
  Gemini because Antigravity (`agy`) is a subscription CLI, not an API account. With `agy` installed it now prints an
  "Antigravity (agy, subscription)" group of informational rows labelled `lane, opt-in` and `unconfirmed slug` (no key
  is asked, nothing there is an API provider or a selection slot), and in CLI-subscriptions mode each preset role table is
  followed by an "Antigravity lane - off until you enable it in step 6c" table. Without `agy` one line says why Gemini is
  absent. `subscription-performance` and `subscription-economy` add a pinned Gemini second opinion on the code-reviewer.
  The lane stays opt-in, off by default and closed behind gates G0-G7; no write goes to agy. Picking an Antigravity model
  in Custom is not supported: use step 6c.

- **Setup mode: "multi-model via your CLI subscriptions".** The mode question (step 2) now has four options: single-
  model, multi-model via your CLI subscriptions, via OpenRouter, via LiteLLM. The new one spreads roles across the
  subscription CLIs (Claude Code, Antigravity, Codex when allowed) with no gateway, nothing billed per token, and no
  gateway credential replacing the claude.ai login, so the gateway warning is not shown for it. Gateway, provider and
  credential steps are skipped (no key is asked), step 6 starts from `subscription-claude-55`, and the lanes step
  (6c) works as before (Antigravity stays opt-in and gated, Codex stays held). Representation: `mode` stays
  `single-model` and an additive `cli_team` flag in `setup-state.yaml` (absent while false) names the team, so an old
  state file loads unchanged and no gateway code path can engage. `setup --backend cli` selects it non-interactively.
  Switching from a gateway mode offers the same cleanup as multi-model to single-model; switching between CLI mode and
  single-model touches no gateway artifact. The review shows `Mode: multi-model (CLI subscriptions)`.

- **Setup step 6 starts with one preset question.** It offers Balanced (recommended), Performance, Low pricing or
  Custom and never asks for a provider, vendor or model. A preset resolves to a kit profile by mode and backend (CLI
  subscriptions, single-model, LiteLLM, OpenRouter; the table is in `docs/multi-model.md`), shows only the role table
  and asks `Use this?`; No returns to the question. Custom is the earlier flow (profile list, models from your
  accounts, per-role customization, compose). Four new curated profiles back the presets: `subscription-performance`
  and `subscription-economy` (Claude aliases, no gateway, checker independence by family kept) and
  `performance-claude-55` and `economy-claude-55` (Claude only, valid in every compatibility cell without
  `--allow-unverified`). `--profile`, `--models`, `--shape`, `--providers`, `--compose` and `--non-interactive` skip
  the question; a saved choice is the default and is never replaced. The state file format is unchanged.

### Fixed

- **`setup` said OpenClaw was not installed when it was.** Detection looked only at the process PATH (and only `agy`
  also checked `~/.local/bin` and fnm), so a run from a service unit or a minimal PATH missed Homebrew, npm-global,
  pnpm, bun, cargo and fnm bins. Every detector (claude, openclaw, cursor, codex, aider, copilot/`gh`, continue,
  opencode, agy) now uses one lookup, probes `--version` by absolute path with the binary's directory on PATH (an npm
  shebang `env node` works), and a binary that exists but fails or times out is reported "installed, version unknown"
  instead of "not installed". OpenClaw's reload-rules package lookup uses the same resolution.
- **Gemini CLI is no longer installable.** Google retired it for personal accounts on 2026-06-18 (replaced by the
  Antigravity CLI, `agy`). It left the tool catalog and every install offer; an existing copy is only an
  informational row ("retired for personal accounts; use agy").
- **Installers audited against the vendor's current docs (2026-10-16, ADR-0008 item 14).** OpenClaw now installs with
  the vendor's installer script (`npm i -g openclaw` needs `--allow-scripts` on newer npm); Aider uses the vendor's
  one-liner because `pipx install aider-chat` fails on Python 3.13+; the Windsurf docs moved to Devin Desktop; not-
  installed hints no longer point at the archived `gh-copilot` extension or the 404 Continue page.

### Changed

- **Setup step 1 shows one list instead of a scan followed by a selector.** Installed tools appear with their version
  and cannot be ticked, the ones the kit can install are tickable (each shows its exact command), and Windsurf is an
  informational `manual install: <url>` row. Enter with nothing ticked skips and installs nothing (the prompt says so).
  The plain scan list is still printed where no selector is shown (`--yes`, `--non-interactive`, no terminal, dry run
  without a terminal). Sign-in left the selector: a separate pre-ticked `Sign in now?` screen follows the install and
  covers only the tools installed in that run; already installed tools get one dim line with the command to run later.
  The `Sign in to installed tools` rows are gone.
- **Sign-in follow-up hardening.** The Cursor login never resolves a bare `agent` through PATH (the sibling of the
  detected `cursor-agent` is used, or the command is printed); a bare `claude`/`agy` is recorded as "started sign-in"
  (exit 0 does not prove a login); the legacy `gh copilot` extension is shown distinctly and never runs bare
  `copilot`; the dry-run preview and the sign-in answers are asked once per run; the `agy` doc URL is the vendor's
  human-readable page.

- **Setup step 1 now offers every AI CLI the scan detects, not only three.** The selector listed only Claude Code,
  Antigravity CLI and OpenClaw, so on a machine that had those the operator saw the cockpit scan with red crosses
  (Cursor, Gemini CLI, Codex CLI, Aider, GitHub Copilot CLI, Continue.dev CLI, OpenCode, Windsurf) and nothing to
  select. Each of those with an official installer is now a selectable row (the exact command is shown before it
  runs; nothing is pre-ticked); Windsurf, a desktop IDE with no CLI installer, is listed as `manual install` with the
  vendor URL and never run. Package-manager installs are used where the vendor documents them (npm, pipx); Claude
  Code, Antigravity and Cursor keep the vendor's piped installer. Every command and its sign-in is recorded with the
  vendor doc URL and the date it was checked (`docs/multi-model.md`, "Tools the setup can install"). The offer is now
  asked before the "no cockpits detected" check, so a machine with no AI CLI at all can install one in step 1.
- **`setup --dry-run` on a terminal previews the selector.** It shows the real checkbox and prints, for what you
  tick, the exact command it would run; nothing is installed or signed in. With `--yes`, `--non-interactive` or
  without a terminal it still only says what it would offer.
- **Setup step 1 lets you choose which tools to install.** The single `Install missing tools now?` yes/no is replaced
  by a checkbox over every tool the kit knows (Claude Code, Antigravity CLI, OpenClaw) with what each is for.
  Installed tools are shown as installed and cannot be reinstalled, nothing is pre-ticked (an installer pipes
  a remote script into a shell, so Enter installs nothing), and selecting none is valid. `--yes` still installs the missing required set without asking; `--dry-run`,
  `--non-interactive` and a run without a terminal behave as before. Consent is unchanged: pressing Enter
  installs nothing; you must tick what you want.

### Added

- `detection.detect_cursor_agent()` (Cursor's terminal agent); `detect_cursor()` also counts it, and
  `detect_copilot()` also recognises the standalone `copilot` binary (npm `@github/copilot`), not only the older
  `gh copilot` extension. `ToolSpec` gains `doc_url`, `doc_checked` and `manual_url`.
- **Optional sign-in after an install.** `ToolSpec` gains `purpose` and `login_cmd` (both optional). A newly
  installed tool with a sign-in (`claude`, `agy`) is offered `Sign in now?`; the tool's own login runs only on a
  terminal, with inherited stdio (nothing captured, no credential handled by the kit), and is recorded as an action.
  Without a terminal, or under `--yes`, the command is printed instead. An already installed tool is not offered a
  sign-in; its command is printed as a dim line.

### Added (Subscription lanes, guided setup wizard and hardening)

- **Codex lane backend, held (ADR-0007 FU-2).** `codex-cli` is a registered lane backend (pool `codex-subscription`,
  profile candidate `codex:<model>`) for read-only report roles only. It is HELD like Antigravity:
  `CODEX_LANE_ADR_ACCEPTED = False`, absent from `WRITABLE_BACKENDS`, never written to `openclaw.json`, refused at
  `lanes prepare`. A host without a `codex` binary reports `not installed`. Not proven against a live OpenClaw.
- **Measured headroom for lane placement (FU-5).** A fresh reading from local state (status-line sidecar, kit counters
  file) scales a pool's weight and separates otherwise equal candidates across pools; absent or stale readings use
  the static weights. No network, no paid API.
- **`doctor` MCP child check (FU-1, kit side).** With lanes enabled, `ai-resources doctor` counts supabase/engram
  children under the gateway unit and warns above 24 with the restart remedy. Read-only; kills nothing. The effect of
  `tools.deny: ["bundle-mcp"]` on live children is not proven.
- **Per-role evidence and effort (FU-3, FU-4).** The composition JSON and `composed` provenance carry
  `evidence: {tag, label, confidence, effort}` per role; a profile `evidence:` / `role_effort:` block is copied into
  `lanes.yaml resolved.roles.<role>`. Read-only metadata: it never changes routing.
- **`ai-resources setup --help` describes the guided flow**: the ten steps, Ctrl-B/Ctrl-C/Ctrl-D, the review's
  "Edit a section...", `--non-interactive`, `--dry-run`, the lanes flags and `NO_COLOR`.
- **Antigravity lane (ADR-0007 section 11, phase 3; disabled on a fresh install and closed today).** `agy` becomes a
  peer lane backend (`agy-cli-lane`) opened only by `--lanes-antigravity` (`--no-lanes-antigravity` closes it), a
  signed Google-account acknowledgement that lists the role (one per account, the account named, per-account role
  sets, the implementer unticked by default), ADR-0007 section 11 Accepted and every phase 3 gate G0-G7 proven
  (`agy_lane_gates`, enforced in setup admission, the OpenClaw writer and `lanes prepare --lane`; each fails closed
  with a message). Operator-owned ToS risk. V5 (slugs), V6 (sandbox), V7 (tool surface), V9 (agy workspace) and V12
  (agy home) are NOT PROVEN, so no agy agent is written. Also: the `agy-cli-lane` plugin backend (registered only
  with an absolute `agyBin`; `agy-cli` unchanged), the writer's catalog stub, tool denials, revocation prune and
  kit-owned agy home path, `lanes.pins`, profile agy candidates, `lanes verify --spawn-error` and `lanes prepare
  --expect-head`, and the all-lane `feature-lanes` example workflow (the explicit `openclaw-agent` sample stays in
  `feature-cross-provider`).

### Fixed

- **Masking leftovers.** `my password is Tr0ub4dor&3xyz` style prose and a value alone on the line after an empty
  `KEY=` / `KEY:` are masked; words, paths, model ids, references and values under 8 characters are kept.

### Added (earlier in this release)

- **Wizard hardening (ADR-0008, 9-11).** A re-run starts every model question from the saved selection and
  pre-checks the saved providers and cockpits; echoed subprocess text drops terminal escapes and markup and is masked
  (unquoted passphrases, `Authorization:` headers, `-u user:pass`, YAML block scalars, PEM keys, padded base64); the
  dry-run diff masks multi-line secrets; a tool installer runs under a spinner; a Ctrl-C inside a cockpit configurator
  is listed as `may be partially written: <cockpit>`; cancelling at the review prints the shared cancel block; a
  golden test pins the whole non-interactive output.
- **Guided setup wizard (ADR-0008).** `ai-resources setup` prints a step header with a progress rail and one line
  of key hints (`Enter confirm`, `Ctrl-B back`, `Ctrl-C cancel`) at each step; the rail wraps between 60 and 79
  columns and collapses to `Step n/10  title` below 60. Under `NO_COLOR` or a non-UTF-8 stream it uses ASCII glyphs
  and no colour (bold stays). `--non-interactive` and a run without a terminal keep the plain header.
- **Ctrl-B Back** in the steps that have made no change on this machine (models, role assignment, lanes, cockpit
  selection). A step after one that installed or saved something refuses it with a notice.
- **Review with "Edit a section...".** Step 9 shows one row per section (credentials only as `set` or `missing`) and
  can re-open an earlier, rewindable step, then returns to the review. Apply, Change models and Cancel are unchanged.
- **Inline validation** of the port (1-65535), the remote URL (http or https), a typed model id and the keys that
  cannot be left empty.
- **Failure log tail.** A failing installer prints its command, the exit code and the last 20 lines with secrets
  masked, after the spinner closes.
- **Masked dry-run diff.** `setup --dry-run` ends with a unified diff per renderable target (`executors.yaml`,
  `litellm.yaml`, `docker-compose.yaml`, `lanes.yaml`, the Claude settings env), `new file` when a target is
  missing, then `DRY RUN: nothing was written`.

- **Resolved lane per role in `lanes.yaml` (ADR-0007 §1).** The file carries a `resolved:` section computed at setup
  and at the `models update` re-render: per role the lane, model, reason and ordered fallbacks, plus `second` and
  `blocked`. The digest now covers every key. SKILL.md tells the orchestrator to dispatch `resolved:`.
- **`lanes prepare --lane` is mandatory whenever `lanes.yaml` exists.** It must be the resolved lane or a recorded
  fallback and the choice is written to the ledger; a hand-edited table fails its digest. Existing phase-2 installs
  and workflows that call `lanes prepare` must pass `--lane <claude-code|kit-lane-id>`; without `lanes.yaml` it is
  optional and nothing changes.
- **Agy lane candidates are held** while the Antigravity lane is not accepted (`AGY_LANE_ADR_ACCEPTED`): they are
  dropped with a visible reason, and the OpenClaw writer only writes `WRITABLE_BACKENDS` (`claude-cli`).
  `agy-cli-lane/<slug>` refs now map to a model family and pool.
- **Hardening of the lanes commands.** A checker role never takes a `second` member; `lanes verify` needs a full
  `--subject` sha; the lanes state directory refuses a tmpfs root (`/tmp`, `/dev/shm`, `/var/tmp`); an explicit exec
  deny on an orchestrator records `verify_capability: none`; a second integrating writer role in one run is refused;
  a cleanup failure after `lanes land` is reported and recorded.

- **MCP scoping for `kit-lane-*` agents (ADR-0007 OQ-5).** Lane agents are written with `tools.deny: ["bundle-mcp"]`
  by default, so a claude-cli lane child is expected to stop loading every server in OpenClaw's `mcp.servers` (supabase/engram
  process accumulation on the gateway; the effect is proven only by the isolated merge gate). In opt-in mode
  `bundle-mcp` is not denied, so bundle-provided servers outside `mcp.servers` stay loaded (known limit). A profile `lanes.mcp: {role: [server, ...]}` opts extra servers in per role.
  Recorded in `lanes_applied.mcp_deny_added`, removed by teardown, warned on by `verify`, shown in `explain`.

- **Subscription lanes (ADR-0007), phase 2.** A separate lanes pass after the model choice places a role on Claude
  Code or on a `kit-lane-*` OpenClaw agent on the Claude CLI backend. Disabled on a fresh install; enabled with
  `ai-resources setup --lanes` (`--no-lanes`, `--lanes-dry-run`, `--lanes-orchestrator ID`), which shows the
  per-account acknowledgement once, writes the rendered `~/.config/ai-resources/lanes.yaml` (tracked, backed up,
  restored on a failed later artifact; `executors.yaml` unchanged) and the OpenClaw lane agents, catalog entries and
  scoped `allowAgents`. Teardown removes only recorded kit ids and contained `kit-lane-*` workspaces and restores
  the config document. `explain` gains a Lane column and a `lanes` JSON key only when a table exists.
  `status`, `verify` and `models update` warn on lane model drift.
- **Kit-only landing path and ledger.** `ai-resources lanes prepare | verify | integrate | land | count` enforce the
  report, scope and integrating contracts with a hash-chained run ledger under `$XDG_STATE_HOME`, `--expect` heads,
  checker independence by model family, a commit-attribution check and `--ff-only` landing. `backend: lane` workflow
  steps, `integrating_writer`, `write_scope`, `integrates` and `lane_member: second` are validated by
  `validate_kit.py`. `skills/kit-orchestration` gains "Lane steps" (precedence table, flow, handoff join, mixed-group
  rule, concurrency cap).
- **`subscription-claude-55` profile (single-model).** A curated reference profile for a Claude subscription without a
  gateway: Claude aliases (`haiku`/`sonnet`/`opus`), Sonnet for writers, tests, review and the planner, Opus for the
  architect, security auditor and verifier. Checker families never intersect writer families and the verifier differs
  from the implementer (profile test). Documents alias drift and the CyberBench CVP caveat. `balanced-claude-55`
  remains the recommended profile and is pinned unchanged by a sha256 test.
- **Cross-provider role dispatch (fields and validator).** A workflow step may carry `backend: claude-code |
  openclaw-agent` (default `claude-code`, today's behaviour) and, with `openclaw-agent`, an `agentId`. The validator
  rejects an unknown backend, a missing or malformed `agentId` (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`) and every
  write-capable role on `openclaw-agent` (the implementer stays on `claude-code`: one writer per run); `tester` and
  `doc-writer` pass with a warning and are never parallel members. The generated workflow skills gain a Backend
  column. No `model` field and no change to `kit-plan.js` / `kit-implement.js`. Contract: `workflows/WORKFLOW_CONTRACT.md`.
- **Cross-provider role dispatch (procedure).** `skills/kit-orchestration` gains "Choosing a backend": the
  availability rule (`sessions_spawn` in the orchestrator's own tool list, otherwise a fixed `Blocked` text and no
  fallback), the child prompt contract, T39 polling (calls of at most 60 s, 20 minutes per attempt), the `.done`
  sentinel, the four-point post-step check that replaces the Claude Code hooks, at most 3 attempts with the
  `<step-id>.attempt-<n>.md` rename, the evidence to record and an illustrative mixed team. The `sessions_spawn`
  contract itself is not verified on a live host yet.
- **Example mixed-provider workflow.** `workflows/feature-cross-provider.workflow.yaml` (and its generated
  `workflow-feature-cross-provider` skill) runs research, plan and review on other OpenClaw agents (placeholder ids
  `agy-flash` and `agy-pro`) and keeps design, implementation, tests, security, verification and documentation on
  Claude Code.
- **ADR-0005 and doc reconciliation.** `docs/decisions/0005-cross-provider-role-dispatch.md` records the
  mechanism, the writers matrix, the security limits and what exists for agy today; its verification tables are
  empty because the live phases have not run. `docs/multi-model.md` and `docs/orchestration.md` now say that agy is
  a chat-orchestrator engine and report-only model surface, not a kit role backend.

- **`balanced-claude-55`, the recommended multi-model profile.** Claude only, gateway-agnostic, three tiers on the
  5.5 family: Haiku for `explore`, `tester` and `package-upgrade`; Sonnet for code, review and verification; Opus for
  the planner, the security auditor and the architect (fallback Sonnet). `tests/test_profile_validity.py` derives
  from `compat.plan` that it is verified, with no note and without `--allow-unverified`, for Claude Code
  (single-model, LiteLLM, OpenRouter), Aider (both gateways) and the OpenClaw claude-code engine, and runs the same
  check on whatever `wizard.RECOMMENDED_PROFILE` names.
- **A role diff when a host's saved profile is not the recommendation.** Step 6 prints one line and, interactively,
  one question (default No) that shows the role-by-role difference; a non-interactive run prints the difference and
  the exact command (`ai-resources setup --profile balanced-claude-55`). Nothing switches and nothing is written.
  The table is the one `executors tune|set|apply` already used, now shared (`profiles.print_role_diff`). Step 6 also
  says that a Claude-only host with no gateway should use the single-model `claude-native` profile.
- **Docs: the opt-in cross-family layer.** `docs/multi-model.md` describes planner, explore and reviewer steps on
  another OpenClaw agent as an opt-in workflow choice (fields exist; the live behaviour is not verified, and the
  evaluation that would decide the reviewer's model has not been run).
- **A mixed-provider team test.** `tests/test_mixed_provider_team.py` walks a profile with roles on Google and the
  example `openclaw-agent` workflow through the real setup planning (`plan_table`, the summary screen),
  `models status` and `compat`, on fixtures and stubs only, and asserts that the dispatched roles are exactly the
  roles on a provider Claude Code cannot run verified, that each dispatched agent's provider has a verified
  OpenClaw engine cell, and that setup and `models status` name the same providers. A dispatch to an agent with no
  engine for its provider, to a missing agent, or a Google role that is not dispatched is reported.
- **A docs test for the profile table.** `tests/test_docs_profile_table.py` fails when a shipped profile is missing
  from `docs/multi-model.md`, when the table names one that does not exist, or when anything but the recommended
  profile is marked recommended.
- **Scored team composition (ADR-0006, Phase A).** `scores/model-scores.yaml` is a kit-shipped, dated model scores
  database (policy, routes, usage surfaces, billing pools, models with dated prices and role-agnostic benchmark
  proxies, and rows keyed by role, model, route and effort), loaded and validated by the new
  `scripts/ai_resources/scoring` package. A pure, deterministic engine proposes a team in three modes (`uniform`,
  `single-provider`, `mixed`) with a why table (text and JSON), balancing billing pools within each role's
  tolerance band. Opt in with the step 6 "compose" choice or `ai-resources setup --compose ...` (`--headroom`,
  `--headroom-live`, `--why-json`, `--allow-fable`); a run without them is byte-identical. `mixed` needs
  `--allow-unverified` and every non-Anthropic cell is marked, because no cross-provider compat cell is verified.
  Antigravity (`agy`) is direct use only and is never composed. The live headroom read is opt-in, read-only and falls back to the static level you chose;
  today only the Claude status-line sidecar is read live (the OpenRouter key and Vertex quota readers exist but
  ship no fetcher, so they are not offered). New:
  `ai-resources models scores show|check|diff|refresh` (nothing is auto-approved). Setup state gains the additive
  `selection.headroom` and `profile.composed` fields. Docs: `docs/team-composition.md`; the evaluation harness is
  Phase B and is not part of this change.
- **Scored team composition, review closure.** A baseline is "default kept" only when it honours the mode's shape
  (`uniform` is one model, `single-provider` one provider). One model keeps one route across the team (a profile
  entry cannot name a route), the live headroom opt-in is offered only where a reader exists (today the Claude
  sidecar), override rows and `models scores refresh --from` rows pass the same evidence rules as shipped rows,
  `models scores check` validates the merged document, the weighted loss is clamped at 0, the Claude sidecar
  reading is dated by the file's own mtime, and the DeepSeek proxy is no longer dated 2026-10-12.
- **OpenAI known models.** `gpt-6.1-sol`, `gpt-6-sol` and `gpt-6-luna` are listed in the openai known-models list.
  The kit still has no dispatch path for them.

### Changed

- **Best model first, on both lanes.** With the setup render path's `effective_ids`, a lane agent on the role's exact
  model outranks a same-family other version; the other version serves writers only as a fallback and is refused for
  a checker (a checker whose only candidate is a drifted claude-cli agent is Blocked). A profile `lanes.pins` entry
  ranks first for that non-checker role only. ADR-0007 sections 1 and 8 are amended.
- **Extended tripwires on every lane.** `~/.claude/CLAUDE.md`, `agents/`, `skills/`, `~/.agents/`, `~/.gitconfig`,
  `~/.config/git/`, `~/.ssh/`, `~/.config/systemd/user/` and (by metadata) `~/.local/bin/`; `crontab -l` and the user
  timer unit list are snapshotted at prepare and verify; a commit adding `SOUL.md`, `IDENTITY.md` or `USER.md` fails.
- **Run-pinned `lanes.yaml` hash and quota circuit breaker.** The first `lanes prepare` of a run records the table
  hash and later prepares refuse a changed table; `verify --spawn-error` can trip a pool for the rest of the run.
- **Held-candidate display.** `setup --lanes` lists held candidates at detail level instead of as warnings.
- **Anthropic acknowledgement text** gains the partial-acknowledgement residual: re-sign once.

- **Setup step headers follow the steps that run.** Step 6 is shown as 6a Models, 6b Role assignment and 6c
  Subscription lanes (the lanes header appears only when a lanes flag is given); the review is `Review and apply`.
  The numbering stays at 10 steps.

- **One writer per run is now one landing path per run.** The kit-orchestration "Who writes what" paragraph, the
  `docs/multi-model.md` cross-provider note and the cross-provider example header state the ADR-0007 invariant: the
  kit (`lanes land`) is the only landing path, at most one integrating writer, every lane writer in its own
  worktree. Runs without lane writers and explicit `openclaw-agent` steps (ADR-0005) are unchanged.

- **Scores DB corrections from the 2026-10-13 web re-check** (`scores/model-scores.yaml`, data only, no engine change).
  The Vals Terminal-Bench 4.0 board header reads "Updated 10/13/2026", so every proxy and the rows derived from it
  carry `as_of` 2026-10-13. Haiku 5.5 is 36.36 at $3.12 per task (score 3.6, was 35.35 and 3.5). Sonnet 5.5 and
  Fable 5.1 now use the provider-fallback-adjusted value (62.63 and 50.00, scores 6.3 and 5.0, raw values kept in
  `raw_value`), the same basis as Opus 5.5; the unsupported 53.0 variant is dropped from the Sonnet note. DeepSeek V4
  Pro is 14.14 and its per-row date is stated as not recorded (the as_of is the board date; reading the 0813 suffix
  as a date stays an inference). Prices: Gemini 3.8 Flash cache read 0.075 (0.15 from 2027-01-01) and Gemini 3.1 Pro
  Preview 0.20 (0.40 above 200k). The GPT-6 long-context tier records `cache_multiplier: 2.0` and
  `out_multiplier: 1.5`; the engine still prices only the input part of that tier (output stays unknown, flagged
  approximate), so these two keys are recorded data, not behaviour. `gemini-3.7-flash` is labelled VENDOR (Google
  routes it to 3.8 Flash server-side). Pool facts: Max has a separate weekly Fable limit, Codex Pro has a weekly
  limit only, and the Antigravity shared Claude and GPT pool claim is marked UNVERIFIABLE. The golden fixture
  `tests/fixtures/scoring_explain_golden.json` is fixture data and did not change; no engine code moved.

- **Reload rules follow the installed OpenClaw (no version pin).** The restart-required classifier reads the
  rule table from the installed package (`dist/config-reload-plan-*.mjs`, a file read) with a strict parse
  instead of trusting a copy pinned to one OpenClaw version, so an OpenClaw upgrade no longer makes every key
  restart-required. The kit's own restart entries always win over the installed table, unreadable or
  unparseable rules make every key restart-required (setup completes, `status` names the reason, `verify`
  warns), and no code path compares the installed version with a constant. `status` gains an `openclaw` row
  (version as information, rules source, prefixes where the kit is stricter). The watch re-classifies with the
  live rules when the recorded version differs. The write gate, the drained window, the busy probe and the
  memory valve are untouched. `PINNED_OPENCLAW_VERSION` is gone (`BASELINE_RULES` and `BASELINE_READ_FROM`
  replace `RULES` and the pin); ADR-0003 is amended and the manual install recipe uses `openclaw@latest`.
- **Kit pins move to the Claude 5.5 family.** `model_pins.DEFAULTS` (and the anthropic adapter's copy) now name
  `claude-opus-5-5`, `claude-sonnet-5-5` and `claude-haiku-5-5`; fable stays `claude-fable-5-1`. The three ids were
  confirmed by the live claude-cli catalogue (read only, 2026-10-13); the catalogue carries no price for them. The
  shipped profiles, the LiteLLM passthrough and alias map, `KNOWN_MODELS` and the golden snapshots follow in the
  same commit (the drift guard forces it). An unfrozen host moves to 5.5 the next time something renders,
  including the OpenClaw `*` primary; a frozen pin wins (`ai-resources models pin <class> <id>`). Haiku 4.5 to 5.5
  is a major bump when it comes through `models update`, but a kit default is not a proposal and needs no
  approval; the kit approves nothing and writes no overlay. No 5.5 id has a price row, so `ai-resources audit`
  shows `n/a` for them until one is added. Under OpenRouter the
  `modelOverrides` map covers the current generation only. The Vertex list names the 5.5 opus and sonnet ids
  without a Vertex verification.
- **The recommended default is `balanced-claude-55`, for a host that has not chosen a profile.** `ProfileState.name`
  used to default to `cost-optimized`, so a fresh state never reached the recommendation (the default was always
  the stale name); it is empty now and the wizard falls back to `RECOMMENDED_PROFILE`. A saved profile (including a
  saved `cost-optimized` or `measured-best`) is kept and is never replaced. `profiles.saved_or_recommended` is what
  apply, the dry run and `doctor` read when the state names none.
- **`measured-best` is the explicit multi-provider profile, not the recommendation.** Its description lists the
  verified cells (the Anthropic refs) and the unverified ones (its google/* and deepseek/* refs for Claude Code and
  for the OpenClaw claude-code engine) and says that setup writes those ids into the Claude subagent files without a
  matrix check today. `by_role` is unchanged; price comments for the 5.5 ids were removed (no price exists).
- **Docs.** `docs/multi-model.md` has the full profile table, the recommended per-role assignment with the quality of
  its evidence, the main-conversation note (`/model haiku` is Claude Code only; OpenClaw orchestrators stay on
  Sonnet) and the no-gateway advice. Rule 017's example uses the current schema (`<vendor>/<model>`, no
  `provider` key). The haiku-orchestrator comments in `openclaw-host.json5` and `openclaw_host.py` cited T29 (the
  restart window); they now point at the runbook's "Why sonnet and not haiku" (comments only).

### Fixed

- **Setup wizard review follow-ups.** A re-run pre-checks only the exact saved model of a family (the saved
  `ref`; previews are never pre-checked); Ctrl-D cancels select and checkbox prompts too; the review is shown on the
  kit-profile path (not in a non-interactive run); "Change models" honours `--dry-run` and returns to the review;
  the progress rail no longer shows a re-run step as skipped; an empty custom model id skips the role; `PASSWORD:
  a b c` and `passphrase: ...` are masked in full while `max_tokens`, `api_key_env: NAME` and digests are kept; a
  diff whose only change is masked says `changed (masked)`; OpenClaw configurator errors mask the command output;
  the docs no longer say nothing is written before Apply (credentials, tool and LiteLLM installs, the image pull
  and the gateway state happen earlier and a cancel lists them). CI tests the Formula's rich, questionary and
  prompt_toolkit pins as well as the newest ones.

- **Ctrl-C and Ctrl-D at a setup prompt exit 130 and apply nothing.** Before, questionary returned `None`, which
  the wizard read as "No", crashed on the port prompt (`int(None)`), or ended the lanes acknowledgement with exit 0.
  The cancel message names the step and lists what was already done in the run (an install, a state save, a file
  written) instead of claiming nothing happened. `cli.main` also turns a stray `KeyboardInterrupt` into exit 130.

- **A bare `pytest tests/` no longer fills the /tmp tmpfs.** `tests/conftest.py` now sets the basetemp to a unique
  `run-*` directory under `$XDG_CACHE_HOME/ai-resources-pytest-runs` (or `~/.cache/ai-resources-pytest-runs`) when no
  `--basetemp`, `PYTEST_DEBUG_TEMPROOT` or non-system `TMPDIR` is given, and prunes run directories older than 6 hours.
  An explicit `--basetemp` still wins.
- **The reload-rule parser failed open on a conditional policy and could backtrack for minutes.** The policy pattern
  ended at the `kind` literal, so `{ prefixes: ["a"], kind: "hot", when: (c) => false }` parsed as an unconditional
  hot policy; and the nested-quantifier list patterns took 40 s on 14 whitespace-separated literals followed by a
  non-matching character. `parse_reload_rules` is now a linear tokeniser: each policy object must be exactly
  `{ prefixes: [...], kind: "..." }` followed only by the data-only `actions` / `replaceChannelPlugins` fields the
  dist carries (any other field makes the whole table unreadable, so every key is restart-required), and input over
  256 KiB is refused.

- **`ai-resources audit` priced a newer minor as its major.** `audit._price` matched by prefix, so
  `claude-opus-5-5` was costed as `claude-opus-5`. A prefix now matches only a dated snapshot or a context
  suffix (`claude-sonnet-5-20260101`, `claude-fable-5-1[1m]`); a model without its own row is unpriced rather than
  mispriced.
- **Gateway guard, last pass (2.2f).** The verification round of 2.2e found four weak points in the second view
  (text that feeds a shell) and the benign cost of the 2.2e widening; this pass closes them and stops. (1) A shell,
  `eval`, `source`, `exec` or a `read`/`mapfile` whose text is run later, reading a process substitution on stdin
  (`sh < <(echo ...)`, `read -r c < <(...); eval "$c"`, `exec 3< <(...); sh <&3`, `sh 0< <(...)`, `bash -s < <(...)`,
  `exec bash < <(...)`) was read as printed-only text; the strict view now looks through the redirect (and an fd
  number) to the command that owns it, and such a text feeds the second view. `cat < <(...)` and `diff <(...)
  <(...)` stay printed. (2) The other-product allow list read an option's VALUE (`sudo -u docker`, `env -u docker`,
  `env -C /opt/docker`) as the command word and a substitution that is an argument of another product's command
  (`docker ps $(${OC:-openclaw} gateway restart)`, backticks, `<(...)`) as part of that command; wrappers are now
  looked through with their options and values (the view is lower-cased: `-C` is folded), a substitution that is
  still open is a statement of its own, a plain wrapper that names a product as a user, host or path
  (`su docker -c ...`, `flock /var/lock/docker ...`) no longer exempts a built command, and the value of a gateway
  option (`gateway --password status restart`) is not a subcommand that answers the verb. (3) In shell-fed text a
  bare `gateway` + verb in one statement (`npm run gateway:restart`, `git log --grep='gateway restart'`, `fly apps
  restart gateway`) needed no second signal that it is OpenClaw: it now needs an `openclaw` word or the unit (as
  before), a word built at run time in front of the two words or in a neighbouring statement, an assembled
  `openclaw`, or an assembled name glued to `gateway`; a comment line that does not name openclaw is not a
  statement; `--grep=gateway` is an option value, not a stored name; an `openclaw` word earlier in the same
  statement counts however far it is (a 900-character option value). The tools that merely carry text (`git`, `grep`,
  `npm`, `gh`, `fly`, ...) were NOT added to the exclusion list: the second signal makes it unnecessary and a list
  entry would open `git -c alias.x='!...'`. (4) `watchdog.off`: `reclaim_stale` compares the modification time as
  well as device and inode before it unlinks; a SIGTERM between the exclusive create and `acquire()` returning no
  longer leaves a marker nobody owns (the signals are held until the identity is recorded); `Marker.remove()`
  deletes only a file whose identity this run recorded and says so when it leaves one; the models script reads the
  marker through `head` under `timeout`, so a FIFO swapped in after the file check cannot block it.
- **Gateway guard, measured (2.2f).** What the independent Verifier measured at c773dc5, through real bash and zsh
  in a sandbox with stub binaries and no network: 24047 adversarial rows, 19086 of which executed a guarded
  command. Executed but allowed: 2.1.0 14739, 2.1.1 14318, cd70a20 5934, f1a26f8 7506, this tree 4045. Executed and
  denied by an older guard yet allowed by this tree: 2.1.0 1, 2.1.1 0, cd70a20 30, f1a26f8 0; no class is worse
  than either published guard (2.1.0, 2.1.1). Benign rows denied (2.1.0 / 2.1.1 / cd70a20 / f1a26f8 / this tree),
  over three corpora: realistic (1008 rows) 0 / 0 / 120 / 0 / 0; shell text with a `PATH=` line or a function
  prelude (1203) 0 / 0 / 664 / 44 / 191; prose and heredoc idioms (680) 0 / 0 / 413 / 70 / 75. The slowest verdict
  was 1.42 s at a load of about 43 and the deadline fired on none; with the deadline forced, this tree allowed 0 of
  the 19086 executed rows (cd70a20 985, f1a26f8 632).
- **Gateway guard, residual classes (2.2f).** Known and NOT fixed; 2.2f is the last pass on the text. The Verifier
  signed `met=false` against the plan's acceptance and the operator chose to document and close. (1) A stored
  command whose assembled OpenClaw name (`${OC:-openclaw}`, `/usr/local/bin/open*`) is run later, with another
  product's word in an option value, inside text fed to a shell: 30 rows against cd70a20, also allowed by f1a26f8
  (the literal name is denied). (2) The comment-line drop misreads quote state when a prose apostrophe in an earlier
  non-shell heredoc flips its polarity, or when `#` follows `{` (`${#x}`): a command after a quoted multi-line
  string can be allowed. The code docstring of `_drop_comment_lines` says such a misread leaves the text "toward
  deny"; that is not true for a flipped polarity (docs-only note; the code is unchanged). (3) The second signal (an
  earlier `PATH=`, or a function whose body has `gateway`) denies other products' bare `gateway` words in
  shell-fed text (`npm run gateway:restart`, `git log --grep='gateway restart'` after a `PATH=` line): benign
  denials above f1a26f8. (4) One row against 2.1.0 only: a script text built from a multi-line quoted string and
  then run (`source <(echo '...')`), already allowed by 2.1.1. (5) `echo $(V='...'` newline `eval "$V")` is allowed
  by every guard, 2.1.1 included. The guard is an accident guard: restart and stop authority belongs at systemd and
  polkit, or an exec policy (pitfall T45), not in this filter.

- **Gateway guard, budget fallback (2.2e).** A prefix of about 8 KB of harmless substitutions spends the work
  budget and the rest of the command was read by the word view, which did not join line continuations and cut at a
  backtick: a guarded command split with `\` or with an inline substitution in an option slot was allowed
  (183 of 1490 guarded rows that 2.1.x denied, measured by the independent audit). The word view now undoes
  continuations and also reads the text with every inline substitution collapsed to one word. The differential of
  this pass found the collapse lost the "unknown verb" signal (`openclaw gateway `echo restart``): the placeholder is
  now an expansion, only innermost substitutions are collapsed and one that names openclaw is kept (7 executed rows
  that cd70a20 denied). The budget fallback is the word fallback, not the fail-closed view: the measurement did not
  need the wide view on top (0 executed rows that an older guard denied are allowed after a budget-spending
  prefix), and it would deny a padded `openclaw gateway status`.
- **Gateway guard, second view (2.2e).** The narrowed second view (text that feeds a shell) required a literal
  `openclaw` before `gateway`: a name assembled from variables, ANSI-C escapes, globs, braces or a quote pair
  around a backtick, and a `gateway` stored in a variable before `openclaw`, was allowed (118 executed rows that
  cd70a20 denied, 0 against 2.1.x). It now reads the gateway words wide, with `openclaw` on either side, and
  EXCLUDES known harmless contexts: another product's gateway (`api-gateway`, `deploy/gateway`, `docker restart
  gateway`, `systemctl restart gateway`), words that are only printed (`echo`, `printf` and `:` reached through
  `eval`, `bash -c`, `find -exec`, `<(...)` or `$(: ...)`, not piped, redirected or substituted), and a subcommand
  that answers (`openclaw gateway status --note restart`). The exclusion work is bounded and fails closed.
- **Gateway guard, deadline fallback (2.2e).** After the deadline fired, a guarded command whose unit is spelled
  `openclaw-gate*`, `openclaw-{gateway,x}` or an ANSI-C-encoded name, with the verb far away, was allowed. The
  cheap, strict and fail-closed views now share one normalisation (continuations undone, `$'...'` strings
  decoded, quotes removed) and one token matcher, unit globs and brace forms included. A deadline that fires after a
  deny was made no longer replaces that deny with the fallback's answer.
- **Gateway guard, measured (2.2e).** Against real bash on 100728 recorded rows (38390 executed a guarded
  command): executed but allowed 536 (2.1.0: 16328, 2.1.1: 11970, 2.2c: 545, 2.2d: 545); 0 executed rows that
  2.1.0, 2.1.1, 2.2c or 2.2d denied are allowed; benign rows denied 54 of 13716 (2.2d: 67). On three fresh corpora
  through real bash and zsh (7718 executed, 1500 behind a budget-spending prefix): executed and denied by 2.2c yet
  allowed 528 before this pass, 0 now. The slowest verdict is 0.8 s on the recorded rows and the deadline fired
  on none.
- **Setup state (2.2e).** A FIFO at the corrupt-state backup name (`setup-state.yaml.corrupt`) no longer hangs
  `load()` (existing copies are opened `O_NOFOLLOW|O_NONBLOCK` and checked for type and size), and a disk that
  fills while the copy is written removes the partial copy and says that no copy could be made instead of raising.
- **Models run and `watchdog.off` (2.2e).** A `models update` or `models rollback` killed by SIGTERM (the unit's
  `TimeoutStartSec`) left `watchdog.off`, which since 2.2c counts as an operator window. The marker is now held
  through `MarkerGuard`, signed with the creator's pid and start time, and removed only while it is still the file
  this run created. A signed marker whose process is gone is reclaimed by the models run and by the restart, and
  the daily job does not stand down for it; an empty or foreign marker (an operator's) still does. A run killed by
  SIGKILL still leaves its marker until the next models run reclaims it.
- **Journal findings (2.2e).** The `host=` field of a refused TLS line starts a word: `?host=x` inside a URL and
  `proxy-host=` no longer supply the host.
- **Reload table pin follows OpenClaw 2026.10.2.** The rule table read from the installed 2026.10.2 dist is
  identical to the 2026.10.1 one (108 rules); only `PINNED_OPENCLAW_VERSION` and the fixture name and version moved.
- **Gateway guard (A).** The gateway guard hook reads a command substitution (`$(...)`, backticks, `<(...)`, also inside double
  quotes, in `${...}` and in an unquoted heredoc body) as the command it runs, `env -S` strings by env's own split
  rules (a backslash-underscore separates, a backslash-c ends, `${VAR}` fails closed when the gateway words are
  there), `$'...'` strings, redirections (even inside a word), a brace in an argument and a `#` inside a word the
  way bash does, and follows nesting with a cost bound (eight levels in this pass, forty since 2.2b). A brace or glob in the verb slot, a unit
  glob and a case variant through the CLI are denied; `echo "<<EOF"` no longer starts a heredoc skip. Against
  real-bash corpora of that pass the executed-but-allowed count fell from 1850 to 185; that figure was measured on
  those corpora only and is superseded: the final numbers, over 100728 recorded rows, are in the 2.2c and 2.2d
  entries below. The 256 KB input cap is kept and tested at 1 MB.
- **Locks and files (C).** `setup` and `apply-pending` run under the setup-state lock (a busy lock is one line);
  stale `.setup-state.yaml.*.tmp` files are removed at the next save; `kit-host.env` is written through a unique temp
  file that keeps the file mode; managed blocks in AGENTS.md-style files are written atomically through a symlink to
  its target with an exclusive backup; `offbox-identity --dry-run`, `graceful-restart --dry-run` and a
  `graceful-restart` refused because it runs inside the gateway cgroup create no lock file. A real `graceful-restart`
  that a gate refuses creates `~/.openclaw/health-restart.lock` (0 bytes, mode 0600) and keeps it, by design: removing
  a lock file while it is locked lets two holders in.
- **Teardown and adoption (D).** Teardown removes the empty `hand-units/<stamp>` directories the kit made, adopts a
  dangling symlinked hand unit as a link and restores it as a link, never moves a symlink planted in the
  moved-aside directory onto a unit path, restores unit files through a temp file so hard links keep their bytes,
  drops the restart-pending entries a drained window just applied, and names an unfinished reason once.
- **Verbs and prompts (E).** The offer to install missing tools defaults to No and `setup --yes` is the explicit yes;
  the pipx installer menu defaults to Skip; `models check` and `models update --dry-run` write no log and take no
  lock; the models-update unit treats exit 75 as a skip; a declined prompt exits 1 in both `apply-pending` and its
  discard form; `openclaw doctor` needs a terminal or `--yes` (the kit's timers pass it) and, like the drained
  window, refuses from inside the gateway cgroup.

- **Gateway guard (2.2b, section 1).** Closes the findings of the 2.2 review: a `case` pattern's `)` no longer ends
  a `$(...)`; text fed to a shell (a heredoc or here-string whose consumer is `sh`, `bash`, `zsh`, `dash`, `ksh`,
  `eval`, or `echo`/`printf` piped into one) is command text; `xargs`, `find -exec` and `parallel` commands, an
  `alias` value, `trap 'CMD' SIG` and `V='...'; eval "$V"` are read; `openclaw --profile p doctor --fix` is
  denied; `builtin`, `coproc` and `$"..."` are read; deep benign nesting is followed (40 levels) instead of being
  read for words, and the word fallback needs the verb within a few words of `gateway`. Corrected in 2.2c: the cost
  is bounded per shape (an `openclaw` word looks at a bounded window; heredoc delimiters are looked up), not linear
  in general, and the "0 regressions" of this entry was measured on its own corpora only. The next independent round
  found, at 999890f, 45 executed rows that 2.1.0 denied and that 2.2b allowed (10 against 2.1.1), and inputs the
  guard took 14 s (60000 adjacent `$(a)`) and 36 s (backticks in double quotes at 262 KB) to read, past the hook's
  10 s timeout, which fails open. The 2.2c entries below carry the final numbers.
- **Restart paths (2.2b, section 2).** The graceful restart, the drained window of `doctor` and `apply-pending`, and
  setup's and `models update`'s gateway restart take one shared non-blocking lock (`health-restart.lock`) and
  create `watchdog.off` with `O_EXCL`: a second path that finds the lock or a window that opened in between
  refuses with one line and stops nothing. Setup's restart also refuses inside the gateway cgroup (T29), and a busy
  probe that cannot tell now counts as busy (fail closed) instead of idle.

- **Install consent (2.2b, section 3).** Consent to an installer is per run and is never stored: an
  `install_tools_answer` saved by an older kit is ignored and cleared on load, the question is asked again on every
  interactive run (default No, also after an earlier "no"), `setup --yes` is the only unattended yes and persists
  nothing. The LiteLLM pipx and venv installs and the image pull now default to No and never run under
  `--non-interactive`; the voice local engine's `brew install whisper-cpp ffmpeg` has its own confirm (default No,
  never unattended); `openclaw bootstrap` asks before each change (default No) unless `--yes`, and an unattended run
  without `--yes` changes nothing.

- **State writers (2.2b, section 4).** The post-setup watch and `executors` regeneration no longer save a
  minutes-old copy of the setup state: each loads it again under the lock immediately before saving and writes only
  its own fields (the watch's `config_watch` record and the restore entries it queued, the subagent file record), so
  a concurrent `setup`, `apply-pending` or teardown is never overwritten, and the lock is not held while a revert
  runs. Lock files (setup state, restart lock) are opened with `O_NOFOLLOW`; a symlinked `kit-host.env` is written at
  its target and the link survives (a link into a missing directory is left alone with one line).

- **Teardown of hand units (2.2b, section 5).** The moved-aside file must resolve, by real path, inside this kit's own
  `hand-units/<stamp>` directory: a symlinked stamp directory plus a crafted state entry no longer moves a file in
  from outside. The original is put back with one `os.replace` (a temp file beside the destination across file
  systems), so the destination is never missing while it is restored.

- **Journal findings (2.2b, section 6).** A journal line is untrusted text. The `host=` token the egress-proxy
  finding names must be a lowercase RFC 1123 host name (labels of 1-63 characters, at most 253 in all; punycode is
  fine) or it is dropped, so `status` and `verify` never print advice naming a host that failed that check; ANSI
  sequences, control and bidi characters are stripped from what is printed; and the journal read has a byte cap
  (8 MiB, the process is stopped there) next to its line and time bounds.

- **Sandbox findings (2.2b, section 8).** `setup` exits 1 (and says setup is NOT complete) when the operator asked for
  a restart-required apply and it was refused or failed, for example inside the gateway cgroup (T29); the keys stay
  pending. Stale `.kit-host.env.*.tmp` and `.AGENTS.md.*.tmp` files a killed write left are removed at the next
  write (regular files of the user, older than an hour). The gateway guard also reads `builtin`, `coproc` and the
  command line GNU `parallel` parses again, and T46 / ADR-0004 list the residual classes (a verb built at run
  time, output produced at run time and then run, `${ cmd; }`, aliases from an earlier call, `xargs`/`parallel`
  fed commands as data) and say the authority to stop the unit belongs at systemd and polkit.

- **Gateway guard (2.2c, sections 1 and 2).** The guard is a best-effort accident guard that answers inside the
  hook's timeout: the precise analysis runs under a wall-clock deadline (4 s, never above 8 s; a signal timer and a
  clock check in the loops), and when the deadline fires, or the analysis fails with an internal error, a strictly
  linear word view decides: the gateway next to a stop/restart verb, or `doctor` next to `--fix`, within 160
  characters (quotes and backslashes removed first). Cost is charged per `shape()` call and per logical line, a
  masked substitution is one character, and what `printf` or a variable expansion would produce is charged before it
  is built. The same word view also reads text that feeds a shell through a heredoc, a here-string or a pipe (a
  shell, `eval "$(cat)"`, `. /dev/stdin`, `xargs`/`parallel` starting a shell) when the precise analysis found
  nothing: a shell given its own literal `-c` string, prose backticks and fenced code are not consumers. The precise
  reader also follows an assignment after `then`/`do`, the command line `parallel` hands to a shell, and a heredoc
  whose delimiter line never comes. Precise parsing of arbitrary shell is not attempted. Measured against real bash
  (recorded stub-binary runs, 100728 rows of which 38390 executed a guarded command; 2.1.0 / 2.1.1 / now):
  executed-but-allowed 16328 / 11970 / **545**; executed and denied by 2.1.0 or 2.1.1 yet allowed now **0**;
  benign rows denied 288 / 17 / **74** of 13716 (10 of them on the realistic corpora: 5 of the 44 hard negatives, 3
  of the 776 generated commands, 2 of the 764; each really feeds a shell or runs the verb); slowest verdict 1.1 s
  with the deadline never firing; the measured cost classes run in under 1.2 s. (Corrected in 2.2d: the second view
  was wider than it says here, it read the bare word `gateway`, and the fallback was blind to a verb wrapped in
  backticks past the work budget or the nesting limit; see the 2.2d entries.)
- **Setup and restart paths (2.2c, section 3).** `status`/`verify` say that any process writing to the unit's journal
  can cause an egress refusal and to bypass a host only if it is recognised and the skipped proxy is understood
  (T44); `setup --dry-run` offers and runs no installer and `--dry-run --yes` says it installs nothing; setup's and
  `models update`'s restart refuse while `watchdog.off` exists; a state file that is not valid YAML is copied aside
  once (0600) and never replaced by an empty record by an unattended writer; an unreadable `/proc/self/cgroup` is
  "cannot tell" and every restart path refuses (a missing file still means outside); `bootstrap()`'s library
  default confirm is No.

- **Sandbox findings (2.2c, section 6).** Teardown refuses a hand-units backup tree that is, or sits behind, a symlink
  (one line, the originals stay where they are, the record is kept). `offbox-identity` and the setup step validate
  the `kit-host.env` target (a directory, a device, a link into a missing directory) before any gcloud call, so
  nothing is created and the failure is one line. A watch that could not record its result because the state lock
  stayed held leaves `setup-state.watch-result.json` that `status` and `verify` report until the next successful
  save. The egress-refusal journal read is `--reverse`, so the byte cap keeps the newest lines; a control
  character inside a `host=` token drops the host instead of truncating it (the line is split on LF only); the
  graceful-restart lock loser prints the same line as `doctor`; a symlinked lock says "is a symlink"; and the busy
  note of the strict restart path says "cannot tell" instead of "treating the gateway as idle".

- **Gateway guard (2.2d, section 1).** The word view the guard falls back to (past the nesting limit, past the
  work budget, on text that does not tokenise) cuts words at a backtick, so `` `openclaw gateway restart` `` is read
  after 2016 or more adjacent substitutions and at nesting depth 41 or more, where 2.2c allowed it. The second view
  (text that feeds a shell through a heredoc, here-string or pipe) now needs the OpenClaw gateway words, `openclaw`
  before `gateway` or the unit `openclaw-gateway`, instead of the bare word: `docker restart gateway` or `kubectl
  rollout restart deploy/gateway` in a heredoc to a shell is allowed (140 such generated rows: 126 denied by 2.2c, 0
  now; the realistic corpora went from 74 to 67 benign rows denied of 13716, with no executed command newly
  allowed). The deadline fallback keeps the wide rule on purpose and says so: when the deadline fires, a benign
  text that names a gateway and a verb is denied.
- **Gateway guard (2.2d, section 6a).** A fired deadline fails closed beyond the 160-character window: a text that
  names OpenClaw (as a command word, or the unit `openclaw-gateway`) together with any of `gateway`, `restart`,
  `stop`, `kill`, `doctor` or `--fix`, anywhere in it and after undoing line continuations, is denied. The class the
  2.2c audit measured at 4.1 s (an open double quote over thousands of lines holding `${...}`, `$(...)` or
  backticks, near the 256 KB cap) is read once instead of twice and is charged against the work budget: 1.2 to 2.1
  s through the whole hook on this host, the deadline does not fire, and a guarded statement appended to it is
  denied.
- **Watch-result marker (2.2d, sections 2 and 6b).** `setup-state.watch-result.json` is written with a temp file
  and `os.replace` over its own name and read only as a regular file of this user: a symlink planted there is
  replaced or unlinked, never followed (the next save no longer deletes or overwrites the link's target). `status`
  and `verify` report a marker only while the state's `config_watch` is the run it names and has no recorded result,
  so a forged or stale file says nothing and never hides a recorded failure; only the save that records that run
  clears it, not any save, and the text no longer says a later setup run clears it.
- **Teardown and refusals (2.2d, section 3).** A symlinked `hand-units` directory keeps the record of what was
  moved aside and makes the teardown unfinished (the kit's own unit stays installed), so a second teardown after the
  link is repaired puts the originals back. A refusal that names a copy of an unreadable state file makes the copy
  (once per distinct content, 0600) before it speaks; `load()` and `save()` agree that `[]`, `0`, `false`, `''` and
  any non-mapping are corrupt, so an interactive setup asks before replacing every file `save()` refuses. `models
  update` and `models rollback` take the maintenance window with one exclusive create and never remove a
  `watchdog.off` they did not create. A CR or a TAB inside a journal `host=` token drops the host.
- **Installer consent (2.2d, section 6d).** `tools.offer` asks only on a real terminal: a line piped into stdin is
  not consent, and without a terminal and without `--yes` it says so and installs nothing.

### Added

- **Findings (F).** `status` and `verify` WARN when the egress proxy refused hosts with `TLS certificate unavailable`
  in the last 24 h (at most five hosts, pointer to T44) and when a recent `gateway.restart_close_failed` or
  `gateway.stop_close_failed` bundle coincides with deferred or lost subagent completions (T39/T41). Bounded, read-only,
  silent when the journal is unavailable.

### Documentation

- **T45 (B).** The guard hook does not cover an OpenClaw-native exec session: the finding, the options
  (`tools.exec`, `tools.deny`, a `before_tool_call` handler) and the operator decision are written down. The kit
  applies none of them.
- **T46 (G).** The guard is an accident guard, not a security boundary; the residual gaps are in ADR-0004.

## [2.1.1] — 2026-10-12 — the post-setup watch no longer reverts a healthy gateway, and the 2.1.0 surfaces are hardened

A patch release. The 2026-10-10 setup run showed the post-setup watch reverting two guard keys on a gateway that
was healthy (its probes could not run in the transient unit); that is fixed at the root and the watch now ends
INCONCLUSIVE, never reverting on a probe it could not run. The rest is hardening found by independent review
rounds: the gateway guard (line continuations, multi-line quotes, heredocs, `env -S`), the off-box identity step,
teardown, the state file and the no-op patch check. No setting changes meaning; `ai-resources setup` is not
required, but run it to refresh hooks and skills.

### Fixed

- **Hardening pass 5.** The gateway guard no longer lets a multi-line quote join hide the next command (a quote
  nested in `$(...)`, a backtick or `${...}`, or a quote after `$$`): a joined group that holds such a construct
  is evaluated as the joined text and line by line, and denied when either view matches, `$$` is read as the PID
  and an unquoted newline separates statements (P1, P8). A backslash before CRLF is no longer read as a
  continuation, and only LF breaks a line (P2). The drained-window confirms reached from the setup wizard (the
  restart-required half of a patch and the legacy revert offer) need a real terminal like `apply-pending`: a
  piped `y` confirms nothing, the entries stay pending and one line says so (P7). The MCP teardown and the
  openrouter plugin check compare by type, so a hand edit that only changes `true` to `1` is not taken for the
  kit's own value (P3), and teardown writes a recorded env value back only under the fixed keys the kit writes,
  not under any `OPENCLAW_*` (an unmanaged `OPENCLAW_OFFBOX_LIST_CMD` is warned about, never restored) (P4).
  `setup-state.yaml` is written through a temp file and `os.replace` (still 0600), `offbox-identity` runs under a
  non-blocking lock on the state so a second run is refused instead of leaving an orphan configuration, a
  custom-named record no longer makes the default plan warn "not set up" and a hand-edited non-dict record does
  not crash (P5). A deeply nested or non-UTF-8 `openclaw.json` is reported like any file the kit cannot read
  instead of raising `RecursionError` or `UnicodeDecodeError` (P6). `offbox-identity --name X --remove` refuses when
  X is not the recorded configuration, and a non-string previous env value is warned about as "not a string"
  (P11).
- **Hardening pass 4.** `apply-pending` (the drained gateway stop and start) without `--yes` needs a real
  terminal, like the discard verb: a piped `y` is not a confirmation, an empty or closed stdin ends in one line
  instead of a traceback, and a refused, declined or empty run no longer rewrites `setup-state.yaml` (S1, S1b).
  The gateway guard hook joins a shell line continuation (backslash-newline outside single quotes) and a quoted
  string that spans lines before tokenising, so `systemctl --user \<newline> restart openclaw-gateway` and
  `sh -c 'openclaw gateway \<newline> restart'` are denied, and a remote `ssh host \<newline> systemctl ...` is
  no longer split into a denied second line (S2).
  The remaining plain-equality sites compare by type as well (`1` is not `true`, `1.0` is not `1`): the host
  profile fill, the legacy-revert offer and the MCP mirror plan, so such a value is listed as kept, patched or
  updated instead of being dropped (S3). The JSON5 reader no longer edits string contents when it drops trailing
  commas: a live `"x,]"` stays `"x,]"` and a change to it is sent (S4).
  `offbox-identity` (and setup) refuse a second `--name` while a recorded configuration exists, in one line that
  says how to remove it, so a rerun can no longer forget the first one (S5). Teardown also survives a non-string
  previous env value (warned by name, reported unfinished), writes `kit-host.env` keys back only under the kit's
  own `OPENCLAW_*` namespace (`LD_PRELOAD=` is never written), and deletes a gcloud configuration only when the
  recorded name is one the kit would create (S6). Single-violation tests pin each condition of the hand-move
  confinement rule (S6) and the adopt-on-rerun and "no entries" line behaviours are pinned (S7).
- **Hardening pass 3.** `offbox-identity` derives the project from `--sa` only when it has the shape of a GCP project
  id, so `--sa x@--log-http.iam.gserviceaccount.com` can no longer reach gcloud as a flag (M1). The value
  validators use `\Z`, so a trailing newline is refused (L1). A gcloud configuration created before a later step
  failed is recorded at once, so `--remove`, teardown or a rerun owns it (L5).
  The "no changed leaf" check compares by type: `true` is not `1`, `1` is not `1.0`, and deleting a key the file
  holds as an explicit `null` is a change, so such a patch is sent instead of dropped (M2).
  Teardown restores `kit-host.env` per key: a previous value it cannot write back safely is warned about by name,
  the teardown reports itself unfinished and a rerun no longer crashes (M3). Teardown acts only on the kit's own
  unit names inside the user unit dir, the hand script under `~/.local/bin` and the transcriber's glossary path,
  never through a symlink or a `..` (L2). The kit's health-restart timer written next to a hand `.service` is
  removed again (T1).
  `dedupe-agents-md --apply` refuses a symlinked file, writes a temp file and renames it over the original (mode
  kept) and says where the backup is when the write fails (L3). The destructive confirms (`dedupe-agents-md
  --apply`, `apply-pending --discard-watch-restores` without `--yes`) need a real terminal: a piped `y` is not a
  confirmation (T2). A refused or dry-run discard no longer rewrites `setup-state.yaml`, prints the "none
  pending" line once and says "dry run:" only when it is one (T3).
  A post-setup watch whose window ended healthy is recorded as finished (`outcome: healthy`) and `status` says
  so, instead of looking like a watch that is still running (T4). The gateway guard hook's fail-closed fallbacks
  (an `env -S` string that cannot be tokenised, or nests past the depth limit) deny only when a stop or restart
  verb sits next to the gateway, so `env -S '... echo openclaw gateway'` is no longer blocked (L4).
- **Hardening pass 2.** A second `ai-resources setup` run no longer cancels the first run's post-setup watch:
  a patch with no changed leaf is never sent, so the first-write hook never fires for it (G1).
  `apply-pending --discard-watch-restores --yes` also closes a 2.1.0 watch record, so `verify` stops reporting
  an ERROR for a revert that can no longer be retried (G2). `apply-pending --discard-watch-restores` and
  `dedupe-agents-md --apply` without a terminal end with one line and a non-zero exit instead of a traceback (G3).
  Teardown puts the operator's hand-installed health-restart units back over the kit's copy (never over a file
  changed since) and removes the default voice glossary only while it is still the kit's (G4). Wording: `offbox-identity
  --yes` says "running", `status` no longer repeats "not configured" beside the uploader, the dedupe diff ends with a
  newline and the uploader identity names the configuration when it has no account (G5).
- Teardown reports itself unfinished (and keeps the OpenClaw state) when the gcloud configuration was left alone (H0).

- **The post-setup watch no longer reverts a healthy gateway (D1, pitfall T42).** The transient unit now gets its
  own PATH (`--setenv=PATH=`), so `openclaw health` finds node. A probe that cannot run (rc 126/127, a missing
  interpreter, `openclaw --version` failing) ends the watch INCONCLUSIVE: nothing is reverted, nothing is
  queued, and `status`/`verify` warn instead of erroring.
- A revert now needs a failure sustained for 5 minutes after a 120 s settle period AND a corroborating signal
  (MainPID changed, restart, unit down, port closed, a stop in the journal). The watch window is 15 minutes
  and every probe logs `tick=N rc=R class=C elapsed=Ts`.
- Setup records `kind` and `openclaw_version` per change; the watch routes by the recorded kind and never
  re-classifies in a degraded environment. A key whose hot reload was applied and answered afterwards is kept.
  A rejected drained-window dry run (exit 7) never queues a restore entry.
- **The off-box uploader's identity (D3, pitfall T43).** `OPENCLAW_OFFBOX_GCLOUD_CONFIG` and
  `OPENCLAW_OFFBOX_IMPERSONATE_SA` give `openclaw-backup-uploader` a named gcloud configuration (the three agent
  credential variables are removed from gcloud's environment for it) and an optional impersonation; with both
  unset nothing changes. The uploader preflights the bucket, fails with one readable `ERROR: <identity> cannot
  access <bucket>` line and a hint, and writes `~/.openclaw/state/offbox-last.json` on every exit.
- `status` and `verify` read that state file (they never run gcloud): WARN when the newest off-box daily is
  older than 30 h, ERROR above 54 h (`OPENCLAW_OFFBOX_WARN_AGE_H`, `OPENCLAW_OFFBOX_ERROR_AGE_H`), WARN when the
  uploader never reported.
- **`ai-resources openclaw offbox-identity [--dry-run|--remove]`** and an opt-in setup step (default No, only
  with `OPENCLAW_OFFBOX_BUCKET` set) create the named configuration: never activated, no credential file, no IAM,
  idempotent, and the removal only deletes what the kit created and that still matches. The IAM grant stays an
  infra step.
- **`env -S` no longer hides a gateway stop or restart from the guard hook.** The split string is re-tokenised and
  checked again (bounded depth); an unparseable string that names the gateway is denied.
- **`ai-resources openclaw dedupe-agents-md [--apply] [PATH...]`**: shows the diff of the hand-written copy of the
  kit's "Long-running commands" section that `verify` flags; `--apply` needs an interactive confirmation, writes
  `<file>.bak-<UTC timestamp>` first and removes only an identical copy (a copy that differs is shown, never
  touched).
- The weekly maintenance script reports a busy-refused doctor (exit 6) as "skipped" with one notice and exit 0,
  instead of an ALERT "doctor failed unexpectedly".
- **Voice language no longer reverts to `auto` (D2).** The voice question starts from the live
  `tools.media` `--language` value (it wins over a saved answer and over the locale, and prints "kept your
  value"), and a run with no saved answer keeps the live value instead of writing `auto` over it.
- `ai-resources openclaw apply-pending` no longer applies leftover 2.1.0 `source=watch` restores; `verify` warns
  they are likely false positives and `apply-pending --discard-watch-restores` lists them (dry run) and drops
  them with `--yes`.
- **`offbox-identity` validates its values (hardening pass).** `--sa`, `--name`, `--account` and `--project` are
  checked against their shapes (and anything starting with `-` is refused) in the plan and in the flag parsing,
  and `kit-host.env` no longer accepts a value with a space: the file is sourced by bash, so `KEY=a /path/cmd`
  would have run `/path/cmd`.
- `dedupe-agents-md` creates its backup with `O_EXCL|O_NOFOLLOW` (mode 0600): a symlink planted at the backup name
  is never written through, and the run reports "nothing written". A file that changed between the check and the
  write is reported as "file changed, nothing written" instead of "removed".
- The off-box uploader, with a named gcloud configuration, removes every `CLOUDSDK_*` variable except
  `CLOUDSDK_ACTIVE_CONFIG_NAME` (and `CLOUDSDK_CONFIG`, `CLOUDSDK_PYTHON*`, which are not identity) plus the two
  `GOOGLE_*` credential variables.
- Teardown keeps the `host_offbox_config_created` record when the gcloud configuration could not be removed
  (changed or active), so a later teardown can retry.
- **Reload table pinned to OpenClaw 2026.10.1.** A host on an older OpenClaw (for example 2026.9.9) classifies
  every key restart-required until the pin follows its version: the classification fails closed, it never guesses.
  `classify(..., '2026.9.9')` therefore no longer reports the two resource-guard keys as hot; the 2026.10.1 test
  is the regression for them.
- Docs: pitfall T44 and a runbook paragraph on `[secrets/egress-proxy] secret egress TLS certificate unavailable`
  (it is not the traffic allowlist; the fix is `secrets.egressProxy.bypassHosts`, a restart-required key, applied
  pending and restarted with `graceful-restart` in the window).

## [2.1.0] — 2026-10-10 — a resource-safe gateway: prevention first, a bounded graceful restart as the valve

This is a minor release because it changes the contract of the health-restart timer: under confirmed memory
pressure it can now restart the gateway gracefully with runs in flight (mode `notify` by default; `on` is
opt-in). See [ADR-0004](docs/decisions/0004-bounded-graceful-restart-under-memory-pressure.md) and pitfall T41.

### Added

- **A resource-safety layer for the OpenClaw gateway.** `ai-resources setup` now fills, fill-only and hot,
  `mcp.sessionIdleTtlMs=1800000` (evicts idle per-session MCP runtimes, the proven leak) and
  `agents.defaults.timeoutSeconds=14400` (caps a run at 4 h); a value you set is kept, `0` included, and
  `OPENCLAW_RESOURCE_GUARDS=off` (the wizard asks) skips both. The agent rules in the managed `AGENTS.md`
  block cover commands without `run_in_background` and forbid the native AskUserQuestion in headless
  sessions.
- **`ai-resources openclaw pressure [--json]`** (read-only): classifies the gateway as `healthy`, `pressure`,
  `hard`, `frozen`, `refused-probe` or `health-fail` from cgroup and `/proc` files, in two samples, using the
  `memory.events high` delta, never the absolute counter.
- **`ai-resources openclaw graceful-restart [--dry-run] [--reason memory|manual]`**: the one sanctioned
  self-initiated restart. Gates (watchdog.off, unit active, mode, confirmed pressure, 3 h cooldown, 2 per
  day, window `02:00-05:00` America/Guayaquil or above 105 % of `MemoryHigh`), `flock`, never from inside
  the gateway cgroup, `timeout 600 openclaw gateway restart` with watchdog.off held by a signal-safe guard,
  success = health plus a new MainPID, snapshots (0700/0600, bounded) and a recovery report.
- **`ai-resources openclaw restart-report --since <epoch|iso>`** (read-only): counts the restart-recovery
  markers in the gateway journal.
- `status` shows the mode, restarts today, the last restart and the current classification; `verify` warns on
  a failed last restart, a frozen last tick, a stale `watchdog.off`, too many snapshots, guards missing from
  the config and `MemoryHigh` drift.
- `hooks/openclaw_gateway_guard.py` also denies `openclaw gateway restart` from an agent call while the
  gateway is live and `watchdog.off` is absent (T29).

### Changed

- **Policy (ADR-0004, supersedes ADR-0003 decision 7 for the memory trigger only).** The health-restart timer
  can now restart the gateway gracefully with runs in flight, on confirmed memory pressure only. It ships in
  mode `notify` (it tells you once what it would do); set `OPENCLAW_GRACEFUL_RESTART=on` after one
  supervised restart. **Open risk:** whether Slack/Telegram messages were duplicated or lost after the
  2026-10-10 13:20 restart is not verified. Setup itself still never restarts a live gateway on its own
  initiative, and its own restart now holds `watchdog.off` through a guard.
- **Behaviour change: `ai-resources openclaw doctor` refuses a gateway with runs in flight** (exit 6, nothing
  stopped) unless `--even-if-busy`.
- **Behaviour change: `openclaw-health-restart.sh --dry-run` is stricter**: no notice, no state, no episode
  file, no marker.
- The health-restart timer runs every 15 minutes (was hourly) with `TimeoutStartSec=50min` (every phase of a restart is bounded; ADR-0004, Time budget); a frozen gateway
  exits 2 and notifies (retried until delivered) instead of staying quiet.

### Fixed (2.0.2 defects)

- The engine section no longer records "changes" whose value equals the live one, and `apply-pending` drops
  restores the live value already satisfies instead of opening a drained window for a no-op.
- The post-setup watch measures a health baseline before the first write: it is not started when
  `openclaw.json` is byte-identical, and it never reverts when the gateway was already failing (it notifies
  once instead).
- The installed OpenClaw version is read from the package's `package.json` first, so a slow gateway no longer
  turns every key into "restart-required".
- `status` shows a next-run time for timers scheduled on the monotonic clock instead of `-`.

### Not in the kit (documented only)

- `~/.config/environment.d/50-agent-wif.conf` puts the read-only agent identity into every user service, so
  `openclaw-backup-uploader` cannot write the bucket; a local drop-in with `UnsetEnvironment=` is the fix.
- `brew upgrade` of node or openclaw under a running gateway deletes files it is using.
- The orphan-MCP reaper is deferred; its orphanhood proof is recorded in ADR-0004.

## [2.0.2] — 2026-10-10 — setup can no longer take a live gateway down

See [ADR-0003](docs/decisions/0003-setup-never-triggers-a-live-gateway-restart.md) and pitfall T40.

### Fixed

- **`ai-resources setup` restarted a live gateway and cut the agent runs in flight (2026-10-09).** It wrote
  `gateway.bind` over the operator's value; OpenClaw treats `gateway.*` keys as restart-required, defers the
  restart for 300 s (not configurable) and then forces it. Setup now knows which keys restart the gateway (a
  pinned copy of OpenClaw's reload table that fails closed on any other version) and applies those only
  after a separate default-No confirm, while no agent run is in flight, inside a drained window. Unattended
  runs and busy gateways apply nothing restart-required and record it as pending.

### Changed

- **Behaviour change: setup keeps values you set (fill-only).** The OpenClaw host profile now fills keys you
  have not set and keeps a differing value, listing it as `kept your value`. Hosts customised under 2.0.0 are
  no longer made canonical. `~/.openclaw/kit-host-overrides.json5` (`keep` / `force`; a lone `"*"` in
  `force` restores the old behaviour) opts keys back in. An agent on a haiku primary is still replaced.
- The engine section still writes `agents.defaults.model.primary` (a wizard choice) and now lists the keys
  it changed.
- `gateway.bind: tailnet` written by 2.0.0 is offered back (default No, drained window) on the next
  interactive run.
- The health-restart timer is part of the kit (16 to 18 units, 9 to 10 timers). It never forces a restart
  over live runs: with runs in flight, or a probe that cannot tell, it notifies once per episode; an idle
  gateway gets the drained doctor. A hand-installed copy is taken over by interactive setup (files moved to
  `~/.openclaw/backup/hand-units/`, never deleted).

### Added

- `ai-resources openclaw busy` (0 idle, 1 busy, 2 unknown), `apply-pending` (applies deferred
  restart-required keys in a drained window) and `config-watch` (started by setup).
- A post-setup watch (transient `openclaw-config-watch-<id>` unit, 10 minutes): after three failed
  `openclaw health` checks it reverts what the run wrote. Hot keys are reverted at once; restart-required
  keys only inside the drained window. It notifies over Telegram. The unit has `RuntimeMaxSec=1800` and
  `TimeoutStopSec=600`: the drained window alone can take up to ~5.5 min, so the unit must outlive it and
  be given time to unwind on SIGTERM.
- `status` and `verify` list pending restart-required keys and the last watch result.

## [2.0.1] — 2026-10-09 — setup no longer binds the gateway to a tailnet that is not there

### Fixed

- **`ai-resources setup` wrote `gateway.bind: tailnet` on hosts without Tailscale.** The OpenClaw host
  profile binds the gateway to the tailnet, and setup applied it unconditionally. Without Tailscale that
  value cannot be honoured (OpenClaw falls back to loopback), and changing `gateway.bind` forces a gateway
  restart that cut in-flight agent runs. Setup now detects a tailnet (the `tailscale` CLI reports an
  IPv4 address); without one it leaves the operator's `bind` as it is and lists the skip with its reason.
  The rest of the `gateway` block (`publicOrigin`, `trustedProxies`) is still written.

### Notes

- A host that ran 2.0.0 setup may already carry `bind: tailnet`. It keeps working through the loopback
  fallback; to make it explicit, set it with `openclaw config patch` (this restarts the gateway).

## [2.0.0] — 2026-10-09 — models you choose, kept current, and honest about what each cockpit can use

This is a major release: the model layer is no longer Claude-only, the wizard asks its questions in
a different order, and a file the kit writes to your home directory has a new on-disk schema.

### Added

- **`ai-resources models`** with the verbs `status`, `check`, `update`, `approve`, `revoke`, `pin`,
  `unpin`, `exclude` and `rollback`. `update` finds newer models for the providers you enabled and shows
  them, `up to date` rows included, instead of staying silent about slots that have nothing newer. On a
  terminal it asks per model (update now, always, not now, never) and applies the answers to every
  config they affect; the answer is remembered per slot. Without a terminal it never prompts: it records
  the proposals and sends the Telegram notice with the exact command. `--dry-run`, `--check`,
  `--no-restart`, `--review`, `--apply-proposals` and `--slot` are available.
- **Provider adapters** for Anthropic, Google, OpenAI, DeepSeek, Moonshot, Vertex, Ollama and OpenRouter
  (`scripts/ai_resources/model_providers/`): version ordering, family detection, catalogue source and
  smoke path per provider. Discovery runs only for providers you enabled and that have credentials; the
  others are listed with the reason they were skipped. Calling the OpenAI, DeepSeek and Moonshot `/models`
  endpoints is opt-in per provider, and the key only ever travels in the Authorization header.
  Aliases (`-latest`), previews, dated snapshots and unknown families are reported and never applied
  unattended. A model with an unknown price always needs approval; OpenRouter slots use its public
  pricing, cached with a timestamp.
- **Wizard, tools first.** `ai-resources setup` detects and offers to install the base tools (Claude Code,
  Antigravity, OpenClaw, the OpenRouter and LiteLLM gateways), then asks which provider accounts have
  credentials, then lists their models and lets you choose a single model, several models from one
  provider, or several providers. It ends with a per-cockpit summary (configured, through a gateway,
  skipped and why) and one confirmation before anything is written. New flags for non-interactive runs:
  `--shape`, `--providers`, `--models`, `--smoke-path`, `--backend`, `--cockpits`, `--allow-unverified`.
- **Cockpit compatibility matrix** (`setup/compat.py`): cockpit x provider x mode, every cell cited to the
  code. It drives the wizard summary, the apply gate and the generated table in `docs/multi-model.md`.
  A cell nobody has verified behaves as a skip unless you pass `--allow-unverified`.
- **Background updates.** `openclaw-models-update` (wrapper, service and daily timer; `UNIT_NAMES` grows
  from 14 to 16) runs under a lock and a cooldown, smoke-tests a model before switching, takes a backup,
  writes `openclaw.json` only through `openclaw config patch`, restarts the gateway only when no agent run
  is in flight, checks health afterwards and rolls everything back if it fails.
- **One declaration of the pins** (`model_pins.py`) with a host overlay; the effective pin is the higher of
  the kit default and the overlay, so a stale overlay never holds a host behind a newer kit.
- **Re-render from the selection:** `executors.yaml`, `litellm.yaml`, the Claude subagent files, Claude
  settings and `~/.aider.conf.yml` follow the selection through one fan-out, each with a backup and a
  restore in reverse order if any step fails. LiteLLM gets a single health-checked restart.
- ADR-0001 (pins and unattended updates) and ADR-0002 (provider-agnostic selection, honest cockpit gating,
  interactive updates).

### Changed

- **Breaking:** the overlay `~/.config/ai-resources/model-pins.json` is schema 2 (per-slot answers). An
  older file is migrated in place and a `.v1.bak` copy is kept; a kit older than 2.0.0 cannot read the new
  schema.
- **Breaking:** the wizard's step order changes (tools first). `--class` is kept as an alias of `--slot`.
- Claude Code in single-model mode no longer turns a non-Claude pick into a Claude model behind your
  back; if it cannot use the model, setup says so and writes no model setting for it.
- Instruction files only claim gateway routing for the cockpits that are actually wired to one (seven
  cockpits were told otherwise).
- Aider under OpenRouter now gets the `/api/v1` base. The base and the id format come from OpenRouter's
  public documentation and have not been checked against the live service.
- `.claude-plugin/plugin.json` now carries the kit version (it was stuck at 1.2.0) and its license is
  corrected to Apache-2.0, matching `LICENSE` and the Homebrew formula. `test_release_consistency.py`
  enforces both.

### Security

- Files that carry a key are created and kept at `0600`: `~/.aider.conf.yml`, its `.kit-bak` backup, and
  `~/.claude/settings.json` when it holds the OpenRouter key. An existing, wider file is tightened.
- A restore never deletes a file that existed before the kit touched it and was not backed up.
- Keys are never printed or logged; credential detection reads only an allowlist of fields from OpenClaw's
  auth status.

### Notes

- Run `brew upgrade ai-resources` and `ai-resources setup` on each host to install the new timer and
  migrate the overlay. Nothing changes a live model until you approve it or set a slot to "always".
- Not yet verified against live services, so they behave as a skip without `--allow-unverified`: Claude
  Code to non-Claude models through LiteLLM or OpenRouter, and OpenClaw running a native Google model.
- Antigravity (`agy`) and Claude Desktop appear as report-only rows; Vertex is report-only.

## [1.15.0] — 2026-10-07 — the backup guard and uploader run as host timers

### Added

- **`openclaw-backup-guard.sh` and `openclaw-backup-uploader.sh`** in `scripts/openclaw/`, carrying
  the same per-tier freshness/size/checksum checks and upload assertions the old Kubernetes CronJobs
  had, using the same `notify()` failure path as the other host scripts. They replace
  `openclaw-backup-guard`/`openclaw-backup-uploader`, the CronJobs that were deleted with the bithome
  k3s cluster (wildbitca/org-gitops#109): a remote cluster's nodes (e.g. gke-iac) cannot reach a
  standalone host's local disk the way `nodeName` + `hostPath` did on the exact machine that owned
  `/srv/openclaw-backups`.
- **Four systemd units** rendered from `templates/systemd/`
  (`openclaw-backup-guard.{service,timer}`, `openclaw-backup-uploader.{service,timer}`), the same
  convention every other host unit uses. `UNIT_NAMES` grows from 10 to 14.
- **`OPENCLAW_OFFBOX_BUCKET`** in `kit-host.env`; the uploader refuses to run without it (the kit
  never picks an upload destination for an operator).
- `openclaw-verify.sh`'s timer-count check moves from 6 to 8 so a silently-disabled guard or
  uploader timer is still caught.

### Notes

- The old `templates/gitops/openclaw-backups/{guard,uploader}.yaml.template` (the k8s CronJob shape)
  stays in place for hosts that really are cluster nodes, with a note on when they no longer apply.
- Companion change: wildbitca/org-gitops#145 (GCP IAM for the uploader's GCS credential path, and
  retirement of the dead Grafana alerts for the removed CronJobs).

## [1.14.0] — 2026-10-06 — what the 2026-10-06 gateway outage taught the kit

### Added

- **Pitfall T39** in `docs/openclaw/pitfalls.md`: blocked tool calls hold a gateway stop for the whole
  `TimeoutStopSec`, then systemd SIGKILLs every child. Counts are lower bounds (9154 events were dropped),
  the 2026-09-18 bundle records no stalled sessions, and the sender of the first SIGTERM is not established.
- **A kit-block rule for every OpenClaw agent workspace**, `## Long-running commands never block a tool call`:
  work over about 2 minutes runs in the background and is polled, `ask_user` is asked once and the turn
  ends, cron jobs report per phase. `ai-resources verify` warns when a workspace repeats that heading by
  hand outside the kit markers (the kit never edits those bytes).
- **Watchdog alerts.** `openclaw-watchdog.sh` sends a Telegram message on a new
  `stop_shutdown_timeout` stability bundle (the first run records a baseline silently) and when the
  gateway stays in `deactivating` past `OPENCLAW_WATCHDOG_DEACTIVATING_ALERT_SEC` (default 240 s; a 360 s
  threshold would not have fired on 10-06). A failed send is retried on the next tick. The watchdog still
  never starts or stops a unit in transition.
- **A `stability` line in `ai-resources openclaw status`** and a `verify` warning (never an error) for a
  stop in the last 7 days that was held by blocked tool calls, plus a warning when the installed watchdog
  unit does not run the kit script.
- **`openclaw-operations` skill:** diagnosis step for stability bundles, the `gateway_restart_sentinel`
  table (read-only) and cron `tool-execution-started` timeouts.

### Upgrade notes

- Run `ai-resources openclaw install-units --dry-run`, then `ai-resources openclaw install-units`
  (daemon-reload only, no restart, no `--enable`): older hosts run a stale `~/.local/bin` watchdog copy.
- Trim the hand-written long-running sections that `ai-resources verify` reports.
- Send `/new` in live topics so sessions pick up the new block (T19).

## [1.13.0] — 2026-09-29 — the wizard asks when to report a gateway running older plugin code

### Added

- **A wizard question for the stale-plugin report.** 1.11.1 taught the kit to detect that the
  gateway is still running the plugin code it loaded at start, and 1.12.1 made the message name the
  rule that tripped. But the only caller that reports it was gated on `antigravity_applied`, so on a
  host with engine `keep` nothing was ever said. Measured on a live host right after upgrading:
  `stale_reason()` answered `"time"`, the plugin was loaded with both CLI backends registered, and
  `ai-resources doctor` reported nothing.

  Changing that gate changes what every host reports, so the wizard now asks, in the OpenClaw
  cockpit: report it **whenever the kit's plugin is linked** (the default), **only when the kit
  manages the engine** (the behaviour up to 1.12.1), or **never**. The answer is
  `openclaw.stale_plugin_check` (`linked | engine | off`); a state file without the key loads as
  `linked`. The default is safe by the rule the neighbouring host answers follow — its consequence
  is a warning, not an action — but it does add an issue to `ai-resources doctor`, and therefore
  makes it exit non-zero, on a host that was previously silent. The question's help text says so.

### Fixed

- **`"linked"` observes the gateway instead of trusting the state file.** A first attempt gated on
  `s.openclaw.plugin_linked`, which was still inert on the host this was written for: that field
  records that *the kit* linked the plugin, and it is only ever set for the antigravity engine. On
  the measured host it was `False` while `openclaw plugins list` said `enabled` and the runtime said
  `Status: loaded` with `agy-cli` and `claude-kit`. The gate now asks the runtime the module already
  queries, so no new probe and no new subprocess.

  This is the third time in one day that a flag recording what the kit DID was used to decide
  something about what IS true — after `antigravity_applied` and the path comparison in
  `plugin_is_stale`. A gate that must reflect the live system has to observe the live system.

### Notes

- `"off"` silences only the staleness report. The antigravity quota check still runs exactly when
  it did before, whatever the answer.
- A missing `agy-cli` backend is still reported for antigravity only: with engine `keep` a backend
  the host never asked for is not a defect.
- The check costs a gateway round-trip (measured at ~9s on a busy host) and fails open: when the
  runtime cannot be read it reports nothing rather than guessing, so a transient failure reads as
  "not stale". That is the same direction every probe in this module takes.

## [1.12.1] — 2026-09-29 — five findings from an independent review of 1.11.0–1.12.0

An independent review of the day's three commits found two medium and three low defects. Both
mediums were reproduced before being fixed, and one of them was a regression introduced by the
previous round's own fix.

### Fixed

- **A dead restart guard was silent again** (`openclaw_host.py`). `_unit_pids` walked the unit's
  cgroup with a bare `os.walk`, whose default `onerror=None` swallows a missing or unreadable
  directory, so the walk yielded nothing and the diagnostic line added in 1.11.0 — which lives in
  the `cgroup.procs` read's error branch — was never reached. On a cgroup v1 or hybrid host
  `gateway_busy()` therefore answered `(0, [])` with no trace at all, and a `brew upgrade` would
  kill every agent turn in flight without a prompt: exactly the regression the guard exists to
  prevent. The walk now checks the directory and reports through `out`, and still fails open.
- **Teardown after a deferral left half the engine behind.** 1.11.1 made the nothing-applied early
  return unlink the plugin, but it still left `antigravity_applied` True and never unregistered the
  agy MCP bridge — which `_register_plugin_and_backends()` registers *before* it reaches the
  restart. Since `doctor.py` gates its engine section on that flag, `ai-resources doctor` warned
  "OpenClaw has not loaded the ai-resources plugin" on every run after a clean teardown, and a
  second teardown took the same early return and skipped the bridge again. The branch now
  unregisters the bridge under the normal path's condition, clears the marker and resets the flag;
  a second teardown is a no-op, and a bridge the user owned is still left alone.
- **A pid listed twice in one `cgroup.procs` was counted twice.** `pids += [... if int(p) not in
  pids]` evaluates the comprehension before `+=` mutates the list, so the guard deduplicated only
  across cgroup directories. A fork racing the read can list a pid twice, and the prompt would then
  tell the operator a restart kills more turns than exist.
- **A DST-ambiguous start time could hide a stale plugin.** `gateway_started_at()` parsed systemd's
  localised timestamp and fell back to `time.mktime()` with `tm_isdst = -1` for any non-UTC zone, so
  during an autumn fall-back hour the result could be an hour out — and an hour out in one direction
  reproduces precisely the false negative 1.11.1 was written to fix. It now asks systemd for
  `--timestamp=unix` and accepts only `@<digits>`; anything else returns None and the path rule
  decides alone.
- **The staleness warning contradicted its own evidence.** `plugin_is_stale()` returns True for two
  different reasons, but the doctor and verify messages both said the gateway runs the plugin "from
  an older kit directory" — and when the *time* rule fires the paths are identical, so an operator
  who checked would find them equal and reasonably dismiss the warning. A new `stale_reason()`
  reports which rule tripped (`"path"`, `"time"` or None); `plugin_is_stale()` is a bool wrapper
  over it, so existing callers are unaffected, and each rule is now worded for what it means.

### Notes

- `--timestamp=unix` needs a systemd that supports it. Where it does not, the time rule is simply
  unavailable and detection falls back to the path comparison — the same behaviour as before 1.11.1,
  and the safe direction.

## [1.12.0] — 2026-09-29 — the Cloudflare `cf` CLI skill

### Added

- **`skills/cloudflare-cf-cli`**: prefer Cloudflare's agent-first `cf` CLI over raw `curl` against
  the Cloudflare API or Wrangler for account, zone, DNS, Workers, D1, R2, KV and Access work. It
  covers the whole API (~3,000 operations against Wrangler's ~280) and defaults to condensed JSON,
  which is the actual reason to prefer it: it costs fewer tokens on every call. The skill also
  pins the safety rules that matter — discover commands with `cf cli search` instead of chaining
  `--help` guesses, keep those queries anonymous (no names, domains, IDs or tokens), request a
  scoped token rather than reusing a production one from a project's `.env`, and never hand-edit
  DNS records that this repo already declares as IaC through Crossplane.

### Changed

- `skills-index.json` regenerated (139 skills), and the `.skill-source.yaml` banner of the 24
  vendored `gpm-*` skills updated to the wording `ai-resources generate` emits today. No skill
  content changed: every `git_revision` is untouched. The churn lands here rather than reappearing
  on the next `generate`.

## [1.11.1] — 2026-09-29 — the kit notices that the gateway runs an older kit

### Fixed

- **`plugin_is_stale()` could not see a `brew upgrade`.** It decided staleness by comparing the
  gateway's runtime plugin root with the expected one, both resolved. But setup links the plugin
  through the version-independent `opt/` path on purpose (`stable_kit_root()`, "so links written by
  setup keep resolving after `brew upgrade`"), and that path does not change across an upgrade — it
  simply resolves to the new Cellar directory. Both sides compared equal, so the doctor's warning
  ("running the ai-resources plugin from an older kit directory. Run: `openclaw gateway restart`")
  never fired. The two intentions contradicted each other: the link is stable by design, so
  staleness could not be detected from the path by design.

  Measured on a live host upgrading 1.10.2 → 1.11.0: the gateway kept its pid and its in-memory
  plugin code from the old version, `plugin_is_stale()` returned False, the agy-cli backend was
  registered, and `ai-resources doctor` reported the plugin correctly loaded. `brew upgrade` has no
  `post_install`, so nothing restarts the gateway either — the restart is the only way the new code
  is loaded, and nothing asked for it.

  The path rule stays and a time rule joins it: also stale when the installed kit is newer than the
  running gateway, read from the unit's start timestamp against `_installed_at()`. Any unreadable
  timestamp falls back to the path rule alone and never invents staleness.
- **`_installed_at()` looks at the install roots, not at the files.** Homebrew preserves the source
  mtimes of everything it installs, so per-file and per-subdirectory mtimes are useless as an
  install marker: on the measured host the plugin directory read 25 September and its `index.js`
  17 September, both older than the gateway. A first attempt compared the plugin directory and
  reported "not stale" on exactly the case it was written for. Two directories are checked, and
  neither is redundant: the kit root is fresh only on a **source build**, because the formula
  creates the venv inside `libexec` after the copy; on a **poured bottle** it carries the build
  machine's time, and only the version directory stays fresh, because brew writes
  `INSTALL_RECEIPT.json` into it. Dropping the version-directory branch brings the false negative
  back on every bottle install, which is what most machines get. Both branches now have a test.
- **Teardown left the plugin linked after a deferred restart.** A deferral leaves `applied` False
  with `plugin_linked` True, so `teardown()` took its nothing-applied early return and never
  unlinked: the kit was torn down while the gateway kept loading its plugin.
- **`setup --dry-run` reported a change it would not make.** It displayed `AGENT_SKILLS_ROOT` as the
  resolved Cellar path and labelled it `(update)`, while a real run writes the stable `opt/` path,
  unchanged. A dry run that reports phantom changes is worse than none: it is the one output an
  operator reads to decide whether applying is safe.

### Notes

- Accepted false positives, documented at `_installed_at()`: `brew pin` and `brew link --overwrite`
  rewrite the receipt and move the version directory's mtime without changing any code, and a repo
  checkout trips the rule when a top-level entry of the kit root is added or removed. Both fail
  toward telling the operator to restart, which is recoverable — and `configure()` acts on a true
  result by restarting the gateway behind the 1.11.0 busy guard, which is stated where it is read.

## [1.11.0] — 2026-09-29 — the gateway restart asks before it kills an agent turn

### Added

- **`ai-resources setup` no longer restarts the OpenClaw gateway out from under live work.**
  `restart_gateway()` ran `openclaw gateway restart` unconditionally, and its only caller reaches
  it exactly when the plugin directory is stale — which is what `brew upgrade` causes, because it
  moves the kit while the running gateway holds the previous path. So the ordinary upgrade killed
  every agent turn in flight, with no warning and no way to say "later". Measured on a real host:
  nine agent workers alive, one over an hour into its turn.
- **`gateway_busy()` (`openclaw_host.py`)**: a pre-restart probe beside the drained doctor it
  mirrors, sharing its unit constant, runner indirection and env handling. It resolves the unit's
  cgroup from `systemctl show -p ControlGroup`, walks that tree's `cgroup.procs`, and counts only
  processes whose `argv[0]` basename is a known agent CLI — never the gateway's own node workers.
  Only the CLI name is kept, never argv: a worker's command line carries tool allow-lists and
  config paths, and none of it reaches the prompt, the logs or the state file.
- **Three answers when turns are in flight**: restart now (the prompt names what it kills), defer,
  or wait — which polls until idle and then restarts, under a 30-minute cap after which it defers.
  An interrupted wait defers and never restarts: whoever pressed Ctrl-C wanted out.
- Without a terminal the answer is always defer. Killing unattended work to finish a setup is the
  worse trade.

### Fixed

- **A deferral can no longer silence itself.** It is recorded as `gateway_restart_pending`, makes
  the engine step fail, raises a `verify` error naming the command, and shows a PENDING block in
  `ai-resources openclaw status` until the unit's MainPID changes. An empty recorded pid, an
  unreadable current pid and a malformed (non-mapping) record all keep it reported — a corrupt
  record must not be read as "nothing is owed". Teardown discharges it, so tearing the kit down
  never leaves a demand to restart a gateway for a plugin the kit no longer registers.

### Notes

- The probe fails open by design: any error means "not busy", because a guard that blocks an
  upgrade when it cannot read `/proc` is worse than no guard. It now leaves one quiet line when it
  does, so a dead guard is distinguishable from an idle host.
- Accepted limits, documented in the code: a turn that starts between the last poll and the
  restart is still killed; the cap is a constant; and an unattended upgrade on an always-busy host
  defers on every run.

## [1.10.4] — 2026-09-29 — the orphan recovery also covers the engine sections

### Fixed

- **`CURRENT_KIT_HEADINGS` only covered the common block.** 1.10.3 added `Worktrees`, but the OpenClaw engine
  part and the voice rule land between the *same* managed markers, and their four headings (`Memory`,
  `Orchestration`, `Voice`, `Voice notes`) were still unknown to the recovery. A block that lost its END
  marker was therefore cut at the first of them and the rest stranded outside the pair — the same defect
  1.10.3 fixed, one renderer further along. The frozenset now lists every heading any renderer that feeds
  the managed pair emits, and a comment says so, so the next renderer added is an obvious edit here too.

### Tests

- `test_every_heading_of_the_rendered_kit_blocks_is_known_to_the_orphan_recovery` now also feeds
  `MEMORY_MD`, `voice.VOICE_MD` and `_antigravity_agents_md`; against the 1.10.3 frozenset it fails naming
  `'Memory'`, which is how the gap was found.
- `test_a_block_whose_end_marker_is_gone_is_rebuilt_without_stranding_kit_sections` deletes the END marker
  from each of the four block shapes (common, antigravity+kit, with-voice, direct) and asserts one marker
  pair, no kit heading left outside it, and the hand-written tail kept.

## [1.10.3] — 2026-09-29 — the orphan recovery knows every kit heading, and this repo commits its own block

### Fixed

- **`CURRENT_KIT_HEADINGS` lacked `Worktrees`.** When the END marker of a kit block was deleted, the recovery
  cut the block at the first heading it did not recognise, so the `## Worktrees` section (added in 1.9.9) was
  left stranded outside the markers. The frozenset now lists it.
- **`docs/orchestration.md`: the subagent return format is back inside its fence.** The opening fence swallowed
  `## OpenClaw agent workspaces` and the Worktrees section, and the Result / Artifacts / Routing content was
  stranded as top-level headings further down. One fence pair now surrounds the return format.

### Added

- **The kit block of this repo's own `AGENTS.md` is committed.** This repo is the workspace of the `ai` agent,
  which delegates, so setup writes the full block there. Excluding it would make the kit work in every
  workspace but its own and leave the `ai` agent as the only one without the kit's instructions. The block
  was regenerated with the installed kit and a second `ai-resources setup --non-interactive` left the file
  byte-identical (determinism measured, not asserted).

### Tests

- `test_every_heading_of_the_rendered_kit_blocks_is_known_to_the_orphan_recovery` pins the frozenset to every
  `## ` heading the kit renderers emit; it fails naming `Worktrees` without the fix.
- `test_two_spellings_of_one_workspace_are_written_once` and
  `test_workspace_targets_resolve_spellings_to_one_key`: a symlinked or dotted spelling of a workspace is still
  one target. Both fail against a `_workspace_targets` that keys by the unresolved path, which no other test does.
- `tests/test_repo_agents_md_block.py`: the committed block equals what the renderer produces. It reads the kit
  root from the block itself, so it stays green on any machine, and it fails when one word of the block changes.

### Chore

- `.openclaw-cli-images/` is git-ignored: the openclaw CLI drops received images in the cwd.

### Host documentation (no code change)

- Four operator-written `AGENTS.md` files documented Telegram topics of a forum that no longer exists. They
  were corrected by hand, outside the kit markers. The kit deliberately does not automate this because it
  never writes outside its markers. If a group is added later without updating that prose, the `verify`
  warning `documents routing the live config does not have` fires again **by design**; it is not noise.

## [1.10.2] — 2026-09-28 — the test suite no longer strips the block from the live main workspace

### Fixed

- **Root cause of the "kit block missing from `main`'s `AGENTS.md`" regression (T36).** `tests/conftest.py`
  redirected `claude.CONFIG_ROOT` and the host env paths but not `openclaw.CONFIG_ROOT`. Teardown tests whose
  config names no `agents.defaults.workspace` therefore resolved to the operator's real
  `~/.openclaw/workspace` and removed the kit block from the live file on every `pytest tests/` run. The
  v1.9.7 report said the block was fixed; it had been written correctly and was undone by the suite.
  conftest now isolates `openclaw.CONFIG_ROOT` for every test, and `tests/test_test_isolation.py` pins it.
  After the fix, a full run leaves the live OpenClaw files unchanged (verified against the host).
- This corrects the 1.10.0 notes (and T36's first draft), which attributed the drift to an agent rewriting
  its own file: that was a hypothesis, and it was wrong. `ai-resources verify` still reports the drop, whatever
  its cause.

## [1.10.1] — 2026-09-28 — an unfilled IDENTITY.md template is no name

### Fixed

- `ai-resources openclaw status` (identity section) and the shared-identity finding of `ai-resources verify`
  printed `**` as the name held by `IDENTITY.md` in a workspace whose file still carries the unfilled
  template line `- **Name:**`. The line is now matched on a single line and an empty value reads as no name.
  Found by running 1.10.0 against the live host after it shipped.

## [1.10.0] — 2026-09-28 — setup now ends by verifying every selected tooling

`ai-resources setup` used to end in a state it had **applied**. It now ends in one it has **verified**:
a read-only check per selected tooling, reachable from three doors that share one implementation.

### Behaviour change: new non-zero exits

**`ai-resources setup`, `ai-resources verify` and `ai-resources doctor` now exit 1 when a verification
finding is an `error`.** They never exit non-zero for a `warn`. Automation that assumed setup always
exits 0 after a successful apply must tolerate this. Only genuinely broken states are errors: a recorded
managed block now missing or duplicated, the gateway unit dead or disabled, the gateway up but not
answering, the `main` catch-all binding not last, the kit plugin linked but not loaded.

### Added

- **`scripts/ai_resources/verify.py`**: `Finding(level, cockpit, message, remedy)` and `run_all(state,
  cockpit_ids)`. A cockpit module may expose an optional `verify(ctx)`; the others get a generic check of
  the managed block and config directory the state file recorded. `run_all` never raises: a cockpit whose
  verify blows up becomes one error naming it.
- **Wizard step 10, Verification** (`TOTAL_STEPS` 9 to 10): verifies exactly the cockpits step 9 applied,
  one line and remedy per finding, closing with all clear or `N of M selected toolings did not reach the
  expected state`. A dry run prints the header with a skip notice and probes nothing.
- **`ai-resources verify [--json] [--cockpit ID]`**, and doctor section 4 folds in the same findings
  (error-level ones only count as issues; doctor still has six sections).
- **openclaw verify**: a recorded kit block that is gone or duplicated (error), routing documented outside
  the markers that the live config lacks (warn), agents sharing a workspace that inherit `IDENTITY.md`'s
  name (warn), string-form models (warn), the catch-all binding order (error), the gateway unit, health,
  timers, backups, listeners and the off-box gap (the literal `OPENCLAW_OFFBOX_LIST_CMD` line; the kit
  chooses no destination). A timeout is "could not measure", never an error. **claude verify**: the
  `CLAUDE.md` block, `settings.json` and the recorded scripts and subagents; it judges no model or
  permission setting.
- **`ai-resources openclaw status` identity section**: each agent's `identity.name`, workspace and the
  name `IDENTITY.md` holds there, with the `openclaw config set ... identity.name ... --dry-run` remedy.
- Setup warns (naming the path and the agents) when an agent workspace directory does not exist, instead
  of skipping it silently. It does not create the directory.
- Pitfalls T36, T37, T38; the runbook's Verification section.

### Decisions (D1-D7 of the phase-E plan) and why

- **D1** One normalizer pair (`model_spec`, `model_primary`) for every read of `model`: the schema allows
  both spellings and `openclaw agents add --model` writes the short one, so a reader must be total.
- **D2** Tolerant read, loud report, never a rewrite. Hand-editing `openclaw.json` is forbidden by the
  host rules and no agent's `model.primary` may change; the remedy is printed for the operator.
- **D3** E3 fixed in three layers (shipped in 1.9.10): resolve the binary, name the budget (600 s), tell
  timeout from missing from failed.
- **D4** One implementation, three doors (wizard, doctor, `verify`): no duplicated logic, and a
  run-on-demand command answers "setup applied but never verified for weeks".
- **D5** Verify never writes: no repair flag, no config write, no `IDENTITY.md` write. The repair for a
  dropped block is `ai-resources setup`, which already refreshes the block on every run.
- **D6** E4 (shared identity) and E5 (no off-box copy) are visibility, not enforcement. The kit will not
  split a shared workspace, invent an identity or pick a backup destination.
- **D7** Severity gates the exit code: only `error` is non-zero.

### Findings recorded so the next reader does not re-chase them

- The E3 cause reported at the time (a 120 s doctor timeout) did **not** reproduce: measured 29 s, rc 0.
  What was fixed instead is the unresolved binary (rc 127) and the collapsed timeout/OSError return codes
  (1.9.10).
- The E1 regression (the kit block missing from `main`'s `AGENTS.md`) was not a targeting skip:
  `_workspace_targets` keys the default and `main` workspace once and setup wrote the block; the file was
  replaced wholesale afterwards. Regression tests pin the writer; verify now detects the drift.

## [1.9.10] — 2026-09-28 — status survives the short model form

`ai-resources openclaw status` no longer crashes on a `model` written as a string, no longer says
"doctor could not run" for four different reasons, and no longer reports the `claude-kit` model probe as
a warning. Only `openclaw_host.py` and its tests change.

### Fixed

- **Short-form `model`.** The OpenClaw schema is `anyOf: [string, {primary, fallbacks}]` and
  `openclaw agents add --model` writes the string. `expand_wildcards` and `collect_status` assumed the
  object and raised `AttributeError`. `model_spec` / `model_primary` now read both spellings.
  The kit never rewrites the operator's config: status lists every short-form agent with the exact
  `openclaw config set agents.entries.<id>.model.primary <ref>` remedy and still exits 0.
- **Status resolves the `openclaw` binary.** `collect_status` called a bare `openclaw` for `health` and
  `doctor`, which is rc 127 in any PATH without brew. Both now use `resolve_openclaw_bin()`.
- **Doctor says why it failed.** `default_runner` returns rc 124 for a timeout only (OSError stays 1;
  every caller tested `rc == 0` only). `report["doctor"]["status"]` is `ok`, `timeout`, `missing` or
  `failed` (the legacy `ran` key stays) and each renders its own line. The budget is one constant,
  `DOCTOR_TIMEOUT = 600`, shared with the drained `doctor --fix`.
- **`Unknown model: claude-kit/...` is known noise.** `claude-kit` is a CLI backend, not a catalogue
  provider. One entry anchored to the `claude-kit/` prefix; `Unknown model: anthropic/...` stays signal.
  A `fix:` remedy line now follows the noise entry it belongs to.

### Added

- `ai-resources openclaw status --no-doctor`: skips the doctor probe and issues no doctor argv.

### Notes

- The reported cause of "could not run" (a 120s timeout) did **not** reproduce: on bithome
  `openclaw doctor --non-interactive` is rc 0 in about 29s. The unresolved binary (rc 127) was the
  reproducible cause; 600s is insurance.
- Regression tests pin that the orchestrator's default workspace and `main`'s are one target with one
  marker pair (the v1.9.7 block writer is correct; a dropped block is post-apply drift).

## [1.9.9] — 2026-09-28 — the per-agent worktree flow, delegated to OpenClaw

Each agent works on its own branch in a managed worktree. The kit teaches the flow and builds none of it.

### Decisions (ratified by the operator, gate D1)

- `worktreeRoot` is **not** pinned in the config. The real path was measured with one real
  `openclaw worktrees create` against a throwaway repository on openclaw 2026.9.6 (`worktreeRoot`
  unset): **`~/.openclaw/worktrees/<repoFingerprint>/<name>`**, branch `openclaw/<name>`. The kit only
  documents it, in the workspace block and in `skills/using-git-worktrees/SKILL.md`.
- The kit reimplements no creation, snapshot or GC: it delegates to
  `openclaw worktrees create|list|remove|restore|gc`.
- Hard rule, written with its reason: never a worktree, a virtualenv or any scratch you need again under
  `/tmp`. It is a tmpfs held in RAM and emptied on every reboot; that is how `/tmp/kitvenv` and the
  `/tmp/wt-*` worktrees died (T26 is the same tmpfs failure).
- Open question resolved: `claude` and `security` keep sharing `~/Development`; the v1.9.6 tie-break
  resolves narration to `security` and the v1.9.7 path dedupe resolves the file.

### Added

- A `## Worktrees` section in the kit block every agent workspace gets (also the `claude` worker), the
  `openclaw worktrees` subcommands, the `using-git-worktrees` skill and the durable root.
- The same section in the repo and umbrella `AGENTS.md` templates.
- `skills/using-git-worktrees/SKILL.md`: an OpenClaw-managed section with the commands, the measured path,
  the never-under-`/tmp` rule and its reason; the raw `git worktree` commands stay as the fallback outside
  OpenClaw.
- `docs/orchestration.md`: the worktree flow.

### Upgrade note

The kit block changes, so the next `ai-resources setup` refreshes it in every agent workspace (only the
marked block; text outside the markers is untouched).

## [1.9.8] — 2026-09-28 — the wizard maintains the Telegram group routing

Routing a basic group to its agent lives in the root `bindings` array, and until now nothing in the kit
wrote it: a second machine reached parity only by hand-editing JSON. The wizard now asks and maintains
it, without ever touching `channels`.

### Decisions (ratified by the operator, gate C1)

- `assert_channels_safe()` and the test that pins it are **unchanged**. The setup writes only the root
  `bindings` array and never `channels.telegram.*`.
- When a routed group is missing from `channels.telegram.groups` under `groupPolicy: "allowlist"`
  (Telegram drops it with no log line), the setup reports it loudly with the exact
  `openclaw config set ... --strict-json --merge` command for the operator to run. Smallest blast
  radius; no justification needed for changing the invariant.

### Added

- One wizard question per routed agent (every agent except `main` and the `claude` worker): its
  Telegram group chat id, pre-filled from the live `bindings` (a re-run is Enter, Enter), validated as
  `-<digits>` with the `-5xxxxxxxxx` shape in the message, warned (not rejected) on `-100...`, empty
  leaves the binding alone, nothing asked without a terminal. Answers are saved in the setup state.
- `openclaw_host.build_bindings()`: rebuilds the array from the live one, upserting only what the kit
  owns (matched on `agentId` plus `match.peer.id`), keeping the operator's `comment` and every entry the
  kit does not own verbatim, and keeping peer-less entries (main's catch-all: no `type`, no `peer`) last.
  Sent whole with `--replace-path bindings`, validated with `--dry-run` first (a passing dry-run is not
  evidence of routing: T35) and applied after its own confirm.
- The pre-kit array is snapshotted in the setup state and put back by teardown, unless the operator
  changed the array since (then it is left alone with a warning).
- The allowlist gap report (`allowlist_gaps()`, `allowlist_fix_command()`), naming the chat id and T35.
- `tests/fixtures/openclaw_bindings.py`: the one shared bindings shape. The hook tests and the cockpit
  tests both use it, and a round-trip test proves that what the cockpit writes is what
  `resolve_target()` reads (the hook is stdlib-only and cannot import the cockpit).
- Docs: the group-routing sequence in `docs/orchestration.md`, the new wizard question in the runbook,
  the setup's behaviour in T35.

### Changed

- The host-wizard test's question count went from 12 to 12 plus one group question per routed agent,
  and its scripted answers gained an empty group-id answer. No assertion was loosened.

## [1.9.7] — 2026-09-28 — every OpenClaw agent workspace learns the kit

None of the six agent workspaces taught its agent that the kit exists: `sessions_spawn` appeared zero
times in all six `AGENTS.md` files and `~/Development` had no kit block at all. Three causes, each
fixed: `_configure_agents_md()` returned early for any workspace whose `AGENTS.md` existed and skipped the
engine-owned `claude` entry; `_write_workspace_block()` emitted the delegation text only on the
antigravity branch (the stored engine is `keep`); and `~/Development` is one file serving two agents
(`claude`, `security`).

### Decisions (ratified by the operator, gate B1)

- The kit block is **engine independent**. "Skip if `AGENTS.md` exists" became "refresh only the marked
  block", never touching content outside the markers.
- Workspaces are **deduplicated by resolved path**, so `~/Development` is written once.
- `claude` is **excluded from the delegation paragraph** (it is the worker and does not delegate to
  itself); it receives the rest of the block. The other agents carry the paragraph.
- `claude` and `security` keep sharing `~/Development` (no workspace split): the v1.9.6 tie-break already
  resolves narration to `security`, and the path dedupe resolves the file.

### Added

- `openclaw_agent_kit_md()`: the agent-agnostic block (kit-orchestration, `workflow-*`, the kit roles and
  no self sign-off, the handoff file, delegation through `sessions_spawn`).
- `openclaw._configure_workspace_blocks()`: refreshes the block in every agent workspace on every run,
  through the single existing writer `_write_workspace_block()` (one marker pair per file, also for the
  orchestrator's engine part), honouring `--dry-run` and recording each path for teardown
  (`agent_kit_blocks` in the setup state).
- `docs/orchestration.md`: the OpenClaw agent workspace rules.

### Changed

- The three workspace templates no longer teach forum-topic routing: the orchestrator template describes
  one basic group per agent, the root `bindings` array and the silent traps of T35. `TOPIC-ROUTING.md` is
  kept only as a legacy pointer for forum hosts.
- `write_managed_block()` keeps every byte of a file it prepends to (trailing newlines included), so
  `remove_managed_block()` gives the original back exactly.

### Upgrade note

The **first** real run after upgrading refreshes a managed block into your hand-shaped `AGENTS.md` files:
expect the block at the top of each one. Text outside the markers is not modified. The block is written
even when the OpenClaw host section was never answered and the engine is `keep`.

## [1.9.6] — 2026-09-28 — team narration reaches a basic-group host again

Migrating a Telegram team from one forum (topics) to one basic group per agent broke narration for
every agent, silently: `resolve_target()` only ever read `channels.telegram.groups[chat].topics[thread]`
for a numeric thread id, and a basic-group host routes through the root `bindings` array instead, which
the hook never looked at. Live regression, all agents mute — caught by resolving against the real host
config for all five group-routed agents and finding `None` for every one of them.

### Fixed

- `resolve_target()` in `hooks/openclaw_team_progress.py` now tries the root `bindings` array first
  (`match.channel == "telegram"` and `match.peer.kind == "group"` and `agentId == agent` →
  `(peer.id, None)`), then falls back to the forum topic scan unchanged. It filters on
  `match.peer.kind`, never on `type`: the catch-all binding (`agentId: "main"`,
  `match.accountId: "*"`) has no `type` and no `peer`, so it can never be mistaken for a target.
- `_agent_for()` left an exact-length workspace tie to dict iteration order. On the live host
  `claude` and `security` both set `~/Development`, `claude` is listed first and has no binding, so
  that tree resolved to nothing. Among tied candidates the bound one now wins; any further tie breaks
  on sorted agent id, so the choice is stable across dict orderings. A single match is unaffected.
- A group-mode target carries no thread (`(chatId, None)`). `launch_watcher()` and `_send()` in the
  hook, and `send()`/`edit()` in `scripts/openclaw/openclaw-team-watch.py`, now omit
  `--thread`/`--thread-id` entirely instead of passing `None` into `Popen`'s argv (which raised
  `TypeError`, swallowed silently by the hook's top-level catch-all).
  `scripts/openclaw/openclaw-team-send.py` was already thread-optional; pinned with a test.

### Added

- A bindings-mode test fixture (five basic-group ids, forum-dead `topics: {"*": ...}`, the
  peer-less catch-all last) alongside the existing forum fixture.
- Regression coverage: the group-bindings routing path, the binding-over-topic precedence, the
  catch-all never becoming a target, malformed/missing bindings resolving to `None` rather than
  raising, the exact-length tie (both orderings), and the argv omission for group-mode sends.
- `docs/openclaw/pitfalls.md` T35: the four traps behind this regression, none of which log — a
  green `config patch --dry-run`, the `-100...` supergroup id that silently kills a binding,
  `groupPolicy: allowlist` dropping a group with the `channel_ingress_events(status=completed,
  attempts=0)` signature, and an array-replace patch on `bindings` losing the catch-all if it is not
  re-appended last. Cross-linked from T31.

### Operator action

`brew upgrade ai-resources` and restart the agent session (the hook is loaded once per session). No
`openclaw setup` step is required for this fix — it changes only the hook and watcher scripts.

## [1.9.5] — 2026-09-25 — the Workshop fix actually reaches the hosts that need it

1.9.4 authored `skills.workshop.autonomous.mode` inside `_build_antigravity_patch`, and that patch is
never rebuilt on the hosts that need the fix most: `_configure_engine` returns early when the saved
engine answer is `keep`, which is what a host the kit already configured reports on every re-run. So
`ai-resources setup` left those hosts on OpenClaw's `auto` default and the unschedulable
`skill-collection-review-<agent>` job stayed. Caught by checking the live state on `bithome`
(`engine: keep`) instead of trusting the code path.

### Fixed

- The Workshop mode is now authored by `_configure_skill_workshop`, a host-level step in
  `configure()` that runs whatever the engine answer is. It is written only when the key is unset
  **and** the kit manages that host's OpenClaw (`openclaw.applied`), so a run that configures
  nothing but voice — or declines every OpenClaw section — still leaves `openclaw.json` untouched.

### Added

- A regression test that a host with `engine: keep` gets the Workshop mode, and one that a host the
  kit does not manage does not.
- `_Recorder.engine_patches()` in the cockpit tests: six assertions counted *all* config patches,
  which stopped being a statement about the engine once a second host-level patch existed.

### Operator action

Same as 1.9.4 — `brew upgrade ai-resources && ai-resources setup`. On 1.9.4 that command was a no-op
for a `keep` host; on 1.9.5 it writes the key.

## [1.9.4] — 2026-09-25 — the weekly Skill Workshop review stops failing forever

OpenClaw's Skill Workshop defaults to `auto`, and `auto` registers a weekly
`skill-collection-review-<agent>` job that rewrites the operator's skill files unsupervised. On a kit host
that job can never run: it needs a rooted runtime, and the kit's `claude` worker is bound to `claude-kit`,
which is unrestricted by design. Nobody chose either behaviour — both came from an inherited default.

### Fixed

- The setup now authors `skills.workshop.autonomous.mode = "propose"` instead of inheriting OpenClaw's
  `auto`. This stops `skill-collection-review-<agent>` from being registered, so the job that sat at
  `status: error (2x)` with `CLI backend "claude-kit" does not declare instruction isolation with exact
  tools` (job `8af32147-cb72-40c4-a9a1-68eeb104ce92`, openclaw `2026.9.6`) no longer exists rather than
  failing every 7 days. It is written **only when the host left the key unset**: an operator who authored a
  mode owns that decision, including `auto`.
- `npm test` in `openclaw-plugin/ai-resources` ran `node --test test/`, which fails on Node 24 with the
  directory form. Now `node --test test/*.test.mjs`. The suite was never run by CI, so this was broken only
  for whoever verified locally.
- `tests/test_openclaw_team_resilience.py` read the operator's real `~/.openclaw/kit-host.env`: the fixture
  isolated `STATE_DIR` and `LOG_PAYLOADS` but not `KIT_HOST_ENV`, so on a host with
  `OPENCLAW_NARRATION_LANG=es` the rate-limit notice came out in Spanish and two English-string assertions
  failed. Green in CI, red on the operator's own machine. The fixture now points the key at a missing file,
  pinning the `en` default.

### Changed

- `claude-kit` is untouched: `bundleMcp: false`, `--dangerously-skip-permissions` and
  `FORBIDDEN_CLAUDE_FLAGS` are byte-identical. The `claude` agent's `model.primary` is untouched too —
  repointing it at a mediated provider would have fixed the job by moving the operator's traffic off their
  Claude CLI subscription onto a metered API, which is not the kit's decision to make.

### Added

- A regression test pinning that `buildClaudeBackend()` does **not** declare
  `isolatesInstructionsWithExactTools`, with the reason in the test: declaring it is a false claim about a
  security property, and the gate's next condition would reject it anyway for `bundleMcp`.
- `docs/openclaw/pitfalls.md` T34 — the full analysis, the measured dead ends, and why the flag is never the
  fix.
- `docs/openclaw/upstream/skill-collection-review-eligibility.md` — the upstream report: OpenClaw's
  eligibility projection (`isCliProvider`) is coarser than its own enforcement
  (`isolatesInstructionsWithExactTools && bundleMcp`), so it schedules a job it will always refuse and never
  auto-disables it.

### Operator action

```
brew upgrade ai-resources && ai-resources setup
```

On a host that already ran with `auto`, setup writes the key on the next run. To apply it without waiting:
`openclaw config set skills.workshop.autonomous.mode propose` — it takes effect without restarting the
gateway.

## [1.9.3] — 2026-09-24 — team narration can speak Spanish

A host whose operator reads Spanish had to choose between the hand-installed team hook (Spanish, no
kit) and the kit's (English, everything else). Now the kit speaks both.

### Added

- `OPENCLAW_NARRATION_LANG=en|es` in `~/.openclaw/kit-host.env`. It applies to everything the team hook
  and its live messages write into Telegram: the request, the team start, each hand-off, the workflow
  banner, the turn close, edits, the pulse, the rate-window notice and the member's live message (the hook
  hands the language to the watcher with `--lang`). Absent or any other value means English, so nothing
  changes for existing hosts. The Spanish strings are written as escapes: the source stays ASCII.
- The key is documented in `openclaw-host.env.example`. `write_host_env` keeps lines it does not manage, so
  `ai-resources setup` does not drop it; the wizard does not ask for it yet.

## [1.9.2] — 2026-09-24 — team narration stops fighting Telegram's rate limit, and stops losing messages

A forum group and all its topics share one Telegram budget (about 20 messages a minute, edits included).
The live-member watchers, the narration hook and the gateway's own streaming drafts were drawing on it
together; the gateway answers a 429 by waiting (calls took up to 76 s), and the narration failed in silence
(pitfall T33).

### Added

- `scripts/openclaw/openclaw-team-send.py`: the delivery queue the hook and the watcher now use.
  - One FIFO queue per chat, kept on disk and shared by every process on the host, so milestones keep their
    order and callers stop waking into the same limit together.
  - Pacing: at least 4 s between two calls to one chat, more after a 429 (it waits the `retry after N` Telegram
    asked for) and after a slow call.
  - Retries: a 429 waits and retries; a transient failure retries with a growing back-off; `message is not
    modified` counts as delivered; a permanent error stops at once.
  - Nothing is lost silently: every rate limit, retry, slow call and failure is a line in
    `~/.openclaw/logs/team-outbox.log`, and a send that could not be delivered is kept in
    `~/.openclaw/logs/team-outbox-dead/`. A recent one is replayed after the next successful send;
    `openclaw-team-send.py --replay` retries them all.
  - Latest wins: an edit is rendered when its turn comes, so a member that waited shows what it is doing now.
- The backup now also takes `~/.local/bin/openclaw-team-send.py`.

### Fixed

- **A rate limit on a member's first message left it without a live message for good.** The watcher called the
  CLI with a 30 s timeout and no retry; a call waiting out a 429 was killed, no message id came back, and the
  watcher exited. It now waits its turn, honours `retry after` and delivers.
- **The hook's fire-and-forget send had nobody to notice a failure.** It now hands the message to the queue,
  which retries and records what it cannot deliver. Without the queue script the hook still calls the CLI
  directly.
- The watcher asks for less: it edits every 6 s at most (was 4) and its heartbeat is 60 s (was 45).

### Notes

- The gateway's own streaming drafts (`telegram/draft-stream`) draw on the same budget and are outside the kit.
- Hosts that installed the hook and the watcher by hand keep working; copy `openclaw-team-send.py` next to
  them (`~/.local/bin`) to get the queue.

## [1.9.1] — 2026-09-24 — a long workflow no longer goes silent in Telegram

An `elinvo` workflow ran a member for hours and its topic heard nothing after the start. Four
independent causes, all fixed in the team-narration hook and its watcher (pitfall T32). Hosts that
narrate at the `every-step` level are the ones that notice; `milestones` gains the rate window and
the log rotation.

### Fixed

- **Workflow members were never watched.** A member started by a Workflow writes its transcript to
  `subagents/workflows/<workflow id>/agent-<id>.jsonl`, and the hook only looked at
  `subagents/agent-<id>.jsonl` (346 of 655 transcripts on the reference host). Both the hook and the
  watcher now look in both places, and the watcher keeps looking while the file does not exist yet.
- **The 60-message lifetime cap is gone.** It silenced sessions that live for days, milestones
  included. A sliding window (30 messages per 10 minutes per session, one notice per window) limits
  the rest, and milestones bypass it: the request, the team start, each hand-off, the workflow banner
  and the turn close are never dropped.
- **The watcher no longer closes after 90 s of silence.** A member blocks for minutes inside one tool
  call. It closes on `SubagentStop`, when its `claude` parent is gone, after 25 minutes of silence
  only if the parent cannot be checked, and at hard ceilings (2 h without a transcript line, 6 h of
  life). While the member is quiet it edits its message every 45 s with the last tool and the idle time.
- **A watcher marker that outlived its process kept the member unwatched.** `watch-<agent_id>` now
  holds the watcher PID, and counts as alive only if that process exists and is that member's watcher.
  Dead markers are swept, do not count against `MAX_WATCHERS`, and the member's next tool call
  relaunches the watcher, which reuses its Telegram message instead of opening a second one.
- `team-hook.jsonl` rotates to `.1` at 2 MB instead of going silent.

### Changed

- The hook launches (or heals) a member's watcher before handling an edit, so a member whose first
  tool is an edit still gets one.
- `stop-<agent_id>` markers older than a day are removed when a watcher is launched.

### Notes

- Pitfall T32 in `docs/openclaw/pitfalls.md` records the four causes, the fix and how to spot a
  member without a live watcher.
- The watcher prints the first line of each tool's command, trimmed to 54 characters, in the live
  message. That predates this release and is unchanged.
- Hosts that installed the hook by hand before the kit already run a patched copy of the same fix;
  `ai-resources setup` replaces it with this one.

## [1.9.0] — 2026-09-19 — the OpenClaw setup is reproducible from the kit

The setup that 1.8.2 documented can now be rebuilt from the kit: the scripts,
units, hooks, config block and backup manifests that lived on one machine's disk
are versioned artifacts, and every one of them is reachable from `ai-resources
setup`. On a fresh host, `brew upgrade ai-resources && ai-resources setup`
reproduces the documented setup up to the secrets and the host-specific values
in `~/.openclaw/kit-host.env`.

### Added

- **`ai-resources setup` asks.** The OpenClaw cockpit gains a host section with
  one opt-in question per capability: team narration (off, milestones or every
  step), the gateway guard hook, the ten `openclaw-*` systemd units, the
  canonical config block, the workboard plugin, an `AGENTS.md` for workspaces
  that have none, and a host check. Every answer defaults to no on a first run
  (the workboard defaults to yes). Host values (domain, operator id, ingress CIDR,
  backup dir) are asked and written to `~/.openclaw/kit-host.env`; secrets are
  never asked. Anything that changes the running gateway is its own confirm, is
  skipped under `--non-interactive`, and nothing restarts the gateway. A second
  run changes nothing, and undoing it removes only what the kit recorded.
- **`ai-resources openclaw` commands**, thin wrappers over the same functions for
  headless runs and disaster recovery: `doctor` (the drained `doctor --fix`),
  `bootstrap` (eight idempotent host checks), `status` (one screen, never
  repairs), `install-units`, `agent-new` and `render-gitops-backups`.
- **Host scripts and units in the kit:** the five scripts (`openclaw-backup.sh`,
  `-maintenance.sh`, `-watchdog.sh`, `-verify.sh`, `openclaw-team-watch.py`) under
  `scripts/openclaw/`, and the ten `openclaw-*` units as templates in
  `templates/systemd/`, run as `/bin/bash <script>` so the exec bit is never
  load-bearing.
- **Hooks:** `hooks/openclaw_team_progress.py` publishes a Claude Code team's work
  into its Telegram topic (OpenClaw discards subagent events on purpose, T31), and
  `hooks/openclaw_gateway_guard.py` denies an undrained `openclaw doctor --fix` or
  gateway stop (T01). A test pins that `hooks.json`, the cockpit registrations and
  `hooks/` agree.
- **`profiles/openclaw-host.json5`:** the canonical host config, applied as one
  atomic `openclaw config patch --stdin` after a `--dry-run`, with the invariant
  that `channels.*` is never touched (only `channels.telegram.streaming`).
- **Three `AGENTS.md` templates** (orchestrator, umbrella, repo).
- **`openclaw-operations` skill**, the operating manual, and the
  `openclaw-host-setup` workflow, whose primary path is `ai-resources setup`.
- **Off-box backup manifests as GitOps templates** in
  `templates/gitops/openclaw-backups/` (bucket, uploader, guard, alerts). Every
  value that belongs to the target infrastructure is a marker.
- **`docs/runbooks/openclaw-host-dr.md`**, which restores `kit-host.env` first
  (with no version pin it is the only record of the installed openclaw version)
  and states that the restore is **unrehearsed**.

### Changed

- **The team narration hook is off unless the host env opts in.** It speaks only
  when the gateway started the session (`OPENCLAW_CLI=1`) and
  `OPENCLAW_NARRATION` is `milestones` or `every-step` in `kit-host.env`. Before
  this, an absent key meant `milestones`, so answering "off" left it publishing.
- `bootstrap` installs the latest openclaw, not a pin, and records
  `OPENCLAW_INSTALLED_VERSION` and `OPENCLAW_PREVIOUS_VERSION` in `kit-host.env`.
- `docs/openclaw/*` now states what is implemented and where it lives, and
  corrects three facts: there are five host scripts, not three; six timers are
  armed, not five; and the config block is applied with `config patch --stdin`,
  not per-key `config set`. The backup had left out the watchdog, verify and
  team-watch scripts and most units by accident; the list is now complete.
- `__version__` in `scripts/ai_resources/__init__.py` (still 1.7.3) now matches
  the release.

### Fixed

- Teardown of the host section removes no more and no less than was added: it
  disables only the timers the kit enabled, and puts back a hand-installed
  `openclaw-team-progress.py` registration that setup had replaced.
- `configure()` no longer reports a teardown as "openclaw.json changed".

### Security

- `setup-state.yaml` is written owner-only (0600), and a credential leaf in the
  config (token, secret, password, api key) records only that it existed: its value
  is never persisted or restored.

### Notes

- Not done: a restore rehearsal, and the live rehearsal of the kit on the
  reference host (both are operator steps in `docs/openclaw/runbook.md` and
  `docs/runbooks/openclaw-host-dr.md`). A traces panel (Phoenix / Langfuse) is
  deferred.
- `Formula/ai-resources.rb` points at tag `v1.9.0`, which is created when the
  release is tagged.

## [1.8.2] — 2026-09-18 — the OpenClaw setup stops living in one person's head

Documentation only: no code, no skills, no formula behaviour changes. It lands
because the setup it describes spent a day being repaired and the repairs were
worth more than the fixes — several of the failures are silent, and one of them
left the gateway dead for half an hour without anyone noticing.

### Added

- `docs/openclaw/` with an index and three documents, written from a live system
  and marked `VERIFIED`, `SESSION` or `UNVERIFIED` line by line so a reader knows
  what to re-check:
  - `inventory.md` — the six agents with their model and workspace, all 48
    configuration changes with the command that produced each one and the reason
    it exists, what was installed, what was written, what was cleaned up, the
    decisions taken, and the command behind every claim.
  - `pitfalls.md` — 31 traps with the literal symptom, the verified cause, the
    fix, and how to spot it next time. The expensive ones: `openclaw doctor --fix`
    stops the gateway and leaves it dead when the cgroup has not drained; a
    project's `CLAUDE.md` is never loaded in a topic session because
    `--setting-sources user` is forced in code; OpenClaw receives every Claude
    Code subagent record and discards it on purpose, so team activity cannot be
    surfaced by configuration; and a DNS record without its `recordId` is named by
    list position, which is a time bomb in that Crossplane composition.
  - `runbook.md` — six phases from a clean machine, recovery from the backup
    tarball, a daily cheat sheet and a 34-box checklist. Every command is marked
    idempotent or once-only, and the eight steps that need a human say so instead
    of hiding inside a block.

### Notes

- Section C of `pitfalls.md` proposes ten customizations that would make this
  setup reproducible *from the kit* rather than only documented — the three host
  scripts as versioned artifacts, a wrapper that drains before calling
  `openclaw doctor --fix`, and the canonical configuration block. None of them are
  implemented here. Until they are, those scripts live on a single disk and are
  recoverable only from a backup tarball. *(Superseded: all ten are implemented in
  1.9.0.)*

## [1.8.1] — 2026-09-17 — the quota remedy stops pointing at dead pools

Follow-up to 1.8.0. Two defects found by review after that tag was already
pushed, plus two tests that could never fail.

### Fixed

- The "pick a model from the other pool" remedy never checked whether the other
  pool had anything left. Both weekly pools are independent and both can sit at
  0% at once, so `ai-resources doctor` and the setup wizard could send a user to
  an equally exhausted bucket. The wizard could also name a pool that
  `agy -p "/usage"` never reported, by inverting an unrecognised label. The
  remedy now offers only reported pools that still have room, flags one that is
  itself nearly spent, and says plainly when every pool is gone.
- The wizard's live quota read resolved `agy` through `PATH` after the guard had
  already resolved its absolute path with `_which_extra()`. Where agy lives only
  in `~/.local/bin` and that directory is off `PATH`, the quota annotation
  vanished with no warning and no detail line — the exact machines the extra-bin
  lookup exists for. `doctor.py` was already correct; the wizard now matches it.
- Two tests shipped in 1.8.0 passed vacuously: the doctor guard test
  re-implemented the production condition inside the test body (so it stayed
  green with the real guard deleted), and a `read_usage` timeout assertion
  short-circuited through `or reason`, which any non-empty string satisfies.

## [1.8.0] — 2026-09-17 — Antigravity's two weekly pools are visible, choosable and diagnosable

Antigravity serves two **independent** weekly quota pools — one for Gemini models, one
shared by Claude and GPT models — and a pool at 0% used to present as an unexplained
hang: agy retries a 429 `RESOURCE_EXHAUSTED` five times with backoff (~93 s), then
OpenClaw's stall detector kills the turn for "no progress" and respawns it, with no quota
error ever shown.

### Added

- Claude/GPT models (`claude-sonnet-4-6`, `claude-opus-4-6-thinking`,
  `gpt-oss-120b-medium`) are now selectable for the antigravity engine, alongside the
  existing Gemini ones — so a Gemini pool at 0% is no longer a dead end. Each choice in
  the wizard is labelled with its weekly pool, and a live read of `agy -p "/usage"` warns
  at choice time when a pool is exhausted, naming the other pool as the way out.
- `ai-resources doctor` gained a quota check: it reports both pools' remaining
  percentage and reset time, and raises an issue when the pool backing the applied model
  is at or near its limit — the same signal the wizard shows, in one place, on demand.

### Changed

- Choosing `agy` for voice notes while the main agent runs a gemini-* model now warns
  that both share one weekly Gemini pool and exhaust together. The default stays
  agy-first, unchanged — the warning is what surfaces the shared pool, not a silent
  change to anyone's setup.

### Documented

- The deceptive failure mode (429 × 5, ~93 s backoff, then a turn interrupted for "no
  progress" instead of a quota error), agy's broken background quota refresh
  (`Singleflight refresh failed`), and that Google Workspace accounts
  (`hd=<domain>`, `auth_method=consumer`) silently sit on the FREE weekly tier —
  documented this release, detected never.

## [1.7.3] — 2026-09-17 — A kit upgrade no longer leaves the bot answering "Unknown CLI backend"

### Fixed

- **After `brew upgrade`, OpenClaw kept running the plugin from the previous kit
  directory** and answered `Unknown CLI backend: agy-cli` to every chat message until the
  gateway was restarted by hand. Setup now compares the directory it links with the one
  the gateway reports, restarts the gateway when they differ or the backend is missing,
  verifies the backend came back, and says which of the two happened — including the exact
  command to run when the restart did not help. `ai-resources doctor` reports the same
  condition instead of leaving a silent, fully broken bot.

## [1.7.2] — 2026-09-17 — The engine step stops mistaking a loaded backend for a missing one

### Fixed

- **Setup refused to patch the `antigravity` engine even with the plugin loaded.** The
  readiness check asked `openclaw models list`, which lists provider models and never
  prints CLI-backend refs, so a working `agy-cli` looked missing and the engine step gave
  up after restarting the gateway. It now reads `openclaw plugins inspect ai-resources
  --runtime --json` and requires the plugin to be loaded with the backend registered.

## [1.7.1] — 2026-09-17 — The antigravity engine applies on a first run

### Fixed

- **`ai-resources setup` could not apply the `antigravity` engine on a machine where the
  plugin was not linked yet**: the engine patch went out first and `openclaw config patch`
  validates model references, so it was rejected with `Unknown model:
  agy-cli/gemini-3.8-flash-low`. Setup now links the plugin, registers the agy MCP bridge
  and restarts the gateway **before** patching, then waits (up to 30 s) for the `agy-cli`
  backend to register and stops with a clear error if it never does. Voice notes were
  unaffected — that step applied correctly on the same run.

## [1.7.0] — 2026-09-17 — Antigravity CLI orchestrates OpenClaw, Claude Code delegates unrestricted

### Added

- **`antigravity` OpenClaw engine: Antigravity CLI (agy) as chat-facing orchestrator, Claude
  Code as an unrestricted delegate.** A new kit-shipped OpenClaw plugin
  (`openclaw-plugin/ai-resources/`) registers two CLI backends — `agy-cli` (agy, a personal
  Google account) and `claude-kit` (the kit's own unrestricted Claude Code backend, run with
  `--dangerously-skip-permissions` and none of the restrictions core applies to the bundled
  `claude-cli` backend) — plus `/claude` and `/equipo` shortcut commands. agy is the
  orchestrator for every chat topic: it replies directly, reads voice notes via its own
  `view_file` tool, and hands code/team work to a `claude` worker agent
  (`sessions_spawn agentId=claude cwd=<project> thread=true`, workspace `~/Development`, no
  `operator.admin`). Voice notes get a third transcriber mode, `agy`
  (45s timeout, one retry, no ffmpeg/whisper/OpenRouter, always a non-empty reply), which
  is the default when `agy` is installed; `cloud` and `local` are untouched. Setup can also detect and offer to install `claude`, `agy` and `openclaw`
  themselves (`ai_resources/setup/tools.py`), and offers to remove a superseded hand-rolled
  `~/.local/bin/openclaw-transcribe`.
- **`gemini-3.8-flash-medium` is never offered** as an OpenClaw model — it leaks its reasoning
  into replies.

### Changed

- **The OpenClaw `direct` engine was removed.** It routed OpenClaw's own runtime through
  OpenRouter; OpenRouter remains a fully supported multi-model backend elsewhere in the kit,
  it is just no longer wired through OpenClaw's own runtime. A `setup-state.yaml` saved with
  the old `direct` engine falls back to `keep` on the next run, with a warning.
  `gemini-cli` is no longer offered as an OpenClaw engine either: Google retired CLI access
  for personal Google accounts on 2026-06-18.
- **The OpenClaw `openrouter` plugin is toggled by the kit's own multi-model backend choice**
  (on under `backend: openrouter`, off otherwise, restored exactly on teardown) for every
  OpenClaw engine, not only `antigravity`.
- Legacy `voice_*` fields (`voice`, `voice_formulas_installed`, `voice_models_downloaded`) left
  over from an earlier whisper-based voice chain are dropped silently on load and never written
  back to `setup-state.yaml`.

### Fixed

- **Four config shapes the kit built were rejected by OpenClaw's own schema**, found with
  `openclaw config patch --dry-run` against a live 2026.9.4 gateway: an `agentRuntime` key on
  `agents.defaults` and on an agent entry (the CLI backend is resolved from the model ref's
  first segment instead), `tools.exec: {enabled, ask: false}` (the schema takes
  `{mode: "full"}`, and `mode` cannot be combined with `ask`), and a three-segment model ref
  (`claude-kit/anthropic/claude-sonnet-5` does not resolve, so the worker model is stored as a
  bare id). The kit also no longer sets `commands.plugins`: it is a boolean that enables the
  `/plugins` **chat** command, letting anyone in the channel toggle plugins, and
  plugin-registered commands never needed it.
- **The plugin could not be installed at all:** its `package.json` had no
  `openclaw.extensions`, so `openclaw plugins install` refused it.
- **Every voice note would have failed, silently.** Two separate bugs in the `agy` mode,
  both found only by running a real note: `--model` sat between `-p` and the prompt, so agy
  took `--model` as the prompt and exited 2; and the note is staged outside agy's workspace,
  so `view_file` needs both `--add-dir <the note's directory>` and
  `--dangerously-skip-permissions` — without them headless agy auto-denies the read, prints
  nothing and exits 0.

### Security

- The `antigravity` engine runs both the orchestrator and the worker agent with
  `--dangerously-skip-permissions`; anyone who reaches the bot (via OpenClaw's
  `channels.telegram.allowFrom`, or by taking over that Telegram account) gets unrestricted
  code execution as whichever OS user runs the gateway. The wizard shows this risk and
  requires an explicit, saved acknowledgement before applying the engine; see SECURITY.md.

## [1.6.0] — 2026-09-17 — OpenClaw gets Claude Code's MCP servers

### Added

- OpenClaw: with the Claude Code engine, setup offers to give the bot the same MCP servers as Claude Code. It mirrors user-level servers from `~/.claude.json`, `~/.claude/settings.json` and enabled plugins, maps the claude.ai ClickUp connector to its public endpoint, adopts identical servers you added by hand, never overwrites different ones, and refuses to copy literal credentials: those servers are reported with the env var to use instead. Setup lists the gateway variables that are missing and the `openclaw mcp login` commands to run; teardown removes or restores only what the kit wrote.
- `LICENSE`: the kit is now published under the Apache License 2.0, matching the organisation's other open-source repositories, and the Homebrew formula declares `license "Apache-2.0"`.

## [1.5.0] — 2026-09-17 — OpenClaw voice notes

### Added

- **OpenClaw voice notes.** When OpenClaw is installed, setup asks how voice notes are
  transcribed: cloud (an OpenRouter audio model, with local whisper.cpp as the fallback), local
  whisper.cpp only, off, or keep. The kit ships the transcriber, registers it through
  `openclaw config patch`, installs whisper.cpp, ffmpeg and a checksum-verified Whisper model
  when needed, and adds the "act on the transcript" rule to the workspace `AGENTS.md` block. The
  `tools.media` it replaced is restored on teardown, and an unattended first run changes nothing.
  - Setup asks for the language: the system locale's by default, else Spanish, with `auto` as an
    explicit choice.
  - Local mode asks whether to correct transcripts with an LLM, which sends the text but never
    the audio, or to stay fully offline.
  - Teardown deletes the Whisper models it downloaded. It uninstalls only the Homebrew formulas
    setup installed itself, and asks first when interactive.

## [1.4.1] — 2026-09-17 — Bare Anthropic model IDs work under OpenRouter

### Fixed

- **A bare Anthropic model ID failed under OpenRouter.** `claude --model claude-sonnet-5` sent the
  ID verbatim and OpenRouter only knows namespaced ones. OpenClaw's claude-cli runtime always
  launches Claude Code that way, so a chat bot lost its main conversation once setup switched to
  OpenRouter. Setup now writes a `modelOverrides` map to the catalogue IDs under OpenRouter
  (Claude Code 2.1.200 or later) and removes only its own entries on any other route.

## [1.4.0] — 2026-09-17 — OpenClaw runs its chat agent on the kit

### Added

- **OpenClaw as a cockpit.** When OpenClaw is installed, setup asks which engine runs its default
  agent — Claude Code, Codex CLI, Gemini CLI, a direct model through OpenRouter, or keep — and
  offers only engines whose CLI is installed. With Claude Code the bot runs the full kit (subagents,
  skills, hooks, workflow scripts); with a direct model it gets the kit skills and instructions.
  Engram is always registered and named the memory of record. Changes go through
  `openclaw config patch`, the replaced values are saved for restore, and an unattended first run
  never repoints a live bot.

## [1.3.0] — 2026-09-16 — OpenRouter as a second backend, one profile set for both

### Added

- **OpenRouter as a second multi-model backend alongside LiteLLM.** OpenRouter is hosted: one key,
  nothing to install, no lifecycle to supervise. The wizard's mode step offers both backends and
  warns up front that a gateway credential replaces the claude.ai subscription, so usage is billed
  per token rather than to the plan.
- **`measured-best` profile, marked recommended.** The assignment that measured best across three
  instrumented workflow runs; the "(recommended)" label previously sat on `cost-optimized` in the
  wizard and in EXECUTOR-CONFIG, contradicting the measurements.
- **One subagent per persona.** Personas (`agents/personas/<role>-<domain>.md`) now generate their
  own subagent — role body plus overlay — instead of being composed at dispatch time, so each one
  can carry its own model. Fifteen role subagents become forty.
- **DeepSeek and Moonshot as first-class providers**, with their own env vars and model catalogues,
  needed to resolve canonical IDs to direct upstreams per vendor.
- **`--non-interactive` and `--profile` now work.** Both flags were declared and never read;
  `--non-interactive` puts every wizard prompt into saved-answer mode, and `--profile` seeds the
  default that mode returns. Without a terminal and without the flag, the wizard used to block
  forever on the first prompt — it now says so and exits.
- **Unit tests in CI.**

### Changed

- **Every multi-model profile carries a canonical `<vendor>/<model>` ID and names no backend.**
  OpenRouter consumes the ID unchanged; LiteLLM registers it as an alias and resolves it through a
  vendor table. All profiles are now offered whichever gateway is configured, instead of being
  split into an OpenRouter set and a LiteLLM set.
- **Model IDs refreshed to the current generation** across `KNOWN_MODELS` for anthropic, google,
  vertex and openai, replacing `claude-opus-4-7`, `claude-sonnet-4-6` and `gemini-2.5-*`.
- **`GOOGLE_CLOUD_LOCATION` now defaults to `global`.** The previous default, `us-east5`, is a
  regional endpoint that serves Claude Sonnet 4.6 and earlier only, which silently ruled out every
  current model on the vertex profile; `global` also carries no multi-region/regional premium.
- **`docs/multi-model.md` rewritten** around two backends presented as a choice, the precedence
  chain that actually resolves a model (subagent frontmatter, then classes, then the session
  model), and the forty generated subagents.

### Fixed

- **Wizard step 6 crashed when the saved profile belonged to the other backend.** The default
  profile choice was hardcoded and not filtered by backend, so `questionary` rejected a default
  outside the offered list; the default is now taken from the backend-filtered list, preferring
  that backend's recommended profile and falling back to its first entry.
- **`--dry-run` wrote credentials to disk.** Only the final step consulted the flag; two earlier
  steps stored an API key on a run whose purpose was to touch nothing. The flag now guards every
  write, and a dry run reports what it would have stored instead.
- **Unrecognized vendors were dropped from the rendered config without warning.** The provider
  chain's `else: continue` silently skipped any vendor it did not know, so a profile naming an
  unconfigured vendor produced a gateway config quietly missing that role; the failure only
  surfaced later as a request for a model the gateway had never heard of. An unmapped vendor now
  raises, naming the role and the vendor.
- **The Claude-alias passthrough depended on the anthropic provider being enabled.** With only a
  hosted gateway configured, that gate left the main conversation with no entry while subagents
  worked. It now resolves each alias through the profile's `classes` block on either backend.
- **`_CLAUDE_PASSTHROUGH_MODELS` still listed retired models** (`claude-opus-4-7`,
  `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) after the model refresh, so the main
  conversation under LiteLLM was being pointed at models no longer served.
- **`doctor` reported one missing gateway credential twice under the openrouter backend**, where
  the gateway credential and the provider credential are the same key: once from the provider loop
  and once from the gateway check, inflating the issue count. The Credentials section now owns the
  check and the count.
- **`ai-resources generate` no longer obeys an `AGENT_SKILLS_ROOT` that points outside the kit.**
  Setup writes that variable so agents can find the installed skills, and it outlives the install
  it names: after `brew upgrade` it still pointed at the previous Cellar version, which Homebrew
  had just deleted. The vendor import then recreated that deleted tree with `copytree`, wrote the
  24 vendored skills into it, and built the index from that root while saving it into the new one —
  leaving the fresh install with a 24-skill index instead of 136. `generate` now resolves the root,
  accepts it only when it is inside the kit, and otherwise fails with both paths and the fix.
- **A second `setup` run deleted every generated subagent.** The prune step asked whether each file
  *changed*, not whether it *should exist*, so a run with nothing to rewrite produced an empty list
  and removed all forty tracked agents — including the twenty-five personas. Measured: forty files
  on the first run, zero on the second. The regression test passed throughout, because both of its
  witness files survive a total wipe by construction; it now asserts the whole population.
- **Teardown deleted the user's own `ANTHROPIC_API_KEY`.** It was listed among the keys the kit
  adds, but only the openrouter backend writes it; under LiteLLM the kit claimed a credential it
  had never set and removed it on the way back to single-model. It is now reclaimed only when the
  run actually introduced it.
- **`--profile` raised `UnboundLocalError`.** The assignment read the state object before
  `state.load()` bound it, so naming a profile on the command line aborted setup outright.
- **Router fallbacks were silently dropped.** LiteLLM keys fallbacks by model, so two roles sharing
  a model shared one entry and the second role's list was discarded; the lists are unioned now. A
  model named only as a fallback was also never registered in `model_list`, which turned the
  fallback into a second failure at the moment the primary was down. `moonshotai` — OpenRouter's
  spelling, and the one the catalogue offers — was missing from the vendor table, so choosing Kimi
  as a primary raised and took down the whole render.
- **Podman and colima were configured but never driven.** Service control compared the runtime
  against the literal `"docker"`, so both matched no branch and start, stop, status, logs and
  update did nothing — while the login-time unit, which interpolates the runtime correctly, kept
  the gateway up: it ran and the kit reported it absent. Image pull coerced the runtime and the
  container start did not, which is why a podman setup downloaded the image and then failed at step
  9. Colima was additionally skipped by teardown (gateway left running, image left pulled) and by
  step 9's compose branch (container never started), and would have tried to exec `colima compose`,
  which is not a command — colima is driven by docker's CLI. Compose detection, hardcoded to
  `docker compose`, now probes the runtime's own.
- **`--profile` was read and then silently overwritten.** Step 6 filters the profile list by the
  resolved mode and backend and falls back to its own default when the saved name is not in it —
  which is right for a name carried over in the saved state, and wrong for one the user typed:
  naming a multi-model profile while the saved state said single-model ran the whole setup under a
  different profile without a word. An explicitly requested profile that is not offered now stops
  setup and lists what is available; a saved one still degrades quietly.

- **The rendered LiteLLM config ignored the profile's retry, timeout and default fallbacks.**
  `num_retries` and `timeout` were literals, so `measured-best`'s `max_retries: 2` came out as 3,
  and `defaults.fallbacks` was never rendered. They now come from the profile, and
  `defaults.fallbacks` becomes the router's `default_fallbacks`, with a deployment registered for
  any fallback-only model.
- **`executors set` and `tune` offered model IDs the active backend rejects.** Bare IDs under
  OpenRouter, OpenRouter spellings under LiteLLM. The menus now follow the configured backend and
  store the canonical `<vendor>/<model>` ID, the same shape profiles use.
- **`executors test` read the smoke result by position.** A failed health or credential probe, or
  a run with no round-trip at all, could be reported as success; an empty result raised
  `IndexError`. It now fails on any failed probe and when no round-trip ran.
- **The `--dry-run` preview showed the LiteLLM settings patch under OpenRouter**, omitting
  `ANTHROPIC_AUTH_TOKEN` and the `ANTHROPIC_DEFAULT_*_MODEL` keys the real run writes.

### Removed

- **The `vertex-enterprise` profile.** Its purpose — single billing line, audit log, VPC-SC — is
  tied to the `vertex` provider, not to a preset, and the preset invited the one mistake that
  defeats it: picking `vertex-enterprise` while the gateway routes elsewhere, keeping the name and
  losing the compliance. The `vertex` provider itself is untouched and still selectable in the
  wizard; a Vertex setup is now assembled per role instead of chosen as a preset.

## [1.2.0] — 2026-09-16 — native skills, deterministic core loop, plugin packaging

### Changed

- **Instruction files use a managed block.** Setup writes the kit's text only between
  `<!-- BEGIN ai-resources … -->` and `<!-- END ai-resources -->` in `~/.claude/CLAUDE.md`,
  `~/.gemini/GEMINI.md`, Codex/Windsurf/Copilot/Aider/Cursor/Continue/OpenCode files.
  Previously setup overwrote the whole file, deleting any user content. Files from older
  kit versions are migrated once (legacy kit sections removed, user sections kept) with a
  `*.ai-resources-backup-<timestamp>` copy.
- **Kit hooks merge with user hooks.** Hook entries are replaced per event only when they
  are the kit's own; the legacy cleanup no longer deletes every `UserPromptSubmit` hook.
- **Generated `CLAUDE.md` block cut from ~110 lines to ~25.** No workflow trigger table,
  no "read skills-index.json" recipe, no subagent table — skills, workflows and agents are
  discovered natively. Other cockpits share one template.
- **Orchestration moved from rules to skills.** New `kit-orchestration` skill (steps,
  delegation by task shape, handoff, parallel groups, return format) replaces rules 010,
  011, 050, 051 and 100. Commands are skills: `delegate`, `setup-project`, `self-update`
  (plus the existing `knowledge-audit`); their `.mdc` rules are removed.
- **Workflows are native skills.** `ai-resources generate` creates one `workflow-<name>`
  skill per workflow YAML (description = trigger) and removes stale ones.
- **Removed the `_auto-delegate` workflow** and the "never read inline / always explore on
  Gemini" policy (rule 017 and generated files); delegation is decided by task shape.
- **Core workflow loop follows the 2026 evidence**: the implementer writes the tests for what it
  changes (TDD) instead of handing that to a separate author, the tester becomes the gate that
  separates new failures from pre-existing ones, the reviewer audits the test diff for weakened or
  skipped assertions, and the verifier checks acceptance criteria against evidence only — spec,
  ADR and knowledge-audit work moved to a new `document` step run by `doc-writer` (feature, bugfix,
  cross-domain, release and security-devsecops; `refactor` keeps `review` as its gate, since a
  refactor changes no external behaviour and promotes nothing into specs).
- **The handoff file starts with YAML front matter** (`status`, `blocked`, `return_to_step`,
  `refs`, `verdicts`, …) so a step reads state without parsing prose.
- **Setup mode prompt is neutral and defaults to single-model** (it recommended multi-model).
- **Model routing depends on the setup mode.** Single-model setup offers `claude-native`
  (Claude aliases per role, default) or `claude-inherit`; gateway profiles are only offered
  in multi-model mode, and gateway-only model names are never written to agents in
  single-model mode.
- **`skills-index.json` paths are relative to the kit root.**
- **Kit paths in generated files use the Homebrew `opt/` path**, so they survive upgrades.

### Added

- **The repository is a Claude Code plugin and its own marketplace** (`.claude-plugin/`), so the
  skills, role subagents, workflow scripts and hooks can be installed with
  `/plugin marketplace add wildbitca/ai-resources` and `/plugin install ai-resources@wildbit-ai-resources`,
  without the Homebrew CLI. `ai-resources generate` keeps the manifest's `agents` list in step with
  `agents/roles/` (the plugin schema takes files, not a directory).
- **`scripts/validate_kit.py` and a CI workflow** that gate every pull request: skill frontmatter is
  valid YAML with a name matching its directory and a description within budget; workflow steps
  reference real roles, skills and routing targets; parallel groups share entry criteria and never
  write the handoff; verifiers never own `knowledge-audit`; workflow scripts obey the runtime's
  rules; hooks compile; `$AGENT_KIT` references resolve; the plugin manifests are consistent; and
  `skills-index.json` matches the tree.
- **An eval suite** (`evals/`) for `claude plugin eval`: one case that should trigger the kit's
  planning loop and one conversational case that must not trigger anything.
- **Deterministic core loop as dynamic workflow scripts** (`workflows/scripts/`, installed by setup
  into `~/.claude/workflows/`): `/kit-plan` runs parallel readers (code, specs, tests and risk),
  three planners with different biases, two judges and a synthesis step that writes one plan file;
  `/kit-implement` runs one writer with TDD, a test gate with a bounded fix loop, three review
  lenses in parallel (correctness, security, test integrity), a skeptic per finding before anything
  is fixed, and a verifier that signs off against the acceptance criteria. Approval sits between the
  two because a workflow script cannot ask the user anything mid-run.
  Setup records which workflow scripts it installed, so a later run prunes only its own and never
  a `/kit-*` workflow the user wrote; switching back to single-model removes them again.
- **Kit hooks for Claude Code** (`hooks/`): `kit_session_start.py` reports in-progress and
  stale handoff files; `kit_subagent_return.py` sends a kit role subagent back when it ends
  a workflow step without the return block; `kit_handoff_guard.py` blocks subagents from
  writing the shared handoff while `.agent-output/parallel-group.json` exists.

### Fixed

- **Claude Code and Codex now discover kit skills.** Setup linked the whole `skills/`
  directory as `~/.claude/skills/ai-resources` (and `~/.agents/skills/ai-resources`), one
  level deeper than agents look, so no kit skill was ever discovered. Setup now links each
  skill as `<skills-dir>/<skill-id>`, removes the legacy aggregate link and stale kit links,
  and never overwrites a user's own skill with the same name. Links target the
  version-independent Homebrew `opt/` path so they survive `brew upgrade`.
- **Removed `TeamCreate` guidance from the generated `CLAUDE.md`.** The tool no longer
  exists; the section now describes parallel subagents, and the model-routing note reflects
  the configured mode instead of always pointing to `executors.yaml`.
- **Added the missing `doc-writer` role** used by `merge-and-document` and
  `incident-response`, registered in every profile and in `KNOWN_ROLES`; dropped the
  nonexistent `shell` role from the delegation map.
- **`parallel_group` hints no longer contradict entry criteria.** Feature: `test` is a
  sequential gate, then `review` and `security` run in parallel (`post-test`). Bugfix: the
  test → security chain is sequential. Parallel steps (feature, infra-triage) no longer edit
  the shared handoff concurrently; `WORKFLOW_CONTRACT.md` defines the join rules.
- **The 24 vendored skills had no usable description.** Frontmatter was parsed line by line, which
  cannot read a YAML block scalar, so every skill written as `description: >` was indexed with the
  literal `">"` — leaving them undiscoverable, since an agent matches on the description. Frontmatter
  is now parsed as YAML, with the line parser kept as a fallback for malformed third-party files.
- **Ten skills had frontmatter that is not valid YAML** (`globs: "a", "b"` instead of a list, and
  unquoted descriptions containing `: `). The kit's tolerant parser hid this; agents that parse YAML
  properly would have lost the name and description. Fixed, and the validator now rejects it.
- **Vendored imports are pinned.** `resources.json` gained a `sha` for the Gentleman-Skills source;
  the import checks out that commit, fails if it resolves to anything else, warns when a source is
  unpinned, and rewrites each imported skill's `name` to match its directory.
- **`ai-resources audit` prices corrected** against official list prices (2026-09-15): Opus
  4.5+ was 3× too high, Haiku and Gemini were too low, current models (Fable 5.1, Opus 5,
  Sonnet 5, Gemini 3.x) were missing. Adds 1-hour cache writes, alias resolution, and
  longest-prefix model matching.

## [1.1.9] — 2026-05-26 — package-upgrade on Haiku + routing policy in generated context files

### Changed

- **`cost-optimized` profile: `package-upgrade` → `claude-haiku-4-5-20251001`** (`profiles/cost-optimized.yaml`).
  Dependency upgrade work is mechanical (version bumping, changelog scanning) and does not
  require Sonnet-level reasoning. Haiku 4.5 handles it at ~5× lower cost.

### Added

- **Routing policy table injected into generated `CLAUDE.md` and `GEMINI.md`** (`cockpits/_shared.py`).
  `ai-resources generate` now embeds the mandatory delegation table (task type → agent → model)
  and the inline-Read/Bash-exploration ban into every cockpit context file, so enforcement
  rules travel with the install rather than requiring manual edits.

## [1.1.8] — 2026-05-26 — mandatory multi-model routing enforcement policy

### Added

- **Mandatory routing enforcement policy** in `rules/017-multimodel-routing.mdc` —
  codifies which agent/model handles each task type and hard-bans inline exploration
  from the main Claude session.

  Key rules:
  - Main Claude session = orchestration + synthesis + edits only. NEVER exploration.
  - All read/grep/glob/find tasks → `explore` subagent (gemini-2.5-flash).
  - `Read` inline allowed ONLY as an immediate precondition for `Edit`/`Write`.
  - `Bash grep/find` inline NEVER for exploration — always spawn `explore`.

  Routing table (task type → agent → model) covers: codebase exploration, SDD
  reads, web research, documentation, test runs, verification, planning, code
  review, security audit, architecture, code editing, infra, and final synthesis.

## [1.1.7] — 2026-05-07 — fix single-model teardown leaves ANTHROPIC_BASE_URL

### Fixed

- **`ai-resources setup` single-model now cleans up `ANTHROPIC_BASE_URL`** from
  `~/.claude/settings.json` when switching from multi-model mode.

  Previously, two conditions could leave `ANTHROPIC_BASE_URL` behind after
  selecting single-model:
  1. The tracking record (`cockpit_env_keys_added`) was empty because the key was
     already present in `settings.json` at the time of the multi-model setup (e.g.
     written by an older kit version before tracking existed), so `env_keys_added_by_patch`
     returned nothing → teardown skipped it.
  2. `configure()` in single-model mode applied a patch without `ANTHROPIC_BASE_URL`
     but `deep_merge_json` never removes keys — only adds or updates them.

  Fix (both applied in `cockpits/claude.py`):
  - **Tracking**: multi-model setup now always adds `_MULTI_MODEL_ONLY_ENV_KEYS`
    (`["ANTHROPIC_BASE_URL"]`) to the teardown record regardless of whether the key
    was pre-existing, ensuring teardown can find it.
  - **Defensive cleanup**: `configure()` in single-model mode explicitly calls
    `remove_env_keys_from_settings` for all multi-model-only keys after applying the
    patch, catching any leftovers even when the tracking record was empty.

## [1.1.6] — 2026-05-07 — fix ai-resources audit

### Fixed

- **`ai-resources audit` rewritten from scratch** — the previous implementation
  called `litellm.container_logs()` (a function that never existed) and expected
  LiteLLM to emit cost lines in a specific log format it never produced. The
  command crashed immediately on every invocation.

  The new implementation reads directly from Claude Code's JSONL session files
  (`~/.claude/projects/<slug>/*.jsonl`), which contain the authoritative token
  usage and model name for every API call made by the cockpit and all subagents.

  New flags:
  - `--days N` — limit report to last N days (default: all time)
  - `--verbose` / `-v` — show per-session breakdown table (date, model, calls,
    output, cache-read, cost)

  Cost estimates use a built-in price table (Anthropic + Google public list
  prices). Gemini calls routed via LiteLLM appear with their correct provider
  model name when the gateway returns it, confirming delegation is working.

## [1.1.5] — 2026-05-07 — full workflow coverage + orchestrator delegation rules

### Added

- **10 new delegation workflows** — the orchestrator now has a matching workflow
  for every common engineering task category. All delegate heavy work to cheap
  models (gemini-2.5-flash for reading/reporting, gemini-2.5-pro for analysis,
  claude-sonnet-4-6 for implementation). New workflows:

  - **`_auto-delegate`** — catch-all for any non-trivial task not covered by a
    specific workflow. Assesses task type (read-only vs write), then delegates
    to `generalPurpose` or `implementer` accordingly. Prevents the orchestrator
    from ever doing multi-step work directly in the main thread.

  - **`merge-and-document`** — merge a feature branch to develop and write/update
    SDD specs from agent-output. Phases: `explore` (gemini-flash) reads diffs and
    existing specs → `doc-writer` (gemini-flash) writes all SDD + BDD files →
    `implementer` (sonnet) commits and merges.

  - **`k8s-debug`** — Kubernetes cluster debugging. Covers pod crashes, OOMKilled,
    pending pods, service/ingress issues, resource pressure, event storms.
    Phases: `explore` gathers kubectl state → `generalPurpose` diagnoses → 
    `implementer` applies fix.

  - **`cloud-ops`** — Cloud resource queries and external API integration
    (AWS, GCP, Azure, Stripe, Twilio, GitHub, etc.). Gather resource state →
    analyze → optionally act.

  - **`incident-response`** — Full production incident lifecycle. Phases: `explore`
    (fast triage, all signals) → `generalPurpose` (root cause, blast radius) →
    `implementer` (hotfix/rollback) → `doc-writer` (postmortem).

  - **`log-triage`** — Log collection and analysis from any source (local files,
    Docker, kubectl, CloudWatch, Datadog, Loki, GCP Logging). Read-only:
    `explore` collects → `generalPurpose` analyzes patterns and root cause.

  - **`ci-debug`** — CI/CD pipeline failure diagnosis. Covers GitHub Actions,
    GitLab CI, failing builds, flaky tests, missing env vars.
    Fetch logs → diagnose → fix.

  - **`refactor`** — Structured code refactoring without behavior change.
    Assess technical debt → plan atomic steps → implement → test for regressions
    → review. First step adds characterization tests if coverage is absent.

  - **`dependency-audit`** — Dependency security and version audit. Runs
    `npm audit`, `cargo audit`, `pip audit`, etc. Plans batched upgrades by
    risk (patch/minor/major). Applies Batch 1+2 automatically; Batch 3
    (breaking) requires explicit user approval per package.

  - **`db-investigation`** — Database performance and schema investigation.
    Covers slow queries (EXPLAIN ANALYZE), missing indexes, N+1 ORM patterns,
    schema drift, migration conflicts, data integrity.

- **Orchestrator context discipline rule** in CLAUDE.md — explicit protocol
  stating the orchestrator must ONLY read handoff files between phases and must
  never use `Read`/`Bash`/`Edit` on project files directly when a workflow is active.

- **Model routing guide** in CLAUDE.md — explicit table mapping each subagent
  type to its model tier with a budget heuristic (< 500 tokens per phase
  transition for the orchestrator).

- **Updated workflow trigger table** in CLAUDE.md — now lists all 17 workflows
  including the new ones. `_auto-delegate` is always last (catch-all). Match
  order is defined; trivial tasks are the only case that skips workflow loading.

## [1.1.4] — 2026-05-07 — executors set / apply / tune

### Added

- **`ai-resources executors set <role> [model]`** — reassign a single role's
  model without running the full setup wizard. If `model` is omitted, an
  interactive provider → model menu is shown. Provider is auto-inferred from
  the model name (e.g. `gemini-2.5-pro` → `google`). Writes `executors.yaml`
  and regenerates `~/.claude/agents/*.md` automatically.

- **`ai-resources executors apply [profile]`** — switch all role assignments
  to a named profile in one command (e.g. `cost-optimized`, `quality-first`,
  `all-gemini`). Omit the profile name for an interactive selection menu that
  shows each profile's description. Displays a before/after diff table and
  asks for confirmation before writing.

- **`ai-resources executors tune`** — interactive bulk editor: shows the
  current role table, lets you checkbox multiple roles to reassign, then
  walks through provider + model selection for each. Optionally edits
  `defaults` (max_retries, timeout). Writes and regenerates on confirm.

- **`cockpits/claude.regenerate_agents(executors, mode)`** — public function
  that regenerates `~/.claude/agents/*.md` from `executors.yaml` without
  running the full setup. Used by the new executors subcommands; can also be
  called programmatically.

## [1.1.3] — 2026-05-07 — software-architect as designer + cost optimizations

### Changed

- **`software-architect` redesigned — Design Mode + Validate Mode** — the agent now
  produces an explicit architecture design *before* validating the planner's plan.
  Mode 1 (Design): layer map, pattern selection, SOLID decisions (DIP/SRP/OCP),
  key interfaces/contracts the implementer must define first, proposed module/file
  structure, Mermaid diagram. Mode 2 (Validate): checks the plan for consistency
  with the design. Output artifact includes `Architecture_design_ref` and
  `Design_summary` fields in the handoff.

- **All 5 stack personas updated** — `software-architect-symfony`, `-angular`,
  `-dart-flutter`, `-api-platform`, `-devops` each have a new `## Design Output`
  section specifying what to produce per stack (layer maps, provider trees,
  resource models, module structure, IaC interface design).

- **`_feature-template.workflow.yaml` architect step** — prompt now has explicit
  Step 1 (design) and Step 2 (validate) so the agent cannot skip design and
  go straight to approve/reject.

- **`infra-triage.workflow.yaml` analyze step** — added architecture design phase
  (module structure, IaC pattern selection, rollback design) before the fix
  strategy, so infrastructure fixes also get proper design upfront.

- **`cost-optimized` profile** — `security-auditor` moved from
  `anthropic/claude-sonnet-4-6` to `google/gemini-2.5-pro`. The audit role is
  read-heavy scanning; Gemini Pro handles it at ~70% lower cost with equivalent
  quality for SAST/dependency/secret detection work.

- **All profiles** — `max_retries` reduced from `3` to `1`. Under rate limiting,
  retrying a 30 K-token request 3× bills the full token count each attempt.
  One retry is sufficient; repeated failures indicate a real issue requiring
  attention, not more retries.

## [1.1.2] — 2026-05-07 — README refresh & version command fix

### Added

- **Orchestrator Delegation Protocol** — added explicit delegation rules to
  `CLAUDE.md`: when to spawn `explore`, `generalPurpose`, or `doc-writer`
  instead of reading files directly, plus a token budget check heuristic.
- **`doc-writer` subagent (Gemini Flash)** — new agent definition for
  documentation update tasks. Handles all reading + drafting; orchestrator
  applies final diffs only.

### Changed

- **`software-architect` routed to `gemini-2.5-pro`** in `executors.yaml`
  and agent frontmatter (was `claude-sonnet-4-6`).

### Documentation

- **README** — comprehensive update for v1.1.x: highlights banner, new CLI
  flags (`--dry-run`, `--non-interactive`, `--profile`), `setup --dry-run`
  example, mode-switching teardown explanation, Claude Code OAuth section,
  expanded directory map (full `setup/` subpackage), new Profiles table,
  and `docs/multi-model.md` / `docs/litellm-service.md` in Further Reading.
- **`ai-resources version`** — now correctly reports `1.1.2` (working-tree
  `__init__.py` was inadvertently left at `1.0.0` after the v1.1.1 release).

## [1.1.1] — 2026-05-07

### Fixed

- **Formula PyPI URLs** — all 16 resource URLs updated to the hash-based path
  format now required by PyPI (legacy `/source/<letter>/…` paths return 404).
  Also corrects the `questionary` sha256 which was wrong in v1.1.0.

## [1.1.0] — 2026-05-07 — Dry-run, teardown & OAuth fixes

### Added

- **`--dry-run` flag** for `ai-resources setup` — renders all generated configs
  (`litellm.yaml`, `docker-compose.yaml`, `settings.json` patch) and starts a
  temporary gateway for live smoke tests, without writing anything to disk.
- **`InstallTracking` audit trail** (`state.py`) — records exactly what the wizard
  installed (LiteLLM method, Docker image, lifecycle unit, env keys, cockpit env
  patches). Pre-existing artifacts are never tracked, so teardown only undoes wizard
  work.
- **Multi-model → single-model teardown** — when the user switches modes, the wizard
  presents a confirmation list and cleanly removes every tracked artifact.
- **Claude passthrough models** in `litellm.yaml` — all current Claude model IDs
  (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) are
  auto-added as passthrough entries when Anthropic is enabled, preventing
  `ProxyModelNotFoundError` for Claude Code internal requests.
- **`allow_requests_on_db_unavailable: true`** in gateway `general_settings` —
  lets OAuth session tokens pass through the localhost gateway without a DB lookup.
- **`credentials.update_env_tracked()` / `credentials.remove_keys()`** — new
  helpers that return which keys were net-new / removed, enabling precise teardown.
- **`_shared.env_keys_added_by_patch()` / `_shared.remove_env_keys_from_settings()`**
  — cockpit-level env teardown helpers.
- **`claude.teardown()`** — removes gateway env keys from `settings.json` on mode
  switch.
- **`claude.is_logged_in_via_oauth()`** — detects OAuth/firstParty mode via
  `claude auth status` JSON (with Keychain/credential-file fallback for older Claude
  Code versions). Used by wizard completion banner and `ai-resources doctor`.
- **`credentials.get_claude_code_keychain_key()` / `credentials.write_claude_code_keychain_key()`**
  — macOS Keychain helpers for reading/restoring the Claude Code API key.
- **`claude.fix_oauth_for_gateway()`** — automated logout → restore flow on macOS:
  saves key from Keychain, runs `claude logout`, writes key back so Claude Code
  starts in API key mode without prompting.
- **New profile `quality-first.yaml`** — replaces `unified-default.yaml`.
  Identical routing strategy; renamed for clarity.
- **`doctor` OAuth awareness** — in multi-model mode, shows an info notice when
  Claude Code is in OAuth mode with actionable guidance.

### Fixed

- **`vertex-enterprise.yaml` model IDs** — removed incorrect `@date` suffix from
  `claude-opus-4-7` and `claude-sonnet-4-6` (bare alias is correct per Vertex AI
  docs); `claude-haiku-4-5@20251001` retains the required suffix.
- **Provider model lists** (`providers.py`) — added `gpt-5-nano`, `gpt-4o-mini`,
  `gemini-2.5-flash-lite`; Vertex list now mirrors these.
- **Default profile in single-model mode** changed from `all-claude` to
  `cost-optimized`.
- **State deserialization** for nested dataclasses (`SetupState` → `InstallTracking`
  and other nested fields) now handled correctly via `_from_dict` registry.
- **Python 3.14+ compatibility** in dependency detection.
- **pip-venv as default LiteLLM install mode** — `pipx` no longer required for
  first-time installs; a dedicated virtualenv at
  `~/.config/ai-resources/venv/` is used instead.
- **Better LiteLLM install error reporting** — verbose log dump on failure.
- **Wizard auto-installs pipx** when selected as install method and pipx is absent
  (cross-platform: Homebrew on macOS, `pip install --user pipx` elsewhere).

### Changed

- `ANTHROPIC_API_KEY` removed from the Claude Code `settings.json` env patch in
  multi-model mode — Claude Code reads its key from the macOS Keychain and the
  gateway now uses `allow_requests_on_db_unavailable` instead.

### Skills

- All community and curated skills: bumped upstream `git_revision` to latest;
  removed redundant `(triggers: …)` inline comment from SKILL.md descriptions.

## [1.0.0] — 2026-05-07 — Multi-model orchestration

Major refactor introducing per-role LLM routing via local LiteLLM gateway.
**Breaking changes** in CLI surface; one-time migration via `ai-resources setup`.

### Added

- **Multi-model mode** — each subagent role runs on a different LLM (Claude,
  Gemini, GPT, Vertex, Ollama) routed through LiteLLM. See
  `docs/multi-model.md` for the full guide.
- **Interactive setup wizard** (`ai-resources setup`) — 9-step `rich` +
  `questionary` UI: cockpit detection, LiteLLM install, provider creds,
  profile selection, per-role customization, lifecycle, smoke tests.
  Re-runs use prior answers as defaults.
- **LiteLLM gateway** — pipx-managed process (default) or remote endpoint.
  No Docker required. Service managed by launchd (macOS) / systemd-user (Linux).
  Lifecycle via `ai-resources daemon {start,stop,status,logs,update}`.
  Auto-start at every login. Wrapper script sources `.env` (chmod 600) and
  execs `litellm` directly — secrets never touch the service file.
- **Profiles** — `unified-default`, `all-claude`, `all-gemini`,
  `cost-optimized`, `vertex-enterprise` at `<kit>/profiles/*.yaml`.
- **`ai-resources doctor`** — full health check across config, credentials,
  gateway, cockpits, and live model round-trips.
- **`ai-resources executors {show,edit,test}`** — inspect or edit role→model
  mapping; test a single role's round-trip.
- **`ai-resources audit`** — cost report from gateway logs (per role / model).
- **Cockpit configurators** — modular per-cockpit setup for Claude Code,
  Cursor, Gemini CLI, Codex, Aider, Copilot, Windsurf, Continue.dev,
  OpenCode. Each writes its canonical config + multi-model protocol section.
- **Engram MCP in Gemini CLI** — same schema as Claude Code; both clients
  hit the same memory backend regardless of cockpit.
- **`rules/017-multimodel-routing.mdc`** — kit-internal rule explaining
  the multi-model architecture.
- **Setup state** — `~/.config/ai-resources/setup-state.yaml` persists
  wizard answers; `executors.yaml`, `litellm.yaml`, `docker-compose.yaml`,
  `.env` (chmod 600) live alongside.

### Changed

- **Package layout** — `scripts/kit.py` is now a thin shim. All logic moved
  to `scripts/ai_resources/` package: `cli.py`, `generate.py`, `daemon.py`,
  `doctor.py`, `executors_cmd.py`, `audit.py`, plus `setup/` subpackage with
  `wizard.py`, `state.py`, `detection.py`, `ui.py`, `credentials.py`,
  `litellm.py`, `providers.py`, `profiles.py`, `smoke.py`, `install.py`,
  and `cockpits/{claude,gemini,cursor,codex,aider,copilot,windsurf,continue_dev,opencode}.py`.
- **Subagent `model:` field** — now passes through verbatim to the gateway.
  Legacy `strong`/`fast`/`inherit` mapping preserved for single-model mode.
- **Homebrew formula** — uses `Language::Python::Virtualenv` to install
  `rich`, `questionary`, `prompt_toolkit`, `pyyaml`, `httpx` (and transitive
  deps) into a venv at `libexec/venv/`. The `ai-resources` wrapper uses
  this venv.
- **`rules/050-subagent-delegation.mdc`** — new section linking to
  multi-model routing rule.

### Removed

- `_MODEL_MAP` constraint to opus/haiku/inherit — replaced by passthrough.
- `_STUB_TARGETS` monolithic dict and `_setup_*` per-target functions —
  replaced by `cockpits/*.py` module registry.
- `cmd_setup` monolith from `kit.py` — replaced by `wizard.py`.
- Argparse-only CLI surface — `setup` is now interactive by default.

### Migration

- v0.7.x users: `ai-resources setup` detects existing single-mode config and
  offers to upgrade. State is migrated to new `setup-state.yaml`.
- The legacy `python3 scripts/kit.py setup --target claude` style still
  works (delegated to the new wizard).

## [0.7.1] — 2026-04-10

### Removed

- **Workflow enforcement hook**: removed the `UserPromptSubmit` prompt hook that blocked trivial/conversational messages. The hook ran an LLM without conversation context on every message, causing false positives that blocked simple questions with "Operation stopped by hook". Workflow routing is already handled by the Workflow Discovery Protocol in CLAUDE.md — which has full conversation context and is more accurate.
- **`_build_hooks_config()`** and **`_merge_settings_with_hooks()`**: no longer needed without the hook.

### Added

- **`_cleanup_stale_hooks()`**: `kit.py setup` now removes stale `UserPromptSubmit` hooks from `settings.json` left by v0.7.0, so users don't need to manually clean up.

## [0.7.0] — 2026-04-10 [YANKED]

### Added

- **Workflow enforcement hook** _(yanked — blocked conversational messages)_: `kit.py setup --target claude` installed a `UserPromptSubmit` prompt hook. Removed in v0.7.1 due to false positives.
- **`_build_hooks_config()`**: generates the hooks structure dynamically from scanned workflow triggers.
- **`_merge_settings_with_hooks()`**: replaces `_merge_json_file` for settings.json to handle both env vars and hooks (array-based deep merge).

### Changed

- **`_setup_claude()`**: now calls `_merge_settings_with_hooks()` instead of `_merge_json_file()` for settings.json, merging env vars and hooks in a single operation.

## [0.5.1] — 2026-04-01

### Added

- **Agent Team blueprints**: 7 pre-defined team compositions (Feature Development, Bug Fix, Security Audit, Production Triage, Infrastructure & Platform, Dependency Maintenance, Code Review) generated into `~/.claude/CLAUDE.md`. Each blueprint includes: trigger conditions, teammate list with roles, workflow sequence, parallel groups, and domain persona hints.
- **Teams vs Subagents guidance**: CLAUDE.md now includes a decision table and step-by-step instructions for when and how to create Agent Teams via `TeamCreate` + `SendMessage`.
- **Specialist agents in `_ROLE_TOOLS`**: `crashlytics-fixer`, `sentry-fixer`, `package-upgrade`, `terraform-maintainer`, and `crossplane-upjet-maintainer` now explicitly listed with full tool access (`None`).

### Changed

- **`_setup_claude()` refactored**: replaced inline agent-table + teams stub (20 lines) with `_build_teams_section()` function that generates the full agent catalog and team blueprints documentation.

## [0.5.0] — 2026-04-01

### Added

- **Claude Code subagent generation**: `kit.py setup --target claude` now auto-generates Claude Code subagent definitions (`~/.claude/agents/*.md`) from `agents/roles/*.md`. Each role gets proper tool restrictions and model mapping (e.g. `strong` → `opus`). Cursor-specific references are rewritten automatically.
- **Claude Code settings.json management**: setup merges `AGENT_SKILLS_ROOT` and `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` into `~/.claude/settings.json` idempotently — no manual env vars or config needed.
- **Agent Teams support**: enabled automatically by setup. Workflow steps with matching `execution_hints.parallel_group` can run as concurrent Agent Team teammates.
- **`execution_hints` in workflow YAML**: new optional field for runtime-aware parallelism. Steps sharing a `parallel_group` name can run concurrently on capable runtimes (Claude Code Agent Teams, Cursor parallel Composers). Runtimes without support ignore hints and execute sequentially.
- **WORKFLOW_CONTRACT.md**: documented `execution_hints` schema with runtime interpretation table.

### Changed

- **Refactored setup dispatch**: replaced 9 per-target `_write_*_stub` functions + 4 Claude-specific helpers (13 functions) with a data-driven `_STUB_TARGETS` table, one `_write_stub()`, and one `_setup_claude()`. Net result: 43 → 38 functions with more functionality.
- **New reusable primitives**: `_merge_json_file()` (idempotent JSON merge), `_ensure_symlink()` (idempotent symlinks), `_install_claude_plugin()` (generic plugin installer). All usable by future targets.
- **Feature workflow** (`_feature-template`): `test`, `review`, `security` steps now have `parallel_group: post-implement`.
- **Bugfix workflow** (`_bugfix-template`): `test`, `security` steps now have `parallel_group: post-fix`.

## [0.4.0] — 2026-03-24

### Added

- **Orchestration guide** (`docs/orchestration.md`): detailed documentation with 10 Mermaid diagrams covering all workflow pipelines, skill discovery flow, subagent delegation, handoff protocol, conditional branching, and domain detection.
- **Expanded README**: directory map, skill categories table, workflow overview, agent roles/personas reference, orchestration diagram, key concepts (skill/workflow discovery, zero-trust engineering).
- **Skill triggers for all 116 skills**: every skill now has meaningful `triggers` keywords (file globs, technology names, action phrases) for accurate auto-discovery. Previously 61/116 had empty triggers.
- **Inline frontmatter parsing** in `kit.py`: the index generator now supports `triggers: "value"` (inline string) and multi-line YAML `description: >` with embedded `(triggers: ...)`, in addition to YAML list format.

### Changed

- **Full English translation**: all documentation, CLI messages, script output, CHANGELOG, README, SKILL.md files, and Python user-facing strings translated from Spanish to English. Includes version-commit.py prompts (`s/n` → `y/n`), reason messages, labels, and comments.
- **normalize-changelog.py**: added English summary keys alongside legacy Spanish keys for backward compatibility with existing changelogs.

## [0.3.1] — 2026-03-24

### Added

- **Workflow Discovery Protocol:** `_discovery_recipe()` now generates a "Workflow Discovery Protocol" section **before** Skill Discovery in every agent stub. The section includes a dynamically-built trigger table (scanned from `workflows/*.workflow.yaml` at setup time) and a MANDATORY rule that forbids substituting built-in tools (e.g. `EnterPlanMode`, ad-hoc Plan agents) for workflow-defined phases.
- **`_scan_workflow_triggers()` helper:** reads all `.workflow.yaml` files and extracts `name` + `trigger` fields for the generated table.
- **`_workflow_recipe()` helper:** builds the Workflow Discovery Protocol markdown from scanned triggers — single source, shared across all 9 agent stubs.
- **Workflow contract** added to Key paths table in generated stubs.

### Changed

- **All 9 agent stubs updated:** Claude Code, Cursor, Gemini CLI, OpenCode, Codex, GitHub Copilot, Windsurf, Continue.dev, and Aider now receive workflow discovery instructions alongside skill discovery — workflows are evaluated first.

## [0.3.0] — 2026-03-24

### Added

- **Universal Skill Discovery Protocol:** every agent stub now includes a 5-step discovery recipe that tells the agent how to read `skills-index.json`, match skills by description/triggers, and load the relevant `SKILL.md` files on demand. This replaces the previous minimal pointer stubs.
- **New setup targets:** `windsurf` (`~/.codeium/windsurf/memories/global_rules.md`), `continue` (`~/.continue/AGENT_KIT.md`), `aider` (`~/.aider/CONVENTIONS.md`). Total: 9 agent targets + MCP + symlinks.
- **Skill symlinks for native discovery:** `kit.py setup` creates symlinks from `~/.claude/skills/ai-resources` and `~/.agents/skills/ai-resources` (Codex) to the centralized `skills/` directory, enabling native SKILL.md discovery in agents that support it.
- **`_discovery_recipe()` helper:** single-source function that generates the discovery instructions, shared across all agent stubs for consistency.

### Changed

- **All existing stubs rewritten:** Claude Code, Cursor, Gemini CLI, OpenCode, Codex, and GitHub Copilot stubs now include the full discovery protocol and refresh hints (previously only had minimal path references).
- **`cmd_setup` refactored:** dispatch table replaces if/elif chain — easier to extend with new agent targets.
- **Stub function signatures unified:** all `_write_*_stub` functions now receive `(ak_s, hint, *, dry_run)` for consistency.
- **CLI `--target` choices expanded:** added `windsurf`, `continue`, `aider` to the argparse choices.

## [0.2.1] — 2026-03-23

### Changed

- **Skills, agents, roles, workflows:** removed all references to internal projects, companies, and repository names; kit is now fully agnostic and reusable by any team or organization.
- **`flutter-icons` skill:** brand color placeholder (`#006a64`) in config examples now carries explicit `← Replace with your brand color` labels on every occurrence to prevent agents from applying it literally.
- **Terraform scripts:** `upgrade-providers.py` regex now matches any Terraform Cloud org (not hardcoded); `upgrade-all-with-deps.py` renamed `WILDBIT_MODULE_RE` → `TF_PRIVATE_MODULE_RE`.

### Fixed

- **`terraform-version-commit` script:** removed non-generic auto-detect fallback for dependent projects; `--dependent-projects` must now be passed explicitly (safe default: no dependents processed if omitted).
- **`maintain.sh`:** dependent-projects argument is now only forwarded when `DEPENDENT_PROJECTS` env var is set (no fictitious default path).

## [0.2.0] — 2026-03-23

### Added

- **Engram as Homebrew dependency:** `Formula/ai-resources.rb` declares `depends_on "gentleman-programming/tap/engram"` — `brew install ai-resources` installs the `engram` binary automatically.
- **Automatic engram setup for Claude Code:** `kit.py setup` (targets `claude` and `all`) runs `claude plugin marketplace add Gentleman-Programming/engram && claude plugin install engram`, registering the MCP server, hooks, and the Memory Protocol skill without manual steps.
- **MCP engram in Cursor:** `resources.json` → `mcp.cursor.mcpServers` includes the `engram` entry (`engram mcp` stdio); applied when running `kit.py setup`.

### Changed

- `Formula/ai-resources.rb`: tag and version updated to `v0.2.0`; removed `revision 1` (it was a build patch, not applicable to the new version).

## [0.1.0] — 2026-03-23

First **usable** release with Homebrew. The previous **`v0.1.0`** tag did not install correctly (nonexistent tap / misplaced formula / `brew tap` without URL pointing to another repo); the current tag includes **`Formula/ai-resources.rb`** at the root and the correct tap command.

### Added

- Unified CLI `scripts/kit.py`: **`generate`** (import from `resources.json` + `skills-index.json`), **`setup`** (MCP, IDE stubs, workflow validation).
- Manifest `resources.json`; optional state `~/.config/ai-resources/state.json` after `setup`.
- **Homebrew:** formula **`Formula/ai-resources.rb`** at the root; **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (URL is required: without it Homebrew looks for `wildbitca/homebrew-ai-resources`) + **`brew install ai-resources`**. Installation via **git** at the tag (no `sha256` tarball). Private repo: `HOMEBREW_GITHUB_API_TOKEN`.

### Changed

- **README** focused on installation and usage with minimal commands.
- **Documentation:** history in this file; releases without helper scripts in repo (see checklist below). Removed `RELEASING.md`, `packaging/homebrew/README.md`, `tag-release.sh`, `bump-formula-sha.sh`.
- **Homebrew:** same repository for code and tap (separate `homebrew-ai-resources` no longer needed); removed `packaging/homebrew/`.

### Fixed

- **`ai-resources` command:** the wrapper no longer uses a fixed path to `opt/python@3.12/bin/python3` (which might not exist); it adds the `bin` of `python@3.12` to `PATH` and uses **`python3`** or **`python3.12`**. Formula **`revision 1`**.

---

When you publish **`vMAJOR.MINOR.PATCH`**, add a new section above `[Unreleased]` with that version and date, then move the relevant items from `[Unreleased]` into it.

## Release manager checklist

1. Choose SemVer and move entries in **`CHANGELOG.md`**.
2. Update **`Formula/ai-resources.rb`:** `tag:` and `version` matching the new tag.
3. Bump **`scripts/ai_resources/__init__.py`** `__version__` to match.
4. Commit as **`chore(release): vX.Y.Z — <summary>`** and open a pull request.
   The repository's ruleset requires both: changes go through a PR, and the message must match
   `^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)(\(scope\))?(!)?: …`.
   A bare `release:` prefix does not match and is only pushed by bypassing the rule.
5. **Tag the merge commit and push the tag:** `git tag -a vX.Y.Z -m "Release X.Y.Z"` · `git push origin vX.Y.Z`
6. **GitHub Release is created automatically** by `.github/workflows/release.yml` when the tag is pushed. Release notes are extracted from the matching `## [X.Y.Z]` block in this file.

Users upgrade with: `brew update && brew upgrade ai-resources` (private repo: `HOMEBREW_GITHUB_API_TOKEN`). The **README** must keep **`brew tap wildbitca/ai-resources https://github.com/wildbitca/ai-resources.git`** (do not omit the URL).
