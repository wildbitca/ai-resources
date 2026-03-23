# Handoff / sub-agent result envelope (unified)

Compatible with **Agent Teams Lite** result contract and **SnoutZone** handoff files.

## 1) Short reply block (sub-agent final message)

Use this in the **sub-agent’s last message** (parent thread stays minimal):

```text
## Result
- Status: (success | partial | blocked)
- Executive summary: (1–3 sentences — ATL-style)
- Summary bullets: (max 5)
- Handoff: (path to updated handoff file)

## Artifacts
- Files touched: (paths, one per line)
- Commands run: (one line each, or "none")
- Specs/docs read: (paths or "none")
- Engram: (topic_keys or observation ids saved, or "none")

## Routing
- Next recommended: (workflow step or phase, or "none")
- Blocked: (yes/no — if yes, Return_to_step / reason)
- Risks: (short, or "none")
```

## 2) Handoff file (`.agent-output/handoff-*.md`)

See `$AGENT_KIT/cursor/rules/051-handoff-protocol.mdc` and template `$AGENT_KIT/cursor/handoff.md.template`.

Fields align with **051**: Status, Goal reached, Changes made, Blocked, Return_to_step, Domain, Git integration.

## 3) ATL alignment

| ATL field | Our field |
|-----------|-----------|
| `executive_summary` | `Executive summary` + first bullet under Summary |
| `artifacts` | Artifacts section + Engram line |
| `next_recommended` | Next recommended |
| `risks` | Risks |
| `status` | Status |

## 4) Engram

When persisting substantial findings: `mem_save` with structured **What / Why / Where / Learned** in `content` (see `014-engram-mcp-protocol.mdc`).
