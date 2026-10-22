"""Claude Code lean-cost env: auto-compact window (fill-only) and the one-time AGENT_TEAMS removal."""
from __future__ import annotations

import json

import pytest

from ai_resources.setup import profiles, state
from ai_resources.setup.cockpits import _shared, claude

WINDOW = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
TEAMS = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    monkeypatch.setattr(claude, "CONFIG_ROOT", root)
    monkeypatch.setattr(claude, "SETTINGS_PATH", root / "settings.json")
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", root / "CLAUDE.md")
    monkeypatch.setattr(claude, "AGENTS_DIR", root / "agents")
    monkeypatch.setattr(claude, "WORKFLOWS_DIR", root / "workflows")
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_trim_engram_hooks", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    root.mkdir()
    return root


def _setup(s=None):
    s = s or state.SetupState()
    ex = profiles.to_executors(profiles.load_profile("claude-native"))
    claude.configure({"state": s, "executors": ex, "master_key": "", "gateway_url": ""})
    return s


def _env(root):
    return json.loads((root / "settings.json").read_text()).get("env", {})


def test_a_fresh_setup_sets_the_window_and_never_writes_agent_teams(home):
    _setup()
    env = _env(home)
    assert env[WINDOW] == "200000"
    assert TEAMS not in env


def test_an_upgrade_removes_the_kit_written_agent_teams_once(home):
    (home / "settings.json").write_text(json.dumps({"env": {TEAMS: "1", "KEEP": "me"}}))
    s = _setup()
    env = _env(home)
    assert TEAMS not in env and env["KEEP"] == "me" and env[WINDOW] == "200000"
    assert claude.AGENT_TEAMS_MIGRATION_ID in s.tracking.cost_migrations_applied


def test_a_different_agent_teams_value_is_the_operators_and_stays(home):
    (home / "settings.json").write_text(json.dumps({"env": {TEAMS: "0"}}))
    _setup()
    assert _env(home)[TEAMS] == "0"


def test_an_agent_teams_value_set_again_after_the_migration_is_not_removed(home):
    s = _setup()
    settings = json.loads((home / "settings.json").read_text())
    settings["env"][TEAMS] = "1"
    (home / "settings.json").write_text(json.dumps(settings))
    _setup(s)
    assert _env(home)[TEAMS] == "1"


def test_an_operator_window_is_kept(home):
    (home / "settings.json").write_text(json.dumps({"env": {WINDOW: "350000"}}))
    _setup()
    assert _env(home)[WINDOW] == "350000"


def test_two_runs_leave_the_same_file(home):
    (home / "settings.json").write_text(json.dumps({"env": {TEAMS: "1"}}))
    s = _setup()
    first = (home / "settings.json").read_text()
    _setup(s)
    assert (home / "settings.json").read_text() == first
    assert s.tracking.cost_migrations_applied.count(claude.AGENT_TEAMS_MIGRATION_ID) == 1


def test_the_state_file_round_trips_the_migration_ids():
    s = state.SetupState()
    s.tracking.cost_migrations_applied.append("x")
    back = state._from_dict(state.SetupState, state._to_dict(s))
    assert back.tracking.cost_migrations_applied == ["x"]
