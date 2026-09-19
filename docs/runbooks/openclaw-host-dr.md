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

One tarball per run, `openclaw-<timestamp>.tar.gz`, with its `.sha256` written last (a tarball
without the checksum is not closed and is never uploaded). Inside:

| Member | What it is |
|---|---|
| `openclaw-state.tar.gz` | OpenClaw's own verified backup: config, state, per-agent databases, secret store |
| `ai-config.tar.gz` | The hand-written config that lives in no repo: `~/.claude/CLAUDE.md`, `settings.json`, `hooks/`, `~/.config/shell/`, **`~/.openclaw/kit-host.env`**, the gateway unit, the systemd units and host scripts that existed, and the `AGENTS.md`, `MEMORY.md`, `USER.md`, `memory/` of every agent workspace |
| `engram.db` | A consistent SQLite snapshot of `~/.engram/engram.db` |
| `INVENTORY.txt` | Versions (openclaw, node, claude, ai-resources), the agents and their models, and the six restore steps |

**Treat every tarball as secret material.** The secret store and the bot token travel inside it.

Omitted on purpose, and why:

| Not in the backup | Why | How it comes back |
|---|---|---|
| `~/.claude` skills and subagents | Regenerable | `ai-resources setup` |
| `~/.claude/projects` transcripts (~1.2 GB) | History, not configuration | Not restored |
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
   `mkdir -p /srv/restore && tar xzf <name>.tar.gz -C /srv/restore`.
4. Read `INVENTORY.txt`. Do not delete the tarball until step 10 passes.

## 3. Restore, in this order

Run as the user that will own the gateway. Nothing here is run by an agent.

1. **Restore `~/.openclaw/kit-host.env` first.**
   `tar xzf /srv/restore/ai-config.tar.gz -C "$HOME" .openclaw/kit-host.env`
   With no version pin (the kit installs the latest openclaw), this file is the only record of
   which version was running: read `OPENCLAW_INSTALLED_VERSION` from it (`OPENCLAW_PREVIOUS_VERSION`
   is the one before). `INVENTORY.txt` prints the openclaw version the tarball was taken from: use it
   to cross-check, not instead. Also in this file: the operator id, backup dir, public domain and
   pod CIDR that the units, scripts and config block are rendered from.
2. **Node and openclaw, at the recorded version.** Install Homebrew's node, then
   `npm install -g --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs openclaw@<recorded version>`
   (npm 11 blocks install scripts on global installs otherwise; pitfall T06). Installing the
   recorded version yourself matters: `ai-resources openclaw bootstrap` would install the latest
   when none is present, and a restore should come back on the version the state was written by.
   Confirm the native `.node` files exist and `openclaw --version` matches.
3. **Install the kit.** `brew install ai-resources`.
4. **State.** `openclaw backup restore /srv/restore/openclaw-state.tar.gz` (or unpack it over
   `~/.openclaw`, the alternative `INVENTORY.txt` gives). Do not edit `openclaw.json` by hand
   afterwards.
5. **Memory.** `mkdir -p ~/.engram && cp /srv/restore/engram.db ~/.engram/engram.db`.
6. **The rest of the hand-written config.** `tar xzf /srv/restore/ai-config.tar.gz -C "$HOME"`.
   This puts back `CLAUDE.md`, `settings.json`, `hooks/`, the workspaces' `AGENTS.md` and memory,
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
   `ai-resources openclaw install-units --enable`: the ten `openclaw-*` units are rendered from
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
    `~/.openclaw/watchdog.off` does not exist.

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
