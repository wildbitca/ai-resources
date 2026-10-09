"""The wizard's model selection: three shapes, flags without prompts, contradictions without writes."""
from __future__ import annotations

import argparse

import pytest

from ai_resources import model_accounts as ma
from ai_resources.setup import detection, model_selection as ms, state, ui, wizard
from wizard_fakes import SENTINEL, FakeCatalog, Script, accounts_for, many, one, slot


def args(**kw):
    base = dict(models="", shape="", providers="", smoke_path="", backend="", cockpits="", allow_unverified=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def host(monkeypatch):
    """claude, openclaw, gemini and cursor installed; google and anthropic credentialed."""
    accounts = accounts_for(anthropic=True, google=True)
    catalog = FakeCatalog()
    monkeypatch.setattr(ms, "detect_accounts", lambda deps=None: accounts)
    monkeypatch.setattr(wizard, "_discover_for_wizard", catalog)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: _detected("claude", "openclaw", "gemini", "cursor"))
    return accounts


class _Det:
    def __init__(self, cid, installed):
        self.name, self.installed, self.version, self.binary_path = cid, installed, "1", f"/bin/{cid}"


def _detected(*installed):
    ids = ("claude", "gemini", "cursor", "codex", "aider", "copilot", "windsurf", "continue", "opencode", "openclaw")
    return {cid: _Det(cid, cid in installed) for cid in ids}


def _state(*installed):
    s = state.SetupState()
    for cid in installed:
        s.cockpits[cid] = state.CockpitState(installed=True)
    return s


# --- the three shapes, interactively ----------------------------------------------------------------

def run_step(monkeypatch, script, s=None, **kw):
    script.install(monkeypatch)
    s = s or _state("claude")
    return s, wizard._step_models(s, args(**kw))


def test_shape_single_one_model(host, monkeypatch):
    script = Script(**{"How do you want": "pick", "Add a key": [], "What do you want": "single",
                       "Which model": one("gemini-3.8-flash"), "Apply": "Apply"})
    s, rc = run_step(monkeypatch, script)
    sel = s.get_selection()
    assert rc == 0 and sel.shape == "single" and list(sel.slots) == ["google:gemini-flash"]
    assert sel.primary == "google:gemini-flash" and sel.slots["google:gemini-flash"].ref == "google/gemini-3.8-flash"


def test_shape_single_provider_two_google_slots(host, monkeypatch):
    script = Script(**{"How do you want": "pick", "Add a key": [], "What do you want": "single-provider",
                       "Which provider": "google", "Models from google": many("gemini-3.8-flash", "gemini-3.5-flash-lite"),
                       "Primary model": "google:gemini-flash"})
    s, rc = run_step(monkeypatch, script)
    sel = s.get_selection()
    assert sel.shape == "single-provider"
    assert list(sel.slots) == ["google:gemini-flash", "google:gemini-flash-lite"] and sel.primary == "google:gemini-flash"
    assert list(sel.providers) == ["google"]


def test_shape_multi_provider_anthropic_and_google(host, monkeypatch):
    script = Script(**{"How do you want": "pick", "Add a key": [], "What do you want": "multi-provider",
                       "Providers:": lambda values: values, "Models from anthropic": many("claude-sonnet-5-5"),
                       "Models from google": many("gemini-3.8-flash"), "Primary model": "anthropic:sonnet"})
    s, rc = run_step(monkeypatch, script)
    sel = s.get_selection()
    assert sel.shape == "multi-provider" and set(sel.slots) == {"anthropic:sonnet", "google:gemini-flash"}
    assert sel.primary == "anthropic:sonnet" and set(sel.providers) == {"anthropic", "google"}


def test_only_credentialed_providers_are_offered_and_previews_are_never_prechecked(host, monkeypatch):
    seen = {}

    def providers_answer(values):
        seen["providers"] = list(values)
        return list(values)

    def google_answer(values):
        seen["google"] = [(v.id, v.channel) for v in values]
        return [v for v in values if v.id == "gemini-3.8-flash"]

    script = Script(**{"How do you want": "pick", "Add a key": [], "What do you want": "multi-provider",
                       "Providers:": providers_answer, "Models from anthropic": many("claude-sonnet-5-5"),
                       "Models from google": google_answer, "Primary model": "anthropic:sonnet"})
    run_step(monkeypatch, script)
    assert seen["providers"] == ["anthropic", "google"]                     # openai, deepseek ... have no credentials
    assert ("gemini-3.1-pro-preview", "preview") in seen["google"]
    ids = dict(seen["google"])
    assert ids["gemini-3.8-flash"] == "stable"                                          # the newest stable flash
    assert "gemini-3.7-flash" not in ids                                                # not the older ones
    channels = [c for _i, c in seen["google"]]
    assert channels.index("preview") > channels.index("stable")


def test_missing_keys_are_asked_for_only_the_providers_without_credentials(host, monkeypatch):
    offered = {}

    def add_key(values):
        offered["values"] = list(values)
        return []

    script = Script(**{"How do you want": "pick", "Add a key": add_key, "What do you want": "single",
                       "Which model": one("gemini-3.8-flash")})
    run_step(monkeypatch, script)
    assert set(offered["values"]) == {"openai", "deepseek", "moonshot"}


def test_choosing_the_kit_profile_changes_nothing(host, monkeypatch):
    script = Script(**{"How do you want": "profile"})
    s, rc = run_step(monkeypatch, script)
    assert rc == 0 and s.get_selection() is None and len(script.asked) == 1


# --- non-interactive flags ----------------------------------------------------------------------------

def test_flags_build_the_selection_with_no_prompt(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    s = _state("claude")
    rc = wizard._preflight_selection(args(providers="google", models="gemini-flash", backend="litellm", allow_unverified=True), s)
    assert rc == 0 and not [a for a in script.asked]                         # zero prompts
    sel = s.get_selection()
    assert list(sel.slots) == ["google:gemini-flash"] and sel.slots["google:gemini-flash"].ref == "google/gemini-3.8-flash"
    assert (s.mode, s.backend) == ("multi-model", "litellm") and sel.smoke_path == "litellm"


def test_without_flags_nothing_is_decided_and_no_prompt_happens(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    s = _state("claude")
    assert wizard._preflight_selection(args(), s) == 0 and s.get_selection() is None
    assert wizard._step_models(s, args()) == 0 and script.asked == []
    s.selection = {"shape": "single", "slots": {"google:gemini-flash": {"ref": "google/gemini-3.8-flash"}}}
    assert wizard._step_models(s, args()) == 0 and script.asked == []        # a saved selection is kept


def test_shape_single_with_two_models_is_a_contradiction_with_no_writes(host, monkeypatch, tmp_path):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    monkeypatch.setattr(state, "save", lambda s: (_ for _ in ()).throw(AssertionError("wrote the state")))
    s = _state("claude")
    rc = wizard._preflight_selection(args(shape="single", models="google:gemini-flash,anthropic:sonnet"), s)
    assert rc == 2 and s.get_selection() is None
    assert any("exactly one model" in m for m in script.shown)


def test_a_cockpit_the_matrix_skips_is_refused_with_its_reason(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    s = _state("claude")
    rc = wizard._preflight_selection(args(cockpits="claude", models="google:gemini-flash"), s)
    assert rc == 2 and s.get_selection() is None
    assert any("runs only Claude models without a gateway" in m for m in script.shown)
    # with a gateway and the explicit opt-in the same request is legal
    rc = wizard._preflight_selection(args(cockpits="claude", models="google:gemini-flash", backend="litellm",
                                          allow_unverified=True), s)
    assert rc == 0 and s.get_selection() is not None
    assert s.profile.customizations["__targets__"]["selected"] == ["claude"]


def test_unverified_gateway_cells_are_refused_without_the_opt_in(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    rc = wizard._preflight_selection(args(cockpits="claude", models="google:gemini-flash", backend="litellm"), _state("claude"))
    assert rc == 2 and any("not verified yet" in m for m in script.shown)


def test_unknown_models_providers_and_cockpits_are_refused(host, monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    assert wizard._preflight_selection(args(models="nonsense-9"), _state("claude")) == 2
    assert wizard._preflight_selection(args(models="gemini-flash", providers="openai"), _state("claude")) == 2   # no credentials
    assert wizard._preflight_selection(args(models="gemini-flash", cockpits="nope"), _state("claude")) == 2
    assert wizard._preflight_selection(args(shape="single"), _state("claude")) == 2          # --shape needs --models


def test_the_wizard_has_ten_steps():
    assert wizard.TOTAL_STEPS == 10


def test_the_sentinel_key_never_reaches_the_transcript(host, monkeypatch):
    monkeypatch.setattr(ms, "detect_accounts", lambda deps=None: accounts_for(anthropic=True))
    written = {}
    from ai_resources.setup import credentials
    monkeypatch.setattr(credentials, "update_env_tracked", lambda updates: (written.update(updates) or (None, list(updates))))
    script = Script(**{"How do you want": "pick", "Add a key": lambda values: ["google"], "GEMINI_API_KEY": SENTINEL,
                       "What do you want": "single", "Which model": one("claude-sonnet-5-5"), "Credentials": True})
    s, rc = run_step(monkeypatch, script)
    assert written == {"GEMINI_API_KEY": SENTINEL}                          # the key went to the env file ...
    assert SENTINEL not in script.transcript                               # ... and nowhere else
