"""Per-provider sign-in for OpenClaw (ADR-0015 D2, acceptance 2 and 9): OpenClaw's own store, names only in kit state."""
from __future__ import annotations

import json

import pytest
import yaml

from ai_resources.setup import cockpit_auth, state
from ai_resources.setup.cockpits import openclaw as oc
from wizard_fakes import Script, fake_openclaw

SENTINEL = "sk-test-SENTINEL-KEY-0123456789abcdef"


@pytest.fixture
def host(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    runs: list[list[str]] = []

    def run(argv, **k):
        runs.append(list(argv))
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(oc.shutil, "which", lambda name: "/fake/bin/openclaw" if name == "openclaw" else None)
    log = fake_openclaw(monkeypatch)
    monkeypatch.setattr(oc.subprocess, "run", run)
    return type("H", (), {"runs": runs, "calls": log, "home": home})


def _s(*installed):
    s = state.SetupState()
    for cid in installed:
        s.cockpits[cid] = state.CockpitState(installed=True)
    return s


def _script(monkeypatch, **answers):
    script = Script(**answers)
    script.install(monkeypatch)
    return script


def test_anthropic_cli_and_openai_api_key_use_openclaws_own_auth_command(host, monkeypatch):
    s = _s("claude")
    script = _script(monkeypatch, **{"Which providers should OpenClaw use": ["anthropic", "openai"],
                                      "How does OpenClaw sign in to anthropic": "cli",
                                      "How does OpenClaw sign in to openai": "api_key",
                                      "Save the openai API key now": True})
    # openai is already signed in in the fixture, so nothing is asked of the operator
    assert oc.ask_auth(__import__("ai_resources.setup.ui", fromlist=["ui"]), s, None, False) == 0
    assert cockpit_auth.providers(s) == {"anthropic": "cli", "openai": "api_key"}
    assert host.runs == []                                       # already signed in: no key command is run
    assert any("openai: already signed in" in m for m in script.shown)


def test_a_provider_without_a_saved_sign_in_runs_paste_api_key_on_the_operators_terminal(host, monkeypatch):
    from ai_resources.setup import ui
    fake_openclaw(monkeypatch, authed=("anthropic",))
    host_runs = []
    monkeypatch.setattr(oc.subprocess, "run", lambda argv, **k: host_runs.append((list(argv), k)) or
                        type("R", (), {"returncode": 0})())
    s = _s("claude")
    script = _script(monkeypatch, **{"Which providers should OpenClaw use": ["anthropic", "openai"],
                                      "How does OpenClaw sign in to anthropic": "cli",
                                      "How does OpenClaw sign in to openai": "api_key",
                                      "Save the openai API key now": True})
    assert oc.ask_auth(ui, s, None, False) == 0
    assert host_runs == [(["/fake/bin/openclaw", "models", "auth", "paste-api-key", "--provider", "openai"],
                          {"cwd": host_runs[0][1]["cwd"]})]
    assert "capture_output" not in host_runs[0][1] and "input" not in host_runs[0][1]       # stdio is the operator's
    assert any("saved a openai API key in OpenClaw's store" in a for a in ui.actions_done())
    assert cockpit_auth.providers(s)["openai"] == "api_key"
    assert SENTINEL not in script.transcript


def test_a_declined_key_save_runs_nothing_and_prints_the_command(host, monkeypatch):
    from ai_resources.setup import ui
    fake_openclaw(monkeypatch, authed=())
    s = _s()
    script = _script(monkeypatch, **{"Which providers should OpenClaw use": ["openai"],
                                      "Save the openai API key now": False})
    assert oc.ask_auth(ui, s, None, False) == 0
    assert host.runs == []
    assert any("openclaw models auth paste-api-key --provider openai" in m for m in script.shown)


def test_a_dry_run_and_a_non_interactive_run_run_no_command(host, monkeypatch):
    from ai_resources.setup import ui
    fake_openclaw(monkeypatch, authed=())
    s = _s()
    _script(monkeypatch, **{"Which providers should OpenClaw use": ["openai"]})
    assert oc.ask_auth(ui, s, None, True) == 0 and host.runs == []
    quiet = _s()
    cockpit_auth.set_providers(quiet, {"anthropic": "cli"})
    Script().install(monkeypatch, non_interactive=True)
    log = fake_openclaw(monkeypatch)
    assert oc.ask_auth(ui, quiet, None, False) == 0
    assert log == [] and cockpit_auth.providers(quiet) == {"anthropic": "cli"}      # saved answers kept, no command


def test_the_engine_provider_is_always_enabled(host, monkeypatch):
    from ai_resources.setup import ui
    s = _s("agy")
    s.openclaw.engine = "antigravity"
    script = _script(monkeypatch, **{"Which providers should OpenClaw use": ["anthropic"],
                                      "How does OpenClaw sign in to anthropic": "api_key"})
    oc.ask_auth(ui, s, None, True)
    assert cockpit_auth.providers(s)["google"] == "cli"
    assert any("google was added" in m for m in script.shown)


def test_cancelling_the_multiselect_is_a_cancel(host, monkeypatch):
    from ai_resources.setup import ui
    _script(monkeypatch, **{"Which providers should OpenClaw use": lambda v: None})
    assert oc.ask_auth(ui, _s(), None, True) == 130


def test_a_missing_catalog_falls_back_and_says_so(host, monkeypatch, tmp_path):
    from ai_resources.setup import ui
    from ai_resources.setup.cockpits import _openclaw_providers as P
    monkeypatch.setattr(oc, "_openclaw", lambda argv, stdin=None, timeout=120: (127, "openclaw not found on PATH"))
    monkeypatch.setattr(P, "docs_providers_dir", lambda binary=None: None)
    script = _script(monkeypatch, **{"Which providers should OpenClaw use": ["anthropic"],
                                      "How does OpenClaw sign in to anthropic": "api_key",
                                      "Save the anthropic API key now": False})
    assert oc.ask_auth(ui, _s(), None, True) == 0
    assert any("could not be read" in m and "providers the kit knows" in m for m in script.shown)


def test_the_state_holds_names_only_and_no_secret_ever_reaches_a_file_or_a_log(host, monkeypatch):
    from ai_resources.setup import ui
    s = _s("claude")
    script = _script(monkeypatch, **{"Which providers should OpenClaw use": ["anthropic", "openai"],
                                      "How does OpenClaw sign in to anthropic": "cli",
                                      "How does OpenClaw sign in to openai": "api_key"})
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)                 # present in the environment, never read by the kit
    oc.ask_auth(ui, s, None, True)
    state.save(s)
    text = state.state_path().read_text()
    assert SENTINEL not in text and SENTINEL not in script.transcript
    doc = yaml.safe_load(text)
    assert cockpit_auth.secret_findings(doc["cockpit_auth"]) == []
    assert doc["cockpit_auth"]["openclaw"]["providers"] == {"anthropic": "cli", "openai": "api_key"}
    for call in host.calls:                                         # only read-only catalog commands were issued
        assert call[:2] == ["models", "list"] or call[:3] == ["models", "auth", "list"]
