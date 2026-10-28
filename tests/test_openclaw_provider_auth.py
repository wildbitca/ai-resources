"""OpenClaw's sign-in step (ADR-0016 D1, AC-10): read what each agent can use LIVE, keep or change; names only in kit state.

The kit has no key prompt, no `paste-api-key` and no provider multiselect of its own. Sign-in belongs to OpenClaw
(`openclaw configure --section model`, handed the terminal only on an explicit choice; see
test_openclaw_delegated_configure.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_resources.setup import cockpit_auth, live_auth, state, ui
from ai_resources.setup.cockpits import openclaw as oc
from wizard_fakes import Script, fake_openclaw

SENTINEL = "sk-test-SENTINEL-KEY-0123456789abcdef"
FIX = Path(__file__).parent / "fixtures" / "openclaw_live"


@pytest.fixture
def host(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    cfg = tmp_path / "openclaw.json"
    cfg.write_text((FIX / "all_anthropic.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(oc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run")))
    log = fake_openclaw(monkeypatch)
    handed = []
    monkeypatch.setattr(live_auth, "handoff", lambda argv: handed.append(list(argv)) or 0)
    return type("H", (), {"calls": log, "home": home, "handed": handed, "cfg": cfg})


def _s(*installed):
    s = state.SetupState()
    for cid in installed:
        s.cockpits[cid] = state.CockpitState(installed=True)
    return s


def _script(monkeypatch, **answers):
    script = Script(**answers)
    script.install(monkeypatch)
    return script


def test_the_providers_openclaw_reports_usable_are_recorded_as_ids_and_method_names(host, monkeypatch):
    s = _s("claude")
    script = _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    assert oc.ask_auth(ui, s, None, False) == 0
    assert cockpit_auth.providers(s) == {"anthropic": "api_key", "openai": "api_key"}      # the fixture's two profiles
    assert host.handed == []
    assert any("OpenClaw agent" in m and "anthropic" in m for m in script.shown)
    assert not any("paste-api-key" in m or "Save the" in m for m in script.shown + script.asked)


def test_every_non_kit_agent_is_read_with_its_own_agent_flag(host, monkeypatch):
    _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    oc.ask_auth(ui, _s(), None, False)
    asked = [c[c.index("--agent") + 1] for c in host.calls if c[:3] == ["models", "auth", "list"] and "--agent" in c]
    assert asked[0] == "main" and "claude" in asked and not [a for a in asked if a.startswith("kit-")]
    assert len(asked) <= oc.MAX_AUTH_AGENTS


def test_only_read_only_commands_are_issued_and_never_a_gateway_call(host, monkeypatch):
    _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    oc.ask_auth(ui, _s(), None, False)
    for call in host.calls:
        assert call[:2] in (["models", "status"], ["config", "file"]) or call[:3] == ["models", "auth", "list"], call
        assert call[0] not in ("gateway", "restart", "agent", "agents", "channels")


def test_nothing_usable_drops_the_saved_providers_and_says_nothing_is_routed(host, monkeypatch):
    """Deliberate behaviour change (AC-10, ADR-0016): a readable OpenClaw that reports no usable provider is authoritative;
    the saved providers are no longer kept for routing."""
    fake_openclaw(monkeypatch, authed=())
    s = _s()
    cockpit_auth.set_providers(s, {"anthropic": "cli"})
    script = _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    assert oc.ask_auth(ui, s, None, False) == 0
    assert cockpit_auth.providers(s) == {} and oc.enabled_providers(s) == []
    assert any("no usable provider" in m and "openclaw configure --section model" in m for m in script.shown)


def test_a_missing_openclaw_changes_nothing_and_asks_only_keep_or_change(host, monkeypatch):
    monkeypatch.setattr(oc, "_openclaw", lambda argv, stdin=None, timeout=120: (127, "openclaw not found on PATH"))
    s = _s()
    script = _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    assert oc.ask_auth(ui, s, None, False) == 0
    assert cockpit_auth.providers(s) == {} and host.handed == []
    assert [m for m in script.asked if m.startswith("select")] == ["select: OpenClaw sign-in:"]


def test_a_non_interactive_run_reads_the_live_auth_read_only_and_never_signs_in(host, monkeypatch):
    quiet = _s()
    cockpit_auth.set_providers(quiet, {"anthropic": "cli"})
    Script().install(monkeypatch, non_interactive=True)
    log = fake_openclaw(monkeypatch)
    assert oc.ask_auth(ui, quiet, None, False) == 0
    assert log and all(argv[0] == "models" for argv in log) and host.handed == []
    assert set(cockpit_auth.providers(quiet)) == {"anthropic", "openai"}       # the live reading is recorded


def test_cancelling_the_question_is_a_cancel(host, monkeypatch):
    _script(monkeypatch, **{"OpenClaw sign-in": lambda v: None})
    assert oc.ask_auth(ui, _s(), None, True) == 130


def test_the_engine_provider_is_never_added_behind_the_operators_back(host, monkeypatch):
    s = _s("agy")
    s.openclaw.engine = "antigravity"
    script = _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    oc.ask_auth(ui, s, None, True)
    assert "google" not in cockpit_auth.providers(s)
    assert not any("was added" in m for m in script.shown)


def test_google_vertex_is_shown_as_adc_never_as_an_api_key_option(host, monkeypatch):
    def runner(argv, stdin=None, timeout=120):
        if argv[:3] == ["models", "auth", "list"]:
            return 0, json.dumps({"profiles": [{"id": "google-vertex:default", "provider": "google-vertex",
                                                "type": "adc"}]})
        return 1, "refused"
    monkeypatch.setattr(oc, "_openclaw", runner)
    s = _s()
    script = _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    oc.ask_auth(ui, s, None, False)
    assert cockpit_auth.providers(s) == {"google-vertex": "cli"}
    assert s.cockpit_auth["openclaw"]["live"] == {"google-vertex": "adc"}
    assert not any("API key" in t for t in script.titles)


def test_the_state_holds_names_only_and_no_secret_ever_reaches_a_file_or_a_log(host, monkeypatch):
    s = _s("claude")
    script = _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)                 # present in the environment, never read by the kit
    oc.ask_auth(ui, s, None, False)
    state.save(s)
    text = state.state_path().read_text()
    assert SENTINEL not in text and SENTINEL not in script.transcript
    doc = yaml.safe_load(text)
    assert cockpit_auth.secret_findings(doc["cockpit_auth"]) == []
    assert doc["cockpit_auth"]["openclaw"]["providers"] == {"anthropic": "api_key", "openai": "api_key"}


def test_routing_uses_only_the_usable_providers(host, monkeypatch):
    """AC-10: a provider with no usable profile is referenced by no route and no fallback."""
    s = _s()
    _script(monkeypatch, **{"OpenClaw sign-in": "keep"})
    oc.ask_auth(ui, s, None, False)
    enabled = oc.enabled_providers(s)
    assert set(enabled) == {"anthropic", "openai"}
    from ai_resources.setup.cockpits import _openclaw_providers as P
    refs = ["anthropic/claude-sonnet-5-5", "google/gemini-3.1-pro", "agy-cli/gemini-3.8-flash-low", "openai/gpt-5.6-sol"]
    assert P.restrict_refs(refs, enabled) == ["anthropic/claude-sonnet-5-5", "openai/gpt-5.6-sol"]
