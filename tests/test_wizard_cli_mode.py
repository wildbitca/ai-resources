"""The subscription team and the per-cockpit sign-in (ADR-0008, ADR-0007, ADR-0015).

Representation: `mode` stays `single-model` (so no gateway code path can engage) and the additive `SetupState.cli_team`
flag marks the team. Since ADR-0015 there is no global "how should they work" question: each ticked tool asks its OWN
sign-in, the old global fields are derived from those answers, and the gateway is not offered.
"""
from __future__ import annotations

import argparse

import pytest
import yaml

from ai_resources import cli
from ai_resources.setup import cockpit_auth, credentials, model_selection as ms, state, ui, wizard
from ai_resources.setup.cockpits import claude
from wizard_fakes import BACK, Script, Seq, host_env

SUB = "balanced"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def _ask_sign_in(monkeypatch, answer, s, **extra):
    script = Script(**{"How does Claude Code sign in": answer, **extra})
    script.install(monkeypatch)
    assert claude.ask_auth(ui, s, None, True) == 0
    return script


# --- the sign-in question (replaces the four-way mode question) ----------------------------------------
def test_the_question_lists_three_methods_and_no_gateway(monkeypatch):
    seen = {}
    s = state.SetupState()
    script = Script(**{"How does Claude Code sign in": lambda values: seen.setdefault("v", list(values)) and "subscription"})
    script.install(monkeypatch)
    claude.ask_auth(ui, s, None, True)
    assert seen["v"] == ["subscription", "api_key", "vertex_adc"]
    text = " ".join(script.titles).lower()
    assert "gateway" not in text and "litellm" not in text and "openrouter" not in text


def test_the_options_are_worded_for_subscriptions_keys_and_vertex():
    text = {k: f"{label} {line}" for k, label, line in claude.auth_options()}
    for needle in ("subscription", "no key is stored"):
        assert needle in text["subscription"], needle
    assert "never stores it" in text["api_key"] and "Application Default Credentials" in text["vertex_adc"]


def test_choosing_the_subscription_records_the_method_and_asks_nothing_else(monkeypatch):
    s = state.SetupState()
    script = _ask_sign_in(monkeypatch, "subscription", s)
    assert cockpit_auth.method(s, "claude") == "subscription"
    assert [m for m in script.asked if not m.startswith("select: How does Claude Code")] == []
    assert not any("gateway credential" in m or "OpenRouter" in m for m in script.shown)


def test_an_api_key_is_never_read_or_stored(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-SENTINEL-NOT-STORED")
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    wrote = []
    monkeypatch.setattr(credentials, "update_env_tracked", lambda updates: wrote.append(updates) or (None, []))
    s = state.SetupState()
    script = _ask_sign_in(monkeypatch, "api_key", s)
    assert cockpit_auth.method(s, "claude") == "api_key" and wrote == []
    assert "SENTINEL" not in script.transcript and "SENTINEL" not in yaml.safe_dump(state._to_dict(s))


def test_a_missing_api_key_is_a_warning_not_a_prompt(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    script = _ask_sign_in(monkeypatch, "api_key", state.SetupState())
    assert not any(m.startswith("password:") for m in script.asked)
    assert any("ANTHROPIC_API_KEY is not set" in m for m in script.shown)


def test_vertex_keeps_only_the_project_and_the_region(monkeypatch):
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    wrote = []
    monkeypatch.setattr(credentials, "update_env_tracked", lambda updates: wrote.append(dict(updates)) or (None, list(updates)))
    s = state.SetupState()
    monkeypatch.setattr(ui, "record_action", lambda msg: None)
    claude_dry = Script(**{"How does Claude Code sign in": "vertex_adc", "GOOGLE_CLOUD_PROJECT": "demo-project",
                           "GOOGLE_CLOUD_LOCATION": "global"})
    claude_dry.install(monkeypatch)
    assert claude.ask_auth(ui, s, None, False) == 0
    assert wrote == [{"GOOGLE_CLOUD_PROJECT": "demo-project", "GOOGLE_CLOUD_LOCATION": "global"}]
    assert s.tracking.env_keys_added == ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"]
    assert cockpit_auth.secret_findings(s.cockpit_auth) == []


def test_a_dry_run_writes_nothing_for_vertex(monkeypatch):
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    monkeypatch.setattr(credentials, "update_env_tracked", lambda updates: (_ for _ in ()).throw(AssertionError("wrote")))
    _ask_sign_in(monkeypatch, "vertex_adc", state.SetupState(), **{"GOOGLE_CLOUD_PROJECT": "p", "GOOGLE_CLOUD_LOCATION": "global"})


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


def test_the_gateway_steps_and_the_mode_question_are_gone():
    ids = {sp.id for sp in wizard._build_steps(False)}
    assert not {"mode", "gateway", "providers", "credentials", "lifecycle"} & ids
    assert "cockpit_auth" in ids


def test_the_sign_in_step_asks_only_for_the_ticked_tools_that_have_a_question():
    import types
    s = state.SetupState()
    c = types.SimpleNamespace(s=s)
    assert _spec("cockpit_auth").skip(c) == ("Sign-in", "")                    # nothing ticked: a silent skip
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    assert _spec("cockpit_auth").skip(c) is None
    assert wizard._auth_cockpits(s) == ["claude"]
    s.profile.customizations = {"__targets__": {"selected": ["claude", "codex", "agy", "openclaw", "cursor"]}}
    assert wizard._auth_cockpits(s) == ["claude", "openclaw", "agy", "cursor"]


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
    script = Script(**{"Use previous answers": True, "How does Claude Code sign in": "subscription",
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
    assert state.load().cli_team is True and state.load().cockpit_auth["claude"]["method"] == "subscription"


# --- lanes ---------------------------------------------------------------------------------------------
def test_lanes_use_the_subscription_profile_in_cli_mode(monkeypatch):
    s = state.SetupState()
    s.cli_team = True
    asked = {}
    real = wizard.profiles.load_profile

    def spy(name, mode=None):
        asked.setdefault("name", name)
        return real(name, mode)

    monkeypatch.setattr(wizard.profiles, "load_profile", spy)
    Script(**{"Orchestrator agent id": "main"}).install(monkeypatch, non_interactive=True)
    wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"]), dry_run=True)
    assert asked["name"] == SUB


# --- review --------------------------------------------------------------------------------------------
def test_review_names_the_sign_in_and_has_no_gateway_rows():
    s = state.SetupState()
    s.cli_team = True
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    cockpit_auth.set_method(s, "claude", "subscription")
    rows = dict(wizard._review_rows(s))
    assert rows["Sign-in"] == "Claude Code: subscription"
    assert not {"Gateway", "Providers", "API keys", "Start at login", "How"} & set(rows)


def test_review_of_a_plain_state_has_no_gateway_rows():
    rows = dict(wizard._review_rows(state.SetupState()))
    assert "Gateway" not in rows and rows["Sign-in"] == "none to ask"


# --- the gateway is never torn down by setup -------------------------------------------------------------
@pytest.mark.parametrize("mode,backend", [("multi-model", "litellm"), ("multi-model", "openrouter")])
def test_migrating_a_saved_gateway_state_never_runs_a_teardown(monkeypatch, mode, backend):
    from ai_resources.setup import litellm
    monkeypatch.setattr(litellm, "execute_multi_model_teardown", lambda p: (_ for _ in ()).throw(AssertionError("teardown")))
    monkeypatch.setattr(wizard, "_teardown_multi_model", lambda p, s: (_ for _ in ()).throw(AssertionError("teardown")))
    Script().install(monkeypatch)
    s = state.SetupState(mode=mode, backend=backend)
    s.last_run = "2026-01-01T00:00:00+00:00"
    wizard._migrate_cockpit_auth(s)
    assert (s.mode, s.cli_team, s.backend) == ("single-model", True, backend)
    assert cockpit_auth.method(s, "claude") == "subscription"


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
    assert "deprecated" in out.lower() and "subscription" in out.lower()


@pytest.mark.parametrize("backend", ["cli", "litellm", "openrouter"])
def test_every_backend_value_becomes_the_subscription_team_with_a_deprecation_note(monkeypatch, backend):
    monkeypatch.setattr(wizard.model_selection, "detect_accounts", lambda: {})
    monkeypatch.setattr(wizard.model_selection, "candidates", lambda a, f: [])
    monkeypatch.setattr(wizard.detection, "detect_all_cockpits", lambda: {})
    script = Script()
    script.install(monkeypatch)
    s = state.SetupState(mode="multi-model", backend="openrouter")
    assert wizard._preflight_selection(_args(backend=backend), s) == 0
    assert (s.mode, s.cli_team) == ("single-model", True)
    assert cockpit_auth.method(s, "claude") == "subscription"
    assert any(f"--backend {backend} is deprecated" in m for m in script.shown)


def test_an_unknown_backend_is_still_refused(monkeypatch):
    errors = []
    monkeypatch.setattr(ui, "error", errors.append)
    assert wizard._preflight_selection(_args(backend="nope"), state.SetupState()) == 2
    assert "litellm, openrouter or cli" in errors[0]


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


# --- ADR-0015: --backend answers Claude Code's sign-in, so that question is not asked --------------------------
@pytest.mark.parametrize("backend", ["cli", "litellm", "openrouter"])
def test_the_backend_flag_skips_the_sign_in_question(monkeypatch, backend):
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    cockpit_auth.set_method(s, "claude", "subscription")
    monkeypatch.setattr(wizard, "_AUTH_PREFILLED", {"claude"})
    script = Script()                              # nothing is scripted: any prompt would raise
    script.install(monkeypatch)
    assert wizard._step_cockpit_auth(s, _args(backend=backend)) == 0
    assert script.asked == []
    assert any("set by --backend" in m for m in script.shown)


def test_a_run_with_backend_cli_never_prompts_the_sign_in_and_records_the_cli_team(monkeypatch, tmp_path):
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
    assert not any("How does Claude Code sign in" in m for m in script.asked)
    assert (seen["s"].mode, seen["s"].cli_team) == ("single-model", True)


# --- the legacy answers, replayed through the migration --------------------------------------------------------
@pytest.mark.parametrize("legacy,expect", [
    (dict(), ("single-model", False, "litellm", "subscription")),
    (dict(cli_team=True), ("single-model", True, "litellm", "subscription")),
    (dict(mode="multi-model", backend="openrouter"), ("single-model", True, "openrouter", "subscription")),
    (dict(mode="multi-model", backend="litellm"), ("single-model", True, "litellm", "subscription")),
    (dict(cli_team=True, cli_team_api=True), ("single-model", True, "litellm", "api_key"))])
def test_each_legacy_answer_migrates_to_the_same_meaning(monkeypatch, legacy, expect):
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    Script().install(monkeypatch)
    s = state.SetupState(**legacy)
    s.last_run = "2026-01-01T00:00:00+00:00"
    wizard._migrate_cockpit_auth(s)
    mode, team, backend, method = expect
    # a never-answered "alone" state keeps cli_team False only until the derived flags run: the team is always on
    assert (s.mode, s.backend) == (mode, backend) and s.cli_team is True
    assert cockpit_auth.method(s, "claude") == method


def test_the_sign_in_default_is_the_saved_answer_and_a_fresh_host_starts_on_the_subscription(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)

    def default_for(s):
        script = Script(**{"How does Claude Code sign in": "subscription"})
        script.install(monkeypatch)
        claude.ask_auth(ui, s, None, True)
        return script.defaults[claude.AUTH_PROMPT][-1]
    assert default_for(state.SetupState()) == "subscription"
    s = state.SetupState()
    cockpit_auth.set_method(s, "claude", "vertex_adc")
    assert default_for(s) == "vertex_adc"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert default_for(state.SetupState()) == "api_key"


def test_back_at_the_team_asks_the_sign_in_again_and_leaks_nothing(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    script = Script(**{"Starting team": Seq(BACK, "custom"), "How do you want to choose models": "profile",
                       "How should kit subagents pick": lambda v: SUB, "Enable subscription lanes?": False,
                       "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    assert wizard.run(_args()) == 0
    assert len([m for m in script.asked if m.startswith("select: How does Claude Code sign in")]) == 2
    assert (seen["s"].mode, seen["s"].cli_team) == ("single-model", True)


# --- ADR-0010 R4: profiles that leave a checker on a writer's family warn under Custom ---------------------------------

def _run_single_profile_step(monkeypatch, pick):
    s = state.SetupState()
    s.cli_team = True
    warnings = []
    Script(**{"How should kit subagents pick": lambda v: pick}).install(monkeypatch)
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "warn", warnings.append)
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "", raising=False)
    monkeypatch.setattr(wizard, "_COMPOSE", {}, raising=False)
    assert wizard._step6_single_model_profile(s) == 0
    return warnings


def test_choosing_claude_inherit_under_custom_warns_that_the_checkers_are_not_independent(monkeypatch):
    warnings = _run_single_profile_step(monkeypatch, "claude-inherit")
    assert len(warnings) == 1 and "claude-inherit" in warnings[0] and "not independent" in warnings[0]


def test_choosing_a_clean_profile_under_custom_does_not_warn(monkeypatch):
    assert _run_single_profile_step(monkeypatch, SUB) == []


def test_the_warned_profiles_are_exactly_the_known_checker_violations():
    import test_profile_checker_independence as ind
    assert set(wizard.OPEN_CHECKER_PROFILES) == set(ind.KNOWN_VIOLATIONS)


def test_all_gemini_gets_the_same_warning(monkeypatch):
    warnings = []
    monkeypatch.setattr(ui, "warn", warnings.append)
    wizard._warn_open_checker_profile("all-gemini")
    assert len(warnings) == 1 and "security-auditor" in warnings[0] and "identical model" in warnings[0]
