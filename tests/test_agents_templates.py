"""The three AGENTS.md templates and `ai-resources openclaw agent-new`.

Detection runs against real throwaway git repositories; registering an agent goes through a
fake runner, so nothing reaches a gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

TEMPLATES = {k: REPO / "templates" / v for k, v in host.AGENTS_TEMPLATES.items()}


def _git(cwd: pathlib.Path, *args: str):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.org",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def _repo(path: pathlib.Path, *, commits: bool, remote: bool) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    if commits:
        (path / "f.txt").write_text("x", encoding="utf-8")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-q", "-m", "init")
    if remote:
        _git(path, "remote", "add", "origin", "git@example.org:o/r.git")
    return path


# --- AC-9.1: content --------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_every_template_carries_the_shared_rules(kind):
    text = TEMPLATES[kind].read_text(encoding="utf-8")
    assert "--setting-sources user" in text and "CLAUDE.md" in text and "(T02)" in text
    assert "T11" in text and "commit" in text.lower()
    assert ".agent-output/" in text and "Durable documentation" in text
    assert "English" in text and "attribution" in text and "--context" in text
    for role in ("planner", "software-architect", "implementer", "tester", "verifier", "doc-writer"):
        assert role in text
    assert "Engram" in text


def test_only_the_orchestrator_names_the_topic_routing_file():
    for kind, path in TEMPLATES.items():
        assert ("TOPIC-ROUTING.md" in path.read_text(encoding="utf-8")) is (kind == "orchestrator")


def test_the_umbrella_and_repo_templates_warn_about_commits_differently():
    umb = TEMPLATES["umbrella"].read_text(encoding="utf-8")
    repo = TEMPLATES["repo"].read_text(encoding="utf-8")
    assert "Never commit at the umbrella level" in umb and "Never commit at the umbrella level" not in repo
    assert "Where commits go" in repo


def test_the_three_files_are_three_different_files():
    """The failure being replaced was three repos sharing one identical AGENTS.md."""
    sums = {hashlib.sha256(p.read_bytes()).hexdigest() for p in TEMPLATES.values()}
    assert len(sums) == 3


@pytest.mark.parametrize("kind", sorted(TEMPLATES))
def test_templates_are_english_with_no_host_literal(kind):
    text = TEMPLATES[kind].read_text(encoding="utf-8")
    assert not [c for c in text if c.isalpha() and ord(c) > 127]
    for literal in ("wildbit", "bithome", "/home/", "7961376547"):
        assert literal not in text


def test_rendering_fills_the_markers_and_leaves_none_behind():
    out = host.render_agents_md("repo", "billing", "Billing API")
    assert "# AGENTS.md — Billing API" in out and "`billing` agent" in out
    assert "@AGENT" not in out


# --- AC-9.2: detection ------------------------------------------------------------------------------------

def test_a_directory_that_is_not_a_repository_is_an_umbrella(tmp_path):
    assert host.detect_workspace_kind(tmp_path) == "umbrella"


def test_a_repository_with_no_tracked_files_and_no_remote_is_an_umbrella(tmp_path):
    assert host.detect_workspace_kind(_repo(tmp_path / "u", commits=False, remote=False)) == "umbrella"


def test_tracked_files_without_a_remote_are_still_an_umbrella(tmp_path):
    assert host.detect_workspace_kind(_repo(tmp_path / "u", commits=True, remote=False)) == "umbrella"


def test_tracked_files_and_a_remote_make_a_repo(tmp_path):
    assert host.detect_workspace_kind(_repo(tmp_path / "r", commits=True, remote=True)) == "repo"


# --- AC-9.3: idempotence and no overwrite -------------------------------------------------------------------------

class _Runner:
    def __init__(self, existing: str = ""):
        self.calls: list[list[str]] = []
        self.existing = existing

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[0] == "git":
            r = subprocess.run(argv, capture_output=True, text=True)
            return r.returncode, (r.stdout + r.stderr).strip()
        if argv[1:3] == ["agents", "list"]:
            return 0, f'[{{"id":"{self.existing}"}}]' if self.existing else "[]"
        return 0, "ok"


def test_agent_new_writes_the_template_matching_the_workspace(tmp_path):
    ws = _repo(tmp_path / "r", commits=True, remote=True)
    res = host.agent_new("billing", ws, runner=_Runner(), openclaw_bin="openclaw")
    assert res["kind"] == "repo" and res["agents_md"] == "written"
    assert "Where commits go" in (ws / "AGENTS.md").read_text(encoding="utf-8")


def test_the_exclude_file_gains_the_openclaw_files_exactly_once(tmp_path):
    ws = _repo(tmp_path / "r", commits=False, remote=False)
    host.agent_new("a", ws, runner=_Runner(), openclaw_bin="openclaw")
    first = (ws / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    res = host.agent_new("a", ws, runner=_Runner(existing="a"), openclaw_bin="openclaw")
    assert res["excluded"] is False
    assert (ws / ".git" / "info" / "exclude").read_text(encoding="utf-8") == first
    lines = first.splitlines()
    assert lines.count("AGENTS.md") == 1 and lines.count("TOPIC-ROUTING.md") == 1


def test_an_existing_agents_md_is_never_overwritten_without_force(tmp_path):
    ws = _repo(tmp_path / "r", commits=False, remote=False)
    (ws / "AGENTS.md").write_text("mine\n", encoding="utf-8")
    assert host.agent_new("a", ws, runner=_Runner(), openclaw_bin="oc")["agents_md"] == "kept"
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") == "mine\n"
    assert host.agent_new("a", ws, force=True, runner=_Runner(), openclaw_bin="oc")["agents_md"] == "overwritten"
    assert (ws / "AGENTS.md").read_text(encoding="utf-8") != "mine\n"


def test_the_agent_is_registered_through_openclaw_agents_add_never_a_file_write(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    runner = _Runner()
    res = host.agent_new("billing", ws, model="anthropic/claude-sonnet-5", runner=runner, openclaw_bin="oc")
    add = [c for c in runner.calls if c[:3] == ["oc", "agents", "add"]]
    assert res["registered"] == "added" and len(add) == 1
    assert add[0][3:] == ["billing", "--workspace", str(ws.resolve()), "--non-interactive",
                          "--model", "anthropic/claude-sonnet-5"]
    assert not any("config" in c for c in runner.calls)


def test_an_agent_that_already_exists_is_reused(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    runner = _Runner(existing="billing")
    assert host.agent_new("billing", ws, runner=runner, openclaw_bin="oc")["registered"] == "existing"
    assert not any(c[:3] == ["oc", "agents", "add"] for c in runner.calls)


def test_the_topic_reminder_is_printed_and_never_touches_channels(tmp_path):
    ws = tmp_path / "w"
    ws.mkdir()
    res = host.agent_new("a", ws, register=False, runner=_Runner(), openclaw_bin="oc")
    assert "/new" in res["reminder"] and "T19" in res["reminder"]


@pytest.mark.parametrize("bad", ["", "Billing", "a b", "1x", "a/b", "x" * 40])
def test_a_bad_agent_id_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        host.agent_new(bad, tmp_path, register=False, runner=_Runner())


def test_a_missing_workspace_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError):
        host.agent_new("a", tmp_path / "nope", register=False, runner=_Runner())
