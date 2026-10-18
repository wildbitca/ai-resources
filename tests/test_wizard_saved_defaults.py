"""On a re-run every question of the model picker offers the saved answer as its default."""
from __future__ import annotations

from ai_resources import selection as sel
from ai_resources.setup import model_selection as ms, ui, wizard


def _cands():
    mk = ms.Candidate
    return {"anthropic": [mk("anthropic", "sonnet", "claude-sonnet-5-5"), mk("anthropic", "opus", "claude-opus-5")],
            "openai": [mk("openai", "gpt", "gpt-5"), mk("openai", "mini", "gpt-5-mini")]}


def _saved():
    return sel.Selection(shape=ms.SHAPE_MANY_PROVIDERS, providers={"anthropic": sel.ProviderSel(), "openai": sel.ProviderSel()},
                         slots={"anthropic:opus": sel.SlotSel("anthropic/claude-opus-5"),
                                "openai:mini": sel.SlotSel("openai/gpt-5-mini")},
                         primary="openai:mini", smoke_path="direct")


def test_the_saved_models_are_the_defaults_of_every_checkbox(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: seen.append((msg, default)) or default)
    out = wizard._pick_candidates(ms.SHAPE_MANY_PROVIDERS, _cands(), _saved())
    assert seen[0] == ("Providers:", ["anthropic", "openai"])
    assert [c.slot for c in seen[1][1]] == ["anthropic:opus"] and [c.slot for c in seen[2][1]] == ["openai:mini"]
    assert [c.slot for c in out] == ["anthropic:opus", "openai:mini"]


def test_without_a_saved_selection_the_first_stable_model_is_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: seen.append(default) or "anthropic")
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: default)
    out = wizard._pick_candidates(ms.SHAPE_ONE_PROVIDER, _cands(), None)
    assert seen == [None] and [c.slot for c in out] == ["anthropic:sonnet"]


def test_one_provider_and_single_defaults_come_from_the_saved_selection(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: seen.append((msg, default)) or default)
    monkeypatch.setattr(ui, "checkbox", lambda msg, choices, default=None, **k: default)
    wizard._pick_candidates(ms.SHAPE_ONE_PROVIDER, _cands(), _saved())
    assert seen[-1] == ("Which provider?", "anthropic")
    wizard._pick_candidates(ms.SHAPE_SINGLE, _cands(), _saved())
    assert seen[-1][0] == "Which model?" and seen[-1][1].slot == "anthropic:opus"


# --- every other step offers the saved answer as its default -----------------------------------------------
import argparse  # noqa: E402

from ai_resources.setup import state  # noqa: E402


def _record(monkeypatch, picks=None, confirm=True, text=""):
    """Replace the ui prompts with recorders; returns {message: [defaults offered]}."""
    seen: dict[str, list] = {}
    picks = picks or {}

    def make(kind):
        def prompt(message, choices=None, default=None, **k):
            seen.setdefault(message, []).append(default)
            if message in picks:
                return picks[message]
            return default
        return prompt

    monkeypatch.setattr(ui, "select", make("select"))
    monkeypatch.setattr(ui, "checkbox", make("checkbox"))
    monkeypatch.setattr(ui, "confirm", lambda message, default=True: seen.setdefault(message, []).append(default) or default)
    monkeypatch.setattr(ui, "text", lambda message, default="", **k: seen.setdefault(message, []).append(default) or default)
    for name in ("info", "detail", "warn", "error", "ok", "section"):
        monkeypatch.setattr(ui, name, lambda *a, **k: None)
    return seen


def test_saved_mode_and_backend_are_the_default_of_the_mode_question(monkeypatch):
    """The saved mode and gateway start selected on screen 2 ("How should they work?", then "Which gateway?")."""
    for mode, backend, how, gateway in (("multi-model", "openrouter", "advanced", None),   # one gateway left: auto-picked, not asked (ADR-0010)
                                        ("multi-model", "litellm", "advanced", "litellm"),
                                        ("single-model", "litellm", "alone", None)):
        seen = _record(monkeypatch)
        s = state.SetupState()
        s.mode, s.backend = mode, backend
        wizard._step1_mode(s, is_first=False)
        assert seen[wizard.HOW_PROMPT] == [how]
        assert seen.get("Which gateway?") == ([gateway] if gateway else None)


def test_saved_gateway_bind_and_port_are_the_defaults(monkeypatch):
    seen = _record(monkeypatch)
    s = state.SetupState()
    s.litellm.local.bind_address, s.litellm.local.port = "0.0.0.0", 4123
    monkeypatch.setattr(wizard, "_validate_port", lambda v: True, raising=False)
    wizard._ask_bind_and_port(s)
    assert seen["Bind interface:"] == ["0.0.0.0"] and seen["Port"] == ["4123"]


def test_saved_lifecycle_choice_is_the_autostart_default(monkeypatch):
    for saved in (True, False):
        seen = _record(monkeypatch)
        s = state.SetupState()
        s.litellm.deployment = "local"
        s.litellm.local.runtime = "pipx"
        s.litellm.local.auto_start = saved
        monkeypatch.setattr(wizard.litellm, "is_container_mode", lambda r: False)
        monkeypatch.setattr(wizard.litellm, "is_macos", lambda: False)
        try:
            wizard._step8_lifecycle(s)
        except Exception:  # noqa: BLE001 - only the prompt default matters here
            pass
        assert seen["Auto-start LiteLLM at every login?"] == [saved]


def test_saved_tool_selection_is_the_default_of_the_tools_list():
    """The tools ticked in an earlier run (or named with --cockpits) start ticked on "Your tools" (ADR-0009: the
    step 7 checkbox became the first screen's list)."""
    import types
    detected = {cid: types.SimpleNamespace(installed=True) for cid in ("claude", "codex")}
    s = state.SetupState()
    s.profile.customizations["__targets__"] = {"selected": ["codex"]}
    assert wizard._default_members(s, detected) == ["codex"]
    s.profile.customizations.pop("__targets__")
    assert wizard._default_members(s, detected) == ["claude", "codex"]


def test_saved_profile_is_the_default_of_the_profile_question(monkeypatch):
    seen = _record(monkeypatch)
    avail = wizard.profiles.list_profiles(mode="multi-model", backend="litellm")
    saved = next(n for n in avail if n != wizard.RECOMMENDED_PROFILE)
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    s.profile.name = saved
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "", raising=False)
    try:
        wizard._step6_profile(s)
    except Exception:  # noqa: BLE001 - only the prompt default matters here
        pass
    assert seen["Choose profile:"] == [saved]


def test_saved_lane_orchestrators_are_reused_without_asking_again(monkeypatch):
    seen = _record(monkeypatch)
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "litellm"
    s.profile.lanes = {"orchestrators": ["main"]}
    args = argparse.Namespace(lanes=True, no_lanes=False, lanes_dry_run=False, lanes_orchestrator=[])
    try:
        wizard._step_lanes(s, args, dry_run=True)
    except Exception:  # noqa: BLE001
        pass
    assert not any("Orchestrator agent id" in m for m in seen)


# --- several candidates in one family ------------------------------------------------------------------
def _family_cands():
    mk = ms.Candidate
    return {"anthropic": [mk("anthropic", "opus", "claude-opus-5"), mk("anthropic", "opus", "claude-opus-4"),
                          mk("anthropic", "opus", "claude-opus-6-beta", channel="preview")]}


def _saved_ref(ref):
    return sel.Selection(shape=ms.SHAPE_SINGLE, providers={"anthropic": sel.ProviderSel()},
                         slots={"anthropic:opus": sel.SlotSel(ref)}, primary="anthropic:opus")


def test_only_the_exact_saved_model_of_a_family_is_pre_checked():
    held = wizard._saved_defaults(_family_cands()["anthropic"], _saved_ref("anthropic/claude-opus-4"))
    assert [c.id for c in held] == ["claude-opus-4"]


def test_a_saved_model_missing_from_the_list_falls_back_to_one_stable_model_of_its_slot():
    held = wizard._saved_defaults(_family_cands()["anthropic"], _saved_ref("anthropic/claude-opus-3"))
    assert [c.id for c in held] == ["claude-opus-5"]


def test_a_preview_is_never_pre_checked(monkeypatch):
    held = wizard._saved_defaults(_family_cands()["anthropic"], _saved_ref("anthropic/claude-opus-6-beta"))
    assert [c.id for c in held] == ["claude-opus-5"]


def test_single_shape_defaults_to_the_saved_model_not_the_first_one(monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: seen.append(default) or default)
    wizard._pick_candidates(ms.SHAPE_SINGLE, _family_cands(), _saved_ref("anthropic/claude-opus-4"))
    assert seen[-1].id == "claude-opus-4"
