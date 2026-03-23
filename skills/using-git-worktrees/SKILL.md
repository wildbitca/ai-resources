---
name: using-git-worktrees
description: "Create and manage git worktrees for isolated development. Use when starting parallel or isolated work, workflow branches, or avoiding dirty main checkout. (triggers: git worktree, worktree, isolated branch, parallel development)"
---

# Using Git Worktrees

## Purpose

Create and manage **git worktrees** so feature or fix work happens in a separate directory and branch without disturbing the primary checkout.

## Steps

1. **Determine base branch** — Use the repository’s agreed integration branch (commonly `develop` or `main`). Ensure it is up to date (`git fetch`, then checkout/pull that branch in the primary repo if your process requires it).
2. **Create worktree** — From the primary repository root, add a linked worktree on a new or existing branch (see Commands).
3. **Branch naming** — Prefer `feature/<short-name>` for features and `fix/<short-name>` for bugfixes; keep names short and unique.
4. **Set handoff path** — If the workflow uses a handoff file, record its path (e.g. a repo-relative path agreed by the team) and pass it into every subsequent step.
5. **Work only in the worktree** — Open the worktree directory as the active workspace; run builds, tests, and commits there until the branch is finished.

## Commands

| Action                                   | Command (patterns)                                        |
|------------------------------------------|-----------------------------------------------------------|
| Add worktree + new branch                | `git worktree add <path> -b feature/<name> <base-branch>` |
| Add worktree for existing branch         | `git worktree add <path> <existing-branch>`               |
| List worktrees                           | `git worktree list`                                       |
| Remove worktree (after merged/abandoned) | `git worktree remove <path>`                              |

Use `git worktree remove --force <path>` only when the worktree path is gone or Git reports a stale lock (understand why before forcing).

## Integration

- **Workspace**: Point the editor/IDE and all terminal sessions at the **worktree path** for the duration of the task.
- **Consistency**: Same remotes and config as the main repo; commits made in the worktree belong to the same Git object database.

## Anti-patterns

- Editing the same branch from two worktrees without coordination.
- Leaving obsolete worktrees listed under `git worktree list` after merge — remove them to avoid confusion.
