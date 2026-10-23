"""Screen 4, "Review", and its "More options..." (ADR-0009).

The review offers Apply, Change tools, Change sign-in, Change team, More options and Cancel. The Claude parallel runs and
the OpenClaw questions are not asked on the default path: each is an entry of More options that opens the existing step
and returns to the review (ADR-0015 removed the gateway and start-at-login entries). Offline; the ui is the scripted fake."""
from __future__ import annotations

import argparse
import types

import pytest

from ai_resources.setup import flow, model_selection as ms, state, ui, wizard
from ai_resources.setup.cockpits import openclaw as openclaw_cockpit
from wizard_fakes import Script, Seq, fake_openclaw, host_env

SUB = "subscription-claude-55"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(wizard, "_PARALLEL_RUNS", False)
    monkeypatch.setattr(wizard, "_OPEN_OPENCLAW", False)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)


def _team(*members):
    s = state.SetupState()
    s.mode, s.cli_team = "single-model", True
    s.profile.name = SUB
    s.profile.customizations = {"__targets__": {"selected": list(members or ("claude",))}}
    return s


def _review(monkeypatch, s, answers):
    seen = {}

    def apply_pick(values):
        seen.setdefault("values", []).append(list(values))
        return answers.pop(0) if isinstance(answers, list) else answers
    script = Script(**{"Apply this?": apply_pick, "More options": lambda v: seen.setdefault("more", list(v)) and "",
                       "Which gateway": "litellm"})
    script.install(monkeypatch)
    return seen, script


# --- the choices ---------------------------------------------------------------------------------------------
def test_the_review_offers_apply_three_changes_more_options_and_cancel(monkeypatch):
    s = _team("claude", "openclaw")
    seen, script = _review(monkeypatch, s, [ms.APPLY])
    assert wizard._step_summary(s, _args()) == 0
    assert seen["values"][0] == [ms.APPLY, wizard.CHANGE_TOOLS, wizard.CHANGE_AUTH, wizard.CHANGE_TEAM,
                                 wizard.MORE_OPTIONS, ms.CANCEL]


def test_more_options_is_absent_when_nothing_applies(monkeypatch):
    s = state.SetupState()                                    # each tool on its own, no OpenClaw, no gateway
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    seen, _ = _review(monkeypatch, s, [ms.APPLY])
    wizard._step_summary(s, _args())
    assert wizard.MORE_OPTIONS not in seen["values"][0]


def test_every_change_jumps_back_to_its_screen(monkeypatch):
    for pick, step in ((wizard.CHANGE_AUTH, "cockpit_auth"), (wizard.CHANGE_TEAM, "preset"), (wizard.CHANGE_TOOLS, "tools")):
        s = _team()
        _review(monkeypatch, s, [pick])
        with pytest.raises(flow.JumpTo) as jump:
            wizard._step_summary(s, _args())
        assert jump.value.step_id == step


def test_change_tools_asks_the_list_again(monkeypatch):
    s = _team()
    s._install_answer_this_run, s._tool_members_this_run = ["claude"], ["claude"]
    _review(monkeypatch, s, [wizard.CHANGE_TOOLS])
    with pytest.raises(flow.JumpTo):
        wizard._step_summary(s, _args())
    assert not hasattr(s, "_install_answer_this_run") and not hasattr(s, "_tool_members_this_run")


def test_cancel_exits_130_with_the_shared_cancel_block(monkeypatch):
    s = _team()
    _review(monkeypatch, s, [ms.CANCEL])
    assert wizard._step_summary(s, _args()) == 130


# --- the entries -----------------------------------------------------------------------------------------------
def test_the_entries_follow_the_run(monkeypatch):
    s = _team("claude", "openclaw")
    assert [c.value for c in wizard._more_options(s)] == ["parallel", "openclaw"]
    # ADR-0015: a saved gateway state offers no gateway or start-at-login entry any more
    g = state.SetupState(mode="multi-model", backend="litellm")
    g.litellm.deployment = "local"
    g.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    assert [c.value for c in wizard._more_options(g)] == []
    assert wizard._more_options(state.SetupState()) == []


@pytest.mark.parametrize("entry,step", [("parallel", "lanes"), ("openclaw", "cockpits")])
def test_each_entry_opens_its_step_and_returns_to_the_review(monkeypatch, entry, step):
    s = _team("claude", "openclaw")
    seen, script = _review(monkeypatch, s, [wizard.MORE_OPTIONS])
    script.answers["More options"] = entry
    with pytest.raises(flow.JumpTo) as jump:
        wizard._step_summary(s, _args())
    assert jump.value.step_id == step
    assert wizard._PARALLEL_RUNS is (entry == "parallel") and wizard._OPEN_OPENCLAW is (entry == "openclaw")


def test_back_to_the_review_asks_the_review_again(monkeypatch):
    s = _team("claude", "openclaw")
    seen, script = _review(monkeypatch, s, [wizard.MORE_OPTIONS, ms.APPLY])
    assert wizard._step_summary(s, _args()) == 0
    assert len(seen["values"]) == 2 and wizard._PARALLEL_RUNS is False


# --- parallel runs are not asked on the default path -------------------------------------------------------------
def test_the_default_path_never_asks_about_parallel_runs(monkeypatch):
    s = _team()
    asked = []
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: asked.append(m) or False)
    assert wizard._step_lanes(s, _args()) == 0
    assert asked == [] and s.profile.lanes is None


def test_parallel_runs_from_more_options_ask_the_orchestrator_and_the_terms_check(monkeypatch):
    s = _team()
    monkeypatch.setattr(wizard, "_PARALLEL_RUNS", True)
    Script(**{"Orchestrator agent id": "main", "Acknowledge for these roles?": True}).install(monkeypatch)
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lanes["orchestrators"] == ["main"] and "anthropic" in s.profile.lane_acks


def test_unattended_runs_never_get_parallel_runs_from_the_menu(monkeypatch):
    s = _team()
    monkeypatch.setattr(wizard, "_PARALLEL_RUNS", True)
    Script().install(monkeypatch, non_interactive=True)
    assert wizard._step_lanes(s, _args()) == 0 and s.profile.lanes is None


@pytest.mark.parametrize("flags", [dict(lanes=True), dict(no_lanes=True), dict(lanes_dry_run=True)])
def test_the_lane_flags_behave_as_before(monkeypatch, flags):
    s = _team()
    Script(**{"Orchestrator agent id": "main", "Acknowledge for these roles?": True}).install(monkeypatch)
    assert wizard._step_lanes(s, _args(lanes_orchestrator=["main"], **flags), dry_run=bool(flags.get("lanes_dry_run"))) == 0
    assert (s.profile.lanes is None) == bool(flags.get("no_lanes") or flags.get("lanes_dry_run"))


# --- OpenClaw: asked on the first configure, then only from More options ------------------------------------------
def _spec(step_id):
    return next(sp for sp in wizard._build_steps(False) if sp.id == step_id)


def test_openclaw_is_asked_on_the_first_configure_only(monkeypatch):
    s = _team("claude", "openclaw")
    c = types.SimpleNamespace(s=s)
    assert _spec("cockpits").skip(c) is None                      # no engine yet: the questions are needed
    s.openclaw.engine = "claude-code"
    skip = _spec("cockpits").skip(c)
    assert skip is not None and skip[1] == ""                      # configured: saved answers re-apply silently
    monkeypatch.setattr(wizard, "_OPEN_OPENCLAW", True)
    assert _spec("cockpits").skip(c) is None                      # More options > OpenClaw opens them again


def test_a_configured_openclaw_is_skipped_silently_and_more_options_opens_it(monkeypatch):
    s = _team("claude", "openclaw")
    s.openclaw.engine = "claude-code"
    c = types.SimpleNamespace(s=s)
    assert _spec("cockpits").skip(c) is not None
    monkeypatch.setattr(wizard, "_OPEN_OPENCLAW", True)
    assert _spec("cockpits").skip(c) is None


def _drive_run(monkeypatch, tmp_path, review_answers, more=""):
    """wizard.run on the subscription path with a seeded OpenClaw block; returns (script, prompts, applied state)."""
    host_env(monkeypatch, tmp_path)
    from ai_resources.setup import detection
    det = lambda n: types.SimpleNamespace(installed=True, version="1", binary_path="/opt/" + n, name=n)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {"claude": det("claude"), "openclaw": det("openclaw")})
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    prompts = []
    monkeypatch.setattr(openclaw_cockpit, "prompt", lambda s, **k: prompts.append("prompt"))
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    fake_openclaw(monkeypatch)                           # OpenClaw's own sign-in question reads a canned catalog
    script = Script(**{"Use previous answers": True, "Model preset": "custom",
                       "Which providers should OpenClaw use": ["anthropic"], "How does OpenClaw sign in to": "cli",
                       "How do you want to choose models": "profile",
                       "How should kit subagents pick": lambda v: SUB, "Enable subscription lanes?": False,
                       "Configure detected cockpits": lambda v: list(v), "Apply this?": Seq(*review_answers),
                       "Which image generation setup": "keep", "More options": more})
    script.install(monkeypatch)
    monkeypatch.setattr(wizard.model_selection, "detect_accounts", lambda: {})
    monkeypatch.setattr(wizard.model_selection, "candidates", lambda a, f: [])
    s = state.SetupState()
    s.mode, s.cli_team, s.backend = "single-model", True, "litellm"
    s.profile.name = SUB
    s.profile.customizations = {"__targets__": {"selected": ["claude", "openclaw"]}}
    s.openclaw.engine, s.openclaw.host, s.openclaw.voice = "claude-code", True, "local"
    s.last_run = "2026-01-01T00:00:00+00:00"
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    before = state._to_dict(state.load())["openclaw"]
    assert wizard.run(_args(backend="cli")) == 0
    return script, prompts, seen["s"], before


def test_a_configured_openclaw_keeps_every_saved_answer_through_a_whole_run(monkeypatch, tmp_path):
    script, prompts, applied, before = _drive_run(monkeypatch, tmp_path, [ms.APPLY])
    assert prompts == []
    assert state._to_dict(applied)["openclaw"] == before


def test_more_options_openclaw_asks_the_openclaw_questions_once(monkeypatch, tmp_path):
    script, prompts, applied, before = _drive_run(monkeypatch, tmp_path, [wizard.MORE_OPTIONS, ms.APPLY],
                                                  more="openclaw")
    assert prompts == ["prompt"]
    assert [m for m in script.asked if "Apply this?" in m].__len__() == 2


# --- Antigravity is never ticked on its own --------------------------------------------------------------------
def test_agy_saved_in_targets_with_the_lanes_cleared_is_not_pre_ticked():
    s = _team("claude", "agy")
    s.profile.lanes = None
    det = {c: types.SimpleNamespace(installed=True) for c in ("claude", "agy")}
    assert wizard._default_members(s, det) == ["claude"]


# --- a Change / More options jump that would re-run a locked step is refused -------------------------------------
def test_a_jump_across_a_locked_step_is_refused_and_the_review_is_asked_again(monkeypatch):
    s = _team("claude", "openclaw")
    seen, script = _review(monkeypatch, s, [wizard.CHANGE_AUTH, ms.APPLY])
    notes = []
    monkeypatch.setattr(ui, "warn", lambda m, *a, **k: notes.append(m))
    steps = wizard._build_steps(False)
    runner = flow.Wizard(steps=steps, on_skip=lambda *a: None, on_enter=lambda *a: None, snapshot=lambda: None,
                         restore=lambda snap: None, notify=lambda m: None, scope=lambda spec, back: __import__("contextlib").nullcontext(),
                         changes=lambda: 0)
    runner.history = [flow._Done(runner.index_of("preset"), None, True)]
    monkeypatch.setattr(wizard, "_RUNNER", runner)
    assert wizard._step_summary(s, _args()) == 0
    assert any("cannot be asked again" in n for n in notes)
    assert len(seen["values"]) == 2
