"""The step 6 preset question: Balanced, Performance, Low pricing or Custom (ADR-0008 addendum).

One plain question comes first. A preset resolves to a kit profile by mode and backend and never asks for a
provider, a vendor or a model. Custom is the earlier flow, unchanged. Everything here is offline: the ui is
the scripted fake of `wizard_fakes`."""
from __future__ import annotations

import argparse

import pytest

from ai_resources import selection as selection_mod
from ai_resources.setup import compat, profiles, state, ui, wizard
from test_profile_validity import CELLS, check_cell, profile_refs
from wizard_fakes import BACK, Script, Seq

ASK = "Model preset:"
CONFIRM = "Use this?"
KEYS = ("balanced", "performance", "low-pricing")

# mode key -> (state setter, expected profile per preset)
TABLE = {
    "cli": {"balanced": "subscription-claude-55", "performance": "subscription-performance",
            "low-pricing": "subscription-economy"},
    "single": {"balanced": "claude-native", "performance": "subscription-performance",
               "low-pricing": "subscription-economy"},
    "litellm": {"balanced": "balanced-claude-55", "performance": "performance-claude-55",
                "low-pricing": "economy-claude-55"},
    "openrouter": {"balanced": "balanced-claude-55", "performance": "performance-claude-55",
                   "low-pricing": "economy-claude-55"},
}


def make_state(key: str) -> state.SetupState:
    s = state.SetupState()
    if key == "cli":
        s.mode, s.cli_team = "single-model", True
    elif key == "single":
        s.mode = "single-model"
    else:
        s.mode, s.backend = "multi-model", key
    return s


def args(**kw):
    ns = dict(profile="", models="", shape="", providers="", compose="")
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture(autouse=True)
def clean_globals(monkeypatch):
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "")
    monkeypatch.setattr(wizard, "_COMPOSE", {})
    monkeypatch.setattr(wizard, "_PRESET", "")
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "role_table", lambda rows, title="": None)


def drive(monkeypatch, s, answer, a=None, confirm=True, non_interactive=False):
    shown = []
    monkeypatch.setattr(ui, "role_table", lambda rows, title="": shown.append((title, rows)))
    script = Script(**{ASK: answer, CONFIRM: confirm})
    script.install(monkeypatch, non_interactive=non_interactive)
    rc = wizard._step_preset(s, a or args())
    return rc, script, shown


# --- the question ----------------------------------------------------------------------------------------

def test_the_question_has_exactly_four_choices_in_order_with_plain_labels(monkeypatch):
    seen = {}

    def grab(values):
        seen["values"] = list(values)
        return "balanced"
    monkeypatch.setattr(ui, "select", lambda m, choices, default=None, **k: (
        seen.update(titles=[c.title for c in choices]) or grab([c.value for c in choices])))
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: True)
    wizard._step_preset(make_state("litellm"), args())
    assert seen["values"] == ["balanced", "performance", "low-pricing", "custom"]
    heads = [t.split(" - ")[0] for t in seen["titles"]]
    assert heads == ["Balanced (recommended)", "Performance", "Low pricing", "Custom"]
    for title in seen["titles"]:
        assert " - " in title and "\n" not in title and len(title) < 140
        assert not any(w in title.lower() for w in ("claude", "gemini", "openai", "anthropic"))


def test_the_preset_table_is_exposed_and_matches(monkeypatch):
    for key, row in TABLE.items():
        s = make_state(key)
        for preset, name in row.items():
            assert wizard.preset_profile(s, preset) == name


# --- every mapped profile exists, loads, is offered for its mode and is valid ---------------------------

@pytest.mark.parametrize("key", sorted(TABLE))
def test_every_preset_maps_to_an_offered_profile_of_the_right_mode(key):
    s = make_state(key)
    mode = "single-model" if key in ("cli", "single") else "multi-model"
    backend = key if mode == "multi-model" else None
    offered = profiles.list_profiles(mode=mode, backend=backend)
    names = [wizard.preset_profile(s, p) for p in KEYS]
    assert len(set(names)) == 3, "three presets must differ so a saved profile maps back to one preset"
    for name in names:
        assert name in offered, (key, name)
        doc = profiles.load_profile(name)
        assert [r for r in profiles.KNOWN_ROLES if not (doc["by_role"].get(r) or {}).get("model")] == []
        executors = profiles.to_executors(doc, backend) if backend else profiles.to_executors(doc)
        assert profiles.role_table(executors)


GATEWAY_PRESET_PROFILES = sorted({n for k in ("litellm", "openrouter") for n in wizard.PRESET_PROFILES[k].values()})


@pytest.mark.parametrize("name", GATEWAY_PRESET_PROFILES)
@pytest.mark.parametrize("cockpit,mode,backend,engine,want", CELLS)
def test_every_gateway_preset_profile_is_verified_in_every_reachable_cell(name, cockpit, mode, backend, engine, want):
    check_cell(name, cockpit, mode, backend, engine, want)


@pytest.mark.parametrize("name", ["performance-claude-55", "economy-claude-55"])
@pytest.mark.parametrize("cockpit,mode,backend,engine,want", CELLS)
def test_the_new_gateway_profiles_are_verified_in_every_reachable_cell(name, cockpit, mode, backend, engine, want):
    check_cell(name, cockpit, mode, backend, engine, want)


@pytest.mark.parametrize("name", ["performance-claude-55", "economy-claude-55"])
def test_the_new_gateway_profiles_are_claude_only_agnostic_and_keep_checkers_off_haiku(name):
    from ai_resources import model_providers
    doc = profiles.load_profile(name)
    assert doc["mode"] == "multi-model" and "backend" not in doc
    assert {model_providers.identify(r).provider for r in profile_refs(doc)} == {"anthropic"}
    by_role = doc["by_role"]
    assert set(by_role) == set(profiles.KNOWN_ROLES)
    for role in ("verifier", "code-reviewer", "security-auditor", "planner", "software-architect", "implementer"):
        assert "haiku" not in by_role[role]["model"], role
    for backend in ("litellm", "openrouter"):
        assert name in profiles.list_profiles(mode="multi-model", backend=backend)


@pytest.mark.parametrize("name", ["subscription-performance", "subscription-economy", "economy-claude-55",
                                  "performance-claude-55"])
def test_the_new_profiles_keep_checkers_independent_of_writers(name):
    import re
    doc = profiles.load_profile(name)
    assert set(doc["by_role"]) == set(profiles.KNOWN_ROLES)
    if name.startswith("subscription"):
        assert doc["mode"] == "single-model"

    def family(ref):
        return re.search(r"(haiku|sonnet|opus)", ref).group(1)
    by_role = doc["by_role"]
    non_writers = {"verifier", "security-auditor", "explore", "planner", "software-architect", "code-reviewer"}
    writers = {family(c["model"]) for r, c in by_role.items() if r not in non_writers}
    checkers = {family(by_role[r]["model"]) for r in ("verifier", "security-auditor")}
    assert not checkers & writers
    for r in ("verifier", "security-auditor"):
        assert not by_role[r].get("fallbacks")
        assert "haiku" not in by_role[r]["model"]
    assert family(by_role["verifier"]["model"]) != family(by_role["implementer"]["model"])
    if not name.startswith("subscription"):
        return
    # aliases only, including fallbacks and classes
    refs = [c["model"] for c in by_role.values()] + [f for c in by_role.values() for f in c.get("fallbacks") or []]
    assert set(refs + list(doc["classes"].values())) <= {"haiku", "sonnet", "opus"}
    for key in ("backend", "agentId", "effort"):
        assert key not in doc


@pytest.mark.parametrize("name", ["subscription-performance", "subscription-economy"])
def test_the_new_subscription_profiles_propose_lanes_without_a_conflict(name):
    from ai_resources.setup import lanes_render
    doc = profiles.load_profile(name)
    assert lanes_render.validate_block(doc["lanes"]) == []
    by_role = profiles.to_executors(doc)["by_role"]
    proposal = lanes_render.propose(doc, by_role, "single-model", claude_ids={
        "haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"})
    assert proposal["roles"]


def test_performance_costs_more_than_balanced_and_economy_less_on_the_subscription_team():
    rank = {"haiku": 1, "sonnet": 2, "opus": 3}

    def total(name):
        return sum(rank[c["model"]] for c in profiles.load_profile(name)["by_role"].values())
    assert total("subscription-economy") < total("subscription-claude-55") < total("subscription-performance")


# --- the path of a preset: two prompts, a role table, no provider/vendor/model question ------------------

@pytest.mark.parametrize("key", sorted(TABLE))
@pytest.mark.parametrize("preset", KEYS)
def test_a_preset_asks_only_the_preset_and_the_confirmation(monkeypatch, key, preset):
    s = make_state(key)
    rc, script, shown = drive(monkeypatch, s, preset)
    assert rc == 0
    assert script.asked == [f"select: {ASK}", f"confirm: {CONFIRM}"]
    assert s.profile.name == TABLE[key][preset]
    assert s.profile.customized is False and s.profile.composed is None
    assert s.get_selection() is None
    assert wizard._PRESET == preset
    assert len(shown) == 1 and shown[0][1], "exactly the resulting role table"
    asked = " ".join(script.asked).lower()
    for word in ("provider", "vendor", "model?", "primary", "customize any role", "choose profile"):
        assert word not in asked


def test_the_role_table_is_the_profiles_own_mapping(monkeypatch):
    s = make_state("litellm")
    _, _, shown = drive(monkeypatch, s, "performance")
    want = profiles.role_table(profiles.to_executors(profiles.load_profile("performance-claude-55"), "litellm"))
    assert shown[0][1] == want


def test_a_preset_replaces_a_stale_account_selection_and_keeps_the_cockpit_targets(monkeypatch):
    s = make_state("litellm")
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}, "planner": {"model": "x/y"}}
    s.profile.customized = True
    s.profile.name = "quality-first"
    drive(monkeypatch, s, "balanced")
    assert s.profile.customizations == {"__targets__": {"selected": ["claude"]}}
    assert s.profile.customized is False and s.profile.name == "balanced-claude-55"


def test_use_this_no_returns_to_the_preset_question(monkeypatch):
    s = make_state("litellm")
    script = Script(**{ASK: Seq("performance", "low-pricing"), CONFIRM: Seq(False, True)})
    script.install(monkeypatch)
    assert wizard._step_preset(s, args()) == 0
    assert script.asked == [f"select: {ASK}", f"confirm: {CONFIRM}", f"select: {ASK}", f"confirm: {CONFIRM}"]
    assert s.profile.name == "economy-claude-55" and wizard._PRESET == "low-pricing"


def test_use_this_no_then_custom_reaches_the_old_flow(monkeypatch):
    s = make_state("litellm")
    script = Script(**{ASK: Seq("performance", "custom"), CONFIRM: False})
    script.install(monkeypatch)
    assert wizard._step_preset(s, args()) == 0
    assert wizard._PRESET == ""
    assert s.profile.name == ""           # nothing is decided by the preset question


def test_ctrl_b_at_either_prompt_goes_back_and_ctrl_c_cancels(monkeypatch):
    for answers in ({ASK: BACK, CONFIRM: True}, {ASK: "balanced", CONFIRM: BACK}):
        script = Script(**answers)
        script.install(monkeypatch)
        with pytest.raises(ui.GoBack):
            wizard._step_preset(make_state("single"), args())
    script = Script(**{ASK: None, CONFIRM: True})
    script.install(monkeypatch)
    assert wizard._step_preset(make_state("single"), args()) == 130


# --- Custom is the earlier flow ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(TABLE))
def test_custom_asks_nothing_more_and_leaves_the_old_steps_running(monkeypatch, key):
    s = make_state(key)
    rc, script, shown = drive(monkeypatch, s, "custom")
    assert rc == 0 and script.asked == [f"select: {ASK}"] and shown == []
    assert wizard._PRESET == ""
    steps = {sp.id: sp for sp in wizard._build_steps(False)}
    ctx = argparse.Namespace(s=s)
    assert steps["models"].skip(ctx) is None and steps["profile"].skip(ctx) is None


@pytest.mark.parametrize("key", sorted(TABLE))
def test_a_preset_skips_the_old_steps_silently(monkeypatch, key):
    s = make_state(key)
    drive(monkeypatch, s, "balanced")
    steps = {sp.id: sp for sp in wizard._build_steps(False)}
    ctx = argparse.Namespace(s=s)
    for step_id in ("models", "profile"):
        skip = steps[step_id].skip(ctx)
        assert skip is not None and skip[1] == "", "a preset skip prints nothing"
    assert steps["lanes"].skip is None


# --- saved choices are the default, never silently replaced ------------------------------------------------

def default_of(monkeypatch, s):
    _, script, _ = drive(monkeypatch, s, "balanced")
    return script.defaults[ASK][0]


@pytest.mark.parametrize("key", sorted(TABLE))
def test_a_fresh_host_defaults_to_balanced(monkeypatch, key):
    assert default_of(monkeypatch, make_state(key)) == "balanced"


@pytest.mark.parametrize("key", sorted(TABLE))
@pytest.mark.parametrize("preset", KEYS)
def test_a_saved_preset_profile_defaults_to_its_preset(monkeypatch, key, preset):
    s = make_state(key)
    s.profile.name = TABLE[key][preset]
    assert default_of(monkeypatch, s) == preset


@pytest.mark.parametrize("saved", ["quality-first", "measured-best", "all-gemini", "claude-inherit"])
def test_another_saved_profile_defaults_to_custom(monkeypatch, saved):
    s = make_state("litellm")
    s.profile.name = saved
    assert default_of(monkeypatch, s) == "custom"


def test_a_customized_a_composed_or_a_picked_team_defaults_to_custom(monkeypatch):
    s = make_state("litellm")
    s.profile.name, s.profile.customized = "balanced-claude-55", True
    s.profile.customizations = {"planner": {"model": "anthropic/claude-opus-5.5"}}
    assert default_of(monkeypatch, s) == "custom"
    s = make_state("litellm")
    s.profile.name, s.profile.composed = "balanced-claude-55", {"mode": "balanced"}
    assert default_of(monkeypatch, s) == "custom"
    s = make_state("litellm")
    s.set_selection(selection_mod.Selection(shape="single", slots={
        "a": selection_mod.SlotSel(ref="anthropic/claude-sonnet-5.5")}))
    assert default_of(monkeypatch, s) == "custom"


def test_a_saved_customized_profile_survives_choosing_custom(monkeypatch):
    s = make_state("litellm")
    s.profile.name, s.profile.customized = "quality-first", True
    s.profile.customizations = {"planner": {"model": "anthropic/claude-opus-5.5"}}
    drive(monkeypatch, s, "custom")
    assert s.profile.name == "quality-first" and s.profile.customized is True
    assert s.profile.customizations == {"planner": {"model": "anthropic/claude-opus-5.5"}}


# --- the flags path bypasses the question -------------------------------------------------------------------

def test_flags_and_non_interactive_never_ask(monkeypatch):
    cases = [dict(a=args(models="x"), ni=False), dict(a=args(shape="single"), ni=False),
             dict(a=args(providers="google"), ni=False), dict(a=args(compose="balanced"), ni=False),
             dict(a=args(), ni=True)]
    for c in cases:
        s = make_state("litellm")
        s.profile.name = "quality-first"
        script = Script()
        script.install(monkeypatch, non_interactive=c["ni"])
        assert wizard._step_preset(s, c["a"]) == 0
        assert script.asked == [] and s.profile.name == "quality-first" and wizard._PRESET == ""


def test_profile_flag_still_wins_and_skips_the_question(monkeypatch):
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "quality-first")
    s = make_state("litellm")
    s.profile.name = "quality-first"
    script = Script()
    script.install(monkeypatch)
    assert wizard._step_preset(s, args(profile="quality-first")) == 0
    assert script.asked == [] and s.profile.name == "quality-first"


def test_the_compose_mode_global_skips_the_question(monkeypatch):
    monkeypatch.setattr(wizard, "_COMPOSE", {"mode": "balanced"})
    script = Script()
    script.install(monkeypatch)
    assert wizard._step_preset(make_state("litellm"), args()) == 0 and script.asked == []


def test_the_state_file_format_gains_no_field(monkeypatch):
    s = make_state("litellm")
    drive(monkeypatch, s, "performance")
    assert not hasattr(s.profile, "preset")


# --- the edit list offers one "Models" entry --------------------------------------------------------------

def test_the_review_edit_list_has_one_models_entry(monkeypatch):
    from ai_resources.setup import flow
    steps = wizard._build_steps(False)
    ids = [sp.id for sp in steps]
    assert ids.index("preset") < ids.index("models") < ids.index("profile") < ids.index("lanes")
    assert steps[ids.index("preset")].back is True
    runner = flow.Wizard(steps)
    for i, sp in enumerate(steps):
        runner.history.append(flow._Done(i, object(), False, skipped=sp.id in ("models", "profile")))
    monkeypatch.setattr(wizard, "_RUNNER", runner)
    labels = [c.title for c in wizard._edit_choices(args(), make_state("litellm"))]
    assert labels.count("Models") == 1
    runner.history = [d for d in runner.history]
    for d in runner.history:
        d.skipped = False
    labels = [c.title for c in wizard._edit_choices(args(), make_state("litellm"))]
    assert labels.count("Models") == 1 and "Role assignment" in labels


# --- the whole wizard, through wizard.run --------------------------------------------------------------------

from wizard_fakes import host_env  # noqa: E402

OLD_STEP_6 = ("How do you want to choose models", "How should kit subagents", "Choose profile", "Customize any role",
              "Which provider", "Primary model", "What do you want to use")


def _run_args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_a_full_run_with_a_preset_asks_none_of_the_old_step_6_questions(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(ui, "role_table", lambda *a, **k: None)
    script = Script(**{"Choose mode": "multi-model:cli", ASK: "performance", CONFIRM: True,
                       "Enable subscription lanes": False, "Configure detected cockpits": lambda v: list(v),
                       "Apply this?": lambda v: v[0]})
    script.install(monkeypatch)
    assert wizard.run(_run_args()) == 0
    asked = " | ".join(script.asked)
    assert not any(q in asked for q in OLD_STEP_6), asked
    assert [m for m in script.asked if "Model preset" in m or "Use this?" in m] == [f"select: {ASK}", f"confirm: {CONFIRM}"]
    saved = state.load()
    assert saved.profile.name == "subscription-performance" and saved.cli_team is True


def test_a_full_run_with_custom_reaches_the_old_questions(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    script = Script(**{"Choose mode": "single-model", ASK: "custom", "How do you want to choose models": "profile",
                       "How should kit subagents": lambda v: v[0], "Configure detected cockpits": lambda v: list(v),
                       "Apply this?": lambda v: v[0]})
    script.install(monkeypatch)
    assert wizard.run(_run_args()) == 0
    asked = " | ".join(script.asked)
    assert "How do you want to choose models" in asked and "How should kit subagents" in asked


def test_a_full_run_with_the_profile_flag_never_asks_the_preset(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    script = Script(**{"Choose mode": "single-model", "How do you want to choose models": "profile",
                       "How should kit subagents": lambda v: "claude-inherit",
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": lambda v: v[0]})
    script.install(monkeypatch)
    assert wizard.run(_run_args(profile="claude-inherit")) == 0
    assert not any("Model preset" in m for m in script.asked)
    assert state.load().profile.name == "claude-inherit"
