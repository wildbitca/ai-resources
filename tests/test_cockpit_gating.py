"""The apply gate: a cockpit gets the model settings its matrix row allows, and nothing it cannot use."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_resources import selection as sel
from ai_resources.setup import compat, model_selection as ms, state
from ai_resources.setup.cockpits import _shared, aider, claude


def _flash_selection(**kw):
    return sel.Selection(shape="single", providers={"google": sel.ProviderSel()},
                         slots={"google:gemini-flash": sel.SlotSel("google/gemini-3.8-flash")},
                         primary="google:gemini-flash", smoke_path="litellm", **kw)


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


def _ctx(s, executors=None, **extra):
    return {"state": s, "executors": executors or {"by_role": {}, "classes": {}}, "master_key": "k",
            "gateway_url": "http://127.0.0.1:4000", **extra}


def _route(cockpit, mode, backend, selection, detected, **kw):
    return next(a for a in ms.plan_table(selection, mode, backend, detected, **kw) if a.cockpit == cockpit)


def test_a_non_claude_single_model_never_writes_an_endpoint_or_a_non_claude_model(claude_home):
    s = state.SetupState()
    s.mode = "single-model"
    s.set_selection(_flash_selection())
    route = _route("claude", "single-model", None, s.get_selection(), ["claude"])
    assert route.action == compat.SKIP
    claude.configure(_ctx(s, route=route))
    settings = json.loads((claude_home / "settings.json").read_text())
    assert "ANTHROPIC_BASE_URL" not in settings["env"] and "modelOverrides" not in settings
    models = {line.split(":", 1)[1].strip() for p in (claude_home / "agents").glob("*.md")
              for line in p.read_text().splitlines() if line.startswith("model:")}
    assert models and all(m in claude.NATIVE_MODELS or m.startswith("claude-") for m in models)
    assert "gemini" not in json.dumps(settings) and "Model calls go through" not in (claude_home / "CLAUDE.md").read_text()


def test_a_skipped_gateway_route_is_written_as_single_model(claude_home):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    s.set_selection(_flash_selection())
    route = _route("claude", "multi-model", "litellm", s.get_selection(), ["claude"])
    assert route.action == compat.SKIP and "not verified yet" in route.reason
    claude.configure(_ctx(s, route=route))
    settings = json.loads((claude_home / "settings.json").read_text())
    assert "ANTHROPIC_BASE_URL" not in settings["env"]


def test_with_the_opt_in_the_gateway_route_is_written_and_labelled(claude_home):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    s.set_selection(_flash_selection(allow_unverified=True))
    route = _route("claude", "multi-model", "litellm", s.get_selection(), ["claude"], allow_unverified=True)
    assert route.action == compat.VIA_GATEWAY and route.unverified
    assert "not verified end to end" in ms.describe(route)
    claude.configure(_ctx(s, {"by_role": {"implementer": {"model": "google/gemini-3.8-flash"}}, "classes": {}}, route=route))
    assert json.loads((claude_home / "settings.json").read_text())["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


def test_resolve_model_raises_a_typed_skip_for_a_non_native_id_when_a_selection_exists():
    with pytest.raises(_shared.SkipCockpit):
        claude._resolve_model("google/gemini-3.8-flash", {}, "single-model", strict=True)
    assert claude._resolve_model("sonnet", {}, "single-model", strict=True) == "sonnet"


def test_without_a_selection_the_legacy_fallback_is_unchanged():
    assert claude._resolve_model("google/gemini-3.8-flash", {"model": "strong"}, "single-model") == "opus"
    assert claude._resolve_model("google/gemini-3.8-flash", {"model": "fast"}, "single-model") == "haiku"
    assert claude._resolve_model("", {}, "single-model") == "inherit"


def test_no_route_in_the_context_means_the_unchanged_behaviour(claude_home):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    claude.configure(_ctx(s))
    assert json.loads((claude_home / "settings.json").read_text())["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:4000"


# --- the written files equal the summary's list --------------------------------------------------

I_COCKPITS = ["gemini", "cursor", "codex", "copilot", "windsurf", "continue", "opencode", "aider"]


@pytest.fixture
def captured(monkeypatch):
    writes: list[str] = []
    monkeypatch.setattr(_shared, "write_text", lambda p, c, **k: writes.append(str(p)) or True)
    monkeypatch.setattr(_shared, "write_managed_block", lambda p, b, **k: writes.append(str(p)) or True)
    monkeypatch.setattr(_shared, "deep_merge_json", lambda p, patch, **k: writes.append(str(p)) or True)
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    return writes


@pytest.mark.parametrize("cid", I_COCKPITS)
def test_the_files_a_cockpit_writes_are_the_files_its_summary_listed(cid, captured):
    from ai_resources.setup.cockpits import ALL
    s = state.SetupState()
    s.set_selection(_flash_selection())
    route = _route(cid, "single-model", None, s.get_selection(), [cid])
    ALL[cid].configure(_ctx(s, route=route))
    assert captured, cid
    assert ms.covers(captured, route.files), (cid, captured, route.files)
    assert all(any(f.split(" (")[0] in w or w.endswith(f.split(" (")[0][1:]) for w in captured) for f in route.files), \
        (cid, "a listed file was not written", captured, route.files)


# --- the wizard's apply loop ------------------------------------------------------------------------

def test_apply_passes_each_cockpit_its_route_and_survives_a_typed_skip(monkeypatch, tmp_path):
    from ai_resources.setup import ui, wizard
    seen = {}

    def fake(cid, raises=False):
        def configure(ctx):
            seen[cid] = ctx.get("route")
            if raises:
                raise _shared.SkipCockpit("not a Claude model")
            return []
        return SimpleNamespace(NAME=cid, configure=configure)

    monkeypatch.setattr(wizard, "ALL_COCKPITS", {"claude": fake("claude", raises=True), "gemini": fake("gemini"),
                                                  "cursor": fake("cursor")})
    monkeypatch.setattr(state, "save", lambda s: None)
    monkeypatch.setattr(state, "executors_path", lambda: tmp_path / "executors.yaml")
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    monkeypatch.setattr(ui, "info", lambda m: None)
    monkeypatch.setattr(ui, "ok", lambda m: None)
    monkeypatch.setattr(ui, "detail", lambda m: None)
    monkeypatch.setattr(ui, "section", lambda *a: None)
    monkeypatch.setattr(ui, "console", lambda: SimpleNamespace(print=lambda *a, **k: None))
    s = state.SetupState()
    s.mode = "single-model"
    for cid in ("claude", "gemini", "cursor"):
        s.cockpits[cid] = state.CockpitState(installed=True)
    s.set_selection(_flash_selection())
    assert wizard._step9_apply(s) == 0
    assert seen["claude"].action == compat.SKIP and seen["gemini"].action == compat.CONFIGURE
    assert seen["cursor"].action == compat.INSTRUCTIONS_ONLY
    assert any("claude: skipped: not a Claude model" in w for w in warned)


def test_apply_without_a_selection_passes_no_route(monkeypatch, tmp_path):
    from ai_resources.setup import ui, wizard
    seen = {}
    monkeypatch.setattr(wizard, "ALL_COCKPITS", {"claude": SimpleNamespace(NAME="c", configure=lambda ctx: seen.update(ctx) or [])})
    monkeypatch.setattr(state, "save", lambda s: None)
    monkeypatch.setattr(state, "executors_path", lambda: tmp_path / "executors.yaml")
    for name in ("warn", "info", "ok", "detail"):
        monkeypatch.setattr(ui, name, lambda m: None)
    monkeypatch.setattr(ui, "section", lambda *a: None)
    monkeypatch.setattr(ui, "console", lambda: SimpleNamespace(print=lambda *a, **k: None))
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=True)
    assert wizard._step9_apply(s) == 0
    assert "route" not in seen and "openai_base" not in seen


# --- a settings.json that carries the gateway token is never group/other readable ------------------------

def _mode(p):
    import os
    import stat
    return stat.S_IMODE(os.stat(p).st_mode)


def test_a_new_settings_json_with_the_openrouter_key_is_created_0600(claude_home):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    claude.configure(_ctx(s))
    path = claude_home / "settings.json"
    assert json.loads(path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "k"
    assert _mode(path) == 0o600


def test_litellm_never_puts_the_gateway_key_in_settings_json(claude_home):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    claude.configure(_ctx(s))
    assert "ANTHROPIC_AUTH_TOKEN" not in json.loads((claude_home / "settings.json").read_text())["env"]


@pytest.mark.parametrize("old_mode", [0o644, 0o666])
def test_an_existing_wider_settings_json_is_tightened_and_keeps_the_users_content(claude_home, old_mode):
    claude_home.mkdir(parents=True)
    path = claude_home / "settings.json"
    path.write_text(json.dumps({"theme": "dark", "env": {"MY_VAR": "1"}}))
    path.chmod(old_mode)
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    claude.configure(_ctx(s))
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["env"]["MY_VAR"] == "1"
    assert data["env"]["ANTHROPIC_AUTH_TOKEN"] == "k"
    assert _mode(path) == 0o600


def test_a_second_run_that_changes_nothing_still_tightens_a_widened_settings_json(claude_home):
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    claude.configure(_ctx(s))
    path = claude_home / "settings.json"
    path.chmod(0o644)
    claude.configure(_ctx(s))
    assert _mode(path) == 0o600


def test_deep_merge_json_without_a_mode_is_unchanged(tmp_path):
    path = tmp_path / "x.json"
    assert _shared.deep_merge_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    path.chmod(0o644)
    _shared.deep_merge_json(path, {"b": 2})
    assert _mode(path) == 0o644


def test_deep_merge_json_with_a_mode_creates_and_tightens(tmp_path):
    path = tmp_path / "y.json"
    assert _shared.deep_merge_json(path, {"a": 1}, mode=0o600)
    assert _mode(path) == 0o600
    path.chmod(0o644)
    assert _shared.deep_merge_json(path, {"a": 1}, mode=0o600) is False   # nothing changed, still tightened
    assert _mode(path) == 0o600
