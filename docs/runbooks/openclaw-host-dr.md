# OpenClaw host disaster recovery

> **STATUS: UNREHEARSED.** Every step below comes from the restore instructions that travel in
> each tarball's `INVENTORY.txt` and from the kit's own commands. Nobody has yet restored a real
> backup over an empty host and brought the gateway up from it (pitfall P5). Until someone does
> that on a throwaway host and writes down the real recovery time (section 6), treat this document
> as a plan, not as a proven procedure, and do not describe the backups as "verified restorable".

Scope: the machine that runs the OpenClaw gateway is lost or rebuilt. Written for the operator;
the gateway is started by the operator, never by an agent. For day-to-day operation see
`$AGENT_KIT/docs/openclaw/runbook.md` and the `openclaw-operations` skill; for a fresh build (no
backup involved) run the `openclaw-host-setup` workflow (`$AGENT_KIT/workflows/openclaw-host-setup.workflow.yaml`),
whose primary path is `ai-resources setup`.

## 1. What a tarball contains, and what it deliberately omits

One tarball per run, `openclaw-<timestamp>.tar.gz` (UTC), with its `.sha256` written last (a tarball
without the checksum is not closed and is never uploaded). From this change on (unreleased) the default is the **minimal**
backup (`OPENCLAW_BACKUP_MODE=minimal`): only state that is not reproducible and not provided by
ai-resources, about 0.3 GB and 80 s on the reference host (the legacy `full` archive was 3.7-11 GB
and 6-30 min). Inside:

| Member | What it is |
|---|---|
| `db/openclaw-global/<snapshot>/` | `openclaw backup sqlite create --global`: a compact, verified snapshot of the shared state database. **The global snapshot sanitizes the delivery queue**: queued outbound deliveries are not carried over, so a restore does not replay messages that were pending at backup time |
| `db/agent-<id>/<snapshot>/` | One snapshot per configured agent that has `agents/<id>/agent/openclaw-agent.sqlite` (an unconfigured directory such as `agents/openclaw` has none and is skipped) |
| `engram/engram.db` | A consistent `VACUUM INTO` snapshot of `~/.engram/engram.db` (engram is not OpenClaw-owned, so the native command does not apply) |
| `files.tar` | The allowlist, relative to `$HOME`: `.openclaw/{openclaw.json,.env,credentials,gateway.systemd.env,kit-host.env,kit-host-overrides.json5,config-journal-fingerprint.key,telegram.token,extensions,bin}`, `.openclaw/agents/main/agent/workshop-skills`, `.openclaw/agents/*/sessions`, the `AGENTS.md MEMORY.md USER.md SOUL.md IDENTITY.md TOOLS.md DREAMS.md DOCS.md memory/ monitors/` of every agent workspace that exists, `.claude/{CLAUDE.md,settings.json,hooks}`, `.config/shell`, `.config/ai-resources/{model-pins.json,operator-rules.md,lanes.yaml,offbox-wif-cred.json,setup-state.yaml}`, `.config/systemd/user` (units and drop-ins), and only `~/.local/bin/openclaw-*`. Excluded inside it: `*.bak`, `*.bak-*`, `*.bak.*`, `*.reconcile-*`, `*.lock`, `*-wal`, `*-shm` |
| `INVENTORY.txt` | Versions (openclaw, node, claude, ai-resources), the mode, the agents and their models, and the restore steps |

A live `.sqlite`, `-wal` or `-shm` is never copied: the OpenClaw docs forbid it, and the snapshots
above are the only database content. In `full` mode the members are the legacy ones instead:
`openclaw-state.tar.gz` (all of `~/.openclaw`), `ai-config.tar.gz`, `engram.db`, `INVENTORY.txt`.

**Treat every tarball as secret material.** The secret store and the bot token travel inside it.

Omitted on purpose, and why:

| Not in the backup | Why | How it comes back |
|---|---|---|
| `~/.claude` skills and subagents | Regenerable | `ai-resources setup` |
| `~/.claude/projects` transcripts (~1.2 GB) | History, not configuration | Not restored |
| `~/.openclaw` tmp, gocache, npm, venvs, worktrees, security-scan, browser, media, logs, `*.bak*` copies | Caches and reproducible or disposable data; they were most of the old 3.7-11 GB | Recreated on use |
| Kit roles, lanes, skills, subagents | `ai-resources setup` regenerates them | `ai-resources setup` |
| `~/.local/bin` other than `openclaw-*` | 559 MB of binaries that are installed, not written by hand | Reinstall |
| Project workspaces | They are git checkouts | `git clone` |
| The ten `openclaw-*` units and the host scripts | Templates and scripts now live in the kit | `ai-resources openclaw install-units --enable` |

**Omitted by accident, not by design:** tarballs taken **before ai-resources 1.9.0** did not carry
the watchdog, verify or team-watch scripts, nor most of the timer and service units (the list held
only `openclaw.json`, `gateway.systemd.env`, the gateway and watchdog units, and the backup and
maintenance scripts). Do not look for them in an older tarball: regenerate them from the kit in
step 9. From 1.9.0 on the list is complete.

## 2. Before you start

1. Get the newest tarball. Local: the backup directory (default `/srv/openclaw-backups/<tier>/`),
   if the disk survived. Off-box: the bucket the GitOps templates created
   (`templates/gitops/openclaw-backups/`); list `daily/`, take the newest `.tar.gz` and its
   `.sha256` (quote the glob: `gcloud storage ls "gs://<bucket>/daily/**"`).
2. Check it: `sha256sum -c <name>.tar.gz.sha256`, then `tar tzf <name>.tar.gz >/dev/null`.
3. Unpack to a staging directory on disk, **not `/tmp`** (a tmpfs sized in RAM):
   `mkdir -p /srv/restore && tar xzf <name>.tar.gz -C /srv/restore`. Keep the parent of the
   directory not group-writable: `openclaw backup sqlite verify` refuses a snapshot under one.
4. Read `INVENTORY.txt`. Do not delete the tarball until step 10 passes.

## 3. Restore, in this order

Run as the user that will own the gateway. Nothing here is run by an agent.

1. **Restore `~/.openclaw/kit-host.env` first.**
   `tar xpf /srv/restore/files.tar -C "$HOME" .openclaw/kit-host.env` (a `full`-mode tarball keeps it
   in `ai-config.tar.gz`: `tar xzf /srv/restore/ai-config.tar.gz -C "$HOME" .openclaw/kit-host.env`)
   With no version pin (the kit installs the latest openclaw), this file is the only record of
   which version was running: read `OPENCLAW_INSTALLED_VERSION` from it (`OPENCLAW_PREVIOUS_VERSION`
   is the one before). `INVENTORY.txt` prints the openclaw version the tarball was taken from: use it
   to cross-check, not instead. Also in this file: the operator id, backup dir, public domain and
   pod CIDR that the units, scripts and config block are rendered from. Restore
   `~/.openclaw/kit-host-overrides.json5` the same way if the host had one (the `keep`/`force` lists that
   decide which of your values setup leaves alone).
2. **Node and openclaw, at the recorded version.** Install Homebrew's node, then
   `npm install -g --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs openclaw@<recorded version>`
   (npm 11 blocks install scripts on global installs otherwise; pitfall T06). Installing the
   recorded version yourself matters: `ai-resources openclaw bootstrap` would install the latest
   when none is present, and a restore should come back on the version the state was written by.
   Confirm the native `.node` files exist and `openclaw --version` matches.
3. **Install the kit.** `brew install ai-resources`.
4. **State.** First verify every snapshot, one per timestamped subdirectory (the command takes the
   snapshot directory, not the repository):
   `for s in /srv/restore/db/*/*/; do openclaw backup sqlite verify "$s"; done`. Then restore each
   to a fresh path with `openclaw backup sqlite restore <snapshot> --target <new path>` (the
   `openclaw-global` snapshot is the shared state database, `agent-<id>` is that agent's) and place
   them where the gateway expects them. A `full`-mode tarball instead:
   `openclaw backup restore /srv/restore/openclaw-state.tar.gz`. Do not edit `openclaw.json` by hand
   afterwards.
5. **Memory.** `mkdir -p ~/.engram && cp /srv/restore/engram/engram.db ~/.engram/engram.db` (a `full`
   tarball has `engram.db` at the top level). Check it: `sqlite3 ~/.engram/engram.db "PRAGMA integrity_check"`.
6. **The rest of the hand-written config.** `tar xpf /srv/restore/files.tar -C "$HOME"` (`full`:
   `tar xzf /srv/restore/ai-config.tar.gz -C "$HOME"`). This puts back config, secrets, credentials,
   extensions, sessions, the workspaces' notes, the systemd units with their drop-ins and the `openclaw-*`
   host scripts, besides `CLAUDE.md`, `settings.json`, `hooks/`, the workspaces' `AGENTS.md` and memory,
   and the gateway unit. It overwrites `~/.claude/settings.json`; step 7 re-registers the kit's hooks.
7. **Regenerate the rest with the kit.** `ai-resources setup`: rebuilds `~/.claude` skills and
   subagents and, in its OpenClaw section, asks again about team narration, the gateway guard, the
   units, the canonical config block and the workboard. Its answers default to no on a fresh
   machine: re-take the ones the old host had (the recorded `kit-host.env` pre-fills the values).
   Anything that touches the running gateway is its own confirm. For a headless host,
   `ai-resources setup --non-interactive` re-applies only what was already agreed; the kit has no
   `--yes` flag.
8. **Workspaces.** `git clone` the project checkouts the agents work in, at the paths
   `openclaw.json` names. For a workspace that has no `AGENTS.md` after step 6,
   `ai-resources openclaw agent-new <agent-id> <workspace>`. Send `/new` in each Telegram topic once
   (pitfall T19) so the agent starts with its `AGENTS.md`.
9. **Units and scripts.** `ai-resources openclaw install-units --dry-run`, then
   `ai-resources openclaw install-units --enable`: the eighteen `openclaw-*` units are rendered from
   templates and run the scripts from the kit. This is also the fix for a tarball that predates
   1.9.0 and lacks them.
10. **Start the gateway (operator).** `openclaw gateway install --force`, then
    `systemctl --user enable --now openclaw-gateway.service`, with
    `loginctl enable-linger <user>` so user timers run without a session. `systemctl --user` from
    SSH needs `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS`; `ai-resources openclaw status` sets
    them itself.
11. **Verify.** `ai-resources openclaw status`, then
    `bash $AGENT_KIT/scripts/openclaw/openclaw-verify.sh`, then
    `ai-resources openclaw bootstrap --dry-run` (expect zero changes). Confirm
    `~/.openclaw/watchdog.off` does not exist: while it exists the watchdog, the health check
    (`openclaw-health-restart.timer`) and the post-setup watch all stand down.

## 4. Off-box side (GitOps)

The bucket is created with `deletionPolicy: Orphan`, so losing the cluster does not delete the data.
If the cluster is what was lost, re-create the four manifests from the templates:
`ai-resources openclaw render-gitops-backups --list-markers`, then `--set KEY=VALUE ... --out <dir>`,
and commit the result to the infrastructure repo, where the real project, bucket, node and identity
values live. The uploader authenticates through workload identity federation with no JSON key: the
federation pool and the uploader's service account must exist before it can run.

## 5. What can go wrong

| Symptom | Likely cause |
|---|---|
| `openclaw` runs but a channel or plugin is missing | The version differs from the recorded one: check `OPENCLAW_INSTALLED_VERSION` |
| `systemctl --user` says it cannot connect to the bus | No `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS`, or linger is off |
| Gateway is `inactive` after a repair | `doctor --fix` was run undrained: use `ai-resources openclaw doctor` (pitfall T01) |
| The hooks are not registered after step 6 | `settings.json` was overwritten: run `ai-resources setup` again |
| The team hook is silent | `OPENCLAW_NARRATION` is not in `kit-host.env`: absent means off |

## 6. Rehearsal: pending operator step

Not done. On a throwaway host or VM (never the production host), follow sections 2 and 3 with a real
tarball from the bucket, time it, and record: what was missing from this document, the real recovery
time, and whether `openclaw backup restore` over an empty `~/.openclaw` brings the gateway up.
Only then remove the UNREHEARSED banner at the top. Track it as its own task.
