"""FU-1 / G4: a non-Anthropic id the matrix has not verified for Claude Code is gated per model (not written
unless --allow-unverified), for measured-best and for any profile that carries foreign ids."""
from __future__ import annotations

import re

import pytest

from ai_resources import repo_root
from ai_resources.setup import profiles, state
from ai_resources.setup.cockpits import _shared, claude


def _models(executors, **kw):
    files = claude.render_subagent_files(executors, str(repo_root()), "multi-model", **kw)
    return {n: re.search(r"^model: (.*)$", t, re.M).group(1) for n, t in files.items()}


@pytest.fixture
def measured_best():
    return profiles.to_executors(profiles.load_profile("measured-best"), "litellm")


def test_foreign_ids_are_not_written_without_the_opt_in(measured_best):
    m = _models(measured_best, allow_unverified=False)
    assert m["explore"] == "inherit"
    assert m["software-architect"] == "inherit"
    assert not any(v.startswith(("google/", "deepseek/")) for v in m.values())


def test_the_anthropic_ids_survive_the_gate(measured_best):
    assert _models(measured_best, allow_unverified=False)["planner"] == "anthropic/claude-opus-5.5"


def test_the_opt_in_keeps_the_foreign_ids(measured_best):
    m = _models(measured_best, allow_unverified=True)
    assert m["explore"].startswith("google/") and m["software-architect"].startswith("deepseek/")


def test_an_all_claude_profile_renders_the_same_with_and_without_the_gate():
    ex = profiles.to_executors(profiles.load_profile("balanced-claude-55"), "litellm")
    assert _models(ex, allow_unverified=False) == _models(ex)


def test_the_gate_drops_a_google_id_on_openrouter_too(measured_best):
    m = _models(measured_best, allow_unverified=False, backend="openrouter")
    assert m["explore"] == "inherit"


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    monkeypatch.setattr(claude, "CONFIG_ROOT", root)
    monkeypatch.setattr(claude, "SETTINGS_PATH", root / "settings.json")
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", root / "CLAUDE.md")
    monkeypatch.setattr(claude, "AGENTS_DIR", root / "agents")
    monkeypatch.setattr(claude, "WORKFLOWS_DIR", root / "workflows")
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    return root


def _written_models(root):
    return {p.stem: re.search(r"^model: (.*)$", p.read_text(), re.M).group(1) for p in (root / "agents").glob("*.md")}


def test_configure_gates_a_profile_chosen_without_a_selection(claude_home, measured_best):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    claude.configure({"state": s, "executors": measured_best, "master_key": "k", "gateway_url": "http://127.0.0.1:4000"})
    assert _written_models(claude_home)["explore"] == "inherit"


def test_configure_with_the_opt_in_writes_the_foreign_ids(claude_home, measured_best):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    claude.configure({"state": s, "executors": measured_best, "master_key": "k",
                      "gateway_url": "http://127.0.0.1:4000", "allow_unverified": True})
    assert _written_models(claude_home)["explore"].startswith("google/")
