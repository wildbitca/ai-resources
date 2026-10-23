"""The sign-in answer reaches Claude Code's settings and nothing else does (ADR-0015 D1, acceptance 5)."""
from __future__ import annotations

import json

import pytest

from ai_resources.setup import cockpit_auth, credentials, profiles, state
from ai_resources.setup.cockpits import _shared, claude

GATEWAY_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
VERTEX_KEYS = ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")


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
    monkeypatch.setattr(credentials, "get_key", lambda key, *a, **k: {"GOOGLE_CLOUD_PROJECT": "demo-project",
                                                                      "GOOGLE_CLOUD_LOCATION": "us"}.get(key, ""))
    root.mkdir()
    return root


def _apply(s):
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    claude.configure({"state": s, "executors": ex, "master_key": "", "gateway_url": ""})


def _env(root):
    return json.loads((root / "settings.json").read_text()).get("env", {})


def _tree(root):
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _state(method):
    s = state.SetupState()
    s.profile.name = "balanced"
    cockpit_auth.set_method(s, "claude", method)
    cockpit_auth.apply_legacy_flags(s)
    return s


def test_a_subscription_writes_no_gateway_or_vertex_env(home):
    _apply(_state("subscription"))
    env = _env(home)
    assert not [k for k in GATEWAY_KEYS + VERTEX_KEYS if k in env]


def test_an_api_key_answer_writes_no_key_and_no_endpoint(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-NEVER-WRITTEN")
    _apply(_state("api_key"))
    text = (home / "settings.json").read_text()
    assert "NEVER-WRITTEN" not in text and not [k for k in GATEWAY_KEYS + VERTEX_KEYS if k in _env(home)]


def test_vertex_writes_the_project_and_the_region_only(home):
    _apply(_state("vertex_adc"))
    env = _env(home)
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "demo-project" and env["CLOUD_ML_REGION"] == "us"
    assert not [k for k in GATEWAY_KEYS if k in env]


def test_a_second_run_changes_nothing_for_every_method(home):
    for method in cockpit_auth.CLAUDE_METHODS:
        s = _state(method)
        _apply(s)
        before = _tree(home)
        _apply(s)
        assert _tree(home) == before, method


def test_the_patch_builder_ignores_the_saved_gateway_backend(home):
    patch = claude._build_settings_patch({}, "", "http://127.0.0.1:4000", "/kit", "single-model", "openrouter",
                                         auth="subscription")
    assert not [k for k in GATEWAY_KEYS if k in patch["env"]]
    vertex = claude._build_settings_patch({}, "", "", "/kit", "single-model", "litellm", auth="vertex_adc")
    assert vertex["env"]["CLAUDE_CODE_USE_VERTEX"] == "1"


def test_the_kit_state_holds_method_names_only(home):
    s = _state("vertex_adc")
    _apply(s)
    assert cockpit_auth.secret_findings(state._to_dict(s)["cockpit_auth"]) == []


# --- acceptance 5: Claude Code on a subscription and OpenClaw on an API key, in one run ------------------------------
def test_claude_on_a_subscription_and_openclaw_on_an_api_key_stay_independent(home, monkeypatch):
    from ai_resources.setup import ui
    from ai_resources.setup.cockpits import openclaw as oc
    from wizard_fakes import Script, fake_openclaw
    secret = "sk-ant-api03-COMBINED-CASE-SECRET-0123456789"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)                  # in the environment; the kit must never read it
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setattr(oc.shutil, "which", lambda name: "/fake/bin/openclaw" if name == "openclaw" else None)
    fake_openclaw(monkeypatch, authed=("anthropic",))               # OpenAI has no stored profile yet
    host_runs = []
    monkeypatch.setattr(oc.subprocess, "run", lambda argv, **k: host_runs.append((list(argv), k)) or
                        type("R", (), {"returncode": 0})())
    script = Script(**{"Which providers should OpenClaw use": ["openai"],
                       "How does OpenClaw sign in to openai": "api_key", "Save the openai API key now": True})
    script.install(monkeypatch)
    s = state.SetupState()
    s.profile.name = "balanced"
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.cockpits["openclaw"] = state.CockpitState(installed=True)

    cockpit_auth.set_method(s, "claude", "subscription")             # Claude Code's own answer
    assert oc.ask_auth(ui, s, None, False) == 0                      # OpenClaw's own question, in the same run
    cockpit_auth.apply_legacy_flags(s)
    _apply(s)                                                         # Claude Code applies its answer

    # Claude Code stays on its subscription: no API key, helper, endpoint or Vertex setting anywhere under ~/.claude
    settings = json.loads((home / "settings.json").read_text())
    assert "apiKeyHelper" not in settings and not [k for k in GATEWAY_KEYS + VERTEX_KEYS if k in settings.get("env", {})]
    for path in home.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore"), path
            assert "apiKeyHelper" not in path.read_text(errors="ignore"), path
    # OpenClaw got its own provider auth, through its own store: the stubbed `paste-api-key` on the operator's terminal
    assert [argv for argv, _k in host_runs] == [["/fake/bin/openclaw", "models", "auth", "paste-api-key", "--provider", "openai"]]
    assert "input" not in host_runs[0][1] and "capture_output" not in host_runs[0][1]       # the kit never sees the key
    # and the two answers stay separate in the state, names only
    doc = state._to_dict(s)["cockpit_auth"]
    assert doc["claude"] == {"method": "subscription"}
    assert doc["openclaw"]["providers"] == {"openai": "api_key"}
    assert cockpit_auth.secret_findings(doc) == []
    assert secret not in json.dumps(state._to_dict(s)) and secret not in script.transcript
