"""Per-cockpit auth in the setup state (ADR-0015 D1): additive field, fill-only migration, no secrets."""
from __future__ import annotations

import pathlib
import shutil

import pytest
import yaml

from ai_resources.setup import cockpit_auth, state

FIX = pathlib.Path(__file__).parent / "fixtures" / "setup_state"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / "xdg"))
    return h


def _load(name):
    state.config_root().mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / f"{name}.yaml", state.state_path())
    return state.load()


def test_an_empty_state_has_no_cockpit_auth_and_writes_no_key(home):
    s = state.SetupState()
    assert s.cockpit_auth == {}
    assert "cockpit_auth" not in state._to_dict(s)


def test_cockpit_auth_round_trips_and_the_schema_version_is_unchanged(home):
    s = state.SetupState()
    cockpit_auth.set_method(s, "claude", "subscription")
    cockpit_auth.set_providers(s, {"anthropic": "cli", "openai": "api_key"})
    state.save(s)
    back = state.load()
    assert back.cockpit_auth["claude"] == {"method": "subscription"}
    assert cockpit_auth.providers(back) == {"anthropic": "cli", "openai": "api_key"}
    assert back.schema_version == state.SCHEMA_VERSION == "1.0"


def test_an_old_state_file_without_the_field_loads_unchanged(home):
    s = _load("cli_team_balanced")
    assert s.cockpit_auth == {}
    state.save(s)
    assert "cockpit_auth" not in yaml.safe_load(state.state_path().read_text())


@pytest.mark.parametrize("name,expect", [
    ("cli_team_balanced", {"claude": "subscription"}),
    ("cli_team_customized", {"claude": "subscription"}),
    ("single_claude_native", {"claude": "subscription"}),
    ("multi_litellm", {"claude": "subscription"}),
    ("multi_openrouter", {"claude": "subscription"}),
])
def test_legacy_answers_map_to_native_auth_with_an_old_to_new_table(home, name, expect):
    s = _load(name)
    rows = cockpit_auth.migrate_legacy(s)
    assert {r.cockpit: r.new for r in rows} == expect
    assert all(r.old for r in rows)
    assert {k: v["method"] for k, v in s.cockpit_auth.items()} == expect


def test_a_gateway_state_never_keeps_the_gateway_as_an_auth_method(home):
    for name in ("multi_litellm", "multi_openrouter"):
        s = _load(name)
        cockpit_auth.migrate_legacy(s)
        assert "gateway" not in str(s.cockpit_auth)
        assert s.backend in ("litellm", "openrouter")          # nothing is torn down: the saved gateway is left as it is


def test_api_key_team_maps_to_api_key_or_vertex_and_openclaw_providers(home):
    s = state.SetupState(cli_team=True, cli_team_api=True)
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.profile.customizations["__targets__"] = {"selected": ["claude", "openclaw", "agy"]}
    rows = cockpit_auth.migrate_legacy(s)
    assert {r.cockpit: r.new for r in rows} == {"claude": "api_key", "openclaw": "api_key", "agy": "google_account"}
    s2 = state.SetupState(cli_team=True, cli_team_api=True)
    s2.cockpits["claude"] = state.CockpitState(installed=True)
    assert [r.new for r in cockpit_auth.migrate_legacy(s2, vertex=True)] == ["vertex_adc"]


def test_second_migration_is_empty_and_the_table_is_shown_once(home):
    s = _load("cli_team_balanced")
    assert cockpit_auth.migrate_legacy(s)
    state.save(s)
    again = state.load()
    assert cockpit_auth.migrate_legacy(again) == []


def test_migration_is_fill_only(home):
    s = _load("cli_team_balanced")
    cockpit_auth.set_method(s, "claude", "vertex_adc")
    assert cockpit_auth.migrate_legacy(s) == []
    assert cockpit_auth.method(s, "claude") == "vertex_adc"


def test_a_fresh_state_migrates_nothing(home):
    s = state.SetupState()
    assert cockpit_auth.migrate_legacy(s) == []
    assert s.cockpit_auth == {}


def test_legacy_flags_are_derived_from_the_answers(home):
    s = state.SetupState(mode="multi-model", backend="openrouter")
    cockpit_auth.set_method(s, "claude", "subscription")
    cockpit_auth.apply_legacy_flags(s)
    assert (s.mode, s.cli_team, s.cli_team_api) == ("single-model", True, False)
    cockpit_auth.set_method(s, "claude", "api_key")
    cockpit_auth.apply_legacy_flags(s)
    assert s.cli_team_api is True
    assert s.backend == "openrouter"                           # left as saved


def test_invalid_methods_are_rejected():
    s = state.SetupState()
    with pytest.raises(ValueError):
        cockpit_auth.set_method(s, "claude", "gateway")
    with pytest.raises(ValueError):
        cockpit_auth.set_providers(s, {"openai": "oauth-token"})


def test_the_serialized_state_holds_no_secret_shaped_key_or_value(home):
    s = state.SetupState()
    cockpit_auth.set_method(s, "claude", "api_key")
    cockpit_auth.set_providers(s, {"openai": "api_key", "anthropic": "cli"})
    state.save(s)
    doc = yaml.safe_load(state.state_path().read_text())
    assert cockpit_auth.secret_findings(doc["cockpit_auth"]) == []
    assert cockpit_auth.secret_findings({"claude": {"api_key": "x"}}) == ["cockpit_auth.claude.api_key"]
    assert cockpit_auth.secret_findings({"claude": {"method": "sk-ant-api03-AAAAAAAAAAAA"}})


def test_default_profile_is_the_one_team_preset():
    assert cockpit_auth.default_profile_for(state.SetupState()) == "balanced"
