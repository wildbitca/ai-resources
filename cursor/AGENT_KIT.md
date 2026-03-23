# ai-resources (pointer)

Set **`AGENT_KIT`** to the root of the **ai-resources** git clone (or run commands via **`bin/agent-kit`**, which sets it automatically).

| What | Path |
|------|------|
| Rules, workflows, skills, templates | `$AGENT_KIT/cursor/` |
| Skill registry + index | `$AGENT_KIT/cursor/skills-registry.md`, `$AGENT_KIT/cursor/skills-index.json` |
| Refresh | `$AGENT_KIT/bin/agent-kit self-update` |
| Engram / delegation rules | `$AGENT_KIT/cursor/rules/014-engram-mcp-protocol.mdc`, `050-subagent-delegation.mdc` |

**`~/.cursor`** is reserved for Cursor IDE runtime files only; this kit does not require copying rules there until an explicit install links them.
