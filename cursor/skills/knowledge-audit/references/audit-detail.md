# knowledge-audit — reference (merge, relink, SDD)

## SDD half-state (what to eliminate)

| Symptom | Fix |
|---------|-----|
| Stale path `knowledge/research/...` without `specs/` | Rewrite to `specs/knowledge/...` |
| Validation `.md` in `searchable/` duplicates verify output already in specs | Merge any unique gap, then `git rm` |
| `research/` dump still open after spec + BDD updated | Merge stragglers, then `git rm` |
| Migration table rows pointing at deleted files | Edit `specs/PROJECT.md` (canonical index) |
| Blocked workflow (no promotion) | Skip prune; leave knowledge as WIP |

## Relink patterns

- **Wrong root:** `knowledge/research/foo.md` → `specs/knowledge/research/foo.md` in prose and links.
- **From `specs/PROJECT.md`:** link to README as `knowledge/README.md` (relative from `specs/`) or repo-root-consistent path used elsewhere.

## Merge-first (before `git rm`)

1. Unique bullets not in canonical specs → smallest home (feature / cross-cutting / ADR).
2. Optional 2-line *Superseded* header, then remove in same change set.

## Stub template (optional)

```markdown
> **Superseded** — Promoted to `specs/features/…/spec.md` (REQ-…). Retained until [issue] only.
```

## When NOT to delete

- Findings not yet tracked in issues/PM tool and not merged into specs
- ClickUp/blocker research sole record of a decision
- Read errors on file — note *verify locally*

## Tiers

| Tier | Actions |
|------|--------|
| A | Inventory, relinks, README + `PROJECT.md` sync |
| B | Merge gaps, `git rm` redundant knowledge |
| C | Spec vs knowledge conflict — human decides; do not overwrite canonical specs silently |
