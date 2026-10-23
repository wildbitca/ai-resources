"""A saved gateway state keeps loading, is migrated to the cockpit's own sign-in and is never torn down (ADR-0015 D1).

This file replaced the ADR-0010 tests of the "Which gateway?" question: LiteLLM and OpenRouter are no longer offered to
any cockpit, so what remains to prove is the migration (old -> new shown once), that nothing on the host is removed, and
that the deprecated `--backend` flag still parses.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil

import pytest

from ai_resources.setup import cockpit_auth, litellm, profiles, state, wizard
from wizard_fakes import Script, host_env

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "setup_state"
GATEWAYS = ("multi_litellm", "multi_openrouter")
MIGRATION_LINE = "Sign-in is now asked per tool instead of once for all of them"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_a_fresh_run_offers_no_gateway_anywhere(monkeypatch):
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    script = Script(**{"How does Claude Code sign in": "subscription"})
    script.install(monkeypatch)
    assert wizard._step_cockpit_auth(s, _args()) == 0
    text = (" ".join(script.asked) + " ".join(script.titles) + " ".join(script.shown)).lower()
    assert not any(w in text for w in ("litellm", "openrouter", "gateway"))
    assert (s.mode, s.cli_team) == ("single-model", True)


def test_the_removed_gateway_constants_are_gone():
    for name in ("HOW_CHOICES", "GATEWAY_CHOICES", "LEGACY_GATEWAY_CHOICE", "LITELLM_LEGACY_NOTICE", "HOW_PROMPT",
                 "_step1_mode", "_how_default", "_run_mode_step", "_gateway_credential_warning"):
        assert not hasattr(wizard, name), name


@pytest.mark.parametrize("name", GATEWAYS)
def test_a_saved_gateway_is_migrated_once_and_the_table_is_shown_once(monkeypatch, tmp_path, name):
    home = host_env(monkeypatch, tmp_path)
    (home / "xdg" / "ai-resources").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / f"{name}.yaml", state.state_path())
    saved = state.load()
    script = Script()
    script.install(monkeypatch)
    wizard._migrate_cockpit_auth(saved)
    shown = " | ".join(script.shown)
    assert shown.count(MIGRATION_LINE) == 1 and "Advanced (gateway) -> subscription" in shown
    assert "left as it is; setup does not remove it" in shown
    state.save(saved)
    again = state.load()
    script2 = Script()
    script2.install(monkeypatch)
    wizard._migrate_cockpit_auth(again)
    assert MIGRATION_LINE not in " | ".join(script2.shown)         # the second load has nothing to fill


@pytest.mark.parametrize("name", GATEWAYS)
def test_the_gateway_installation_on_the_host_is_left_untouched(monkeypatch, tmp_path, name):
    """No teardown, no litellm state reset: only the global fields are derived and the sign-in is filled."""
    home = host_env(monkeypatch, tmp_path)
    (home / "xdg" / "ai-resources").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / f"{name}.yaml", state.state_path())
    monkeypatch.setattr(litellm, "execute_multi_model_teardown", lambda p: (_ for _ in ()).throw(AssertionError("teardown")))
    monkeypatch.setattr(litellm, "plan_multi_model_teardown", lambda p: (_ for _ in ()).throw(AssertionError("teardown")))
    s = state.load()
    before = state._to_dict(s)
    Script().install(monkeypatch)
    wizard._migrate_cockpit_auth(s)
    after = state._to_dict(s)
    for key in ("litellm", "providers", "tracking", "backend", "openclaw"):
        assert after[key] == before[key], key
    assert after["mode"] == "single-model" and after["cockpit_auth"]["claude"]["method"] == "subscription"


@pytest.mark.parametrize("name", GATEWAYS)
def test_a_gateway_profile_is_replaced_by_the_default_when_it_is_not_a_single_model_profile(monkeypatch, tmp_path, name):
    home = host_env(monkeypatch, tmp_path)
    (home / "xdg" / "ai-resources").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / f"{name}.yaml", state.state_path())
    s = state.load()
    s.profile.name = "gateway/performance"
    s.profile.customized = False
    Script().install(monkeypatch)
    wizard._migrate_cockpit_auth(s)
    assert s.profile.name == cockpit_auth.default_profile_for(s)
    assert s.profile.name in profiles.list_profiles(mode="single-model")


def test_backend_flag_litellm_is_accepted_with_one_deprecation_note(monkeypatch):
    monkeypatch.setattr(wizard.model_selection, "detect_accounts", lambda: {})
    monkeypatch.setattr(wizard.model_selection, "candidates", lambda a, f: [])
    monkeypatch.setattr(wizard.detection, "detect_all_cockpits", lambda: {})
    s = state.SetupState(mode="multi-model", backend="litellm")
    script = Script()
    script.install(monkeypatch)
    assert wizard._preflight_selection(_args(backend="litellm"), s) == 0
    assert sum("--backend litellm is deprecated" in m for m in script.shown) == 1
    assert (s.mode, s.backend) == ("single-model", "litellm")          # the saved gateway fields are left as they are


def test_state_backend_default_stays_litellm_and_is_silent(monkeypatch):
    """The `backend` default is untouched (older kits read it) and a fresh run prints no gateway line."""
    assert state.SetupState().backend == "litellm"
    s = state.SetupState()
    script = Script()
    script.install(monkeypatch)
    wizard._migrate_cockpit_auth(s)
    assert script.shown == []


def test_dry_run_yes_from_a_saved_litellm_renders_the_same_team(monkeypatch, tmp_path):
    """--dry-run --yes over the saved litellm state: the executors the apply renders are those of the saved profile."""
    home = host_env(monkeypatch, tmp_path)
    (home / "xdg" / "ai-resources").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "multi_litellm.yaml", state.state_path())
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    script = Script(**{"Use previous": True, "How does Claude Code sign in": "subscription",
                       "Starting team": lambda values: values[0], "Apply this?": lambda values: values[0]})
    script.install(monkeypatch)
    before = state.state_path().read_bytes()
    assert wizard.run(_args(dry_run=True, yes=True)) == 0
    assert state.state_path().read_bytes() == before                    # a dry run rewrites nothing
    s = seen["s"]
    assert (s.mode, s.backend) == ("single-model", "litellm")
    assert sum(MIGRATION_LINE in m for m in script.shown) == 1
