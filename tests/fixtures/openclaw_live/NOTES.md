Round 4 S3 findings (docs only, no live host calls)

- Live agents sit under `agents.entries.<id>.model` (a dict keyed by agent id) on openclaw 2026.10.x, not `agents.list[]`;
  `live_openclaw.live_agents` reads `entries` first and tolerates a `list` of `{id, ...}` as well.
- `agents.defaults.models.<provider/model>.agentRuntime.id` is the per-model runtime ("openclaw" in the fixture).
- `openclaw configure --section model` (docs/cli/configure.md) does not document a gateway restart. Setup still routes any key
  it classifies as restart-required to host_restart_pending and never restarts (ADR-0003). The command is interactive and is
  only ever handed the terminal on an explicit operator choice.
- `openclaw channels add --channel <id> --use-env` is registered per channel only (docs/cli/channels.md): telegram declares it
  and requires TELEGRAM_BOT_TOKEN. A channel that leaves it out falls back to the interactive native command.
- `claude auth status` JSON: the subscription shape is observed (loggedIn, authMethod "claude.ai", apiProvider "firstParty",
  subscriptionType). API-key, Vertex and Bedrock shapes are UNVERIFIED and classify as "unknown" unless the environment or
  settings prove otherwise.
- providers/google.md: the Antigravity terms prohibit third-party tools from using Antigravity OAuth.
