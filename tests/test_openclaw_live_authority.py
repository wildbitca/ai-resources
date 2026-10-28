"""ADR-0016 D1 (AC-10, S8): the live reading is authoritative. A readable OpenClaw with no usable provider restricts
routing to nothing; an unreadable one changes nothing; usability is read per agent."""
from __future__ import annotations

import json
from pathlib import Path

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


# --- 3.2.8 item 3: unreadable is not empty; config corroboration; auto reads the live auth ---------------------------

def _unreadable(reason="OpenClaw state is in an offline maintenance window", config=()):
    live = live_auth.LiveReading({"main": {p: live_auth.LiveAuth(live_auth.API_KEY, live_auth.B_API, "API key")
                                           for p in config}})
    live.readable, live.reason, live.config_providers = False, reason, set(config)
    return live


def test_an_unreadable_reading_clears_a_none_usable_left_by_an_earlier_run():
    s = state.SetupState()
    cockpit_auth.set_none_usable(s, True)
    oc._record_openclaw_auth(s, _unreadable())
    assert not cockpit_auth.none_usable(s) and oc.enabled_providers(s) is None
    assert cockpit_auth.read_status(s) == "unreadable" and "maintenance" in cockpit_auth.read_reason(s)


def test_an_unreadable_reading_with_config_profiles_records_them():
    s = state.SetupState()
    oc._record_openclaw_auth(s, _unreadable(config=("anthropic", "google")))
    assert oc.enabled_providers(s, "main") == ["anthropic", "google"]


def test_a_readable_empty_reading_with_config_corroboration_is_not_none_usable():
    s = state.SetupState()
    live = _reading({"main": []})
    live.config_providers = {"anthropic"}
    oc._record_openclaw_auth(s, live)
    assert not cockpit_auth.none_usable(s)


def test_the_unreadable_message_names_the_reason_and_keeps_the_saved_providers():
    shown = []

    class U:
        def warn(self, m): shown.append(m)
        def info(self, m): shown.append(m)
    oc._show_live_auth(U(), _unreadable())
    assert shown == ["could not read OpenClaw auth (OpenClaw state is in an offline maintenance window); "
                     "keeping the saved providers"]


def test_kit_role_note_distinguishes_never_read_from_unreadable():
    s = state.SetupState()
    assert "never read" in oc._no_providers_note("Kit-role agents", s)
    oc._record_openclaw_auth(s, _unreadable())
    assert "could not be read" in oc._no_providers_note("Kit-role agents", s)


def _real_shaped_cli(monkeypatch, tmp_path, status_rc=0, status_text=None, banner=""):
    live = Path(__file__).parent / "fixtures" / "openclaw_live"
    status = status_text if status_text is not None else (live / "models_status_live_2026_10_7.json").read_text()
    listing = (live / "auth_list_live_main.json").read_text()
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"agents": {"entries": {"main": {}, "ai": {}}},
                               "auth": {"profiles": {"anthropic:default": {"provider": "anthropic", "mode": "api_key"}}}}),
                   encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    calls = []

    def runner(argv, stdin=None, timeout=120):
        calls.append(list(argv))
        if argv[:2] == ["models", "status"]:
            return status_rc, banner + status
        if argv[:3] == ["models", "auth", "list"]:
            return 0, banner + listing
        return 1, "refused"
    monkeypatch.setattr(oc, "_openclaw", runner)
    return calls


def test_an_auto_run_reads_and_records_the_live_auth_without_asking(monkeypatch, tmp_path):
    calls = _real_shaped_cli(monkeypatch, tmp_path)
    Script().install(monkeypatch, non_interactive=True)
    s = state.SetupState()
    assert oc.ask_auth(ui, s, None, False) == 0
    for agent in ("main", "ai"):
        assert {"agy-cli-lane", "anthropic", "google"} <= set(oc.enabled_providers(s, agent))
    assert all(c[0] == "models" for c in calls)                         # read-only: no configure, no write


def test_an_auto_run_on_a_maintenance_window_keeps_saved_providers_and_says_why(monkeypatch, tmp_path):
    from ai_resources.setup import live_auth as la
    monkeypatch.setattr("time.sleep", lambda s: None)
    _real_shaped_cli(monkeypatch, tmp_path, status_rc=1,
                     status_text="OpenClaw state is undergoing offline maintenance; retry when it finishes")
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    s = state.SetupState()
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    cockpit_auth.set_none_usable(s, True)
    assert oc.ask_auth(ui, s, None, False) == 0
    assert not cockpit_auth.none_usable(s) and "anthropic" in (oc.enabled_providers(s) or [])
