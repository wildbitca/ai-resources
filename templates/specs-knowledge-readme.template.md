# Knowledge — working artifacts (not product truth)

This directory is under **`specs/`** so docs stay centralized. **Canonical behavior** live in `specs/features/`, `specs/cross-cutting/`, `specs/architecture/` (ADRs).

| Subfolder     | Use                                              |
|---------------|--------------------------------------------------|
| `research/`   | Codebase exploration, bug analysis, extractions  |
| `decisions/`  | Plans, migration notes, fix rationales           |
| `searchable/` | Validation reports, security/architecture audits |

**Lifecycle:** When content is promoted into specs and work is done, **delete** obsolete files here. Git history preserves the past.

Audit/cleanup: use **`/knowledge-audit`** or the global **`knowledge-audit`** skill (`$AGENT_KIT/skills/knowledge-audit/SKILL.md`) — no repo script required.
