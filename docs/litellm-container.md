# LiteLLM Container — Operational Guide

The kit ships LiteLLM as a containerized gateway. Local install is the
default; remote endpoint is also supported.

## Container details

- **Image:** `ghcr.io/berriai/litellm:main-stable` (override in setup wizard)
- **Container name:** `ai-resources-litellm`
- **Port:** `127.0.0.1:4000` by default (host bind to localhost)
- **Config volume:** `~/.config/ai-resources/litellm.yaml:/app/config.yaml:ro`
- **Env volume:** `~/.config/ai-resources/.env:/app/.env:ro`
- **Health check:** `/health/liveliness` every 30s

## Lifecycle

```sh
ai-resources daemon start      # docker compose up -d
ai-resources daemon stop       # docker compose down
ai-resources daemon restart
ai-resources daemon status
ai-resources daemon logs --tail 200
ai-resources daemon update     # pull latest image + restart
```

## Auto-start at login

| Platform | File |
|---|---|
| macOS | `~/Library/LaunchAgents/com.ai-resources.litellm.plist` |
| Linux | `~/.config/systemd/user/ai-resources-litellm.service` |

These wait for the container runtime to be ready (`docker info` returns 0)
before starting the container.

## Container runtime support

| Runtime | Detection | Notes |
|---|---|---|
| Docker | `docker version` | Default. Requires Docker Desktop or daemon. |
| Podman | `podman version` | Linux. `podman compose` plugin required. |
| Colima | `colima version` | macOS Docker alternative — works transparently. |

The wizard picks the first available. To change after install, edit
`~/.config/ai-resources/setup-state.yaml` (`litellm.local.runtime`) and re-run
`ai-resources setup`.

## Network security

- Default bind is `127.0.0.1` — gateway is **localhost only**.
- Setting bind to `0.0.0.0` exposes the gateway to your LAN. **Don't** do that
  unless you know what you're doing and have set up TLS + auth.
- Master key (`LITELLM_MASTER_KEY`) is required for all gateway calls. Stored
  in `.env` (chmod 600).

## Cost / observability

LiteLLM logs every call. Default log location:
- `docker logs ai-resources-litellm` (and via `ai-resources daemon logs`)
- macOS: `~/.config/ai-resources/logs/litellm.log` (when auto-start active)

To enable verbose cost logging, edit `~/.config/ai-resources/litellm.yaml`:

```yaml
litellm_settings:
  set_verbose: true   # logs cost lines parseable by `ai-resources audit`
```

Then restart: `ai-resources daemon restart`.

## Caching

LiteLLM has two caching layers:

1. **Anthropic prompt caching** — preserved when routing to Claude models.
   Anthropic's `cache_control` headers flow through unchanged. Saves 30-90%
   on repeated context.

2. **LiteLLM semantic cache** — local in-memory cache by default
   (`cache_params.type: local`). Returns cached responses for identical
   prompts. Useful for repeated `code-reviewer` runs over the same diff.

To use Redis instead of local cache (multi-machine team setup):

```yaml
litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: redis.internal
    port: 6379
```

## Remote LiteLLM (team / CI)

For teams or CI pipelines that share a gateway:

```sh
ai-resources setup
# at the gateway step, choose "Use existing remote endpoint"
# enter URL + master key
```

The kit writes the gateway URL into each cockpit's config and stores the
master key in `~/.config/ai-resources/.env`. No local container needed.

Validate remote reachability:

```sh
ai-resources doctor
```

## Updating the LiteLLM image

```sh
ai-resources daemon update
```

This pulls the latest tag (per `setup-state.yaml`) and restarts the
container. The kit pins to a specific tag at setup time; `--image <tag>`
overrides for one-off updates.

## Common failures

**"docker compose plugin not detected"**
Install: `brew install docker-compose` (or use Docker Desktop which bundles it).

**"Container failed to pass health check"**
- Check logs: `ai-resources daemon logs --tail 100`
- Validate config: `~/.config/ai-resources/litellm.yaml` syntax
- Verify credentials in `.env`

**"Container running but health check timeout"**
- LiteLLM may take 30-60s to fully initialize on first run
- Increase wait: edit `~/.config/ai-resources/docker-compose.yaml`,
  `healthcheck.start_period: 60s`

**Supply-chain warning from Anthropic docs**
Anthropic's official guide on LLM gateways notes two compromised PyPI
versions of `litellm` historically. The kit pins to specific GitHub
container registry tags — not PyPI. Always pull via `ghcr.io/berriai/litellm`
with explicit version tags. Never `pip install litellm` for the proxy.
