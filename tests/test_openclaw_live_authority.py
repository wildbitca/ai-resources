"""ADR-0016 D1 (AC-10, S8): the live reading is authoritative. A readable OpenClaw with no usable provider restricts
routing to nothing; an unreadable one changes nothing; usability is read per agent."""
from __future__ import annotations

import json

from ai_resources.setup import cockpit_auth, live_auth, state, ui
from ai_resources.setup.cockpits import _openclaw_image as img
from ai_resources.setup.cockpits import openclaw as oc
from ai_resources.setup.cockpits import openclaw_roles
from wizard_fakes import Script, fake_openclaw


def _reading(per_agent, readable=True):
    live = live_auth.LiveReading({a: {p: live_auth.LiveAuth(live_auth.API_KEY, live_auth.B_API, "API key") for p in ps}
                                  for a, ps in per_agent.items()})
    live.readable = readable
    return live


def test_an_empty_readable_reading_drops_the_saved_providers_and_restricts_to_nothing():
    s = state.SetupState()
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    oc._record_openclaw_auth(s, _reading({"main": []}))
    assert cockpit_auth.providers(s) == {} and oc.enabled_providers(s) == [] and oc.enabled_providers(s, "main") == []


def test_an_unreadable_reading_keeps_whatever_was_saved():
    s = state.SetupState()
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    oc._record_openclaw_auth(s, _reading({"main": []}, readable=False))
    assert oc.enabled_providers(s) == ["anthropic"]


def test_a_later_reading_with_providers_lifts_the_empty_restriction():
    s = state.SetupState()
    oc._record_openclaw_auth(s, _reading({"main": []}))
    oc._record_openclaw_auth(s, _reading({"main": ["openai"]}))
    assert oc.enabled_providers(s) == ["openai"]


def test_usability_is_recorded_and_read_per_agent():
    s = state.SetupState()
    oc._record_openclaw_auth(s, _reading({"main": ["anthropic"], "claude": ["openai"]}))
    assert oc.enabled_providers(s, "main") == ["anthropic"]
    assert oc.enabled_providers(s, "claude") == ["openai"]
    assert sorted(oc.enabled_providers(s)) == ["anthropic", "openai"]          # combined, for callers without an agent
    assert oc.enabled_providers(s, "never-read") == ["anthropic", "openai"]


def test_each_agent_entry_gets_fallbacks_from_its_own_usable_set():
    doc = {"agents": {"entries": {"main": {"model": {"primary": "x"}}, "claude": {"model": {"primary": "x"}}}}}
    patch = oc.build_patch(oc.ENGINES["claude-code"], "claude-sonnet-5-5", doc, "/kit/skills", "engram", fallbacks=True,
                           enabled_providers=["google"], enabled_by_agent={"main": ["google"], "claude": ["anthropic"]})
    entries = patch["agents"]["entries"]
    assert entries["main"]["model"]["fallbacks"] and "fallbacks" not in entries["claude"]["model"]


def test_the_warning_names_the_native_flow_and_nothing_is_routed(monkeypatch, tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    fake_openclaw(monkeypatch, authed=())
    s = state.SetupState()
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    script = Script(**{"OpenClaw sign-in": "keep"})
    script.install(monkeypatch)
    assert oc.ask_auth(ui, s, None, False) == 0
    assert any("openclaw configure --section model" in m and "untouched" in m for m in script.shown)
    assert oc.enabled_providers(s) == []


def test_kit_role_agents_are_not_written_when_nothing_is_usable(monkeypatch):
    s = state.SetupState()
    oc._record_openclaw_auth(s, _reading({"main": []}))
    monkeypatch.setattr(openclaw_roles, "configure", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote roles")))
    assert oc._configure_roles({}, s, {}, "/kit", dry_run=False, run_changes=[]) is False


def test_the_image_model_is_refused_when_the_main_agent_has_no_usable_provider(monkeypatch):
    s = state.SetupState()
    oc._record_openclaw_auth(s, _reading({"main": []}))
    route = img.ROUTES["openai-key"]
    # the gateway-wide view still claims a profile; the main agent's own reading says nothing is usable
    monkeypatch.setattr(oc, "_image_accounts", lambda: ({route.provider: {"kind": "profiles", "profiles": 1}}, []))
    assert oc._image_sign_in(s, route, dry_run=False) is False
    oc._record_openclaw_auth(s, _reading({"main": [route.provider]}))
    assert oc._image_sign_in(s, route, dry_run=False) is True


def test_the_engine_patch_writes_no_fallbacks_when_nothing_is_usable(monkeypatch, tmp_path):
    """The antigravity patch writes `fallbacks` whenever lanes are on, even an empty list that would wipe the operator's."""
    s = state.SetupState()
    s.cockpits["agy"] = state.CockpitState(installed=True)
    s.openclaw.engine = "antigravity"
    s.openclaw.risk_acknowledged = True
    s.profile.lanes = {"x": {"model": "y"}}
    oc._record_openclaw_auth(s, _reading({"main": []}))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    patches = []
    monkeypatch.setattr(oc, "_apply_hot", lambda patch, **k: patches.append(patch) or (True, "ok"))
    doc = {"agents": {"defaults": {"model": {"primary": "anthropic/claude-sonnet-5-5",
                                             "fallbacks": ["openai/gpt-5"]}}}}
    oc._configure_engine({"state": s, "dry_run": True}, doc, tmp_path / "openclaw.json", "/kit", [])
    assert patches
    assert "fallbacks" not in patches[0]["agents"]["defaults"]["model"]
    assert all("fallbacks" not in e.get("model", {}) for e in patches[0]["agents"].get("entries", {}).values())
