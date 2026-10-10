# Changelog

All notable changes to **ai-resources** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). **Release versions match Git tags** `vMAJOR.MINOR.PATCH`.

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
