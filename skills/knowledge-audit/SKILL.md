---
name: knowledge-audit
description: "SDD closure + full specs/knowledge sync: relink, merge into canonical specs, rewrite README, prune — mandatory after workflows that write knowledge. (triggers: specs/knowledge, knowledge audit, SDD closure, /knowledge-audit, obsolete research)"
---

# Knowledge audit (SDD closure + knowledge ↔ specs sync)

## When to use

- User invokes **`/knowledge-audit`** or asks to audit, clean, prune, relink, or align `specs/knowledge/` with canonical specs
- **After any SDD workflow step** that produced or updated files under `specs/knowledge/` (see **SDD closure** below)
- After a migration: repair links, rewrite indexes, drop obsolete artifacts
- Periodic hygiene before a release

## Truth model

| Location                                                         | Role                                                                   |
|------------------------------------------------------------------|------------------------------------------------------------------------|
| `specs/features/`, `specs/cross-cutting/`, `specs/architecture/` | **Canonical** product + architecture                                   |
| `specs/knowledge/{research,decisions,searchable}/`               | **Working** evidence — **must not** hold long-term rules once promoted |

## SDD closure (mandatory — avoid “data a medias”)

**Rule:** If a workflow run **promoted** content into `specs/` (or decided specs are authoritative), the **same run** must execute this skill **before** the handoff file is deleted — unless the workflow **Blocked** with no spec/knowledge promotion.

| Global workflow                                                 | Run knowledge-audit at                                                    |
|-----------------------------------------------------------------|---------------------------------------------------------------------------|
| `$AGENT_KIT/workflows/_feature-template.workflow.yaml`          | end of **`verify`** (after specs + validation artifact updates)           |
| `$AGENT_KIT/workflows/_bugfix-template.workflow.yaml`           | end of **`verify`**                                                       |
| `$AGENT_KIT/workflows/explore-and-plan.workflow.yaml`           | end of **`plan`** (after decisions/ + optional research)                  |
| `$AGENT_KIT/workflows/release-dart-flutter.workflow.yaml`       | end of **`verify`** (after release notes + knowledge artifacts)           |
| `$AGENT_KIT/workflows/cross-domain-backend-infra.workflow.yaml` | end of **`verify`**                                                       |
| `$AGENT_KIT/workflows/security-devsecops.workflow.yaml`         | end of **`report`** (after copy to `specs/knowledge/searchable/` if used) |

Orchestrators and one-shot agents: load this file, then **execute** the steps below in repo root. Do not only summarize. Extended patterns: `references/audit-detail.md`.

## Steps

1. **Exists?** If `specs/knowledge/` is missing → *"No `specs/knowledge/` — nothing to sync"* and stop.

2. **Inventory**
   ```bash
   find specs/knowledge -type f \( -name '*.md' -o -name '*.log' \) ! -path '*/README.md' | sort
   ```

3. **Stale paths — fix in place**
   ```bash
   rg -n '(^|[^/])knowledge/(research|decisions|searchable)/' specs .cursorrules .cursor 2>/dev/null || true
   ```
   Rewrite hits to `specs/knowledge/...`. Repair broken markdown links under `specs/`.

4. **Canonical gap pass** — For each inventory file: merge unique requirements into the right spec/ADR; then remove file or stub per `references/audit-detail.md`.

5. **`specs/PROJECT.md`** — Remove stale rows in any **Migration Artifacts** (or similar) table; keep in sync with disk.

6. **`specs/knowledge/README.md`** — Update **Current inventory** and policy pointers.

7. **Prune** — `git rm` when substance is canonical or file is empty placeholder; cite targets in the session summary. Tier C (conflict / huge merge) → flag user, keep file.

8. **Verify** — Re-run `rg` from step 3; table ↔ disk.

9. **Output** — Short Markdown: edits, removals, relinks, remaining files + one-line rationale.

## Anti-patterns

- Leaving workflow-generated validation/research in `specs/knowledge/` after the same facts live in `specs/features/` or traceability matrices
- Duplicating long-term rules only in knowledge
- Repo-local scripts as the sole cleanup mechanism
