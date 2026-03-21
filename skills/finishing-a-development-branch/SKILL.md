---
name: finishing-a-development-branch
description: "Complete work on a development branch: merge locally, open a PR, or keep the branch; clean up worktrees and handoff files. Use when closing a feature/fix workflow or finishing isolated work. (triggers: merge branch, finish branch, worktree remove, handoff, close workflow)"
---

# Finishing a Development Branch

## Purpose

Close out **feature/** or **fix/** work cleanly: integrate with the base branch, or hand off via PR, or stop without merging — and always clean up **worktrees** and **handoff** artifacts when the workflow ends.

## Options

| Option | When | Default |
|--------|------|--------|
| **1 — Merge locally** | Team merges on the integration machine; no PR required | **Default** |
| **2 — Pull request** | Review or CI must run before merge | When policy requires it |
| **3 — Keep branch** | Experiment or pause; no merge yet | Explicit human choice |

## Option 1 — Merge to base locally

1. **Commit** — In the worktree (or branch checkout), ensure all intended changes are committed; working tree clean.
2. **Checkout base** — In the **primary** repository checkout: `git checkout <base-branch>` and update (`git pull` if applicable).
3. **Merge** — `git merge <feature-or-fix-branch>` (or merge via your team’s preferred strategy, e.g. `--no-ff`).
4. **Remove worktree** — `git worktree remove <worktree-path>` (from repo root); confirm with `git worktree list`.
5. **Delete branch** — After a successful merge, optionally `git branch -d <branch>` locally; delete remote branch if your flow no longer needs it.
6. **Handoff** — Delete the workflow handoff file if your process uses one.

## Option 2 — Pull request

1. **Push branch** — From the worktree: `git push -u origin <branch>` (or your remote name).
2. **Open PR** — e.g. `gh pr create` with title/body per team template; wait for review and CI.
3. **After merge on remote** — Update local base, remove local worktree when done, delete handoff file.

## Option 3 — Keep branch as-is

- Do not merge; leave the branch and worktree in place **or** remove only the worktree after pushing the branch so work is not lost.
- Still **delete the handoff file** if the current workflow leg is complete and the handoff would otherwise become stale.

## Cleanup (all options)

- **Handoff file**: Remove on workflow completion so the next run does not read obsolete state.
- **Worktree**: Remove when no longer needed so `git worktree list` stays accurate.

## Anti-patterns

- Merging with uncommitted changes in the worktree.
- Removing a worktree before pushing or merging when that branch exists only locally.
