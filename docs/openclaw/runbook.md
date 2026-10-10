# Runbook — standing up and recovering the OpenClaw setup on bithome

Reference state: **2026-09-18**, OpenClaw `2026.9.4`, Node `26.9.0` (Homebrew),
Claude CLI `2.1.276`, ai-resources `1.8.1`, host `bithome` (192.168.100.6, single k3s node
and workstation at the same time).

Conventions used here:

- **`[idem]`** safe to run again.
- **`[1×]`** not idempotent: it changes state or creates something that must not be
  created twice.
- **`STOP — HUMAN ACTION REQUIRED`** a human decision or a secret. It cannot be
  automated and it never hides inside a block: it always stands on its own.
- Every `kubectl`/`flux`/`helm` carries an explicit **`--context default`**. That is not
  cosmetic: any `az aks get-credentials` rewrites `current-context` and points it at
  another client's cluster. A hook at `~/.claude/hooks/kubectl-context-guard.py` blocks the
  call when the flag is missing.

Literal error messages and command output are quoted in their original language
throughout, because that is what you will actually see on screen.

**Since ai-resources 1.9.0 most of this is done by the kit.** Where a step below has a kit
equivalent it is marked **[kit]** with the command; the manual recipe stays because it is what the kit
does, and it is what to fall back on if the kit is not installed. The primary way in is
`ai-resources setup` (next section). For a machine loss, start from
`docs/runbooks/openclaw-host-dr.md`, which restores `~/.openclaw/kit-host.env` first.

---

## How setup asks

`ai-resources setup` is the primary surface. After it detects the tools on the machine, step 7 lets
you pick the cockpits to configure; when OpenClaw is one of them it asks, in this order. Every answer
defaults to **no** on a first run, except the workboard, which defaults to yes:

| # | Question | What a yes does | Touches the running gateway? |
|---|---|---|---|
| 1 | Configure this machine as an OpenClaw host? | Enables the rest; a no asks nothing more (and offers to undo an earlier run) | no |
| 2 | Narrate the team's work into Telegram: **off** / milestones / every step | Writes `OPENCLAW_NARRATION` to `~/.openclaw/kit-host.env` and registers the narration hook in `~/.claude/settings.json`. Off removes the key, and the hook then publishes nothing | no |
| 2b | Telegram group chat id, once per routed agent (not `main`, not `claude`) | Pre-filled from the live `bindings`; empty leaves the agent alone; `-100...` warns. Writes only the root `bindings` array (validated with `--dry-run`, then its own confirm), `main`'s catch-all last. Never writes `channels`: a group missing from `channels.telegram.groups` is reported with the exact `openclaw config set` command (T35) | yes: `config patch` on `bindings` |
| 3 | Install the gateway guard hook? | Registers `openclaw_gateway_guard.py`, which denies an undrained `doctor --fix` or gateway stop | no |
| 4 | Install the `openclaw-*` systemd units? | Renders the eighteen units into `~/.config/systemd/user` and enables the ten timers (a separate confirm shows what will be written). A hand-installed health-restart copy is taken over here: its timer is disabled and its files are moved to `~/.openclaw/backup/hand-units/<timestamp>/`, never deleted | reloads systemd, not the gateway |
| 5 | Apply the canonical config block? | Fills the keys you have not set (a value you set is **kept** and listed), validates the patch with `config patch --dry-run`, applies the hot keys after a confirm, and offers the restart-required ones (today `gateway.bind`) in a drained window only when no run is in flight; otherwise they are recorded as pending | hot keys: yes; restart-required keys: only in the drained window |
| 5b | Let the health check restart the gateway gracefully under memory pressure? **notify** / on / off (asked with 4) | Writes `OPENCLAW_GRACEFUL_RESTART` to `kit-host.env`. Unattended runs keep an existing value, else `notify` (see "Resource safety") | no |
| 5c | Let setup fill the resource guards? **on** / off (asked with 5) | Writes `OPENCLAW_RESOURCE_GUARDS`; `on` fills `mcp.sessionIdleTtlMs` and `agents.defaults.timeoutSeconds` when unset | no |
| 6 | Your Telegram id, backup dir, and (with 5) domain and ingress CIDR | Written to `kit-host.env`; each value is validated as you type. Empty skips the keys that need it | no |
| 7 | Enable the workboard plugin? | `openclaw plugins enable workboard`, after a confirm | yes: needs a restart |
| 8 | Write an `AGENTS.md` into workspaces that have none? | A template per workspace; an existing file is never replaced. The marked kit block is refreshed in every workspace on every run, whatever this answer (v1.9.7) | no |
| 9 | Check this host against the documented setup? | Runs `bootstrap --dry-run`, reports, and offers to fix; every fix asks again | maybe |

Rules the wizard keeps: **secrets are never asked** (use `openclaw configure`); anything that changes
the running gateway is its own confirm, printed in full and defaulting to no; **setup never makes the
gateway restart while it is live** (ADR-0003, T40): a key OpenClaw restarts for is applied only after its own
default-No confirm, while no agent run is in flight (the count is shown; an unreadable probe counts as busy),
inside a drained window, and never by an unattended run; a second run changes nothing; undoing it (answer no to the
first question on a later run and confirm the offer to undo the earlier one) removes exactly what the kit recorded, disables only
the timers it enabled, puts back a hand-installed hook it replaced, and does not restore a credential
value because it never stored one. `--dry-run` previews all of it and writes nothing.

The kit has no `--yes` flag. `ai-resources setup --non-interactive` reuses the saved answers and
prompts for nothing: it re-applies only the local pieces already agreed (`kit-host.env`, hooks,
`AGENTS.md`) and refreshes the kit block in every agent workspace and skips every confirm that would touch the running gateway.

The `ai-resources openclaw <verb>` commands are wrappers over the same functions, for headless runs
and disaster recovery: `status`, `doctor`, `bootstrap`, `install-units`, `agent-new`,
`render-gitops-backups`, `busy` (runs in flight; exit 0 idle, 1 busy, 2 unknown), `apply-pending`,
`pressure`, `graceful-restart`, `restart-report` (see "Resource safety") and `config-watch` (started by setup).

### Operator overrides (`kit-host-overrides.json5`)

The host profile only fills keys you have not set. To make setup manage a key you already set, or to
protect an array from additions, write `~/.openclaw/kit-host-overrides.json5` (it is backed up with
`kit-host.env`; it is parsed, never shell-sourced):

```json5
{
  // opt these keys (or everything under them) back into the profile value
  "force": ["gateway.bind", "tools"],
  // never touch these, not even to add array entries
  "keep": ["gateway.controlUi.allowedOrigins"],
}
```

A `*` segment matches any one segment; a lone `"*"` in `force` restores the 2.0.x behaviour (the profile
wins everywhere). A credential path is refused, and so is a path in both lists. The summary prints `kept your
value: ...` and `filled: ...` as key paths only. An agent still on a haiku primary is replaced regardless
(T29). Delete the file to return to the defaults.

### Pending restart-required keys (`apply-pending`)

A restart-required key that setup did not apply (unattended run, runs in flight, probe error, declined
confirm, teardown) is recorded in `setup-state.yaml`. `ai-resources openclaw status` and `ai-resources verify`
list the paths and the command:

```
ai-resources openclaw busy            # 0 idle, 1 busy, 2 unknown
ai-resources openclaw apply-pending   # dry run, then watchdog.off, stop, drain, patch, start, health
```

It refuses while runs are in flight and asks before it stops the gateway (see `--help` for the
non-interactive form). A hand-run `openclaw config set gateway.*` is outside this protection: OpenClaw
forces that restart after 300 s whatever is running (T40).

### Post-setup watch (`openclaw-config-watch-*`)

After a run that wrote OpenClaw config, setup starts a transient user unit that checks `openclaw health` every
30 s for 10 minutes. Three failures in a row revert the run: hot keys at once, restart-required keys in the
same drained window (never a blind patch on a live gateway), then a Telegram notice with key paths. Follow it
with `journalctl --user -u openclaw-config-watch-<id>`, cancel it with `systemctl --user stop
openclaw-config-watch-<id>`. It stands down on `watchdog.off`, and a newer setup run supersedes it. Without
`systemd-run` or a user bus setup says `post-setup watch not started`; there is no foreground fallback.
The unit runs with `RuntimeMaxSec=1800` and `TimeoutStopSec=600`: the drained window of a revert can take up to
~5.5 min by itself, so the unit must outlive it and be given time to unwind on SIGTERM.

## Verification: what setup left behind

`ai-resources setup` ends with step 10, **Verification**: a read-only check of exactly the cockpits step
9 applied, one line per finding with its remedy. The same checks run from `ai-resources verify` (add
`--json` for one object per finding, `--cockpit ID` to narrow it) and from section 4 of
`ai-resources doctor`. It writes nothing, needs no network and never restarts the gateway.

| Level | Meaning | Exit code |
|---|---|---|
| `error` | genuinely broken: a recorded managed block missing or duplicated, the gateway unit inactive or disabled, the gateway up but not answering, the `main` catch-all binding not last, the kit plugin linked but not loaded | 1 (setup, verify and doctor) |
| `warn` | advisory: stale routing text outside the markers, a shared identity (T37), a short-form model (T38), a disabled timer, a stale daily backup, a gateway bound to all interfaces, no off-box copy configured | 0 |

Setup and doctor now exit non-zero on an `error`, never on a `warn`. A probe that times out is reported as
"could not measure", never as broken. The off-box warning carries the literal line to add
(`OPENCLAW_OFFBOX_LIST_CMD=<your listing command>` in `~/.openclaw/kit-host.env`); the kit will not choose
a destination for you.

Repair for a dropped block (T36) is to re-run `ai-resources setup`. Standing host rules apply to
anything you do about a finding: never `openclaw doctor --fix` bare (use `ai-resources openclaw doctor`);
never stop the gateway from a session it launched (check `OPENCLAW_CLI`); never hand-edit
`~/.openclaw/openclaw.json` (`openclaw config set|patch --dry-run` first); whoever creates
`~/.openclaw/watchdog.off` removes it; update with `openclaw update`, never `npm i -g`.

## Pending operator steps

These need the real host and a maintenance window, so they were **not** run when 1.9.0 was built:

1. `touch ~/.openclaw/watchdog.off` first, and confirm it is gone at the end.
2. `ai-resources openclaw doctor`: confirm it drains, fixes and the gateway answers.
3. `ai-resources openclaw bootstrap --dry-run`: expect zero changes on the reference host.
4. `ai-resources openclaw install-units --dry-run`: diff against the live units.
5. `bash "$(brew --prefix ai-resources)/libexec/scripts/openclaw/openclaw-verify.sh"` once.
6. `ai-resources setup` against the real `~/.claude/settings.json`: confirm a delegated subagent
   narrates into its topic and a plain terminal session publishes nothing.
7. The restore rehearsal in `docs/runbooks/openclaw-host-dr.md` section 6 (on a throwaway host).

---

# PART A — Prerequisites and ordering

## A1. What must exist first

| Requirement | How to check | Why it blocks |
|---|---|---|
| Homebrew (linuxbrew) | `brew --version` | Node comes from here, and openclaw is installed with its npm |
| Tailscale joined to the tailnet | `tailscale ip -4` → `100.76.56.42` | the gateway binds that IP and the Ingress points at it |
| k3s with Traefik and servicelb | `kubectl --context default get svc -n kube-system traefik` → EXTERNAL-IP `192.168.100.6` | terminates TLS for `iai.wildbit.dev` |
| cert-manager with a ClusterIssuer | `kubectl --context default get clusterissuer` → `letsencrypt-prod` Ready | issues the cert over DNS-01, so no inbound reachability is needed |
| Crossplane + Cloudflare provider + `ProviderConfig wildbit-iac` | `kubectl --context default get providerconfig` | creates the DNS record and the bucket as IaC |
| Flux tracking `refs/heads/main` of `wildbitca/org-gitops` | `flux --context default get sources git` | this is what materialises the cluster side |
| A Claude account with a **Max subscription** | `~/.claude/.credentials.json` → `subscriptionType: max` | the `claude-cli` runtime uses native auth; without the subscription there is no inference |
| Claude CLI installed | `~/.local/bin/claude --version` | it is the binary that runs every turn |
| A Telegram bot and its token | `~/.openclaw/telegram.token` | the channel |
| `ai-resources` from brew | `brew list --versions ai-resources` | provides skills, subagents, hooks and the `/equipo` plugin |

**Identity values to have at hand** (these belong to this environment; change them if you
rebuild elsewhere): Telegram supergroup `-1003678125825`, operator `7961376547`, tailnet IP
`100.76.56.42`, cluster pod CIDR `10.42.0.0/24`, GCP project `wildbit-iac`, domain
`iai.wildbit.dev`.

## A2. Phase order and dependencies

```
A. prerequisites ───────────────────────────────────────────┐
                                                            │
B1 runtime (brew node + openclaw + unit + linger)           │
   │                                                        │
   ├──> B2 base config  ──┬──> B3 agents and workspaces      │
   │                      │         │                       │
   │                      │         └──> B6 extras (team hook,
   │                      │                whisper, workboard, streaming)
   │                      │
   │                      └──> B5 exposure ── needs: tailnet + Traefik
   │                              │            + cert-manager + Cloudflare/Flux
   │                              └──> (back to B2: publicOrigin, allowedOrigins,
   │                                    trustedProxies, allowRealIpFallback)
   │
   └──> B4 backup and watch ── the cluster side needs Flux + Crossplane
```

Two loops worth seeing before you start:

1. **B5 loops back into B2.** You cannot set `publicOrigin` or
   `controlUi.allowedOrigins` before the domain is decided, and `allowRealIpFallback` only
   reveals itself as necessary once the Ingress is actually in front. That is why B2 is
   applied in two passes.
2. **B4 straddles host and cluster.** The scripts and timers are host-side and can be left
   working locally; the off-box upload needs the `org-gitops` PR to be on `main`. Until
   then there is a backup but **no off-box backup**, which for a disk disaster is the same
   as having none.

---

# PART B — Rebuild from scratch

## B1. Runtime

### B1.1 System Node from Homebrew `[idem]`

```bash
brew install node          # 26.9.0 in the reference state
/home/linuxbrew/.linuxbrew/bin/node -v    # must be >=26.1 (or >=24.16 <25)
```

`openclaw@2026.9.4` declares `engines: {"node": ">=24.16.0 <25 || >=26.1.0"}`. Brew's
Node 26 satisfies the second branch.

Brew will warn that `node`, `npm` and `npx` are shadowed by fnm if fnm comes first in your
interactive PATH. **That is correct and must not be "fixed"**: your projects stay on fnm's
Node and the gateway uses absolute brew paths.

### B1.2 OpenClaw via brew's npm, allowing install scripts `[idem]`

**[kit]** `ai-resources openclaw bootstrap` does this (and B1.1, B1.3, linger and the backup
directories) idempotently. It installs the **latest** openclaw, not a pin, and records
`OPENCLAW_INSTALLED_VERSION` and `OPENCLAW_PREVIOUS_VERSION` in `~/.openclaw/kit-host.env`; the manual
recipe below pins `2026.9.4`, the reference state.

```bash
PATH=/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin \
  npm install -g --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs \
  openclaw@2026.9.4
/home/linuxbrew/.linuxbrew/bin/openclaw --version
```

**`--allow-scripts` is not optional.** npm 11 blocks install scripts for global installs by
default, and without them `koffi`, `tree-sitter-bash` and `protobufjs` end up half-built
while the install still reports success. Check that the native bits landed:

```bash
find /home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/node_modules \
  -name "*.node" -path "*linux-x64*" | head
# expected: tree-sitter-bash/prebuilds/linux-x64/tree-sitter-bash.node
#           @lydell/node-pty-linux-x64/prebuilds/linux-x64/pty.node
```

### B1.3 systemd unit generated by openclaw `[1× per runtime change]`

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
export PATH=/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$HOME/.local/share/pnpm

/home/linuxbrew/.linuxbrew/bin/node \
  /home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/openclaw.mjs \
  gateway install --force
```

Three things that cannot be simplified:

- **Brew's node is invoked by absolute path**, not `openclaw gateway install --force`. The
  shebang of `openclaw.mjs` is `#!/usr/bin/env node`: if fnm comes first in PATH, the unit
  is baked with fnm's node and you are back where you started.
- **The invoking shell's PATH is baked into the unit.** Make sure it carries no ephemeral
  directories (fnm's `fnm_multishells/<pid>_<ts>/bin` changes name every session).
- **Do not add drop-ins** under `openclaw-gateway.service.d/`. Doctor refuses to repair a
  unit whose environment comes from an operator drop-in, and it aborts halfway.

Expected result in `~/.config/systemd/user/openclaw-gateway.service`:

```ini
ExecStart=/home/linuxbrew/.linuxbrew/opt/node/bin/node --max-old-space-size=8192 \
  /home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/dist/index.js gateway --port 18789
Restart=always
TimeoutStopSec=330
KillMode=mixed
EnvironmentFile=-/home/bitgandtter/.openclaw/gateway.systemd.env
```

`opt/node/bin` is a stable brew symlink: it survives Node upgrades. That is the difference
from the fnm path, which was pinned to `node-versions/v24.21.0`.

### B1.4 Start on machine reboot `[idem]`

```bash
loginctl enable-linger $USER
systemctl --user enable --now openclaw-gateway.service
systemctl --user is-enabled openclaw-gateway.service   # enabled
loginctl show-user $USER -p Linger                     # Linger=yes
```

Without `linger`, a *user* service does not start until somebody logs in. That is the
difference between "starts on reboot" and "starts when I connect".

### B1.5 Memory ceiling without breaking unit ownership `[idem]`

```bash
systemctl --user set-property openclaw-gateway.service MemoryHigh=12G
systemctl --user show openclaw-gateway.service -p MemoryHigh --value
```

This writes into `~/.config/systemd/user.control/`, which is **not** the directory doctor
inspects, so the drop-in warning does not come back. It was added because the gateway
peaked at 16.9 GB RSS and 5.3 GB of swap within an hour; `--max-old-space-size` only covers
V8's heap, not the children (claude, engram, npx).

### B1.6 Exactly one copy of openclaw `[1×]`

```bash
~/.local/share/fnm/aliases/default/bin/npm -g uninstall openclaw   # if migrating from fnm
command -v openclaw        # must resolve to /home/linuxbrew/.linuxbrew/bin/openclaw
openclaw update status     # Install: npm · stable · up to date
```

Two copies mean `openclaw update` updates one while the service runs the other.

## B2. Base configuration

Everything through `openclaw config set` (**`[idem]`**, validated against the schema).
Never hand-edit `openclaw.json`: some keys require canonical form and doctor itself
rewrites legacy refs.

### B2.1 Models and effort

```bash
# claude-cli runtime for both models in use
openclaw config set 'agents.defaults.models["anthropic/claude-sonnet-5"].agentRuntime.id' claude-cli
openclaw config set 'agents.defaults.models["anthropic/claude-haiku-4-5"].agentRuntime.id' claude-cli

# haiku stays as utility/default; every orchestrating agent runs sonnet
openclaw config set agents.defaults.model.primary anthropic/claude-haiku-4-5
openclaw config set agents.defaults.thinkingDefault medium
for a in main snoutzone elinvo devops ai; do
  openclaw config set agents.entries.$a.model.primary anthropic/claude-sonnet-5
done
openclaw config set agents.entries.main.thinkingDefault high
openclaw config set agents.entries.claude.model.primary claude-kit/claude-sonnet-5
openclaw config set agents.entries.claude.tools.exec.mode full
```

`thinkingDefault` maps to the Claude CLI's `--effort` and to `MAX_THINKING_TOKENS`
(`medium`→8192, `high`/`xhigh`→16384, `max`→32768). Verifiable while a turn runs:

```bash
pgrep -af "/.local/bin/claude" | grep -oE "--effort [a-z]+|--model [a-z0-9.-]+"
```

**Why sonnet and not haiku**: on haiku the orchestrator announced work it had not done
(invented topics, workflow scripts with syntax errors). The marginal cost is small anyway:
the kit's subagents already run opus/sonnet through their frontmatter, and that is where
most of the consumption goes.

### B2.2 Heartbeats: off except for the orchestrator

```bash
openclaw config set agents.defaults.heartbeat.every 0m
openclaw config set agents.defaults.heartbeat.model anthropic/claude-haiku-4-5
openclaw config set agents.entries.main.heartbeat.every 2h
```

All five were at 30 min with `target: none`: roughly 240 agent turns a day delivering
nothing to anyone. The scheduler reconciles hot; check with
`openclaw cron list | grep -i heartbeat`.

### B2.3 Tools

```bash
openclaw config set tools.profile coding
openclaw config set tools.alsoAllow '["group:messaging"]'
```

The `coding` profile brings `group:fs`, `group:runtime`, `group:web`, `group:sessions`,
`group:memory`, `cron`… but **not** `group:messaging`, which is the `message` tool an agent
needs to create a topic or write into another one. Without `alsoAllow` the only path is
Bash + CLI, and that is exactly what the weaker model failed to find.

### B2.4 MCP: switch off what is not authorised

```bash
for s in sentry supabase stripe; do openclaw mcp configure $s --disable; done
```

All three demanded OAuth and failed **on every session start**. `supabase-admin` (token
based) is the one that works; the OAuth `supabase` entry was a duplicate.

⚠️ `supabase-admin` uses `npx -y @supabase/mcp-server-supabase@latest`: a floating
dependency resolved on every session. Pinning the version is outstanding debt.

### B2.5 Logs out of `/tmp`

```bash
openclaw config set logging.file /home/bitgandtter/.openclaw/logs/gateway.log
openclaw config set logging.maxFileBytes 33554432
```

By default the log lives in `/tmp/openclaw/openclaw-<date>.log`, and on bithome `/tmp` is
**a 16 GB tmpfs in RAM**: it evaporates on reboot, exactly when you want to know why it
rebooted.

### B2.6 Assorted hygiene

```bash
openclaw config set plugins.entries.browser.enabled false
openclaw config set browser.extensionRelay.allowLegacyAuth false
openclaw config set desktop.host.enabled false
openclaw config set plugins.entries.device-pair.config.publicUrl https://iai.wildbit.dev
openclaw config unset 'channels.telegram.groups["*"]'   # [1×] if it existed
```

`browser.enabled false` goes together with `allowLegacyAuth false`: declaring the `browser`
section makes doctor propose enabling the plugin, and pinning it to `false` silences the
proposal without enabling anything.

### B2.7 Second pass: exposure (after B5)

```bash
openclaw config set gateway.bind tailnet
openclaw config set gateway.publicOrigin https://iai.wildbit.dev
openclaw config set gateway.controlUi.allowedOrigins '["https://iai.wildbit.dev"]'
openclaw config set gateway.trustedProxies '["10.42.0.0/24"]'
openclaw config set gateway.allowRealIpFallback true
```

- `bind: tailnet` listens on `100.76.56.42:18789` **and** on `127.0.0.1:18789`. The second
  one is what keeps the local CLI and the scripts alive.
- `trustedProxies` is the pod CIDR: the gateway sees Traefik's pod IP (`10.42.0.242`), there
  is no SNAT.
- `allowRealIpFallback` is the non-obvious piece: this Traefik does **not** send
  `X-Forwarded-For` to this backend, and without it the gateway answers
  `403 {"error":{"type":"proxy_attribution_required"}}`.

### B2.8 Password authentication

**STOP — HUMAN ACTION REQUIRED**
The password is your choice and must not travel through shell history. In zsh (`read -p`
is bash syntax and leaves the variable empty):

```bash
read -rs "P?Password del UI de OpenClaw: "; echo
printf '%s' "$P" | openclaw secrets store set GATEWAY_AUTH_PASSWORD --kind secret --value-file -
unset P
openclaw secrets store list | grep PASSWORD    # GATEWAY_AUTH_PASSWORD [secret]
```

Then the wiring `[idem]`:

```bash
openclaw config set gateway.auth.password --ref-provider default --ref-source store --ref-id GATEWAY_AUTH_PASSWORD
openclaw config set gateway.auth.mode password
openclaw gateway restart
openclaw health          # the CLI MUST still answer
```

⚠️ Writing `auth.password` makes **`gateway.auth.token` disappear from the config** (the
value stays in the store). Rollback is two commands, not one:

```bash
openclaw config set gateway.auth.token --ref-provider default --ref-source store --ref-id GATEWAY_AUTH_TOKEN
openclaw config set gateway.auth.mode token && openclaw gateway restart
```

### B2.9 Closing the phase

```bash
openclaw config validate     # Config valid
openclaw gateway restart
openclaw health
```

## B3. Agents, topics and workspaces

### B3.1 Telegram channel

**STOP — HUMAN ACTION REQUIRED**
Bot token and access decisions:

```bash
install -m 600 /dev/null ~/.openclaw/telegram.token
# paste the BotFather token inside
```

```bash
openclaw config set channels.telegram.enabled true
openclaw config set channels.telegram.tokenFile '~/.openclaw/telegram.token'
openclaw config set channels.telegram.dmPolicy pairing
openclaw config set channels.telegram.groupPolicy allowlist
openclaw config set 'channels.telegram.groups["-1003678125825"].allowFrom' '["7961376547"]'
openclaw config set 'channels.telegram.groups["-1003678125825"].requireMention' false
openclaw config set channels.telegram.allowFrom '["7961376547"]'
openclaw config set channels.telegram.groupAllowFrom '["7961376547"]'
openclaw config set commands.ownerAllowFrom '["telegram:7961376547"]'
openclaw config set channels.telegram.actions.createForumTopic true
openclaw config set channels.telegram.actions.editForumTopic true
openclaw config set channels.telegram.commands.native true
openclaw config set channels.telegram.configWrites true
```

**STOP — HUMAN ACTION REQUIRED**
Bot privacy mode, in Telegram, by hand: chat with **@BotFather** → `/setprivacy` → pick the
bot → **Disable**. Without it the Bot API does not deliver group messages that do not
mention the bot. Objective verification:

```bash
T=$(cat ~/.openclaw/telegram.token)
curl -s "https://api.telegram.org/bot$T/getMe" | python3 -c \
  "import sys,json;print(json.load(sys.stdin)['result'].get('can_read_all_group_messages'))"
# True = privacy mode disabled
```

The `openclaw channels status` warning about privacy mode **is static**: it fires because
`requireMention=false` and never queries the real flag. Do not chase it.

**STOP — HUMAN ACTION REQUIRED**
The bot must be an administrator of the supergroup with "Manage Topics", or
`createForumTopic` answers `400 Bad Request: not enough rights to create a topic`.

```bash
ID=$(curl -s "https://api.telegram.org/bot$T/getMe" | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['id'])")
curl -s "https://api.telegram.org/bot$T/getChatMember?chat_id=-1003678125825&user_id=$ID" \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['result'];print(r['status'],r.get('can_manage_topics'))"
# administrator True
```

### B3.2 Create the topics `[1×]`

```bash
openclaw message thread create --channel telegram --target -1003678125825 \
  --thread-name "snoutzone" --message "Topic del proyecto pacha." --json
```

`--json` returns `topicId`. **A topic with no message in it never shows up in Telegram**,
which is why the command sends one immediately. Repeat per project and write down each id.

### B3.3 Register and bind the agents `[1× per agent]`

```bash
~/.openclaw/bin/oc-bind-topic.sh main      ~/.openclaw/workspace                  -1003678125825 1
~/.openclaw/bin/oc-bind-topic.sh snoutzone ~/Development/wildbit/pacha            -1003678125825 8
~/.openclaw/bin/oc-bind-topic.sh elinvo    ~/Development/wildbit/elinvo           -1003678125825 67
~/.openclaw/bin/oc-bind-topic.sh devops    ~/Development/wildbit/org-iac          -1003678125825 90
~/.openclaw/bin/oc-bind-topic.sh ai        ~/Development/wildbit/ai-resources     -1003678125825 315
```

The script creates the workspace if missing, registers the agent with its own `agentDir`
and session store, and points the topic at it. The gateway hot-reloads: no restart needed.

The `claude` agent is different: **it has no topic**. It is the unrestricted worker behind
`/claude` and `/equipo`.

```bash
openclaw agents add claude --workspace ~/Development --non-interactive
openclaw config set agents.entries.claude.model.primary claude-kit/claude-sonnet-5
openclaw config set agents.entries.claude.tools.exec.mode full
openclaw config set agents.defaults.subagents.allowAgents '["claude"]'
openclaw config set agents.defaults.systemAgent.agentId main
openclaw config set talk.agentId main
openclaw config set bindings '[{"agentId":"main","match":{"channel":"telegram","accountId":"*"}}]'
```

Orchestrator identity (what shows up in chat):

```bash
openclaw config set agents.entries.main.identity.name Jarvis
openclaw config set agents.entries.main.identity.emoji ⚡
openclaw config set agents.entries.main.identity.theme "directo, sin rodeos, con criterio propio"
```

### B3.4 One `AGENTS.md` per workspace `[1×, then maintained]`

**This is the piece most often forgotten and the one that hurts most.** OpenClaw launches
the Claude CLI with `--setting-sources user` and enforces it in code:

```
cli-runtime-args-*.mjs:53
  if (value !== "" && value !== "user")
    throw new Error("Claude CLI settings must be limited to user settings.");
```

Consequence: **a repository's `CLAUDE.md` is NEVER loaded in topic sessions**; only
`~/.claude/CLAUDE.md` is. What does get injected every session is the `AGENTS.md` of the
agent's workspace. So project rules live there, not in `CLAUDE.md`.

What does load in those sessions: **user-scope** subagents and skills (`~/.claude/agents`,
`~/.claude/skills`) and the hooks in `~/.claude/settings.json`.

Every `AGENTS.md` must carry at least:

1. What the workspace is (**umbrella** with no commits vs. a real repo) and the ban on
   committing at the root when it is an umbrella.
2. The project's critical rules, with verified values (the kubectl context and namespace
   that actually exist, not a list of contexts that do not).
3. The durable-documentation convention (never `.agent-output/`).
4. The **Work reporting** block (the hook posts team start/finish; the agent's own messages
   are for meaning, not for narrating tool calls).
5. Memory: `memory/YYYY-MM-DD.md`, `MEMORY.md`, `USER.md`.
6. Red lines: English in every written artifact, no Claude attribution in commits or PRs,
   nothing destructive without asking.

The four reference files are backed up in the session scratchpad (`agents-md-backup/`,
`main-AGENTS.md.bak`). They must also be excluded from the repo:

```bash
cat >> <repo>/.git/info/exclude <<'EOF'

# openclaw agent workspace files (local only, never commit)
/AGENTS.md
/SOUL.md
/IDENTITY.md
/USER.md
/MEMORY.md
/DREAMS.md
/BOOTSTRAP.md
/BOOTSTRAP.md.done
/memory/
EOF
```

`.git/info/exclude` rather than `.gitignore`: it is local and is not shared with the repo's
team.

⚠️ **Live sessions do not pick up a new `AGENTS.md`**: context is injected at session
start. After editing it, send **`/new`** in the topic.

## B4. Backup and watch

### B4.1 Staging directories `[1×, needs sudo]`

```bash
sudo install -d -o $USER -g $USER -m 755 \
  /srv/openclaw-backups /srv/openclaw-backups/{daily,weekly,monthly}
mkdir -p ~/.openclaw/logs
```

**Under `/srv`, never `/tmp`.** That is the literal lesson from the k3s backup runbook:
`/tmp` is a 16 GB tmpfs in RAM and an `rsync` that does not fit can leave an incomplete
backup reported as good.

### B4.2 The host scripts `[idem]`

**[kit]** The scripts ship in the kit under `scripts/openclaw/` and the units run them from there as
`/bin/bash <script>`: nothing is copied to `~/.local/bin` any more. Host values come from
`~/.openclaw/kit-host.env`. (On the reference host they originally lived in `~/.local/bin`; that is
historical.)

| Script | What it does |
|---|---|
| `openclaw-backup.sh <daily\|weekly\|monthly\|manual>` | `openclaw backup create --no-include-workspace --verify` + engram `VACUUM INTO` + tar of the hand-written config + `INVENTORY.txt`; one tarball and its `.sha256` **written last**; rotates 7/4/3; notifies on Telegram on failure |
| `openclaw-maintenance.sh` | pauses the watchdog, stops, **waits for the drain**, `doctor --fix`, `sessions cleanup --all-agents`, starts, polls health up to 2 min, checks backup age and new versions |
| `openclaw-watchdog.sh` | if the gateway is not active it starts it and **says so on Telegram**; paused with `~/.openclaw/watchdog.off` |
| `openclaw-verify.sh` | one-pass read-only check that the setup is as expected (checks 1-4 and 9 everywhere; the ingress, OTLP, Alloy and Flux checks are opt-in through `OPENCLAW_VERIFY_*`); `--notify`, from the daily timer, messages the operator only when something fails |
| `openclaw-team-watch.py` | the per-member live message launched by the narration hook at `every-step` |

Invariants that must survive any port of these scripts:

- The `.sha256` is written **after** the tarball: it is the only witness that the tarball is
  closed, and the uploader skips those without it.
- No `|| true` on anything that matters.
- The result is **asserted**: size ≥ 1 MB, `tar tzf` listable, `sha256sum -c`.
- `sessions cleanup` **requires `--all-agents`** with several agents, or it fails with
  "Multiple agents are configured, but session-store selection has no explicit owner."
- Poll health **for up to 2 minutes**: 12 s is not enough on a cold start.

### B4.3 Units and timers `[idem]`

**[kit]** `ai-resources openclaw install-units --enable` (or the setup question) renders the
eighteen units from `templates/systemd/` and enables the **ten** timers (three backup tiers,
the backup guard, the off-box uploader, maintenance, watchdog, `openclaw-verify.timer`, the daily models
update and the health check `openclaw-health-restart.timer`). The
recipe below is what it does.

```
~/.config/systemd/user/openclaw-backup@.service          Type=oneshot, ExecStart=…openclaw-backup.sh %i
~/.config/systemd/user/openclaw-backup-daily.timer       OnCalendar=*-*-* 03:30:00
~/.config/systemd/user/openclaw-backup-weekly.timer      OnCalendar=Sun *-*-* 03:45:00
~/.config/systemd/user/openclaw-backup-monthly.timer     OnCalendar=*-*-01 04:00:00
~/.config/systemd/user/openclaw-backup-guard.{service,timer}     OnCalendar=*-*-* 00,06,12,18:20:00
~/.config/systemd/user/openclaw-backup-uploader.{service,timer}  OnCalendar=*-*-* 04:40:00
~/.config/systemd/user/openclaw-maintenance.{service,timer}  OnCalendar=Sun *-*-* 04:30:00
~/.config/systemd/user/openclaw-watchdog.{service,timer}     OnBootSec=2min OnUnitActiveSec=2min
```

The guard and uploader are the host-native replacement for a hostPath-mounted Kubernetes
CronJob: that shape only works when the pod is scheduled on the exact host that owns the
backup directory, which a remote cluster's nodes cannot do. Running them as systemd user
timers on the host itself, alongside the producer timers above, needs no such placement
trick. Set `OPENCLAW_OFFBOX_BUCKET` in `~/.openclaw/kit-host.env` before enabling the
uploader timer, or it refuses to run.

All timers use `Persistent=true` (a run missed while the machine was off fires at boot) and
`RandomizedDelaySec`.

```bash
systemctl --user daemon-reload
systemctl --user enable --now openclaw-backup-daily.timer openclaw-backup-weekly.timer \
  openclaw-backup-monthly.timer openclaw-backup-guard.timer openclaw-backup-uploader.timer \
  openclaw-watchdog.timer openclaw-maintenance.timer openclaw-verify.timer
systemctl --user list-timers "openclaw-*" --no-pager
```

⚠️ `systemctl --user enable` without `--now` does not start the timer until the next boot:
if `list-timers` does not show it, `start` it.

First backup by hand, so you do not wait for the timer `[idem]`:

```bash
systemctl --user start openclaw-backup@daily.service   # runs the kit's openclaw-backup.sh
ls -lh /srv/openclaw-backups/daily/     # ~18 MB + .sha256
```

### B4.4 Cluster side: bucket, uploader, guard and alerts

**[kit]** The four manifests exist as templates in `templates/gitops/openclaw-backups/`; render them
with `ai-resources openclaw render-gitops-backups --list-markers`, then `--set KEY=VALUE ... --out DIR`,
and commit the result to the infrastructure repo. The real values (project, bucket, WIF pool, node)
live only there.

This lives in **`wildbitca/org-gitops`** and Flux materialises it. Reference files:

```
apps/wildbit/gcp/backups-openclaw.yaml             Bucket + BucketIAMMember
infrastructure/backup-guard/uploader-openclaw.yaml  CronJob 04:40 (uploads and ASSERTS)
infrastructure/backup-guard/cronjob-openclaw.yaml   guard every 6 h (age, size, sha)
apps/wildbit/grafana/alerts-openclaw-backup.yaml    RuleGroup with 4 rules
```

Design keys to preserve if they are recreated:

- Bucket `wildbit-iac-openclaw-backups` in project `wildbit-iac`,
  `deletionPolicy: Orphan`, uniform BLA, versioning off, and retention **by prefix**:
  `daily/` 8 d, `weekly/` 35 d, `monthly/` 100 d, `manual/` 8 d. ~270 MB steady state.
- **No JSON key.** It reuses the `k3s-backup@wildbit-iac` service account through workload
  identity federation with the projected token of the `monitoring:k3s-backup-uploader`
  ServiceAccount and its credential ConfigMap. The `BucketIAMMember` scopes
  `roles/storage.objectAdmin` **to that bucket only**.
- The uploader **asserts**: at the end it checks the newest daily is in the bucket and fails
  otherwise. And it is idempotent (`gcloud storage ls` before copying).
- The 4 rules measure `last_schedule_time - last_successful_time` (not
  `kube_job_status_failed`, which stays firing forever) with a threshold **above one full
  period**.

Register each file in its `kustomization.yaml` and validate before committing `[idem]`:

```bash
kubectl --context default kustomize apps/wildbit/gcp >/dev/null
kubectl --context default kustomize apps/wildbit/grafana >/dev/null
kubectl --context default kustomize infrastructure/backup-guard >/dev/null
python3 - <<'EOF'
import yaml,json
d=yaml.safe_load(open('apps/wildbit/grafana/alerts-openclaw-backup.yaml'))
for r in d['spec']['forProvider']['rule']:
    for q in r['data']: json.loads(q['model'])   # blows up if the embedded JSON is broken
print("models OK")
EOF
```

**STOP — HUMAN ACTION REQUIRED**
Merging to `main` deploys. The `branch-default` ruleset requires 1 approving review and
GitHub does not let you approve your own PR; the required check is `ci`. Either another
account approves, or the admin bypass is used deliberately:

```bash
gh pr merge <N> --repo wildbitca/org-gitops --squash --admin --delete-branch
flux --context default reconcile source git wildbit
flux --context default reconcile kustomization apps
```

Before branching: **`git switch main && git pull`**. Branching off somebody else's working
branch drags their commits into your PR.

## B5. Exposure at `iai.wildbit.dev`

### B5.1 DNS record pointing at the tailnet IP `[1×]`

In `apps/wildbit/cloudflare/projects.yaml`, at the **end** of the zone's `dnsRecords` list:

```yaml
        - content: 100.76.56.42
          name: iai
          type: A
          proxied: false
          recordId: d2163d897dfd916d726275b912bc44ee
```

- `proxied: false` is **mandatory**: Cloudflare cannot proxy a CGNAT tailnet address.
- **At the end** because the composition names each managed resource by its **index** in
  the list while it has no `recordId`; inserting above renames the MR, Crossplane prunes
  the old one (left alive and orphaned by `Orphan`) and creates a colliding new one:
  `400 {"code":81058,"message":"An identical record already exists."}`.
- A new record is born **without** `recordId` because Cloudflare assigns it. It must be
  **backfilled the same day** and, in the meantime, recorded in
  `docs/dns-pendientes-de-adoptar.yaml`:

```bash
kubectl --context default get records.dns.upjet-cloudflare.upbound.io -o json \
  | python3 -c "
import sys,json
for it in json.load(sys.stdin)['items']:
    if it['spec']['forProvider'].get('name')=='iai':
        print(it['metadata']['annotations']['crossplane.io/external-name'])"
```

After adoption the MR is renamed to `…-dns-wildbit-dev-<recordId>` and stays
`Ready/Synced=True`: that is the proof the adoption went cleanly.

### B5.2 Selector-less Service + EndpointSlice + Ingress `[idem]`

`infrastructure/host-services/openclaw-ui.yaml` — the same pattern as `code-server` and
`purplemux`: the process lives on the host, the cluster only routes.

```yaml
Service        host-services/openclaw-ui   ClusterIP, port 18789 → 18789 (no selector)
EndpointSlice  addresses: [100.76.56.42], port 18789
Ingress        host iai.wildbit.dev, entrypoint websecure, tls secret openclaw-ui-tls,
               annotation cert-manager.io/cluster-issuer: letsencrypt-prod
```

The cert is issued over **DNS-01**, so it needs no inbound reachability: a domain that
resolves to a tailnet address still gets a valid certificate.

### B5.3 Verification `[idem]`

```bash
kubectl --context default get ingress,certificate -n host-services | grep openclaw
getent hosts iai.wildbit.dev                     # 100.76.56.42
curl -sS -o /dev/null -w "%{http_code}\n" https://iai.wildbit.dev      # 200
openssl s_client -connect iai.wildbit.dev:443 -servername iai.wildbit.dev </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
```

If you get **403 `proxy_attribution_required`**: `gateway.allowRealIpFallback true` is
missing (B2.7). If you get **502 / connection refused**: the gateway is on `loopback` and
the EndpointSlice points at a closed port → `gateway.bind tailnet`.

## B6. Extras

### B6.1 Local whisper for voice notes `[idem]`

```bash
brew install whisper-cpp      # if missing
mkdir -p ~/.local/share/whisper-cpp
curl -sL -o ~/.local/share/whisper-cpp/ggml-base.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin?download=true"
```

OpenClaw **only autodetects** models in `/opt/homebrew|/usr/local|/usr/share/whisper-cpp` —
none of which applies under linuxbrew — so the explicit override is the only path without
sudo:

```bash
printf 'WHISPER_CPP_MODEL=%s/.local/share/whisper-cpp/ggml-base.bin\n' "$HOME" \
  >> ~/.openclaw/gateway.systemd.env
chmod 600 ~/.openclaw/gateway.systemd.env
```

Also export it in the interactive environment, or doctor keeps complaining from your shell
(it evaluates **its own** environment, not the gateway's): add it to
`~/.config/shell/paths.env` guarded by `[ -r … ]`.

Real test:

```bash
ffmpeg -y -f lavfi -i "sine=frequency=440:duration=1" -ar 16000 -ac 1 /tmp/t.wav
whisper-cli -m ~/.local/share/whisper-cpp/ggml-base.bin -l es -nt /tmp/t.wav
```

### B6.2 GH_TOKEN for the Control UI `[idem]`

```bash
printf 'GH_TOKEN=%s\n' "$(gh auth token)" >> ~/.openclaw/gateway.systemd.env
chmod 600 ~/.openclaw/gateway.systemd.env
openclaw config set gateway.controlUi.github.token --ref-provider default --ref-source env --ref-id GH_TOKEN
```

The SecretRef points at the **gateway's env**: no plaintext token in `openclaw.json`, and
doctor's warning disappears from any shell.

⚠️ `EnvironmentFile` is applied **before** the unit's `Environment=` lines, so it **cannot
override `PATH`**. For every other variable it works.

### B6.3 Team visibility hook `[idem]`

**[kit]** The hook is `hooks/openclaw_team_progress.py`; `ai-resources setup` registers it on
`PreToolUse` (`*`), `SubagentStop`, `UserPromptSubmit` and `Stop` when you choose a narration level.
**Off is the default and it is real**: the hook speaks only when the gateway started the session
(`OPENCLAW_CLI=1`) **and** `~/.openclaw/kit-host.env` carries `OPENCLAW_NARRATION=milestones` or
`every-step`. A hand-installed copy is replaced and teardown puts it back.

The original hand-written hook, `~/.claude/hooks/openclaw-team-progress.py`, posted one line in the
topic when a subagent or workflow started and when a member finished. Its registration, kept for the
manual route:

```json
"PreToolUse":  [ { "matcher": "Agent|Workflow", "hooks": [ { "type": "command",
                   "command": "python3 \"$HOME/.claude/hooks/openclaw-team-progress.py\"",
                   "timeout": 30 } ] } ],
"SubagentStop":[ { "hooks": [ { "type": "command",
                   "command": "python3 \"$HOME/.claude/hooks/openclaw-team-progress.py\"",
                   "timeout": 30 } ] } ]
```

Three design decisions to keep:

- **It only speaks when `OPENCLAW_CLI=1`**, the variable the gateway injects into its
  children. In an ordinary terminal session it posts nothing.
- **It resolves the topic from `cwd`**: each workspace is unique and each agent is bound to
  a topic, so it never needs to know the OpenClaw session. `~/Development` (the `/equipo`
  worker, which has no topic of its own) falls back to topic 1.
- **It never posts raw `tool_input`** or tool output: only the role and a truncated
  description. And it always exits 0: a hook that breaks a tool is worse than no hook.

### B6.4 Workboard and progress streaming `[idem]`

```bash
openclaw plugins enable workboard
openclaw config set channels.telegram.streaming.mode progress
openclaw config set channels.telegram.streaming.progress.toolProgress true
openclaw config set channels.telegram.replyToMode first
openclaw gateway restart
```

With this, Telegram shows a status message edited live with one row per tool, and the final
answer arrives separately quoting the request. `commandText: "raw"` is deliberately left
**off**: a command can carry tokens or sensitive paths and that text stays in the chat
history forever.

Confirm the plugin loaded:

```bash
grep -o "http server listening ([0-9]* plugins[^)]*)" ~/.openclaw/logs/gateway.log | tail -1
# …, telegram, workboard, xai; …
```

---

# PART C — Recovery from backup (bithome is gone)

## C1. Where the tarball comes from

```
gs://wildbit-iac-openclaw-backups/
  daily/    openclaw-<YYYYMMDD-HHMMSS>.tar.gz + .sha256    retention   8 d
  weekly/   …                                              retention  35 d
  monthly/  …                                              retention 100 d
  manual/   …                                              retention   8 d
```

```bash
gcloud storage ls -r "gs://wildbit-iac-openclaw-backups"
gcloud storage cp "gs://wildbit-iac-openclaw-backups/daily/<file>" .
gcloud storage cp "gs://wildbit-iac-openclaw-backups/daily/<file>.sha256" .
sha256sum -c <file>.sha256        # BEFORE restoring anything
```

⚠️ The active `gcloud` account on the host may be a service account from another project.
Use `gcloud auth login <your-account> --update-adc` or `--account=`. From inside the
cluster, the uploader's WIF identity is the one with access to that bucket.

## C2. What it contains and what to do with each piece

| Piece | What it is | On restore |
|---|---|---|
| `openclaw-state.tar.gz` | all of `~/.openclaw`: config, state (sqlite holding the **secret store**), per-agent DBs, media | restored **as is** |
| `engram.db` | consistent snapshot (`VACUUM INTO`) of the persistent memory | copied to `~/.engram/engram.db` |
| `ai-config.tar.gz` | what lives in no repo: `~/.claude/CLAUDE.md`, `settings.json`, `hooks/`, `~/.config/shell/`, `~/.openclaw/kit-host.env`, the units and scripts that existed (all of them from 1.9.0; older tarballs lack the watchdog, verify and team-watch scripts and most units, which `ai-resources openclaw install-units` regenerates), and `AGENTS.md`/`MEMORY.md`/`USER.md`/`memory/` of the 5 workspaces | unpacked over `$HOME` |
| `INVENTORY.txt` | that day's versions (openclaw, node, claude, ai-resources), the agent table with model and workspace, and the restore steps | read it first |

**Deliberately not in the backup**: the `~/.claude` skills and subagents (regenerable),
`~/.claude/projects` (1.2 GB of transcripts) and the project checkouts (they are git repos).

## C3. Recovery sequence

The kit-driven order, with `~/.openclaw/kit-host.env` restored first, is in
`docs/runbooks/openclaw-host-dr.md`, and it says plainly that the restore has never been rehearsed.
The sequence below is the original, on the reference host.

```bash
# 0. PART A prerequisites on the new machine, and all of B1 (runtime)
tar xzf openclaw-<ts>.tar.gz && cat INVENTORY.txt

# 1. openclaw state
systemctl --user stop openclaw-gateway.service
tar xzf openclaw-state.tar.gz -C /        # the archive carries payload/posix/home/<user>/.openclaw
#    (or unpack into a temp dir and copy ~/.openclaw by hand)

# 2. memory
mkdir -p ~/.engram && cp engram.db ~/.engram/engram.db

# 3. hand-written config (includes the units and scripts)
tar xzf ai-config.tar.gz -C "$HOME"
systemctl --user daemon-reload

# 4. the regenerable half
brew install ai-resources && ai-resources setup     # rebuilds ~/.claude/skills and agents

# 5. the service
openclaw gateway install --force      # with brew's node, see B1.3
loginctl enable-linger $USER
systemctl --user enable --now openclaw-gateway.service
```

**STOP — HUMAN ACTION REQUIRED**
Secrets the tarball does carry and that must be reviewed: the store
(`~/.openclaw/state/openclaw.sqlite`) comes with `GATEWAY_AUTH_PASSWORD`,
`GATEWAY_AUTH_TOKEN` and `OPENROUTER_API_KEY`; `~/.openclaw/telegram.token` and
`gateway.systemd.env` too. If the backup could have been exposed, **rotate** the bot token
in BotFather, the gateway password and the API keys before starting.

**STOP — HUMAN ACTION REQUIRED**
The Claude CLI needs its own session: run `claude` and log in with the Max subscription
account. Without it every turn fails even if OpenClaw is perfect.

**STOP — HUMAN ACTION REQUIRED**
If the tailnet IP changed (new machine = new tailnet node), update in `org-gitops`: the
`content` of the `iai` record and the `EndpointSlice` of `openclaw-ui`. And
`gateway.trustedProxies` if the pod CIDR changed.

## C4. Validate that recovery is complete

```bash
openclaw health                                   # Telegram configured + event loop ok
openclaw agents list | grep -E "^- |Model"        # 6 agents with model and workspace
openclaw config validate                          # Config valid
openclaw doctor                                   # no "Doctor warnings" blocks
ls ~/.claude/agents | wc -l                       # ~40  (0 means ai-resources setup is missing)
ls ~/.claude/skills | wc -l                       # ~137
openclaw agent --agent main --session-key "probe-$(date +%s)" -m "Responde solo: ok"
systemctl --user start openclaw-backup@manual.service   # the whole cycle works again
```

And the check that really closes the loop: send a message in a topic from Telegram and see
the right agent answer, on the right model.

---

# PART D — Daily operation

## Stop and start without fighting the watchdog

Only the operator does this, in a maintenance window: never from a tool running inside a gateway
session, which is a child of the unit and dies with it.

```bash
touch ~/.openclaw/watchdog.off            # the watchdog goes quiet
systemctl --user stop openclaw-gateway.service
# … whatever you need to do …
rm -f ~/.openclaw/watchdog.off
systemctl --user start openclaw-gateway.service
```

Without the pause file the watchdog brings it up within 2 min in the middle of your
manoeuvre. And the other way round: leave the file behind and nobody is watching.

## Run doctor without killing the gateway

**[kit]** `ai-resources openclaw doctor` (add `--dry-run` to see the sequence, `--cleanup-sessions` to
also run `sessions cleanup --all-agents`). It is the only supported way: the gateway guard hook denies
a bare `openclaw doctor --fix`. The weekly maintenance timer calls it. By hand, which is exactly what
it does:

```bash

touch ~/.openclaw/watchdog.off
systemctl --user stop openclaw-gateway.service
while [ "$(systemctl --user show openclaw-gateway.service -p TasksCurrent --value)" != "[not set]" ]; do sleep 3; done
openclaw doctor --fix
rm -f ~/.openclaw/watchdog.off
systemctl --user start openclaw-gateway.service
```

**Draining is mandatory.** `doctor --fix` stops the gateway, re-inspects the unit and
requires the cgroup to be empty; if children are still alive (claude, engram, npx) the
state comes back `unknown`, `GatewayServiceUpdateOwnershipError` is raised
("Gateway service ownership or manager identity changed; inspect it before restarting
manually.") **after the stop and before the start**, and the gateway stays dead. Since it
was an explicit stop, `Restart=always` does not cover it. Note the `stop` itself can take
~5 min because of `TimeoutStopSec=330`.

## Resource safety: prevention first, a graceful restart as the valve

**[kit]** Since the resource-safe gateway work ([ADR-0004](../decisions/0004-bounded-graceful-restart-under-memory-pressure.md)),
`ai-resources setup` configures four layers. Why: on 2026-10-09/10 the gateway cgroup reached 13.75 GiB
against `MemoryHigh=12G` (the process itself was ~2 GB; the rest was ~130 per-session MCP stacks nothing
evicted), the main thread froze in state `D`, and the old health timer, which never acted with runs in
flight, logged `healthy`.

| Layer | What | Where |
|---|---|---|
| Prevent | `mcp.sessionIdleTtlMs=1800000` (evict an idle per-session MCP runtime after 30 min) and `agents.defaults.timeoutSeconds=14400` (cap one run at 4 h). Hot keys, FILL-ONLY: a value you set, `0` included, is kept | `profiles/openclaw-host.json5` |
| Instruct | The kit block in every `AGENTS.md`: wrap commands in `timeout <N>` or detach them when `run_in_background` is unavailable; never call the native AskUserQuestion in a headless or subagent session | `scripts/ai_resources/setup/cockpits/_shared.py` |
| Detect | `ai-resources openclaw pressure [--json]`: reads cgroup and `/proc`, two samples `OPENCLAW_HEALTH_CONFIRM_S` apart, classifies `healthy`, `pressure`, `hard`, `frozen`, `refused-probe` or `health-fail`. Read-only; an unreadable signal is `refused-probe` and nothing acts | `scripts/ai_resources/openclaw_pressure.py` |
| Act | `ai-resources openclaw graceful-restart`: gated, snapshotted, reported. Called by the health timer in mode `on`, or by you | `scripts/ai_resources/openclaw_host.py` |

### Modes and knobs

`OPENCLAW_GRACEFUL_RESTART` in `~/.openclaw/kit-host.env`: `off` (log only), `notify` (the shipped
default: tell you once per episode what it WOULD do, restart nothing) or `on`. Anything else behaves as
`notify`. **Open risk:** whether Slack or Telegram messages were duplicated or lost after the 2026-10-10
13:20 restart has not been verified, so enable `on` only after one supervised restart (below).

| Knob | Default | Meaning |
|---|---|---|
| `OPENCLAW_RESTART_WINDOW` | `02:00-05:00` | `HH:MM-HH:MM`, may wrap midnight; empty = never inside a window |
| `OPENCLAW_RESTART_TZ` | `America/Guayaquil` | the zone the window is read in. The host clock is UTC; the window is NOT the host's local time |
| `OPENCLAW_RESTART_HARD_PCT` | `105` | % of `MemoryHigh` above which the ceiling overrides the window |
| `OPENCLAW_RESTART_PRESSURE_PCT` | `90` | % of `MemoryHigh` that counts as pressure (swap must also be >= 90 %) |
| `OPENCLAW_RESTART_COOLDOWN_S` | `10800` | 3 h between restarts |
| `OPENCLAW_RESTART_DAILY_CAP` | `2` | restarts per local day; `0` = no cap |
| `OPENCLAW_HEALTH_CONFIRM_S` | `60` | seconds between the two samples |
| `OPENCLAW_FROZEN_MIN_S` | `600` | "still starting" for this long counts as frozen |
| `OPENCLAW_RESTART_SETTLE_S` | `120` | wait after the restart before the post-snapshot |
| `OPENCLAW_RESTART_SNAPSHOTS_KEEP` | `10` | snapshot directories kept |
| `OPENCLAW_RESOURCE_GUARDS` | `on` | `off`: setup does not fill the two prevention keys |

The timer runs every 15 minutes. Opt out of the prevention keys with `OPENCLAW_RESOURCE_GUARDS=off` (the
wizard asks). The overrides `keep` list protects a value that already exists; it cannot say "do not fill an
unset key", which is why the env switch exists. `off` does not revert a value already written
(`openclaw config unset mcp.sessionIdleTtlMs`).

### What the gates are

`graceful-restart` refuses, naming the gate, when: it runs inside the gateway cgroup (T29); another
graceful restart holds `~/.openclaw/health-restart.lock`; `watchdog.off` exists; the unit is not `active`;
the mode is not `on` (a manual run skips this and the pressure gate); the pressure is not confirmed; the
cooldown or the daily cap says no; or it is outside the window and below the ceiling. It never passes
`--force`, never kills a process and never restarts a `frozen` gateway. Success is health plus a NEW
MainPID. A `GATEWAY_RESTART_PREPARATION_REFUSED` is retried after 20, 40 and 80 s and reported as
`refused`, never as frozen.

### Look first, then do it once by hand

```bash
ai-resources openclaw pressure --json                    # the classification and its numbers
ai-resources openclaw graceful-restart --dry-run          # every gate verdict; changes nothing
ai-resources openclaw restart-report --since 2026-10-10T13:15:00Z   # recovery counts from the journal
# One SUPERVISED restart, inside the window, from a shell that is not a gateway child:
ai-resources openclaw graceful-restart --reason manual
```

Read the report (`~/.openclaw/logs/restart-snapshots/<UTC timestamp>/report.txt`), watch Slack and Telegram
for duplicates or losses, and only then set `OPENCLAW_GRACEFUL_RESTART=on`. `ai-resources openclaw status`
shows the mode, restarts today against the cap, the last restart (reason, result, memory freed, recovery
counts, report path) and the current classification; `ai-resources verify` warns on a failed last restart, a
frozen last tick, a stale `watchdog.off`, too many snapshots, guards missing from the config and
`MemoryHigh` drift.

### A frozen gateway

`frozen` means the main thread is in state `D` waiting on the `MemoryHigh` throttle (or `openclaw health`
says "still starting" too long): the restart is refused ("database is locked") and the kit does NOT try
one. The check logs the evidence, sends one notice (retried until delivered), and exits 2 so systemd shows
the run as failed. The remedy is yours: stop the heavy agent children (Unity, tsc, Gradle) by hand until the
cgroup is under `MemoryHigh` (pitfall T41). The kit does not raise `MemoryHigh`, at runtime or otherwise.

### Snapshots and rollback

Each restart writes `~/.openclaw/logs/restart-snapshots/<UTC timestamp>/{before,after}/` (directories 0700,
files 0600): busy count, unit properties, `free -b`, cgroup files, `ps` without arguments and a whitelisted
session list, plus `report.txt`. Rollback needs no code change: `OPENCLAW_GRACEFUL_RESTART=off` disables the
valve, `OPENCLAW_RESOURCE_GUARDS=off` stops the fill, and `touch ~/.openclaw/watchdog.off` stands every
automation down.

`ai-resources openclaw doctor` now refuses a gateway with runs in flight (exit 6, nothing stopped) unless
you pass `--even-if-busy`.

## Force a backup and check it left the node

```bash
systemctl --user start openclaw-backup@daily.service
tail -5 ~/.openclaw/logs/backup.log
ls -lh /srv/openclaw-backups/daily/

kubectl --context default create job --from=cronjob/openclaw-backup-uploader \
  upload-now-$(date +%s) -n monitoring
kubectl --context default logs -n monitoring job/upload-now-<…> | tail -5
# expected: "OK: el daily mas reciente esta off-box"

gcloud storage ls -r "gs://wildbit-iac-openclaw-backups"
kubectl --context default create job --from=cronjob/openclaw-backup-guard \
  guard-now-$(date +%s) -n monitoring     # "daily y weekly estan al dia"
```

## Get into the UI

In a browser, on the tailnet: **https://iai.wildbit.dev** plus the gateway password.

If you need to pair a browser with a one-time token (expires in ~15 min) — `openclaw
dashboard` builds the URL from the local bind, not from `publicOrigin`, so it has to be
rewritten:

```bash
openclaw dashboard --json --no-open | python3 -c "
import json,sys,urllib.parse as u
d=json.load(sys.stdin)
x=d['browserUrl'].replace('http://127.0.0.1:18789','https://iai.wildbit.dev')
print(x.replace(u.quote('ws://127.0.0.1:18789',safe=''),u.quote('wss://iai.wildbit.dev',safe='')))"
```

## Pair the phone

```bash
openclaw qr                      # ASCII QR for the mobile app
openclaw qr --json --no-ascii    # gatewayUrl wss://iai.wildbit.dev, auth: password
```

It already points at the domain because it reads
`plugins.entries.device-pair.config.publicUrl` (the output's `urlSource` field says so).

## Restart a topic session so it picks up a new `AGENTS.md`

Send **`/new`** in the topic. Context is injected at session start, so a live session keeps
the old rules. It shows: an old session claimed it could commit at the umbrella root, a new
one answered the opposite.

## Read logs

```bash
tail -f ~/.openclaw/logs/gateway.log         # no longer in /tmp
tail -f ~/.openclaw/logs/backup.log
tail -f ~/.openclaw/logs/maintenance.log
tail -f ~/.openclaw/logs/watchdog.log
journalctl --user -u openclaw-gateway.service -n 100 --no-pager
```

The gateway log is one JSON object per line. To filter by time window and level:

```bash
python3 - <<'EOF'
import json
for line in open('/home/bitgandtter/.openclaw/logs/gateway.log',errors='ignore'):
    try: d=json.loads(line)
    except: continue
    if d.get('_meta',{}).get('logLevelName') in ('ERROR','FATAL'):
        print(d.get('time'), str(d.get('message'))[:160])
EOF
```

## Upgrade the models (automatic and manual)

`openclaw-models-update.timer` runs `scripts/openclaw/openclaw-models-update.sh` daily at 04:45 (30 min
jitter). It runs `ai-resources models update --unattended --json`, logs to
`~/.openclaw/logs/models-update.log` and messages you only when something changed or needs a decision.

```bash
ai-resources models status                  # effective pins, config drift, pending approvals, last run
ai-resources models check --refresh         # read-only: exit 0 nothing, 10 approval pending, 11 ready
ai-resources models update --dry-run        # smoke-tests and validates the patch; applies nothing
ai-resources models approve haiku claude-haiku-5-5   # the exact command the Telegram notice prints
ai-resources models approve --slot google:gemini-flash gemini-3.9-flash   # a non-Claude slot is named in full
ai-resources models update                  # on a terminal: shows what was found and asks (never from the timer)
ai-resources models pin sonnet claude-sonnet-5       # freeze a class; `models unpin sonnet` releases it
ai-resources models exclude 'claude-opus-5-5'        # never apply ids matching the glob
ai-resources models rollback                # inverse patch, restart, health check
```

- **Pause:** `touch ~/.openclaw/watchdog.off` (the wrapper stands down) or
  `systemctl --user disable --now openclaw-models-update.timer`.
- **Manual rollback:** `ai-resources models rollback`. If the CLI itself is broken, take the copy of
  `openclaw.json` from `~/.openclaw/backups/models-update/<timestamp>/` and send the lines you need back
  with `openclaw config patch --stdin` (never edit the live file). Deleting
  `~/.config/ai-resources/model-pins.json` returns to the kit defaults.
- **Exit code 6** means the rollback failed: check `openclaw health` and `ai-resources models status` at once.
- **Why nothing auto-applies yet:** a bump applies by itself only when `audit.PRICES` has an exact, verified
  price for the new id and it is not dearer. Add the price (from the vendor price page) to
  `scripts/ai_resources/audit.py`, or approve the bump by hand.

## From a tool shell (no login session)

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
```

Without those two variables `systemctl --user` fails with "Failed to connect to user scope
bus: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined" and `openclaw gateway
restart` cannot manage the service.

---

# PART E — Final checklist

Runtime and service:

- [ ] `/home/linuxbrew/.linuxbrew/bin/node -v` → `v26.x` (≥26.1)
- [ ] `command -v openclaw` → `/home/linuxbrew/.linuxbrew/bin/openclaw` (exactly one copy)
- [ ] `openclaw --version` → `OpenClaw 2026.9.4`
- [ ] `grep ^ExecStart ~/.config/systemd/user/openclaw-gateway.service` → brew's node and dist
- [ ] `ls ~/.config/systemd/user/openclaw-gateway.service.d 2>/dev/null` → does not exist (no drop-ins)
- [ ] `systemctl --user is-enabled openclaw-gateway.service` → `enabled`
- [ ] `loginctl show-user $USER -p Linger` → `Linger=yes`
- [ ] `systemctl --user show openclaw-gateway.service -p MemoryHigh --value` → `12884901888`
- [ ] `openclaw update status` → `Install: npm`, `up to date`

Config and agents:

- [ ] `openclaw config validate` → `Config valid`
- [ ] `openclaw agents list` → 6 agents; the 5 topic ones on `anthropic/claude-sonnet-5`, `claude` on `claude-kit/claude-sonnet-5`
- [ ] `openclaw health` → `Heartbeat interval: 2h (main), disabled (rest)`
- [ ] `pgrep -af "/.local/bin/claude"` during a turn → `--effort high --model claude-sonnet-5` (main)
- [ ] `ls ~/.claude/agents | wc -l` ≈ 40 and `ls ~/.claude/skills | wc -l` ≈ 137
- [ ] `AGENTS.md` present in the 5 workspaces and **none** of them shows up in `git status`

Channel:

- [ ] `openclaw channels status` → telegram `connected`
- [ ] `getMe.can_read_all_group_messages` → `True`
- [ ] the bot's `getChatMember` → `administrator`, `can_manage_topics: True`
- [ ] the 5 topics resolve to their agent (`channels.telegram.groups.<chat>.topics`)

Exposure:

- [ ] `ss -lntp | grep 18789` → `127.0.0.1:18789` **and** `100.76.56.42:18789`
- [ ] `getent hosts iai.wildbit.dev` → `100.76.56.42`
- [ ] `curl -o /dev/null -w "%{http_code}" https://iai.wildbit.dev` → `200`
- [ ] cert `CN=iai.wildbit.dev`, Let's Encrypt issuer, not expired
- [ ] `kubectl --context default get certificate -n host-services` → `openclaw-ui-tls` Ready

Backup and watch:

- [ ] `systemctl --user list-timers "openclaw-*"` → 10 timers with a next elapse (or `ai-resources openclaw status`)
- [ ] `ls /srv/openclaw-backups/daily/` → ~18 MB tarball **with** its `.sha256`
- [ ] `gcloud storage ls -r gs://wildbit-iac-openclaw-backups` → objects under `daily/` and `weekly/`
- [ ] guard Job by hand → "daily y weekly estan al dia", exit 0
- [ ] 4 Grafana rules (group "Backups de OpenClaw") in `state: normal`
- [ ] the series exists: `kube_cronjob_status_last_successful_time{cronjob=~"openclaw-backup-.*"}` returns data

GitOps:

- [ ] `flux --context default get kustomizations` → all `Ready=True`, none suspended
- [ ] `kubectl --context default get managed` → 0 resources with `Ready`/`Synced` ≠ True
- [ ] the `iai` record **has** `recordId` in `projects.yaml` and `docs/dns-pendientes-de-adoptar.yaml` is still `pendientes: []`

Closing:

- [ ] `openclaw doctor` → `Doctor complete.`, exit 0, **zero** `Doctor warnings` blocks
- [ ] a message in each topic is answered by the right agent
- [ ] a voice note gets transcribed
- [ ] 2FA active on the operator's Telegram account (it is the only thing between `/equipo` and full execution on the host)
