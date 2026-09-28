"""The per-agent worktree flow is taught, not built (v1.9.9).

The kit delegates creation, snapshot and cleanup to `openclaw worktrees create|list|remove|restore|gc`,
documents the measured managed root and states the never-under-/tmp rule with its reason. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from test_openclaw_host_wizard import _run_wizard, _state, script, sim  # noqa: F401
from ai_resources import openclaw_host as host
from ai_resources.setup.cockpits import _shared

REPO = pathlib.Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "using-git-worktrees" / "SKILL.md"
SUBCOMMANDS = ("create", "list", "remove", "restore", "gc")
# A path BENEATH /tmp is a location; the bare word `/tmp` in a prohibition is not.
TMP_PATH = re.compile(r"/tmp/[A-Za-z0-9_.*-]")


@pytest.mark.parametrize("delegates", [True, False])
def test_the_worktree_block_names_the_subcommands_the_skill_and_the_durable_root(delegates):
    text = _shared.openclaw_agent_kit_md("/kit", delegates=delegates)
    for sub in SUBCOMMANDS:
        assert f"openclaw worktrees {sub}" in text or f"`{sub} <id>`" in text or f"`{sub}`" in text, sub
    assert "openclaw worktrees create <repoRoot> --name <agent>-<task>" in text
    assert "using-git-worktrees" in text
    assert "~/.openclaw/worktrees/<repo-fingerprint>/<name>" in text


@pytest.mark.parametrize("delegates", [True, False])
def test_no_tmp_path_appears_in_the_worktree_block_and_the_rule_carries_its_reason(delegates):
    text = _shared.openclaw_agent_kit_md("/kit", delegates=delegates)
    assert not TMP_PATH.search(text)
    assert "Never" in text and "`/tmp`" in text and "tmpfs" in text and "reboot" in text and "T26" in text


def test_the_kit_reimplements_no_worktree_creation_snapshot_or_gc():
    """It delegates to `openclaw worktrees`: no `git worktree add` anywhere in the setup code."""
    text = _shared.openclaw_agent_kit_md("/kit")
    assert "does not create, snapshot or clean worktrees" in text
    assert "git worktree add" not in text
    scripts = REPO / "scripts" / "ai_resources"
    for py in scripts.rglob("*.py"):
        body = py.read_text(encoding="utf-8")
        assert "worktree add" not in body, py


def test_a_written_workspace_block_has_the_worktree_section_and_no_tmp_path(sim, script):
    from ai_resources.setup.cockpits import openclaw
    s = _state()
    openclaw.configure({"state": s})
    for name in ("app", "claude", "main"):
        text = (sim.workspaces[name] / "AGENTS.md").read_text(encoding="utf-8")
        assert "openclaw worktrees create" in text and not TMP_PATH.search(text)


def test_the_setup_does_not_pin_worktree_root(sim, script):
    s = _state()
    _run_wizard(s)
    sent = json.dumps([p for _, p in sim.oc.calls if p])
    assert "worktreeRoot" not in sent
    assert "worktreeRoot" not in json.loads(sim.cfg.read_text(encoding="utf-8")).get("agents", {}).get("defaults", {})


@pytest.mark.parametrize("kind", ["repo", "umbrella"])
def test_the_workspace_templates_teach_the_worktree_flow(kind):
    text = host.render_agents_md(kind, "billing", "Billing API")
    for needle in ("openclaw worktrees create", "~/.openclaw/worktrees/", "using-git-worktrees", "Never under `/tmp`",
                   "tmpfs", "reboot"):
        assert needle in text, needle
    assert not TMP_PATH.search(text)


def test_the_worktree_skill_states_the_rule_its_reason_and_keeps_the_git_fallback():
    text = SKILL.read_text(encoding="utf-8")
    assert "## OpenClaw-managed worktrees" in text
    for sub in SUBCOMMANDS:
        assert f"openclaw worktrees {sub}" in text
    assert "~/.openclaw/worktrees/<repoFingerprint>/<name>" in text and "openclaw/<name>" in text
    assert "Never put a worktree" in text and "`/tmp`" in text
    assert "tmpfs" in text and "reboot" in text and "/tmp/kitvenv" in text and "/tmp/wt-*" in text and "T26" in text
    assert "git worktree add <path> -b feature/<name> <base-branch>" in text, "the raw fallback stays"
    assert "outside OpenClaw" in text
