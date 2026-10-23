"""Effort reaches the Claude Code subagent files (ADR-0015 S3, acceptance 6); operator edits are kept."""
from __future__ import annotations

import re

import pytest

from ai_resources.setup import effort, profiles, state
from ai_resources.setup.cockpits import _shared, claude


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    for attr, sub in (("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"), ("AGENTS_DIR", "agents"),
                      ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, root / sub)
    monkeypatch.setattr(claude, "CONFIG_ROOT", root)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_trim_engram_hooks", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    root.mkdir()
    return root


def _setup(s, profile="balanced"):
    s.profile.name = profile
    ex = profiles.to_executors(profiles.load_profile(profile, "single-model"))
    claude.configure({"state": s, "executors": ex, "master_key": "", "gateway_url": ""})
    return s


def _effort(home, name):
    text = (home / "agents" / f"{name}.md").read_text()
    m = re.search(r"^effort: (\S+)$", text.split("\n---", 2)[0], re.M)
    return m.group(1) if m else None


def test_balanced_planner_and_architect_carry_high_and_explore_low(home):
    _setup(state.SetupState())
    assert (_effort(home, "planner"), _effort(home, "software-architect"), _effort(home, "explore")) == (
        "high", "high", "low")
    assert _effort(home, "implementer") == "medium" and _effort(home, "verifier") == "high"


def test_a_rerun_changes_nothing_and_records_what_it_wrote(home):
    s = _setup(state.SetupState())
    before = {p.name: p.read_text() for p in (home / "agents").glob("*.md")}
    _setup(s)
    assert {p.name: p.read_text() for p in (home / "agents").glob("*.md")} == before
    assert s.tracking.subagent_effort["planner"] == "high"


def test_an_operator_edit_is_kept_and_listed(home, monkeypatch):
    s = _setup(state.SetupState())
    path = home / "agents" / "planner.md"
    path.write_text(path.read_text().replace("effort: high", "effort: max"))
    said = []
    monkeypatch.setattr(claude.ui, "info", lambda msg, *a, **k: said.append(msg))
    _setup(s)
    assert any("kept your own `effort:`" in m and "planner" in m for m in said)
    assert _effort(home, "planner") == "max"
    assert s.tracking.subagent_effort["planner"] == "high"          # still the kit's own value: the edit stays an edit


def test_a_profile_change_moves_the_kit_value_but_not_an_edit(home):
    s = _setup(state.SetupState())
    path = home / "agents" / "planner.md"
    path.write_text(path.read_text().replace("effort: high", "effort: max"))
    _setup(s, "performance")
    assert _effort(home, "planner") == "max"                         # edited: kept
    assert _effort(home, "tester") == "medium"                       # untouched: follows performance


def test_without_the_capability_flag_nothing_is_emitted(home, monkeypatch):
    monkeypatch.setattr(effort, "CLAUDE_EFFORT_FRONTMATTER", False)
    _setup(state.SetupState())
    assert _effort(home, "planner") is None


def test_a_profile_without_a_table_emits_nothing(home):
    s = _setup(state.SetupState(), "claude-native")
    assert _effort(home, "planner") is None and s.tracking.subagent_effort == {}


def test_the_profile_still_has_no_effort_key():
    for name in ("balanced", "performance"):
        doc = profiles.load_profile(name, "single-model")
        assert "effort" not in doc and all("effort" not in v for v in doc["by_role"].values())
