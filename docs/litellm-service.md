# LiteLLM Service — Operational Guide

The kit runs LiteLLM as a **native OS service** managed by launchd (macOS) or
systemd-user (Linux). No Docker required. Auto-start at every login.

## How it works

```
┌──────────────────────────────────────────────────────┐
│  launchd / systemd-user  (auto-start at login)       │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│  ~/.config/ai-resources/bin/litellm-run  (wrapper)   │
│   1. Sources .env (chmod 600)                        │
│   2. Execs `litellm --config ... --port 4000`        │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│  litellm  (pipx-installed, ~/.local/bin/litellm)     │
│  HTTP server on 127.0.0.1:4000                       │
└──────────────────────────────────────────────────────┘
```

## Components on disk

| Path | Purpose |
|---|---|
| `~/.config/ai-resources/litellm.yaml` | Gateway config (model_list, routing, fallbacks) |
| `~/.config/ai-resources/.env` | Credentials — chmod 600, sourced by wrapper |
| `~/.config/ai-resources/bin/litellm-run` | Wrapper script — exports env, execs litellm |
| `~/.config/ai-resources/logs/litellm.{log,err}` | macOS log files (Linux uses journald) |
| `~/Library/LaunchAgents/com.ai-resources.litellm.plist` | macOS LaunchAgent |
| `~/.config/systemd/user/ai-resources-litellm.service` | Linux systemd-user unit |

## Run modes

| Mode | Where litellm lives | When to use |
|---|---|---|
| **pipx** (default) | `~/.local/bin/litellm` (pipx isolation) | Clean isolation, easy update via `pipx upgrade` |
| **pip-venv** | `~/.config/ai-resources/venv/bin/litellm` | Want a fully self-contained install under the kit |
| **remote** | URL to existing service | Team/CI shared gateway |

## Lifecycle commands

```sh
ai-resources daemon status     # is the service running?
ai-resources daemon start      # load + start the service
ai-resources daemon stop       # unload / stop
ai-resources daemon restart    # stop + start
ai-resources daemon logs --tail 100
ai-resources daemon update     # pipx upgrade + restart
```

Underneath:
- **macOS:** `launchctl load|list|unload <plist>`
- **Linux:** `systemctl --user enable --now|is-active|stop <unit>`

## Manual operations

If something goes wrong, drive the service directly:

**macOS — run the wrapper alone:**
```sh
~/.config/ai-resources/bin/litellm-run
# Should print "Uvicorn running on http://127.0.0.1:4000"
```

**Linux — view systemd journal:**
```sh
journalctl --user -u ai-resources-litellm.service -f
```

**Both — check the process:**
```sh
lsof -iTCP:4000 -sTCP:LISTEN
ps aux | grep litellm | grep -v grep
```

## Updating LiteLLM

```sh
ai-resources daemon update
```

This runs `pipx upgrade litellm` (or `pip install --upgrade` for venv mode)
and restarts the service.

To pin a specific version (rare):

```sh
pipx install --force 'litellm[proxy]==1.55.10'
ai-resources daemon restart
```

## Auto-start

Default is enabled. To disable:

**macOS:**
```sh
launchctl unload ~/Library/LaunchAgents/com.ai-resources.litellm.plist
rm ~/Library/LaunchAgents/com.ai-resources.litellm.plist
```

**Linux:**
```sh
systemctl --user disable --now ai-resources-litellm.service
rm ~/.config/systemd/user/ai-resources-litellm.service
```

Re-enable: re-run `ai-resources setup`.

## Network security

- Default bind: `127.0.0.1:4000` — **localhost only**.
- Setting bind to `0.0.0.0` exposes the gateway to LAN. Don't do that without
  TLS + auth.
- Master key (`LITELLM_MASTER_KEY`) required for all calls. Stored in `.env`
  with chmod 600. The wrapper sources `.env` so the key never appears in the
  service file or in environment of unrelated processes.

## Caching

Two layers:

1. **Anthropic prompt caching** — preserved when routing to Claude. Saves
   30–90% on repeated context.
2. **LiteLLM local in-memory cache** — `cache_params.type: local` (TTL 600s).

To use Redis (multi-machine team setup), edit `litellm.yaml`:

```yaml
litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: redis.internal
    port: 6379
```

Then `ai-resources daemon restart`.

## Common failures

**`pipx: command not found`**
```sh
brew install pipx                                        # macOS
python3 -m pip install --user pipx && python3 -m pipx ensurepath  # Linux
```
Re-run `ai-resources setup`.

**Service won't start (macOS)**
```sh
~/.config/ai-resources/bin/litellm-run    # run wrapper directly
```
Common cause: `litellm` not in PATH for the LaunchAgent. The plist
includes `~/.local/bin`. If pipx put it elsewhere, edit the plist or
re-run setup with the corrected path.

**Service won't start (Linux)**
```sh
journalctl --user -u ai-resources-litellm.service -n 50
```
Common cause: `.env` not chmod 600 or owner mismatch.

**Port 4000 already in use**
```sh
lsof -iTCP:4000 -sTCP:LISTEN
```
Kill the conflicting process, or change the port in the wizard (step 3).

**`litellm` binary missing after system update**
```sh
pipx reinstall litellm
ai-resources daemon restart
```

## Switching to remote mode later

```sh
ai-resources setup                # step 3 → remote → URL + master key
```

The wizard updates state and removes the local lifecycle file automatically.
