"""The wizard's model selection: three shapes, flags without prompts, contradictions without writes."""
from __future__ import annotations

import argparse
import pathlib

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


# --- step 6: the recommended profile, and no silent change for a saved one (balanced-claude-55) ---------------

RECOMMENDED = "balanced-claude-55"


class _Step6:
    """ui stubs for step 6 that remember the default each prompt offered and accept it."""

    def __init__(self, monkeypatch, *, non_interactive=False, confirms=None):
        self.selects: list[dict] = []
        self.confirms: list[tuple[str, bool]] = []
        self.out: list[str] = []
        answers = confirms or {}

        def select(message, choices, default=None, **kw):
            self.selects.append({"message": message, "default": default,
                                 "labels": [getattr(c, "title", str(c)) for c in choices],
                                 "values": [getattr(c, "value", c) for c in choices]})
            return default

        def confirm(message, default=True):
            self.confirms.append((message, default))
            for key, value in answers.items():
                if key in message:
                    return value
            return default

        monkeypatch.setattr(ui, "select", select)
        monkeypatch.setattr(ui, "confirm", confirm)
        monkeypatch.setattr(ui, "is_non_interactive", lambda: non_interactive)
        for name in ("info", "detail", "warn", "error", "ok", "section"):
            monkeypatch.setattr(ui, name, lambda *a, _s=self, **k: _s.out.append(" ".join(str(x) for x in a)))
        monkeypatch.setattr(ui, "role_table", lambda rows, title="": self.out.append(
            title + "\n" + "\n".join("  ".join(r) for r in rows)))
        monkeypatch.setattr(ui, "console", lambda: _Printer(self))

    @property
    def text(self) -> str:
        return "\n".join(self.out)


class _Printer:
    def __init__(self, sink):
        self.sink = sink

    def print(self, *a, **k):
        self.sink.out.append(" ".join(str(x) for x in a))

    def rule(self, *a, **k):
        pass


def _multi(backend: str, profile: str | None = None) -> state.SetupState:
    s = state.SetupState()
    s.mode, s.backend = "multi-model", backend
    if profile is not None:
        s.profile.name = profile
    return s


@pytest.mark.parametrize("backend", ["litellm", "openrouter"])
def test_a_fresh_multi_model_host_defaults_to_the_recommended_profile(monkeypatch, backend):
    """TS-BC55-010: a state that never chose a profile offers balanced-claude-55, labelled (recommended)."""
    ui_stub = _Step6(monkeypatch)
    s = _multi(backend)
    assert s.profile.name == ""                                   # nothing chosen yet
    assert wizard._step6_profile(s) == 0
    ask = ui_stub.selects[0]
    assert ask["default"] == RECOMMENDED and wizard.RECOMMENDED_PROFILE == RECOMMENDED
    labels = dict(zip(ask["values"], ask["labels"]))
    assert "(recommended)" in labels[RECOMMENDED]
    assert [v for v, lab in labels.items() if "(recommended)" in lab] == [RECOMMENDED]
    assert s.profile.name == RECOMMENDED


def test_a_saved_profile_stays_the_default_even_when_it_is_the_old_untouched_one(monkeypatch):
    """TS-BC55-011 (saved name): a host that saved cost-optimized or measured-best keeps it."""
    for saved in ("measured-best", "cost-optimized"):
        ui_stub = _Step6(monkeypatch)
        s = _multi("litellm", saved)
        assert wizard._step6_profile(s) == 0
        assert ui_stub.selects[0]["default"] == saved and s.profile.name == saved


def test_accepting_the_default_for_a_saved_profile_leaves_executors_yaml_byte_identical(monkeypatch):
    """TS-BC55-011: step 6 never writes executors.yaml, and a saved measured-best is not replaced."""
    path = state.executors_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"version: 1\nprofile_name: measured-best\nby_role: {}\n")
    before = path.read_bytes()
    _Step6(monkeypatch, confirms={"Show how": False})
    s = _multi("litellm", "measured-best")
    assert wizard._step6_profile(s) == 0
    assert s.profile.name == "measured-best" and not s.profile.customized
    assert path.read_bytes() == before


def test_a_fresh_single_model_host_defaults_to_claude_native(monkeypatch):
    """TS-BC55-012."""
    ui_stub = _Step6(monkeypatch)
    s = state.SetupState()
    s.mode = "single-model"
    assert wizard._step6_single_model_profile(s) == 0
    assert ui_stub.selects[0]["default"] == "claude-native" and s.profile.name == "claude-native"


def test_a_non_interactive_run_prints_the_diff_and_the_command_and_never_switches(monkeypatch):
    """TS-BC55-013."""
    ui_stub = _Step6(monkeypatch, non_interactive=True)
    s = _multi("litellm", "measured-best")
    assert wizard._step6_profile(s) == 0
    assert s.profile.name == "measured-best"
    text = ui_stub.text
    assert "software-architect" in text and "deepseek/deepseek-v4-pro" in text and "anthropic/claude-opus-5.5" in text
    assert f"ai-resources setup --profile {RECOMMENDED}" in text
    assert not any("Show how" in m for m, _d in ui_stub.confirms)       # nothing was asked


def test_an_interactive_run_offers_the_diff_default_no_and_never_switches(monkeypatch):
    """D4: one informational question, default No; a Yes only shows the table."""
    ui_stub = _Step6(monkeypatch)
    s = _multi("litellm", "measured-best")
    assert wizard._step6_profile(s) == 0
    asked = [(m, d) for m, d in ui_stub.confirms if "Show how" in m]
    assert len(asked) == 1 and asked[0][1] is False
    assert "deepseek/deepseek-v4-pro" not in ui_stub.text.split("Profile: measured-best")[0]
    assert s.profile.name == "measured-best"

    yes = _Step6(monkeypatch, confirms={"Show how": True})
    s2 = _multi("litellm", "measured-best")
    assert wizard._step6_profile(s2) == 0
    assert "software-architect" in yes.text and f"ai-resources setup --profile {RECOMMENDED}" in yes.text
    assert s2.profile.name == "measured-best"


def test_no_notice_when_the_saved_profile_is_the_recommendation_or_was_requested(monkeypatch):
    ui_stub = _Step6(monkeypatch)
    s = _multi("litellm", RECOMMENDED)
    wizard._step6_profile(s)
    assert not any("Show how" in m for m, _d in ui_stub.confirms)
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "measured-best")
    again = _Step6(monkeypatch)
    s2 = _multi("litellm", "measured-best")
    wizard._step6_profile(s2)
    assert not any("Show how" in m for m, _d in again.confirms) and "ai-resources setup --profile" not in again.text


def test_step_6_hints_single_model_for_a_claude_only_host_with_no_gateway(monkeypatch):
    """D7."""
    ui_stub = _Step6(monkeypatch)
    wizard._step6_profile(_multi("litellm"))
    assert "Each on its own" in ui_stub.text and "no gateway" in ui_stub.text.lower()


def test_leaving_multi_model_resets_the_profile_to_the_single_model_default():
    """TS-BC55-014: the teardown reset keeps claude-native (it is not the recommended multi-model profile)."""
    assert wizard.SINGLE_MODEL_DEFAULT_PROFILE == "claude-native"
    assert wizard.RECOMMENDED_PROFILE != wizard.SINGLE_MODEL_DEFAULT_PROFILE
    src = pathlib.Path(wizard.__file__).read_text(encoding="utf-8")
    assert "state.ProfileState(name=SINGLE_MODEL_DEFAULT_PROFILE, customized=False)" in src


def test_a_state_that_never_chose_a_profile_runs_under_the_recommendation():
    """Readers of s.profile.name (apply, dry run, doctor) never see an empty or a stale default."""
    from ai_resources.setup import profiles
    assert profiles.saved_or_recommended("") == RECOMMENDED
    assert profiles.saved_or_recommended("measured-best") == "measured-best"
    assert profiles.RECOMMENDED_PROFILE == wizard.RECOMMENDED_PROFILE == RECOMMENDED
