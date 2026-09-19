# OpenClaw AI setup on bithome — exhaustive inventory (2026-09-18)

Everything that was installed, configured, written, cleaned up, decided and verified during
the 2026-09-18 session. Written to be usable as a reconstruction base: every value below was
read back from the live system unless marked otherwise.

**Verification legend**
- `VERIFIED` — read back from the live system while writing this document.
- `SESSION` — taken from the session record; the command ran and its output was seen, but the
  transient state it produced cannot be re-read now (for example a pre-change value).
- `NOT VERIFIED` — stated but not proven; treat as a lead, not a fact.

Companion documents: `doc-02-trampas-y-recomendaciones.md` (30 pitfall cards, pending items,
kit proposals). This file answers *what is true now and how it got here*; that one answers
*what bites and what to build next*.

---

## 1. Final state of the setup

### 1.1 Runtime `VERIFIED`

| Piece | Value |
|---|---|
| OpenClaw | `2026.9.4 (3a9d69d)` |
| Binary | `/home/linuxbrew/.linuxbrew/bin/openclaw` → `/home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/openclaw.mjs` |
| Node (service) | `/home/linuxbrew/.linuxbrew/opt/node/bin/node` — `v26.9.0` (Homebrew) |
| Node (interactive shell) | fnm `v24.21.0` still first in `$PATH`; untouched on purpose |
| Claude CLI | `2.1.276` (`~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.276`) |
| ai-resources kit | `1.8.1` (Homebrew) |
| Host | `bithome`, single-node k3s control-plane `v1.36.3+k3s1`, Ubuntu 26.04.1, tailnet IP `100.76.56.42` |

Service unit `~/.config/systemd/user/openclaw-gateway.service` `VERIFIED`:

```
ExecStart=/home/linuxbrew/.linuxbrew/opt/node/bin/node --max-old-space-size=8192 \
          /home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/dist/index.js gateway --port 18789
Restart=always   TimeoutStopSec=330   KillMode=mixed   OOMPolicy=continue
```

`ExecStart` points at brew's `opt/node` and at openclaw's install root, so it survives both node
upgrades and openclaw upgrades. There is **no** `openclaw-gateway.service.d` drop-in; the only
override lives in `~/.config/systemd/user.control/openclaw-gateway.service.d/50-MemoryHigh.conf`,
which `openclaw doctor` does not inspect. `MemoryHigh=12884901888` (12 GiB) `VERIFIED`.

### 1.2 Agents `VERIFIED`

| Agent | Telegram topic | Model | Workspace | Notes |
|---|---|---|---|---|
| `main` (Jarvis ⚡) | 1 — General | `anthropic/claude-sonnet-5` | `~/.openclaw/workspace` | `thinkingDefault: high`, heartbeat `2h`, orchestrator, holds config-write |
| `snoutzone` | 8 | `anthropic/claude-sonnet-5` | `~/Development/wildbit/pacha` | heartbeat disabled |
| `elinvo` | 67 | `anthropic/claude-sonnet-5` | `~/Development/wildbit/elinvo` | heartbeat disabled |
| `devops` | 90 | `anthropic/claude-sonnet-5` | `~/Development/wildbit/org-iac` | heartbeat disabled |
| `ai` | 315 — IA | `anthropic/claude-sonnet-5` | `~/Development/wildbit/ai-resources` | created by the user mid-session; heartbeat disabled |
| `claude` | none | `claude-kit/claude-sonnet-5` | `~/Development` | `tools.exec.mode: full`; the unrestricted worker behind `/claude` and `/equipo` |

Defaults: `model.primary = anthropic/claude-haiku-4-5` (utility only), `thinkingDefault = medium`,
`heartbeat = {target: none, every: 0m, model: anthropic/claude-haiku-4-5}`,
`systemAgent.agentId = main`, `subagents.allowAgents = ["claude"]`.

Effective heartbeat state from `openclaw health` `VERIFIED`:
`2h (main), disabled (snoutzone), disabled (elinvo), disabled (devops), disabled (claude), disabled (ai)`.

### 1.3 Telegram channel `VERIFIED`

| Key | Value |
|---|---|
| Bot | `@wildjarbisbot`, `can_read_all_group_messages: true` (privacy mode disabled in BotFather) |
| Group | `-1003678125825`, `requireMention: false`, `allowFrom: ["7961376547"]` |
| Topics | `1→main`, `8→snoutzone`, `67→elinvo`, `90→devops`, `315→ai`, `*` (default, `requireMention:false`) |
| Policies | `dmPolicy: pairing`, `groupPolicy: allowlist` |
| Actions | `createForumTopic`, `editForumTopic`, `sendMessage`, `reactions` — all `true` |
| Streaming | `mode: progress`, `preview.toolProgress: true`, `progress.toolProgress: true` |
| Replies | `replyToMode: first` |
| Rendering | `richMessages: true`, `markdown.tables: code`, `commands.native: true`, `errorPolicy: once` |
| Owner | `commands.ownerAllowFrom: ["telegram:7961376547"]` |

### 1.4 Plugins and MCP `VERIFIED`

15 plugins loaded at startup: `ai-resources, anthropic, canvas, cua-computer, device-pair,
file-transfer, geolocation, linux-node, memory-core, ollama, openai, talk-voice, telegram,
workboard, xai`.

Explicitly disabled in config: `openrouter`, `browser`. Explicitly enabled: `anthropic`,
`llama-cpp`, `ai-resources`, `workboard`. The kit plugin is loaded from
`/home/linuxbrew/.linuxbrew/opt/ai-resources/libexec/openclaw-plugin/ai-resources` and carries
absolute binary paths (`agyBin`, `claudeBin`, `python3Bin`).

| MCP server | State | Transport |
|---|---|---|
| `engram` | active | stdio `/home/linuxbrew/.linuxbrew/bin/engram` |
| `clickup` | active | streamable-http, OAuth |
| `supabase-admin` | active | stdio `npx @supabase/mcp-server-supabase@latest` (floating version — see doc-02) |
| `sentry` | **disabled** | needed interactive OAuth |
| `supabase` | **disabled** | duplicate of `supabase-admin` |
| `stripe` | **disabled** | needed interactive OAuth |

### 1.5 Tools and media `VERIFIED`

`tools.profile: coding` plus `tools.alsoAllow: ["group:messaging"]` — the profile alone has no
messaging tool, which is why forum-topic creation used to require shelling out to the CLI.

Audio understanding: one `cli` media model running the kit's transcriber
(`libexec/scripts/ai_resources/voice/openclaw_transcribe.py --mode local --language es`),
`capabilities: [audio]`, 180 s timeout, `echoTranscript: true`.

### 1.6 Gateway exposure and auth `VERIFIED`

```json
{ "mode":"local", "port":18789, "bind":"tailnet",
  "auth":{"mode":"password","password":{"source":"store","provider":"default","id":"GATEWAY_AUTH_PASSWORD"}},
  "publicOrigin":"https://iai.wildbit.dev",
  "trustedProxies":["10.42.0.0/24"],
  "allowRealIpFallback":true,
  "controlUi":{"enabled":true,"allowedOrigins":["https://iai.wildbit.dev"],
               "github":{"token":{"source":"env","provider":"default","id":"GH_TOKEN"}}},
  "tailscale":{"mode":"off"} }
```

Listening sockets: `127.0.0.1:18789` **and** `100.76.56.42:18789` — `bind: tailnet` keeps
loopback, which is what saves the local CLI and every script that depends on it.

Public entry point: `https://iai.wildbit.dev` → Cloudflare A record to the tailnet IP
(`proxied: false`) → Traefik on the node → Service/EndpointSlice `host-services/openclaw-ui`
→ `100.76.56.42:18789`. Let's Encrypt cert `openclaw-ui-tls`, `READY=True`.
`tailscale.mode` stays `off`: exposure is through Traefik, not Tailscale Serve.

Secret store (`~/.openclaw/state/openclaw.sqlite`, write-only entries) `VERIFIED`:
`GATEWAY_AUTH_PASSWORD`, `GATEWAY_AUTH_TOKEN` (kept for rollback), `OPENROUTER_API_KEY`.

Service environment file `~/.openclaw/gateway.systemd.env`, mode `600` `VERIFIED`:
`GEMINI_API_KEY`, `GH_TOKEN`, `WHISPER_CPP_MODEL`.

Mobile pairing payload `VERIFIED`: `gatewayUrl: wss://iai.wildbit.dev`, `auth: password`,
`urlSource: plugins.entries.device-pair.config.publicUrl`. Paired devices: `openclaw-control-ui`
and `openclaw-tui`, both with operator scopes, both survived the auth-mode switch.

### 1.7 Scheduling `VERIFIED`

`loginctl show-user bitgandtter -p Linger` → `yes`, so user units start at boot with no login.

| Unit | Enabled | Active | Next run |
|---|---|---|---|
| `openclaw-gateway.service` | enabled | active | — |
| `openclaw-watchdog.timer` | enabled | active | every 2 min |
| `openclaw-backup-daily.timer` | enabled | active | Sat 03:32 (03:30 + jitter) |
| `openclaw-backup-weekly.timer` | enabled | active | Sun 03:45 |
| `openclaw-backup-monthly.timer` | enabled | active | Oct 1, 04:03 |
| `openclaw-maintenance.timer` | enabled | active | Sun 04:33 |

All backup/maintenance timers carry `Persistent=true`, so a missed window fires after boot.

### 1.8 Logging and disk `VERIFIED`

`logging.file = ~/.openclaw/logs/gateway.log`, `logging.maxFileBytes = 33554432` (32 MiB).
Logs no longer live in `/tmp` (tmpfs, wiped on reboot).

| Path | Size |
|---|---|
| `~/.claude` | 812 MB (was 4.5 GB) |
| `~/.claude/jobs` | 28 KB (was 3.2 GB) |
| `~/.openclaw/state` | 14 MB |
| `~/.engram` | 33 MB |
| `/srv/openclaw-backups` | 71 MB (4 tarballs) |
| Free on `/home` | 371 GB |

---

## 2. Every configuration change, with its command and its reason

All `openclaw config set` calls validate against the schema and most report whether a restart is
needed. Order below is thematic, not chronological.

### 2.1 Models and reasoning

| Command | Final value | Why |
|---|---|---|
| `openclaw config set agents.entries.main.model.primary anthropic/claude-sonnet-5` | sonnet-5 | haiku-4-5 as orchestrator hallucinated created Telegram topics and invented a permissions excuse; the kit's subagents already ran opus/sonnet, so the orchestrator was the weak link |
| same for `snoutzone`, `elinvo`, `devops` | sonnet-5 | same reason for project agents |
| `openclaw config set agents.entries.ai.model.primary anthropic/claude-sonnet-5` | sonnet-5 | the `ai` agent inherited the haiku default when the user created it |
| `openclaw config set agents.defaults.thinkingDefault medium` | medium | explicit instead of implicit; maps to `--effort` + `MAX_THINKING_TOKENS` on the Claude CLI |
| `openclaw config set agents.entries.main.thinkingDefault high` | high | the orchestrator decides bindings and dispatch; verified in the child process as `--effort high` |

`agents.defaults.model.primary` stays `anthropic/claude-haiku-4-5` on purpose: it is the utility
model (titles, small internal calls), not an agent's model.

### 2.2 Heartbeats

| Command | Final value | Why |
|---|---|---|
| `openclaw config set agents.defaults.heartbeat.every 0m` | `0m` | five heartbeats every 30 min (~240 agent turns/day) with `target: none`, i.e. delivering to nobody — pure quota burn |
| `openclaw config set agents.entries.main.heartbeat.every 2h` | `2h` | keep one, on the orchestrator only |
| `openclaw config set agents.defaults.heartbeat.model anthropic/claude-haiku-4-5` | haiku | a heartbeat does not need sonnet |

Effect confirmed by the scheduler: the five `Heartbeat (agent)` cron jobs collapsed to one,
`heartbeat:main`, every 2h `SESSION`.

### 2.3 Tools

| Command | Final value | Why |
|---|---|---|
| `openclaw config set tools.alsoAllow '["group:messaging"]'` | `["group:messaging"]` | profile `coding` excludes the `message` tool, so creating a forum topic or sending to a topic required Bash + CLI. With this, it is a native tool call |

### 2.4 MCP servers

| Command | Why |
|---|---|
| `openclaw mcp configure sentry --disable` | required interactive OAuth; failed on **every** session start |
| `openclaw mcp configure supabase --disable` | duplicate: `supabase-admin` with a token already works |
| `openclaw mcp configure stripe --disable` | required interactive OAuth |

### 2.5 Browser, desktop

| Command | Why |
|---|---|
| `openclaw config set browser.extensionRelay.allowLegacyAuth false` | legacy relay auth was on with **zero** paired extensions (`openclaw browser extension status`: 0 native hosts) |
| `openclaw config set plugins.entries.browser.enabled false` | after the key above, doctor started proposing to enable the browser plugin; pinning it off silences the suggestion without enabling surface we do not use |
| `openclaw config set desktop.host.enabled false` | explicit default. Note: it does **not** silence the "Host desktop disabled" hint, and `config unset` is rejected by the schema |

### 2.6 Telegram channel

| Change | How | Why |
|---|---|---|
| Remove the `groups["*"]` wildcard | `openclaw config unset 'channels.telegram.groups["*"]'` | with `groupPolicy: allowlist` it added nothing and blocked membership probing; only the explicit group id and the per-group `topics["*"]` default remain |
| `streaming.mode: partial → progress` | applied by the `main` agent (Jarvis) from Telegram | one live-edited status message with tool rows instead of partial text |
| `streaming.progress.toolProgress: true` | same | show tool rows (`🛠️ Bash: …`) inside that message |
| `replyToMode: first` | same | the final answer quotes the original request |
| `plugins enable workboard` | `openclaw plugins enable workboard` | agent-work dashboard in the Control UI; it was `disabled` |

Jarvis wrote its own backup before editing: `~/.openclaw/openclaw.json.bak-1789756633`
(11 085 bytes, 18:37) `VERIFIED`. It could not restart the gateway itself, which is why the
changes only took effect when the restart was done from this session.

`streaming.progress.commandText` was deliberately **not** set to `raw`: a command line can carry
tokens or sensitive paths and that text would stay in Telegram history `SESSION`.

### 2.7 Logging and resource limits

| Command | Why |
|---|---|
| `openclaw config set logging.file /home/bitgandtter/.openclaw/logs/gateway.log` | logs lived in `/tmp` (tmpfs, 16 GB, in RAM), 7.6 MB/day, wiped exactly when you need them after a reboot |
| `openclaw config set logging.maxFileBytes 33554432` | bounded rotation |
| `systemctl --user set-property openclaw-gateway.service MemoryHigh=12G` | the gateway peaked at 16.9 GB RSS + 5.3 GB swap in one hour; `--max-old-space-size` only bounds V8's heap, not children. `set-property` writes to `user.control/`, which doctor does not read, so it does not reopen the "operator-owned drop-in" fight |

### 2.8 Network and exposure

| Command | Why |
|---|---|
| `openclaw config set gateway.bind tailnet` | the gateway was loopback-only, so the cluster's EndpointSlice pointed at a closed port. `tailnet` binds `100.76.56.42` **and** keeps `127.0.0.1` |
| `openclaw config set gateway.publicOrigin https://iai.wildbit.dev` | OAuth callbacks, session links and viewer links must use the real origin |
| `openclaw config set gateway.trustedProxies '["10.42.0.0/24"]'` | only Traefik (pod IP `10.42.0.242`, no SNAT) may supply forwarded client identity |
| `openclaw config set gateway.allowRealIpFallback true` | **the non-obvious one**: this k3s Traefik does not send `X-Forwarded-For` to this backend, so the gateway answered `403 proxy_attribution_required`. Isolated with a pod curl: without XFF → 403, with XFF → 200 |
| `openclaw config set gateway.controlUi.allowedOrigins '["https://iai.wildbit.dev"]'` | a public domain origin needs an explicit allowlist even when it resolves to a CGNAT address |
| `openclaw config set plugins.entries.device-pair.config.publicUrl https://iai.wildbit.dev` | fixes the mobile QR payload and silences doctor's `node-hosting-preconditions` warning about the loopback bind |

### 2.9 Authentication

```bash
# value entered by the user, never in a transcript (zsh syntax — see doc-02)
read -rs "P?Password: "; printf '%s' "$P" | openclaw secrets store set GATEWAY_AUTH_PASSWORD --kind secret --value-file -
openclaw config set gateway.auth.password --ref-provider default --ref-source store --ref-id GATEWAY_AUTH_PASSWORD
openclaw config set gateway.auth.mode password
openclaw gateway restart
```

Reason: log into the Control UI with a fixed password instead of minting a single-use pairing URL
each time. Side effect worth knowing: writing `auth.password` **removed `auth.token` from the
config** (the value survives in the store), so rollback is two commands, not one.

Also, `gateway.controlUi.github.token` was set as a SecretRef to the **env** var `GH_TOKEN`
(`--ref-source env --ref-id GH_TOKEN`) so no token text lands in `openclaw.json` and doctor's
GitHub-projects warning clears from any shell.

### 2.10 Claude Code side (not OpenClaw)

| Change | File | Why |
|---|---|---|
| `cleanupPeriodDays: 14` | `~/.claude/settings.json` | transcript retention; chosen by the user against the recommendation to keep 30 |
| `PreToolUse` matcher `Agent|Workflow` → `openclaw-team-progress.py` (kit: `hooks/openclaw_team_progress.py`, registered by `ai-resources setup`, off unless `OPENCLAW_NARRATION` is set) | `~/.claude/settings.json` | publish one line to the topic when a team member or workflow starts |
| `SubagentStop` → `openclaw-team-progress.py` | `~/.claude/settings.json` | publish one line when a member finishes |
| `export WHISPER_CPP_MODEL=...` (guarded by a file test) | `~/.config/shell/paths.env` | doctor evaluates this variable against the **CLI's** environment, not the gateway's |

Pre-existing kit hooks were left intact and the new ones sit beside them: `PreToolUse/Bash →
kubectl-context-guard.py`, `PreToolUse/Write|Edit|MultiEdit|NotebookEdit|Bash →
kit_handoff_guard.py`, `SessionStart → kit_session_start.py`, `SubagentStop →
kit_subagent_return.py` `VERIFIED`.

---

## 3. Installations and migrations

### 3.1 Node and OpenClaw off the version manager `SESSION` + `VERIFIED` (end state)

```bash
brew install node                      # v26.9.0; engines of openclaw: >=24.16 <25 || >=26.1
export PATH=/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin
npm install -g --allow-scripts=openclaw,@google/genai,koffi,tree-sitter-bash,protobufjs openclaw@2026.9.4
```

`--allow-scripts` is mandatory: npm 11 blocks install scripts for global installs, and the first
attempt silently skipped `koffi`, `tree-sitter-bash` and `protobufjs`. Native artifact parity with
the old fnm copy was then compared file by file (`tree-sitter-bash.node`, `pty.node`).

```bash
# regenerate the unit invoking brew's node EXPLICITLY: openclaw.mjs has a
# `#!/usr/bin/env node` shebang, and with fnm first in PATH it would bake the wrong node
/home/linuxbrew/.linuxbrew/bin/node \
  /home/linuxbrew/.linuxbrew/lib/node_modules/openclaw/openclaw.mjs gateway install --force

~/.local/share/fnm/aliases/default/bin/npm -g uninstall openclaw   # single source of truth
```

What to verify after: `grep ExecStart` on the unit points at `opt/node` and at brew's
`node_modules`; `command -v openclaw` resolves to brew even with fnm first in `$PATH`;
`openclaw update status` reports `Install: npm · stable · up to date`; and the whole
`Gateway service config` warning block disappears from doctor.

### 3.2 Whisper model `VERIFIED`

```bash
mkdir -p ~/.local/share/whisper-cpp
curl -sL -o ~/.local/share/whisper-cpp/ggml-base.bin \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin?download=true"
```

`147 951 465` bytes, magic `ggml`, exercised with
`whisper-cli -m ~/.local/share/whisper-cpp/ggml-base.bin -l es <wav>`. OpenClaw only
auto-discovers models under `/opt/homebrew|/usr/local|/usr/share/whisper-cpp`, none of which apply
on linuxbrew, so the path is passed via `WHISPER_CPP_MODEL` in both the service env file and the
shell env.

### 3.3 Backup staging directory `VERIFIED`

```bash
sudo -n install -d -o bitgandtter -g bitgandtter -m 755 \
  /srv/openclaw-backups /srv/openclaw-backups/{daily,weekly,monthly}
```

Mirrors `/srv/k3s-backups` so the cluster uploader mounts it with the same `hostPath` pattern, and
so staging never happens in `/tmp` (the tmpfs lesson from the k3s runbook).

### 3.4 Boot autostart `VERIFIED`

`loginctl enable-linger bitgandtter` was already satisfied (`Linger=yes`); the unit is `enabled`.
No change was needed.

---

## 4. Files created or rewritten

### 4.1 Agent instructions `VERIFIED` (line counts read now)

| Path | Lines | Content |
|---|---|---|
| `~/.openclaw/workspace/AGENTS.md` | 92 | Jarvis: orchestrator role, table of the 6 agents with topic and workspace, pointer to `TOPIC-ROUTING.md` (which the runtime does **not** inject), topics vs `/equipo`, the timers it must not fight, memory conventions, red lines, `## Work reporting` |
| `~/Development/wildbit/pacha/AGENTS.md` | 91 | umbrella layout of the 5 subprojects with repo and docs home, the "durable docs never go to `.agent-output/`" contract from `DOCS.md`, the `app/supabase` symlink trap, kit roles, `## Work reporting` |
| `~/Development/wildbit/elinvo/AGENTS.md` | 113 | umbrella layout (api/ops/site/ui), deploy target (context `default`, ns `elinvo-dev`), local Skaffold workflow, CORS/Stripe origins, docs contract, `## Work reporting` |
| `~/Development/wildbit/org-iac/AGENTS.md` | 100 | umbrella layout (org-gitops + 3 upjet providers + upstream upjet), cluster separation with mandatory `--context default`, docs contract, the `github-credentials.yaml` secret, DevOps kit roles, `## Work reporting` |

All four replaced the identical 6 306-byte generic OpenClaw template (same md5 in the three
original repos). Each repo's OpenClaw files were added to `.git/info/exclude` (local, not the
shared `.gitignore`): `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `MEMORY.md`, `DREAMS.md`,
`BOOTSTRAP.md*`, `memory/`.

The `## Work reporting` block was added later, from Jarvis's own draft: report plans, handoffs and
completion; never narrate individual tool calls (streaming already shows those); never secrets.

| Path | Lines | Content |
|---|---|---|
| `~/Development/wildbit/ai-resources/MEMORY.md` | 45 | local, git-excluded memory for the `ai` agent: its scope (the AI ecosystem, not products), that kit changes propagate through brew + `ai-resources setup`, that the repo's tracked `AGENTS.md` is the contributor guide and must not be rewritten, gateway rules |
| `~/.openclaw/workspace/IDENTITY.md` | — | `- Name: ai` → `- Name: Jarvis`; the `ai` agent had overwritten it while sharing `main`'s workspace |
| `~/Development/wildbit/elinvo/CLAUDE.md` | 49 | deploy-target section rewritten: context `default` (k3s bithome), ns `elinvo-dev`/`elinvo`, explicit `--context`, stop on `aks-*`. The old text demanded `docker-desktop`/`orbstack`/`kind`/`minikube`, none of which exist on this machine |

### 4.2 Host automation `VERIFIED`

**Implemented in the kit since 1.9.0.** The scripts below are now `scripts/openclaw/*` in the
ai-resources repo and the units are rendered from `templates/systemd/*.template`; the team hook is
`hooks/openclaw_team_progress.py`. The `~/.local/bin` and `~/.claude/hooks` paths in the first column
are **historical**: where the files lived when this inventory was taken, on one disk. Line counts are
those of the originals.

| Path (as taken; kit location in the last column) | Lines | Content |
|---|---|---|
| `~/.local/bin/openclaw-backup.sh` → `scripts/openclaw/openclaw-backup.sh` | 149 | tiered backup (`daily|weekly|monthly|manual`), local retention 7/4/3. Bundles openclaw's own verified backup, an engram `VACUUM INTO` snapshot, the config that lives in no repo, and an `INVENTORY.txt` with versions plus restore steps. Stages under `/srv`, writes `.sha256` last, asserts size + listability + checksum, notifies failures over Telegram |
| `~/.local/bin/openclaw-maintenance.sh` → `scripts/openclaw/openclaw-maintenance.sh` | 103 | weekly window: pause watchdog → stop → **wait for the cgroup to drain** → `doctor --fix` → `sessions cleanup --all-agents` → start → poll health up to 2 min → check backup age → report a new OpenClaw version → Telegram only when something is off |
| `~/.local/bin/openclaw-watchdog.sh` → `scripts/openclaw/openclaw-watchdog.sh` | 40 | starts the gateway when it is not active and **says so** on Telegram; paused by `touch ~/.openclaw/watchdog.off` |
| `~/.local/bin/openclaw-verify.sh` → `scripts/openclaw/openclaw-verify.sh` | — | read-only one-pass check of the setup (daily timer with `--notify`); it was missing from the first version of this table |
| `~/.local/bin/openclaw-team-watch.py` → `scripts/openclaw/openclaw-team-watch.py` | — | the per-member live message launched by the team hook; also missing from the first version |
| `~/.claude/hooks/openclaw-team-progress.py` → `hooks/openclaw_team_progress.py` | 125 | maps `cwd → agent → topic` from `openclaw.json` and posts one line when a subagent/workflow starts and when a member stops. Only speaks when `OPENCLAW_CLI=1`, never publishes raw tool input, always exits 0 |

Systemd units, all `VERIFIED`: `openclaw-watchdog.service` + `.timer` (every 2 min),
`openclaw-backup@.service` (templated by tier) + `openclaw-backup-{daily,weekly,monthly}.timer`,
`openclaw-maintenance.service` + `.timer`, and `openclaw-verify.service` + `.timer` (six timers in
all, ten unit files, not counting `openclaw-gateway.service`, which openclaw generates).

**What the backup left out.** Before 1.9.0 the list in `openclaw-backup.sh` held only
`openclaw.json`, `gateway.systemd.env`, the gateway and watchdog units, and the backup and
maintenance scripts. The watchdog, verify and team-watch scripts and the newer unit files were
omitted **by accident, not by design**. From 1.9.0 the list is complete and includes
`~/.openclaw/kit-host.env`; a restore from an older tarball regenerates the units with
`ai-resources openclaw install-units`.

### 4.3 GitOps (org-gitops, on `main`) `VERIFIED`

| Commit | Files |
|---|---|
| `ea8ab1f` — `feat(backups): off-box backups for the OpenClaw AI setup (#57)` | `apps/wildbit/gcp/backups-openclaw.yaml` (84), `apps/wildbit/grafana/alerts-openclaw-backup.yaml` (212), `infrastructure/backup-guard/cronjob-openclaw.yaml` (128), `infrastructure/backup-guard/uploader-openclaw.yaml` (121), plus 3 `kustomization.yaml` edits — 553 insertions |
| `5cdae9b` — `fix(dns): adopt the iai record by recordId` | `apps/wildbit/cloudflare/projects.yaml` (+7/−3) |

Pre-existing from earlier in the day, by the user: `dcf2fd8` (#54) exposing the Control UI at
`iai.wildbit.dev` and `02bf135` (#55) keyless read-only gcloud identity.

Also written but **not** committed: nothing. The working tree of `org-gitops` is clean and on
`main` `VERIFIED`.

---

## 5. Cleanups

| What | Result |
|---|---|
| 3 MCP servers requiring OAuth | disabled; they were failing at every session start `VERIFIED` |
| `openclaw-gateway.service.d/10-path.conf` | removed; it was an operator-owned drop-in that blocked doctor's repairs `VERIFIED` (no such directory) |
| OpenClaw copy under fnm | uninstalled; one install left, and `openclaw update` now hits the right one `VERIFIED` |
| `~/.claude/jobs/09a61dbb` | deleted — 6 516 PNG + 435 MP4 of elinvo E2E runs (Sep 15–16). `~/.claude/jobs` 3.2 GB → 28 KB, `~/.claude` 4.5 GB → 812 MB `VERIFIED` |
| Transcript retention | `cleanupPeriodDays: 14` `VERIFIED` |
| `channels.telegram.groups["*"]` | removed `VERIFIED` |
| Dead local branches | `feat/openclaw-backups`, `feat/openclaw-gateway-tailnet` deleted; no `feat/openclaw*` left on origin `VERIFIED` |
| PR #58 | closed as a duplicate of #54, branch deleted `SESSION` |
| Verification Jobs in `monitoring` | deleted after use; only the two CronJobs remain `VERIFIED` |
| `org-iac/github-credentials.yaml` | `chmod 600` (was 644, a real k8s Secret manifest) `VERIFIED` |
| `~/Development/wildbit/org-iac/BOOTSTRAP.md` | deleted by me out of scope, restored from openclaw's template, and then disappeared again hours later. No session or transcript shows what removed it — `NOT VERIFIED` cause. It is the stock template and its purpose is to be deleted after first run |

---

## 6. Decisions taken

| Question | Options offered | Chosen | Note |
|---|---|---|---|
| Orchestrator model | sonnet-5 / keep haiku | **sonnet-5 for the 4 agents** | as recommended |
| Agent `ai` | sonnet + own workspace / only model / leave | **sonnet + own workspace** (`ai-resources`) | as recommended; ended the workspace collision with Jarvis |
| Node migration | later / now / never | **now, "completely well installed"** | against the recommendation to defer; it went clean |
| Extras | watchdog / GH_TOKEN / whisper model / none | **all three** | — |
| elinvo `CLAUDE.md` drift | fix with reality / point to AGENTS.md / leave | **fix with reality** | as recommended |
| Transcript retention | keep 30 days / pin 30 / drop to 14 | **14 days** | against the recommendation; today's forensics needed two-day-old transcripts |
| Guard `weekly` tier | keep hard / soft like monthly / grace period | **keep hard** | as recommended |
| `recordId` backfill | small PR / bundle later / leave | **straight to main** | user's wording: "todo directo a main" |
| PR #57 merge blocked by the review ruleset | admin bypass / approve elsewhere / auto | **`--admin`**, explicitly authorized | governance decision, not mine |
| UI login | pairing URL / fixed password | **fixed password** in the secret store | value entered by the user, never in a transcript |
| 2FA on Telegram | — | **done**, reported by the user | not verifiable from here by design |
| BotFather privacy mode | — | **done**, verified via `getMe` | `can_read_all_group_messages: true` |
| 3.2 GB of E2E artifacts | delete / keep logs only / leave | **delete** | — |
| First-upload verification | already covered / notify on success / check tomorrow | **already covered** | — |

---

## 7. How each thing was verified

| Claim | Check | Result |
|---|---|---|
| Gateway healthy on brew node | `openclaw health`; `ps` on the main PID | `event loop ok`; process runs `/home/linuxbrew/.linuxbrew/opt/node/bin/node` `VERIFIED` |
| `bind: tailnet` keeps loopback | `ss -lntp \| grep 18789` | `127.0.0.1:18789` and `100.76.56.42:18789` `VERIFIED` |
| Agents run sonnet with high effort | gateway log `cli exec: …`; `ps` on the child | `model=claude-sonnet-5`; child args `--effort high --model claude-sonnet-5` `SESSION` |
| Kit teams visible to the agents | fresh-session probe on `snoutzone` | lists `planner/implementer/software-architect/tester/code-reviewer/verifier`, the kit skills, and both `Agent` and `Workflow` `SESSION` |
| Project `CLAUDE.md` is not loaded | ran the CLI as OpenClaw does inside `elinvo` | only `~/.claude/CLAUDE.md` loaded `SESSION` |
| `/equipo` is unrestricted Claude Code | captured the child process args | no `--setting-sources`, no `--disallowedTools`, no `--strict-mcp-config`, plus `--dangerously-skip-permissions` `SESSION` |
| New `AGENTS.md` is read | fresh-session probes on `main`, `snoutzone`, `elinvo`, `devops` | each answered from its own file (topic 90 → `devops`, migrations → `services/supabase/migrations/`, `--context default`, `watchdog.off`) `SESSION` |
| doctor is clean | `openclaw doctor` from a login shell | exit 0, zero `[warning]`, zero `Doctor warnings` blocks `SESSION` |
| The maintenance window works | ran `openclaw-maintenance.sh` for real | `Doctor complete`, `Repaired legacy bindings … in 1 session`; found 3 bugs that were fixed (`--all-agents`, health poll, ~5 min stop) `SESSION` |
| The watchdog revives the gateway | let `doctor --fix` leave it dead at 15:32:47 | back up by itself at 15:34:21 `SESSION` |
| Backup content is restorable | extracted the tarball | `engram.db` opens (60 objects), 86 config entries, `INVENTORY.txt` present `SESSION` |
| Backups exist locally | `ls /srv/openclaw-backups/*/*.tar.gz` | `daily/…170412`, `weekly/…170504`, 2 `manual/`, 71 MB `VERIFIED` |
| Backups are off-box | `gcloud storage ls -r` as the user account | 4 objects under `daily/` and `weekly/` with their `.sha256` `VERIFIED` |
| Lifecycle rules are live | `gcloud storage buckets describe` | Delete at 8d `daily/`, 35d `weekly/`, 100d `monthly/`, 8d `manual/` `SESSION` |
| The uploader asserts | ran the CronJob manually | `SUBIENDO daily/… (17.6M)`, `46.5MiB/s`, `OK: el daily mas reciente esta off-box`; second pass `YA daily/…` (idempotent) `SESSION` |
| The guard is green | ran it manually and it also ran on schedule | `OK daily 0.0h 18 MB`, `OK weekly 0.0h`, `VACIO monthly (tier blando)`; scheduled Job `openclaw-backup-guard-29829260` `Complete` `VERIFIED` |
| Alerts are loaded and watching | Grafana rule list + a PromQL query | 4 rules `state: normal, health: ok`; `kube_cronjob_status_last_successful_time` exists for both CronJobs `SESSION` |
| GitOps is synced | `flux get kustomizations`; `kubectl get managed` | 39 kustomizations all Ready; **1268 managed resources, 0 unsynced** `SESSION` |
| The DNS record is adopted | Crossplane MR after the commit | renamed `…-iai-17` → `…-d2163d897dfd916d726275b912bc44ee`, `Ready/Synced=True`, DNS and HTTPS intact `SESSION` |
| The public URL works | `curl https://iai.wildbit.dev`; `openssl s_client` | `200 text/html`; cert `CN=iai.wildbit.dev`, Let's Encrypt, valid to 2026-12-17 `SESSION` |
| Ingress and cert in the cluster | `kubectl get ingress,certificate -n host-services` | `openclaw-ui` on `iai.wildbit.dev`; `openclaw-ui-tls READY=True` `VERIFIED` |
| Password auth did not break the CLI | `openclaw health`; backup script; agent turn | all three fine after the switch `SESSION` |
| Mobile pairing follows the domain | `openclaw qr --json` | `wss://iai.wildbit.dev`, `auth: password`, `urlSource: device-pair.config.publicUrl` `VERIFIED` |
| Paired devices survived | `openclaw devices list` | `openclaw-control-ui` and `openclaw-tui` still present with operator scopes `VERIFIED` |
| BotFather privacy mode | Bot API `getMe` | `can_read_all_group_messages: true` `VERIFIED` |
| **The team hook publishes** | audit vs gateway log correlation | `ai Agent started 18:46:51` ↔ `outbound send ok … threadId=315 messageId=507` at **18:46:51**, same second `VERIFIED` |
| Workboard is loaded | startup line in the gateway log | `15 plugins: … workboard …` `VERIFIED` |
| Timers armed | `systemctl --user list-timers 'openclaw-*'` | 6 timers (the five listed at the time plus `openclaw-verify.timer`), all enabled and active, with their next run `VERIFIED` |

---

## 8. Reconstruction order

If this has to be rebuilt from zero, this is the dependency order that worked. Details for each
step are in sections 2–4; the traps are in `doc-02-trampas-y-recomendaciones.md`.

1. Homebrew node, then `npm i -g openclaw@<version>` **with `--allow-scripts`**.
2. `openclaw gateway install --force` invoking brew's node by absolute path; `enable-linger`.
3. Config: models and thinking, heartbeats, `tools.alsoAllow`, MCP, logging, `MemoryHigh`.
4. Agents and their workspaces; then the `AGENTS.md` of each workspace (project `CLAUDE.md` will
   not be read) and `.git/info/exclude`.
5. Exposure: `bind: tailnet` → DNS A record to the tailnet IP → Service/EndpointSlice/Ingress →
   `publicOrigin`, `trustedProxies`, `allowRealIpFallback`, `controlUi.allowedOrigins`,
   `device-pair.publicUrl`.
6. Auth: password into the secret store, SecretRef, `auth.mode`.
7. Backups: staging dir, the host scripts, the units and timers (`ai-resources openclaw
   install-units --enable`); then the bucket, the uploader, the guard and the alerts in GitOps
   (`templates/gitops/openclaw-backups/`, rendered with `ai-resources openclaw render-gitops-backups`).
8. Observability of the teams: the kit's team narration hook (`ai-resources setup`, question
   "narrate the team") plus `streaming.mode:
   progress` and the `## Work reporting` block.
9. Verify with section 7 as the checklist, and only then call it done.
