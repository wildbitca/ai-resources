"""Screen 4, "Review", and its "More options..." (ADR-0009).

The review offers Apply, Change tools, Change how, Change team, More options and Cancel. The Claude parallel runs, the
gateway and API keys, start at login and the OpenClaw questions are not asked on the default path: each is an entry
of More options that opens the existing step and returns to the review. Offline; the ui is the scripted fake."""
from __future__ import annotations

import argparse
import types

import pytest

from ai_resources.setup import flow, model_selection as ms, state, ui, wizard
from ai_resources.setup.cockpits import openclaw as openclaw_cockpit
from wizard_fakes import Script, Seq, host_env

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
    assert seen["values"][0] == [ms.APPLY, wizard.CHANGE_TOOLS, wizard.CHANGE_HOW, wizard.CHANGE_TEAM,
                                 wizard.MORE_OPTIONS, ms.CANCEL]


def test_more_options_is_absent_when_nothing_applies(monkeypatch):
    s = state.SetupState()                                    # each tool on its own, no OpenClaw, no gateway
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    seen, _ = _review(monkeypatch, s, [ms.APPLY])
    wizard._step_summary(s, _args())
    assert wizard.MORE_OPTIONS not in seen["values"][0]


def test_every_change_jumps_back_to_its_screen(monkeypatch):
    for pick, step in ((wizard.CHANGE_HOW, "mode"), (wizard.CHANGE_TEAM, "preset"), (wizard.CHANGE_TOOLS, "tools")):
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
    g = state.SetupState(mode="multi-model", backend="litellm")
    g.litellm.deployment = "local"
    g.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    assert [c.value for c in wizard._more_options(g)] == ["gateway", "lifecycle"]
    g.backend = "openrouter"
    assert [c.value for c in wizard._more_options(g)] == ["gateway"]
    assert wizard._more_options(state.SetupState()) == []


@pytest.mark.parametrize("entry,step", [("parallel", "lanes"), ("openclaw", "cockpits"), ("gateway", "gateway"),
                                        ("lifecycle", "lifecycle")])
def test_each_entry_opens_its_step_and_returns_to_the_review(monkeypatch, entry, step):
    s = _team("claude", "openclaw") if entry in ("parallel", "openclaw") else state.SetupState(
        mode="multi-model", backend="litellm")
    if entry in ("gateway", "lifecycle"):
        s.litellm.deployment = "local"
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


def test_a_configured_openclaw_keeps_every_saved_answer_without_a_question(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    asked = []
    monkeypatch.setattr(openclaw_cockpit, "prompt", lambda s, **k: asked.append("prompt"))
    s = _team("claude", "openclaw")
    s.openclaw.engine, s.openclaw.host, s.openclaw.voice = "claude-code", True, "local"
    before = state._to_dict(s)["openclaw"]
    c = types.SimpleNamespace(s=s)
    assert _spec("cockpits").skip(c) is not None
    assert state._to_dict(s)["openclaw"] == before and asked == []
    monkeypatch.setattr(wizard, "_OPEN_OPENCLAW", True)
    assert _spec("cockpits").skip(c) is None
    assert wizard._step7_cockpit_config(s) == 0 and asked == ["prompt"]
