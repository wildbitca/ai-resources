"""S12: a moved slot re-renders every file that embeds its old id, or restores them all."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from ai_resources import model_fanout as fo
from ai_resources import model_pins as mp
from ai_resources import model_rerender as rr
from ai_resources import models
from ai_resources import selection as sel
from ai_resources.setup import compat, litellm, model_selection as ms, profiles, state
from ai_resources.setup.cockpits import _shared, aider, claude

OLD, NEW = "gemini-3.7-flash", "gemini-3.8-flash"
SENTINEL = "FAKEKEY-DO-NOT-LEAK"
CHANGE = fo.Change({"google:gemini-flash": (OLD, NEW)})


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"


class Host:
    """A temp host: executors.yaml, litellm.yaml, Claude subagents + settings, aider.conf.yml."""

    def __init__(self, tmp_path, monkeypatch, *, mode="multi-model", backend="litellm", old=OLD):
        self.tmp, self.old = tmp_path, old
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        root = tmp_path / "claude"
        for attr, value in (("CONFIG_ROOT", root), ("SETTINGS_PATH", root / "settings.json"),
                            ("AGENTS_DIR", root / "agents")):
            monkeypatch.setattr(claude, attr, value)
        monkeypatch.setattr(aider, "CONF_PATH", tmp_path / ".aider.conf.yml")
        self.restarts: list[int] = []
        self.healthy = True
        monkeypatch.setattr(litellm, "restart_service", lambda: self.restarts.append(1) or True)
        monkeypatch.setattr(litellm, "wait_for_health", lambda *a, **k: self.healthy)
        monkeypatch.setattr(litellm, "health_check", lambda *a, **k: True)
        self.state = state.SetupState(mode=mode, backend=backend)
        self.state.litellm.deployment = "local"
        self.state.providers = {"google": state.ProviderState(enabled=True), "anthropic": state.ProviderState(enabled=True)}
        for cid in ("claude", "aider"):
            self.state.cockpits[cid] = state.CockpitState(installed=True)
        self.selection = sel.Selection(shape="single", providers={"google": sel.ProviderSel()},
                                       slots={"google:gemini-flash": sel.SlotSel(f"google/{old}")},
                                       primary="google:gemini-flash", smoke_path="litellm", allow_unverified=True)
        self.state.set_selection(self.selection)
        self.executors = {"version": 1, "backend": backend, "classes": {}, "by_persona": {},
                          "defaults": {"fallbacks": [], "max_retries": 3, "timeout_seconds": 600},
                          "by_role": {"implementer": {"model": f"google/{old}"}, "explore": {"model": f"google/{old}"},
                                      "planner": {"model": "anthropic/claude-opus-5"}}}
        if mode == "multi-model":
            profiles.write_executors(self.executors, state.executors_path())
            litellm.write_configs(self.executors, {"google": {"enabled": True}, "anthropic": {"enabled": True}})
        ak = str(_shared.stable_kit_root(Path(__file__).resolve().parents[1]))
        claude.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in claude.render_subagent_files(self.executors if mode == "multi-model" else {"by_role": {}},
                                                       ak, mode, strict=False).items():
            (claude.AGENTS_DIR / f"{name}.md").write_text(text, encoding="utf-8")
        claude.SETTINGS_PATH.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000"}, "x": 1}), encoding="utf-8")
        aider.CONF_PATH.write_text(yaml.safe_dump({"openai-api-base": "http://127.0.0.1:4000", "openai-api-key": "keep-me",
                                                   "model": f"google/{old}", "architect-model": "anthropic/claude-opus-5",
                                                   "weak-model": f"google/{old}"}, sort_keys=False), encoding="utf-8")

    def artifacts(self):
        return rr.build(state=self.state, selection=self.selection, detected=["claude", "aider"])

    def plan(self, allow=True, **override):
        actions = {a.cockpit: a for a in ms.plan_table(self.selection, self.state.mode, self.state.backend,
                                                       ["claude", "aider"], allow_unverified=allow)}
        for k, v in override.items():
            actions[k] = dataclasses.replace(actions[k], action=v)
        return actions

    def ctx(self, **kw):
        return fo.Ctx(selection=self.selection, plan=kw.pop("plan", self.plan()),
                      extras={"backup_dir": self.tmp / "bk", "state": self.state, **kw})

    def snapshot(self):
        files = [state.executors_path(), state.litellm_path(), claude.SETTINGS_PATH, aider.CONF_PATH,
                 *sorted(claude.AGENTS_DIR.glob("*.md"))]
        return {str(p): digest(p) for p in files}


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


def test_every_file_that_embeds_the_old_id_carries_the_new_one_and_nothing_else_moves(host):
    before = host.snapshot()
    used_it = {p.stem for p in claude.AGENTS_DIR.glob("*.md") if f"model: google/{OLD}" in p.read_text()}
    assert {"implementer", "explore"} <= used_it and "planner" not in used_it       # personas inherit their role's model
    res = fo.apply_all(host.artifacts(), CHANGE, host.ctx())
    assert res.ok, res.message
    assert set(res.payloads) == {"executors", "litellm", "claude-subagents", "aider-conf"}      # settings: litellm backend
    ex = yaml.safe_load(state.executors_path().read_text())
    assert ex["by_role"]["implementer"]["model"] == f"google/{NEW}" and ex["by_role"]["planner"]["model"] == "anthropic/claude-opus-5"
    assert f"gemini/{NEW}" in state.litellm_path().read_text() and f"gemini/{OLD}" not in state.litellm_path().read_text()
    conf = yaml.safe_load(aider.CONF_PATH.read_text())
    assert conf["model"] == conf["weak-model"] == f"google/{NEW}" and conf["architect-model"] == "anthropic/claude-opus-5"
    assert conf["openai-api-key"] == "keep-me" and conf["openai-api-base"] == "http://127.0.0.1:4000"
    after = host.snapshot()
    changed_agents = {Path(p).stem for p in after if p.endswith(".md") and after[p] != before[p]}
    assert changed_agents == used_it                                                             # only the files that used it
    assert after[str(claude.SETTINGS_PATH)] == before[str(claude.SETTINGS_PATH)]                 # nothing else under ~/.claude
    for name in changed_agents:
        text = (claude.AGENTS_DIR / f"{name}.md").read_text()
        assert f"model: google/{NEW}" in text and OLD not in text
    assert host.restarts == [1]                                                                  # exactly one LiteLLM restart


def test_the_ids_are_replaced_as_whole_strings_only(host):
    # claude-sonnet-5 is a prefix of claude-sonnet-5-5: a bump of one must never rewrite the other
    mapping = rr.substitution({"anthropic:sonnet": ("claude-sonnet-5", "claude-sonnet-5-5")})
    new, changed = rr.substitute({"a": "anthropic/claude-sonnet-5", "b": "anthropic/claude-sonnet-5-5", "c": ["claude-sonnet-5"]}, mapping)
    assert changed and new == {"a": "anthropic/claude-sonnet-5.5", "b": "anthropic/claude-sonnet-5-5", "c": ["claude-sonnet-5-5"]}
    assert rr.substitution({"google:x": ("a", "a")}) == {}


def test_a_minorless_claude_id_maps_the_openrouter_spelling_by_style_not_position():
    m = rr.substitution({"anthropic:sonnet": ("claude-sonnet-5", "claude-sonnet-5-5")})
    assert m["anthropic/claude-sonnet-5"] == "anthropic/claude-sonnet-5.5"
    assert m["claude-sonnet-5"] == "claude-sonnet-5-5"
    m = rr.substitution({"anthropic:opus": ("claude-opus-4-7", "claude-opus-5")})
    assert m["anthropic/claude-opus-4.7"] == "anthropic/claude-opus-5"
    assert m["claude-opus-4-7"] == "claude-opus-5"


def test_openrouter_spellings_follow_the_namespace_and_the_dotted_minor():
    m = rr.substitution({"anthropic:haiku": ("claude-haiku-4-5", "claude-haiku-5-5"), "moonshot:kimi-code": ("kimi-k2.6-code", "kimi-k2.7-code")})
    assert m["anthropic/claude-haiku-4.5"] == "anthropic/claude-haiku-5.5"
    assert m["moonshotai/kimi-k2.6-code"] == "moonshotai/kimi-k2.7-code"


def test_a_single_model_host_changes_no_claude_file(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, mode="single-model")
    before = h.snapshot()
    res = fo.apply_all(h.artifacts(), CHANGE, h.ctx(plan=h.plan(allow=False)))
    assert res.ok and res.payloads == {}
    assert h.snapshot() == before and h.restarts == []


def test_a_cockpit_the_matrix_skips_keeps_its_files(host):
    before = host.snapshot()
    res = fo.apply_all(host.artifacts(), CHANGE, host.ctx(plan=host.plan(claude="skip", aider="skip")))
    assert res.ok and set(res.payloads) == {"executors", "litellm"}
    after = host.snapshot()
    assert after[str(aider.CONF_PATH)] == before[str(aider.CONF_PATH)]
    assert all(after[p] == before[p] for p in after if p.endswith(".md") or p.endswith("settings.json"))


def test_an_unhealthy_litellm_restores_every_file_byte_for_byte_and_brings_the_old_config_up(host):
    before = host.snapshot()
    host.healthy = False
    res = fo.apply_all(host.artifacts(), CHANGE, host.ctx())
    assert not res.ok and res.failed == "litellm" and "healthy" in res.message
    assert host.snapshot() == before
    assert host.restarts == [1, 1]                                    # the failed restart and the one that restored the old config


def test_restoring_a_recorded_change_puts_every_file_back(host):
    before = host.snapshot()
    arts = host.artifacts()
    res = fo.apply_all(arts, CHANGE, host.ctx())
    assert res.ok and host.snapshot() != before
    ok, msg = fo.restore_all(arts, res.payloads, host.ctx())
    assert ok, msg
    assert host.snapshot() == before


def _all_key_env_vars(monkeypatch):
    """Put the sentinel in every environment variable a renderer could read a key from."""
    from ai_resources.setup import credentials
    names = {"LITELLM_MASTER_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"}
    names |= {v for vs in credentials.PROVIDER_KEYS.values() for v in vs}
    names |= {env for _prefix, env in litellm._VENDOR_UPSTREAM.values()}
    for n in names:
        monkeypatch.setenv(n, SENTINEL)


def _written_files(host):
    return [state.executors_path(), state.litellm_path(), claude.SETTINGS_PATH, aider.CONF_PATH,
            *claude.AGENTS_DIR.glob("*.md")]


def test_no_key_value_is_written_by_the_re_render(host, monkeypatch):
    _all_key_env_vars(monkeypatch)
    res = fo.apply_all(host.artifacts(), CHANGE, host.ctx())
    assert res.ok and "aider-conf" in res.payloads                 # aider.conf.yml really was re-rendered
    for p in _written_files(host):
        assert SENTINEL not in p.read_text(), p
    assert yaml.safe_load(aider.CONF_PATH.read_text())["openai-api-key"] == "keep-me"
    assert "os.environ/" in state.litellm_path().read_text()
    assert "api_key: gemini" not in state.litellm_path().read_text()


def test_no_key_value_reaches_the_claude_settings_under_openrouter(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, backend="openrouter")
    _all_key_env_vars(monkeypatch)
    claude.SETTINGS_PATH.write_text(json.dumps({
        "env": {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-4.5"}}))
    res = fo.apply_all(h.artifacts(), fo.Change({"anthropic:haiku": ("claude-haiku-4-5", "claude-haiku-5-5")}), h.ctx())
    assert res.ok and "claude-settings" in res.payloads
    text = claude.SETTINGS_PATH.read_text()
    assert SENTINEL not in text and json.loads(text)["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok"


# --- M0: the credential-bearing files keep their permission bits ------------------------------------------------

def _mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def test_a_locked_down_aider_conf_and_settings_stay_0600_after_the_render(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, backend="openrouter")
    claude.SETTINGS_PATH.write_text(json.dumps({
        "env": {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-4.5"}}))
    aider.CONF_PATH.write_text(yaml.safe_dump({"openai-api-key": "k", "model": "anthropic/claude-haiku-4.5"}))
    for p in (claude.SETTINGS_PATH, aider.CONF_PATH):
        p.chmod(0o600)
    before = h.snapshot()
    res = fo.apply_all(h.artifacts(), fo.Change({"anthropic:haiku": ("claude-haiku-4-5", "claude-haiku-5-5")}), h.ctx())
    assert res.ok and {"claude-settings", "aider-conf"} <= set(res.payloads)
    assert _mode(claude.SETTINGS_PATH) == 0o600 and _mode(aider.CONF_PATH) == 0o600
    assert h.snapshot() != before
    ok, msg = fo.restore_all(h.artifacts(), res.payloads, h.ctx())
    assert ok, msg
    assert _mode(claude.SETTINGS_PATH) == 0o600 and _mode(aider.CONF_PATH) == 0o600


def test_a_restore_puts_a_widened_file_back_to_its_original_mode(host):
    aider.CONF_PATH.chmod(0o600)
    res = fo.apply_all(host.artifacts(), CHANGE, host.ctx())
    assert res.ok
    aider.CONF_PATH.chmod(0o644)                                     # something widened it after the render
    ok, msg = fo.restore_all(host.artifacts(), res.payloads, host.ctx())
    assert ok, msg
    assert _mode(aider.CONF_PATH) == 0o600


def test_a_new_file_the_atomic_write_creates_is_0600(tmp_path):
    target = tmp_path / "new.conf"
    rr._atomic_write(target, "openai-api-key: k\n")
    assert _mode(target) == 0o600 and target.read_text() == "openai-api-key: k\n"
    assert not (tmp_path / "new.conf.tmp").exists()


# --- M11: nothing the artifact did not create is ever deleted -----------------------------------------------------

def test_a_restore_without_a_backup_never_deletes_a_file_that_existed(tmp_path):
    f = tmp_path / "executors.yaml"
    f.write_text("mine: 1\n")
    payload = rr.backup_file(f, None, "executors")
    assert payload["existed"] is True and payload["backup"] is None
    with pytest.raises(fo.ArtifactError, match="no backup"):
        rr.restore_file(payload)
    assert f.read_text() == "mine: 1\n"


def test_a_restore_removes_only_a_file_the_artifact_created(tmp_path):
    f = tmp_path / "executors.yaml"
    payload = rr.backup_file(f, None, "executors")
    assert payload["existed"] is False
    f.write_text("created: 1\n")
    rr.restore_file(payload)
    assert not f.exists()


def test_a_failed_write_without_a_backup_dir_leaves_the_users_file(host, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(rr.AiderConfArtifact, "_write", boom)
    before = digest(aider.CONF_PATH)
    art = next(a for a in host.artifacts() if a.id == "aider-conf")
    ctx = fo.Ctx(selection=host.selection, plan=host.plan(), extras={"state": host.state})     # no backup_dir
    with pytest.raises(fo.ArtifactError, match="disk full"):
        art.render(CHANGE, ctx)
    assert digest(aider.CONF_PATH) == before


def test_under_openrouter_the_claude_pins_and_overrides_move_too(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, backend="openrouter")
    claude.SETTINGS_PATH.write_text(json.dumps({
        "env": {"ANTHROPIC_AUTH_TOKEN": "tok", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "anthropic/claude-haiku-4.5"},
        "modelOverrides": {"claude-haiku-4-5": "anthropic/claude-haiku-4.5"}}))
    change = fo.Change({"anthropic:haiku": ("claude-haiku-4-5", "claude-haiku-5-5")})
    res = fo.apply_all(h.artifacts(), change, h.ctx())
    assert "claude-settings" in res.payloads and "litellm" not in res.payloads        # nothing to restart for OpenRouter
    doc = json.loads(claude.SETTINGS_PATH.read_text())
    assert doc["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "anthropic/claude-haiku-5.5" and doc["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok"
    assert doc["modelOverrides"]["claude-haiku-5-5"] == "anthropic/claude-haiku-5.5" and "claude-haiku-4-5" in doc["modelOverrides"]
    assert h.restarts == []


def test_a_new_claude_id_gets_a_passthrough_entry_in_litellm(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    change = fo.Change({"anthropic:opus": ("claude-opus-5", "claude-opus-5-5")})
    res = fo.apply_all(h.artifacts(), change, h.ctx())
    assert res.ok and "litellm" in res.payloads
    names = [m["model_name"] for m in yaml.safe_load(state.litellm_path().read_text())["model_list"]]
    assert "claude-opus-5-5" in names


# --- through run_update: all or nothing --------------------------------------------------------------

def _run_env(tmp_path, monkeypatch, host, *, healthy=True):
    from test_models_update import _provider_env
    host.healthy = healthy
    (tmp_path / "env").mkdir()
    e = _provider_env(tmp_path / "env", monkeypatch, "google")
    mp.save_overlay({"approvals": {"google:gemini-flash": "gemini-3.9-flash"}}, e.overlay)
    deps = dataclasses.replace(e.deps(), artifacts=host.artifacts, route_plan=host.plan)
    e.deps = lambda: deps
    return e


@pytest.fixture
def run_host(tmp_path, monkeypatch):
    # the effective id of the slot is the kit default (3.8); the catalog's newest is 3.9
    (tmp_path / "host").mkdir()
    return Host(tmp_path / "host", monkeypatch, old="gemini-3.8-flash")


def test_an_update_leaves_overlay_executors_litellm_claude_and_aider_consistent(tmp_path, monkeypatch, run_host):
    e = _run_env(tmp_path, monkeypatch, run_host)
    r = e.run()
    assert r.rc == 0 and r.outcome == "switched"
    assert set(r.rendered) == {"executors", "litellm", "claude-subagents", "aider-conf"}
    assert e.state()["pins"]["google:gemini-flash"] == "gemini-3.9-flash"
    assert "gemini-3.9-flash" in state.executors_path().read_text() and "gemini-3.8-flash" not in state.executors_path().read_text()
    assert "gemini/gemini-3.9-flash" in state.litellm_path().read_text()
    assert yaml.safe_load(aider.CONF_PATH.read_text())["model"] == "google/gemini-3.9-flash"
    assert "model: google/gemini-3.9-flash" in (claude.AGENTS_DIR / "implementer.md").read_text()
    assert r.as_dict()["rerendered"] == r.rendered
    assert e.restarts == 0 and e.patches == []                       # no verified OpenClaw runtime for Google: openclaw.json untouched


def test_an_unhealthy_litellm_rolls_the_whole_update_back(tmp_path, monkeypatch, run_host):
    e = _run_env(tmp_path, monkeypatch, run_host, healthy=False)
    before = run_host.snapshot()
    r = e.run()
    assert r.rc == models.EXIT_ROLLED_BACK and r.outcome == "rolled_back"
    assert run_host.snapshot() == before
    assert "google:gemini-flash" not in e.state().get("pins", {})
