"""The explicit "multi-model via your CLI subscriptions" mode (ADR-0008, ADR-0007).

Representation: `mode` stays `single-model` (so no gateway code path can engage) and the additive
`SetupState.cli_team` flag marks the subscription team. Old state files carry no such key and load unchanged.
"""
from __future__ import annotations

import argparse

import pytest
import yaml

from ai_resources import cli
from ai_resources.setup import model_selection as ms, state, ui, wizard
from wizard_fakes import BACK, Script, Seq, host_env

SUB = "subscription-claude-55"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def _ask_mode(monkeypatch, answer, s):
    script = Script(**{"Choose mode": answer})
    script.install(monkeypatch)
    assert wizard._step1_mode(s) == 0
    return script


def test_the_question_lists_three_ways_with_the_team_first(monkeypatch):
    seen = {}
    s = state.SetupState()
    script = Script(**{"Pick how they work": lambda values: seen.setdefault("v", list(values)) and "alone"})
    script.install(monkeypatch)
    wizard._step1_mode(s)
    assert seen["v"] == ["team", "alone", "advanced"]


def test_advanced_asks_which_gateway(monkeypatch):
    seen = {}
    s = state.SetupState()
    script = Script(**{"Pick how they work": "advanced",
                       "Which gateway": lambda values: seen.setdefault("v", list(values)) and "openrouter"})
    script.install(monkeypatch)
    wizard._step1_mode(s)
    assert seen["v"] == ["litellm", "openrouter"] and (s.mode, s.backend, s.cli_team) == (
        "multi-model", "openrouter", False)
    assert [m for m in script.asked if m.startswith("select")] == [
        "select: Pick how they work together:", "select: Which gateway?"]


def test_the_team_option_is_worded_for_subscriptions_without_a_gateway(monkeypatch):
    labels = {}

    def pick(message, choices, default=None, **k):
        labels.update({c.value: c.title if hasattr(c, "title") else str(c) for c in choices})
        return "alone"

    monkeypatch.setattr(ui, "select", pick)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    wizard._step1_mode(state.SetupState())
    text = labels["team"]
    for needle in ("As a team", "subscriptions", "no API keys", "each role"):
        assert needle in text, needle
    assert "Each on its own" in labels["alone"] and "own sign-in" in labels["alone"]
    assert "Advanced" in labels["advanced"] and "gateway" in labels["advanced"] and "API keys" in labels["advanced"]
    for label in labels.values():
        assert "cockpit" not in label.lower() and "single-model" not in label


def test_choosing_cli_sets_the_state_and_shows_no_gateway_warning(monkeypatch):
    s = state.SetupState()
    script = _ask_mode(monkeypatch, "multi-model:cli", s)
    assert s.mode == "single-model" and s.cli_team is True
    assert not any("gateway credential" in m or "billed per token to the gateway" in m for m in script.shown)
    assert not any("OpenRouter" in m for m in script.shown)


@pytest.mark.parametrize("pick,backend", [("single-model", None), ("multi-model:openrouter", "openrouter"),
                                          ("multi-model:litellm", "litellm")])
def test_every_other_choice_clears_the_cli_flag(monkeypatch, pick, backend):
    s = state.SetupState()
    s.cli_team = True
    _ask_mode(monkeypatch, pick, s)
    assert s.cli_team is False
    if backend:
        assert s.mode == "multi-model" and s.backend == backend


def test_gateway_choices_keep_the_warning(monkeypatch):
    s = state.SetupState()
    script = _ask_mode(monkeypatch, "multi-model:litellm", s)
    assert any("gateway credential" in m for m in script.shown)


def test_a_saved_cli_state_is_the_default_of_the_question(monkeypatch):
    s = state.SetupState()
    s.cli_team = True
    script = _ask_mode(monkeypatch, "multi-model:cli", s)
    assert script.defaults[wizard.HOW_PROMPT] == ["team"]


def test_a_saved_gateway_or_single_state_keeps_its_default(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="openrouter")
    script = _ask_mode(monkeypatch, "multi-model:openrouter", s)
    assert script.defaults[wizard.HOW_PROMPT] == ["advanced"]
    assert script.defaults["Which gateway?"] == ["openrouter"]
    s2 = state.SetupState()
    script2 = _ask_mode(monkeypatch, "single-model", s2)
    assert script2.defaults[wizard.HOW_PROMPT] == ["alone"]


# --- saved state -------------------------------------------------------------------------------------
def test_old_state_file_has_no_cli_key_and_round_trips_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    state.save(s)
    doc = yaml.safe_load((tmp_path / "setup-state.yaml").read_text())
    assert "cli_team" not in doc
    loaded = state.load()
    assert loaded.cli_team is False and loaded.mode == "single-model"
    state.save(loaded)
    again = yaml.safe_load((tmp_path / "setup-state.yaml").read_text())
    again["last_run"] = doc["last_run"]
    assert again == doc


def test_a_gateway_state_without_the_key_keeps_its_meaning(tmp_path, monkeypatch):
    path = tmp_path / "setup-state.yaml"
    monkeypatch.setattr(state, "state_path", lambda: path)
    path.write_text(yaml.safe_dump({"mode": "multi-model", "backend": "openrouter"}))
    loaded = state.load()
    assert (loaded.mode, loaded.backend, loaded.cli_team) == ("multi-model", "openrouter", False)


def test_the_cli_flag_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.cli_team = True
    state.save(s)
    assert state.load().cli_team is True


# --- steps ---------------------------------------------------------------------------------------------
def _spec(step_id):
    return next(sp for sp in wizard._build_steps(False) if sp.id == step_id)


def test_gateway_provider_credential_and_lifecycle_steps_are_skipped():
    import types
    s = state.SetupState()
    s.cli_team = True
    c = types.SimpleNamespace(s=s)
    for step_id in ("gateway", "providers", "credentials", "lifecycle"):
        assert _spec(step_id).skip(c) is not None, step_id
    assert _spec("profile").skip(c) is None and _spec("lanes").skip is None   # no preset answered here


def test_non_gateway_paths_skip_the_gateway_steps_without_a_word(monkeypatch):
    """ADR-0009: steps 3, 4, 5 and 8 print nothing on a path with no gateway; the ten steps stay."""
    import types
    from ai_resources.setup import flow
    assert wizard.TOTAL_STEPS == 10 and len(flow.STEP_LABELS) == 10
    for cli_team in (False, True):
        s = state.SetupState()
        s.cli_team = cli_team
        c = types.SimpleNamespace(s=s)
        for step_id in ("gateway", "providers", "credentials", "lifecycle"):
            assert _spec(step_id).skip(c)[1] == "", (cli_team, step_id)
    shown = []
    monkeypatch.setattr(ui, "section", lambda *a: shown.append(a))
    monkeypatch.setattr(ui, "info", shown.append)
    wizard._report_skip(_spec("gateway"), *_spec("gateway").skip(types.SimpleNamespace(s=state.SetupState())))
    assert shown == []


def test_a_gateway_path_that_skips_a_step_still_says_why():
    import types
    s = state.SetupState(mode="multi-model", backend="openrouter")
    c = types.SimpleNamespace(s=s)
    assert "OpenRouter is hosted" in _spec("gateway").skip(c)[1]
    assert "no local gateway" in _spec("lifecycle").skip(c)[1]
    assert _spec("providers").skip(c) is None and _spec("credentials").skip(c) is None


def test_the_profile_step_defaults_to_the_subscription_team(monkeypatch):
    s = state.SetupState()
    s.cli_team = True
    script = Script(**{"How should kit subagents pick": lambda v: SUB})
    script.install(monkeypatch)
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "", raising=False)
    monkeypatch.setattr(wizard, "_COMPOSE", {}, raising=False)
    assert wizard._step6_single_model_profile(s) == 0
    assert script.defaults["How should kit subagents pick their Claude model?"] == [SUB]
    assert s.profile.name == SUB


def test_a_plain_single_model_state_still_defaults_to_claude_native(monkeypatch):
    s = state.SetupState()
    script = Script(**{"How should kit subagents pick": lambda v: v[0]})
    script.install(monkeypatch)
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "", raising=False)
    monkeypatch.setattr(wizard, "_COMPOSE", {}, raising=False)
    wizard._step6_single_model_profile(s)
    assert script.defaults["How should kit subagents pick their Claude model?"] == [wizard.SINGLE_MODEL_DEFAULT_PROFILE]


def test_no_key_is_asked_in_cli_mode(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    script = Script(**{"Use previous answers": True, "Choose mode": "multi-model:cli",
                       "Model preset": "custom", "How do you want to choose models": "profile",
                       "How should kit subagents pick": lambda v: SUB,
                       "Enable subscription lanes?": False,
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    s = state.SetupState()
    s.last_run = "2026-01-01T00:00:00+00:00"
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    wizard.run(_args(non_interactive=False))
    assert not any(m.startswith("password:") for m in script.asked)
    assert not any("gateway credential" in m for m in script.shown)
    assert state.load().cli_team is True


# --- lanes ---------------------------------------------------------------------------------------------
def test_lanes_use_the_subscription_profile_in_cli_mode(monkeypatch):
    s = state.SetupState()
    s.cli_team = True
    asked = {}
    real = wizard.profiles.load_profile

    def spy(name):
        asked.setdefault("name", name)
        return real(name)

    monkeypatch.setattr(wizard.profiles, "load_profile", spy)
    Script(**{"Orchestrator agent id": "main"}).install(monkeypatch, non_interactive=True)
    wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"]), dry_run=True)
    assert asked["name"] == SUB


# --- review --------------------------------------------------------------------------------------------
def test_review_names_the_team_and_has_no_gateway_rows():
    s = state.SetupState()
    s.cli_team = True
    rows = dict(wizard._review_rows(s))
    assert rows["How"] == "As a team, on your subscriptions (no gateway)"
    assert not {"Gateway", "Providers", "API keys", "Start at login"} & set(rows)


def test_review_of_plain_single_model_is_unchanged():
    rows = dict(wizard._review_rows(state.SetupState()))
    assert rows["How"] == "Each on its own (no gateway)" and "Gateway" not in rows


# --- teardown -------------------------------------------------------------------------------------------
def _switch(monkeypatch, prev, picked):
    calls = []
    monkeypatch.setattr(wizard, "_teardown_multi_model", lambda p, s: calls.append((p, s)) or 0)
    Script(**{"Choose mode": picked}).install(monkeypatch)
    import types
    s = state.SetupState(mode=prev.mode, backend=prev.backend)
    s.cli_team = prev.cli_team
    c = types.SimpleNamespace(s=s, prev_s=prev, is_first=False)
    assert wizard._run_mode_step(c) == 0
    return calls


def test_gateway_to_cli_offers_the_cleanup(monkeypatch):
    prev = state.SetupState(mode="multi-model", backend="litellm")
    assert len(_switch(monkeypatch, prev, "multi-model:cli")) == 1


def test_cli_and_single_model_never_touch_gateway_artifacts(monkeypatch):
    cli_prev = state.SetupState()
    cli_prev.cli_team = True
    assert _switch(monkeypatch, cli_prev, "single-model") == []
    assert _switch(monkeypatch, state.SetupState(), "multi-model:cli") == []
    assert _switch(monkeypatch, cli_prev, "multi-model:cli") == []


def test_teardown_into_cli_keeps_the_flag_and_picks_the_subscription_profile(monkeypatch):
    prev = state.SetupState(mode="multi-model", backend="litellm")
    s = state.SetupState()
    s.cli_team = True
    monkeypatch.setattr(wizard.litellm, "plan_multi_model_teardown", lambda p: [])
    assert wizard._teardown_multi_model(prev, s) == 0
    assert s.cli_team is True


def test_teardown_reset_uses_the_subscription_profile_in_cli_mode(monkeypatch):
    prev = state.SetupState(mode="multi-model", backend="litellm")
    s = state.SetupState()
    s.cli_team = True
    monkeypatch.setattr(wizard.litellm, "plan_multi_model_teardown", lambda p: [("a", "do a")])
    monkeypatch.setattr(wizard.litellm, "execute_multi_model_teardown", lambda p: [("a", True, "ok")])
    Script(**{"Proceed with cleanup": True}).install(monkeypatch)
    wizard._teardown_multi_model(prev, s)
    assert s.profile.name == SUB and s.cli_team is True


# --- flags ----------------------------------------------------------------------------------------------
def test_backend_cli_is_a_valid_flag_and_documented(capsys):
    parser = cli.build_parser()
    ns = parser.parse_args(["setup", "--backend", "cli"])
    assert ns.backend == "cli"
    with pytest.raises(SystemExit):
        parser.parse_args(["setup", "--help"])
    out = capsys.readouterr().out
    assert "cli" in out and "subscription" in out.lower()


def test_backend_cli_sets_the_cli_team_without_a_gateway(monkeypatch):
    monkeypatch.setattr(wizard.model_selection, "detect_accounts", lambda: {})
    monkeypatch.setattr(wizard.model_selection, "candidates", lambda a, f: [])
    monkeypatch.setattr(wizard.detection, "detect_all_cockpits", lambda: {})
    s = state.SetupState(mode="multi-model", backend="openrouter")
    assert wizard._preflight_selection(_args(backend="cli"), s) == 0
    assert (s.mode, s.cli_team) == ("single-model", True)


def test_backend_gateway_flag_clears_the_cli_flag(monkeypatch):
    monkeypatch.setattr(wizard.model_selection, "detect_accounts", lambda: {})
    monkeypatch.setattr(wizard.model_selection, "candidates", lambda a, f: [])
    monkeypatch.setattr(wizard.detection, "detect_all_cockpits", lambda: {})
    s = state.SetupState()
    s.cli_team = True
    assert wizard._preflight_selection(_args(backend="litellm"), s) == 0
    assert (s.mode, s.backend, s.cli_team) == ("multi-model", "litellm", False)


def test_an_unknown_backend_is_still_refused(monkeypatch):
    errors = []
    monkeypatch.setattr(ui, "error", errors.append)
    assert wizard._preflight_selection(_args(backend="nope"), state.SetupState()) == 2
    assert "litellm, openrouter or cli" in errors[0]


# --- saved claude-native profile and interactive lanes ---------------------------------------------------
def test_picking_cli_replaces_a_saved_claude_native_profile(monkeypatch):
    s = state.SetupState()
    s.profile = state.ProfileState(name=wizard.SINGLE_MODEL_DEFAULT_PROFILE)
    _ask_mode(monkeypatch, "multi-model:cli", s)
    assert s.cli_team is True and s.profile.name == SUB


def test_picking_cli_keeps_a_customized_profile(monkeypatch):
    s = state.SetupState()
    s.profile = state.ProfileState(name=wizard.SINGLE_MODEL_DEFAULT_PROFILE, customized=True)
    _ask_mode(monkeypatch, "multi-model:cli", s)
    assert s.profile.name == wizard.SINGLE_MODEL_DEFAULT_PROFILE


def test_cli_mode_does_not_ask_about_parallel_runs_on_the_default_path(monkeypatch):
    s = state.SetupState()
    s.cli_team = True
    s.profile = state.ProfileState(name=SUB)
    asked = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: asked.append((m, default)) or False)
    assert wizard._step_lanes(s, _args()) == 0
    assert asked == []                       # Review > More options > Parallel runs is where they are asked for
    assert s.profile.lanes is None


# --- ADR-0009: --backend decides the mode, so the mode question is not asked -------------------------------
@pytest.mark.parametrize("backend", ["cli", "litellm", "openrouter"])
def test_the_backend_flag_skips_the_mode_question(monkeypatch, backend):
    s = state.SetupState()
    if backend == "cli":
        s.mode, s.cli_team = "single-model", True
    else:
        s.mode, s.backend = "multi-model", backend
    script = Script()                              # nothing is scripted: any prompt would raise
    script.install(monkeypatch)
    assert wizard._step1_mode(s, _args(backend=backend)) == 0
    assert script.asked == []
    assert any(f"--backend {backend}" in m for m in script.shown)


def test_the_backend_flag_keeps_the_gateway_warning(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="openrouter")
    script = Script()
    script.install(monkeypatch)
    wizard._step1_mode(s, _args(backend="openrouter"))
    assert any("gateway credential" in m for m in script.shown)
    assert any("OpenRouter key" in m for m in script.shown)
    quiet = Script()
    quiet.install(monkeypatch)
    wizard._step1_mode(state.SetupState(), _args(backend="cli"))
    assert not any("gateway credential" in m for m in quiet.shown)


def test_a_run_with_backend_cli_never_prompts_the_mode_and_records_the_cli_team(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    script = Script(**{"Model preset": "custom", "How do you want to choose models": "profile",
                       "How should kit subagents pick": lambda v: SUB, "Enable subscription lanes?": False,
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    monkeypatch.setattr(wizard.model_selection, "detect_accounts", lambda: {})
    monkeypatch.setattr(wizard.model_selection, "candidates", lambda a, f: [])
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    assert wizard.run(_args(backend="cli")) == 0
    assert not any("Choose mode" in m or "How should they work" in m for m in script.asked)
    assert (seen["s"].mode, seen["s"].cli_team) == ("single-model", True)


# --- ADR-0009 S6: screen 2, "How should they work?" ------------------------------------------------------------
@pytest.mark.parametrize("legacy,expect", [
    ("single-model", ("single-model", False, "litellm")),
    ("multi-model:cli", ("single-model", True, "litellm")),
    ("multi-model:openrouter", ("multi-model", False, "openrouter")),
    ("multi-model:litellm", ("multi-model", False, "litellm"))])
def test_each_legacy_answer_replayed_through_screen_two_saves_the_same_state(monkeypatch, legacy, expect):
    s = state.SetupState()
    _ask_mode(monkeypatch, legacy, s)
    assert (s.mode, s.cli_team, s.backend) == expect


def test_the_default_is_team_with_two_tools_each_alone_with_one_and_a_saved_answer_wins(monkeypatch):
    def default_for(s, **kw):
        script = Script(**{"Pick how they work": "alone"})
        script.install(monkeypatch)
        wizard._step1_mode(s, **kw)
        return script.defaults[wizard.HOW_PROMPT][-1]

    def with_tools(*ids):
        s = state.SetupState()
        s.profile.customizations = {"__targets__": {"selected": list(ids)}}
        return s
    assert default_for(with_tools("claude", "codex")) == "team"
    assert default_for(with_tools("claude")) == "alone"
    assert default_for(with_tools("claude", "codex"), is_first=False) == "alone"      # a saved answer is kept
    s = with_tools("claude", "codex")
    s.cli_team = True
    assert default_for(s, is_first=False) == "team"
    assert default_for(state.SetupState(mode="multi-model", backend="litellm"), is_first=False) == "advanced"


def test_back_at_screen_two_asks_it_again_and_leaks_nothing(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    script = Script(**{"Pick how they work": Seq(BACK, "team"), "Model preset": "custom",
                       "How do you want to choose models": "profile", "How should kit subagents pick": lambda v: SUB,
                       "Enable subscription lanes?": False, "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    assert wizard.run(_args()) == 0
    assert len([m for m in script.asked if m.startswith("select: Pick how they work")]) == 2
    assert (seen["s"].mode, seen["s"].cli_team) == ("single-model", True)
