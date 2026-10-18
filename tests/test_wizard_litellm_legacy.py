"""LiteLLM leaves the new-setup gateway choice but saved litellm states keep loading (ADR-0010 N1, S5)."""
from __future__ import annotations

import argparse

from ai_resources.setup import state, wizard
from wizard_fakes import Script

NOTICE = ("LiteLLM is no longer offered for new setups; your saved gateway keeps working. "
          "Choose OpenRouter to switch.")


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_fresh_advanced_offers_no_litellm(monkeypatch):
    """AC-S5a: a fresh run picking Advanced is never shown LiteLLM and is not asked which gateway."""
    s = state.SetupState()
    script = Script(**{"Pick how they work": "advanced"})
    script.install(monkeypatch)
    assert wizard._step1_mode(s) == 0
    assert (s.mode, s.backend) == ("multi-model", "openrouter")
    assert not any(a.endswith("Which gateway?") for a in script.asked)
    assert not any("LiteLLM" in t for t in script.titles)
    assert not any("LiteLLM" in m for m in script.shown)


def test_the_advanced_text_does_not_name_litellm():
    assert all("LiteLLM" not in line for _k, _l, line in wizard.HOW_CHOICES)
    assert [k for k, _l, _t in wizard.GATEWAY_CHOICES] == ["openrouter"]


def test_saved_litellm_is_offered_as_the_default_with_one_notice(monkeypatch):
    """AC-S5b (selection part): the saved gateway stays selectable and the notice prints exactly once."""
    s = state.SetupState(mode="multi-model", backend="litellm")
    seen = {}
    script = Script(**{"Pick how they work": "advanced",
                       "Which gateway": lambda values: seen.setdefault("v", list(values)) and "litellm"})
    script.install(monkeypatch)
    assert wizard._step1_mode(s, is_first=False) == 0
    assert seen["v"] == ["litellm", "openrouter"]
    assert script.defaults["Which gateway?"] == ["litellm"]
    assert any("LiteLLM (saved; no longer offered for new setups)" in t for t in script.titles)
    assert (s.mode, s.backend) == ("multi-model", "litellm")
    assert sum(NOTICE in m for m in script.shown) == 1


def test_saved_litellm_can_switch_to_openrouter(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="litellm")
    Script(**{"Pick how they work": "advanced", "Which gateway": "openrouter"}).install(monkeypatch)
    assert wizard._step1_mode(s, is_first=False) == 0
    assert s.backend == "openrouter"


def test_backend_flag_litellm_is_accepted_with_the_notice(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="litellm")
    script = Script()
    script.install(monkeypatch)
    assert wizard._step1_mode(s, _args(backend="litellm")) == 0
    assert sum(NOTICE in m for m in script.shown) == 1


def test_backend_flag_openrouter_prints_no_notice(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="openrouter")
    script = Script()
    script.install(monkeypatch)
    wizard._step1_mode(s, _args(backend="openrouter"))
    assert not any(NOTICE in m for m in script.shown)


def test_single_model_state_with_default_litellm_backend_prints_no_notice(monkeypatch):
    """AC-S5c: the state.backend default is untouched and silent for cli and single states."""
    assert state.SetupState().backend == "litellm"
    for how in ("alone", "team"):
        s = state.SetupState()
        script = Script(**{"Pick how they work": how})
        script.install(monkeypatch)
        assert wizard._step1_mode(s) == 0
        assert not any(NOTICE in m for m in script.shown)
