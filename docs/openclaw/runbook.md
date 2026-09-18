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

### B4.2 The three scripts `[idem]`

Copy them from the backup or from the kit into `~/.local/bin/` and `chmod +x`:

| Script | What it does |
|---|---|
| `openclaw-backup.sh <daily\|weekly\|monthly\|manual>` | `openclaw backup create --no-include-workspace --verify` + engram `VACUUM INTO` + tar of the hand-written config + `INVENTORY.txt`; one tarball and its `.sha256` **written last**; rotates 7/4/3; notifies on Telegram on failure |
| `openclaw-maintenance.sh` | pauses the watchdog, stops, **waits for the drain**, `doctor --fix`, `sessions cleanup --all-agents`, starts, polls health up to 2 min, checks backup age and new versions |
| `openclaw-watchdog.sh` | if the gateway is not active it starts it and **says so on Telegram**; paused with `~/.openclaw/watchdog.off` |

Invariants that must survive any port of these scripts:

- The `.sha256` is written **after** the tarball: it is the only witness that the tarball is
  closed, and the uploader skips those without it.
- No `|| true` on anything that matters.
- The result is **asserted**: size ≥ 1 MB, `tar tzf` listable, `sha256sum -c`.
- `sessions cleanup` **requires `--all-agents`** with several agents, or it fails with
  "Multiple agents are configured, but session-store selection has no explicit owner."
- Poll health **for up to 2 minutes**: 12 s is not enough on a cold start.

### B4.3 Units and timers `[idem]`

```
~/.config/systemd/user/openclaw-backup@.service          Type=oneshot, ExecStart=…openclaw-backup.sh %i
~/.config/systemd/user/openclaw-backup-daily.timer       OnCalendar=*-*-* 03:30:00
~/.config/systemd/user/openclaw-backup-weekly.timer      OnCalendar=Sun *-*-* 03:45:00
~/.config/systemd/user/openclaw-backup-monthly.timer     OnCalendar=*-*-01 04:00:00
~/.config/systemd/user/openclaw-maintenance.{service,timer}  OnCalendar=Sun *-*-* 04:30:00
~/.config/systemd/user/openclaw-watchdog.{service,timer}     OnBootSec=2min OnUnitActiveSec=2min
```

All timers use `Persistent=true` (a run missed while the machine was off fires at boot) and
`RandomizedDelaySec`.

```bash
systemctl --user daemon-reload
systemctl --user enable --now openclaw-backup-daily.timer openclaw-backup-weekly.timer \
  openclaw-backup-monthly.timer openclaw-watchdog.timer openclaw-maintenance.timer
systemctl --user list-timers "openclaw-*" --no-pager
```

⚠️ `systemctl --user enable` without `--now` does not start the timer until the next boot:
if `list-timers` does not show it, `start` it.

First backup by hand, so you do not wait for the timer `[idem]`:

```bash
~/.local/bin/openclaw-backup.sh daily
ls -lh /srv/openclaw-backups/daily/     # ~18 MB + .sha256
```

### B4.4 Cluster side: bucket, uploader, guard and alerts

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

`~/.claude/hooks/openclaw-team-progress.py` posts one line in the topic when a subagent or
workflow starts and when a member finishes. Registration in `~/.claude/settings.json`:

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
| `ai-config.tar.gz` | what lives in no repo: `~/.claude/CLAUDE.md`, `settings.json`, `hooks/`, `~/.config/shell/`, the units, the three scripts, and `AGENTS.md`/`MEMORY.md`/`USER.md`/`memory/` of the 5 workspaces | unpacked over `$HOME` |
| `INVENTORY.txt` | that day's versions (openclaw, node, claude, ai-resources), the agent table with model and workspace, and the restore steps | read it first |

**Deliberately not in the backup**: the `~/.claude` skills and subagents (regenerable),
`~/.claude/projects` (1.2 GB of transcripts) and the project checkouts (they are git repos).

## C3. Recovery sequence

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
~/.local/bin/openclaw-backup.sh manual            # the whole cycle works again
```

And the check that really closes the loop: send a message in a topic from Telegram and see
the right agent answer, on the right model.

---

# PART D — Daily operation

## Stop and start without fighting the watchdog

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

```bash
~/.local/bin/openclaw-maintenance.sh      # does the right sequence, or by hand:

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

## Force a backup and check it left the node

```bash
~/.local/bin/openclaw-backup.sh daily
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

- [ ] `systemctl --user list-timers "openclaw-*"` → 5 timers with a next elapse
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
