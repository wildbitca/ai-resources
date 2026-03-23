# Shared skill references

| File | Source | Purpose |
|------|--------|---------|
| `persistence-contract.md` | [agent-teams-lite](https://github.com/Gentleman-Programming/agent-teams-lite) (Apache-2.0) | Mode resolution, who reads/writes Engram vs filesystem, SDD sub-agent rules |
| `engram-convention.md` | same | `topic_key` naming, two-step recovery (`mem_search` → `mem_get_observation`) |
| `LICENSE.agent-teams-lite` | upstream | License text |

Local rule that applies these in Cursor: `$AGENT_KIT/cursor/rules/014-engram-mcp-protocol.mdc`.

Agent-agnostic overview: `$AGENT_KIT/docs/engram-protocol-summary.md` (created alongside this setup).
