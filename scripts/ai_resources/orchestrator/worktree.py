"""One worktree and branch per team (ADR-0015 D3), through the same primitive the lanes use.

`lanes_cmd.create_worktree` runs `openclaw worktrees create <repo> --name <n> --base-ref <ref> --json`, which prints the path
and the `openclaw/<name>` branch. The runner is injected so the engine and its tests never need a real `openclaw`. A
workspace that is not a git checkout gets no worktree (the team works in place, and the run says so): one writer per run is
the safety rule, the worktree is what keeps two teams apart.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .. import lanes_cmd

Runner = Callable[..., "tuple[int, str, str]"]


def worktree_name(run_id: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", f"team-{run_id}".lower()).strip("-")[:60]


def is_git_checkout(path: Path, runner: Runner) -> bool:
    rc, out, _ = runner(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], None)
    return rc == 0 and out.strip() == "true"


def create(run_id: str, workspace: str, base_ref: str, runner: Runner | None = None) -> dict[str, Any] | None:
    """The worktree record `{id, path, branch, name}`, or None when `workspace` is not a git checkout."""
    run = runner or lanes_cmd.default_runner
    repo = Path(workspace)
    if not is_git_checkout(repo, run):
        return None
    ctx = lanes_cmd.Ctx(run, repo, run_id, Path("/nonexistent-orchestrator-state"))
    return lanes_cmd.create_worktree(ctx, worktree_name(run_id), base_ref or "HEAD")


def remove(record: dict[str, Any], workspace: str, runner: Runner | None = None) -> bool:
    """Remove a team's worktree (`openclaw worktrees remove` snapshots it first). False when it could not be removed."""
    run = runner or lanes_cmd.default_runner
    ctx = lanes_cmd.Ctx(run, Path(workspace), str(record.get("name") or "team"), Path("/nonexistent-orchestrator-state"))
    try:
        lanes_cmd.remove_worktree(ctx, str(record.get("id") or record.get("name")))
    except lanes_cmd.LaneCmdError:
        return False
    return True
