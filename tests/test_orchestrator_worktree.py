"""One worktree per team through the lanes primitive (ADR-0015 D3)."""
from __future__ import annotations

import pytest

from ai_resources import lanes_cmd
from ai_resources.orchestrator import worktree
from orchestrator_fakes import Runner


def test_a_git_checkout_gets_a_worktree_named_after_the_run(tmp_path):
    r = Runner(tmp_path)
    rec = worktree.create("20261023-120000-feature-ab12", "/repo", "main", r)
    assert rec["branch"] == "openclaw/team-20261023-120000-feature-ab12" and rec["path"].endswith(rec["name"])
    create = next(c for c in r.calls if c[:3] == ["openclaw", "worktrees", "create"])
    assert create[3] == "/repo" and create[create.index("--base-ref") + 1] == "main" and "--json" in create


def test_a_directory_that_is_not_a_checkout_gets_none_and_no_create_call(tmp_path):
    r = Runner(tmp_path, git=False)
    assert worktree.create("r1", "/not-a-repo", "HEAD", r) is None
    assert not any(c[:3] == ["openclaw", "worktrees", "create"] for c in r.calls)


def test_names_are_safe_and_bounded():
    assert worktree.worktree_name("Run ID/../x") == "team-run-id-x"
    assert len(worktree.worktree_name("a" * 200)) <= 60


def test_a_failing_create_raises_the_lanes_error_after_its_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(lanes_cmd.time, "sleep", lambda s: None)
    r = Runner(tmp_path, fail_create=True)
    with pytest.raises(lanes_cmd.LaneCmdError):
        worktree.create("r1", "/repo", "HEAD", r)
    assert sum(1 for c in r.calls if c[:3] == ["openclaw", "worktrees", "create"]) == 3


def test_remove_uses_openclaw_worktrees_remove_and_reports_failure(tmp_path):
    r = Runner(tmp_path)
    rec = worktree.create("r1", "/repo", "HEAD", r)
    assert worktree.remove(rec, "/repo", r) is True
    assert r.calls[-1] == ["openclaw", "worktrees", "remove", rec["id"]]
    bad = lambda argv, cwd=None: (1, "", "nope")
    assert worktree.remove(rec, "/repo", bad) is False


def test_the_worktree_is_never_created_under_the_state_dir_or_tmp_by_the_kit(tmp_path):
    """The kit delegates placement to `openclaw worktrees`; it passes a name and a base ref, never a path."""
    r = Runner(tmp_path)
    worktree.create("r1", "/repo", "HEAD", r)
    create = next(c for c in r.calls if c[:3] == ["openclaw", "worktrees", "create"])
    assert "--path" not in create and not any(a.startswith("/tmp") for a in create)
