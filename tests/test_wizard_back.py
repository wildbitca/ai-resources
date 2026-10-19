"""Ctrl-B Back through the real wizard (operator decision: only in steps without side effects)."""
from __future__ import annotations

import argparse


from ai_resources.setup import credentials, state, ui, wizard
from wizard_fakes import BACK, Script, Seq, host_env

SUB = "subscription-claude-55"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def _snapshot(root):
    return {str(p): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and not p.name.endswith(".lock")}


def _single(**extra):
    base = {"Choose mode": "single-model", "Model preset": "custom", "How do you want to choose models": "profile",
            "How should kit subagents pick": lambda v: SUB, "Configure detected cockpits": lambda v: list(v),
            "Apply this?": lambda v: v[0]}
    base.update(extra)
    return base


def test_back_is_offered_only_in_the_steps_without_side_effects():
    steps = {s.id: s for s in wizard._build_steps(False)}
    assert [i for i, s in steps.items() if s.back] == ["cockpit", "preset", "models", "profile", "lanes", "cockpits"]   # ADR-0010 S11: the main-tool step
    assert not steps["gateway"].rewindable and wizard._build_steps(True)[2].rewindable
    for sid in ("tools", "review", "apply"):
        assert not steps[sid].rewindable


def test_back_at_the_profile_reasks_the_models_with_the_previous_answer_and_writes_nothing(monkeypatch, tmp_path, capsys):
    home = host_env(monkeypatch, tmp_path)
    before = _snapshot(home)
    script = Script(**_single(**{"How should kit subagents pick": Seq(BACK, lambda v: SUB)}))
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    models = [m for m in script.asked if "How do you want to choose models" in m]
    profile = [m for m in script.asked if "How should kit subagents pick" in m]
    assert len(models) == 2 and len(profile) == 2
    assert script.defaults["How do you want to choose models?"] == ["profile", "profile"]
    # The only write of a whole run is the final state save: Back wrote nothing of its own.
    assert set(_snapshot(home)) - set(before) == {str(state.state_path())}


def test_back_at_models_is_refused_after_a_credential_was_saved(monkeypatch, tmp_path):
    home = host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(credentials, "update_env_tracked", lambda updates: (state.env_path(), list(updates)))
    script = Script(**{"Choose mode": "multi-model:openrouter", "Update OPENROUTER_API_KEY": True,
                       "OPENROUTER_API_KEY": "new-secret",
                       "Model preset": Seq(BACK, "custom"), "How do you want to choose models": "profile",
                       "Choose profile": lambda v: v[0],
                       "Customize any role": False, "Configure detected cockpits": lambda v: list(v),
                       "Apply this?": lambda v: v[0]})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    notices = [m for m in script.shown if "Back stops here" in m]
    assert len(notices) == 1 and "API keys already changed this machine" in notices[0]
    assert [m for m in script.asked if "Starting team" in m].__len__() == 2      # the first question of step 6
    assert [m for m in script.asked if m.startswith("password")].__len__() == 1      # credentials not re-asked


def test_back_inside_the_lanes_step_returns_to_the_profile_and_stores_nothing(monkeypatch, tmp_path):
    home = host_env(monkeypatch, tmp_path)
    before = _snapshot(home)
    script = Script(**_single(**{"Orchestrator agent id": Seq(BACK, "main"),
                                 "Acknowledge for these roles?": False}))
    script.install(monkeypatch)
    rc = wizard.run(_args(lanes=True))
    assert rc == 0
    assert len([m for m in script.asked if "How should kit subagents pick" in m]) == 2
    assert not state.lanes_path().exists()
    assert set(_snapshot(home)) - set(before) == {str(state.state_path())}   # only the final state save
    assert "lane_acks" not in state.state_path().read_text(encoding="utf-8")
    assert len([m for m in script.asked if "Acknowledge for these roles?" in m]) == 1


def test_non_interactive_never_raises_goback_and_never_attaches_a_binding(monkeypatch, tmp_path):
    home = host_env(monkeypatch, tmp_path)
    s = state.SetupState()
    s.mode, s.backend, s.profile.name = "single-model", "litellm", SUB
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    attached = []
    monkeypatch.setattr(ui, "_attach_keys", lambda q, back: attached.append(q))
    warned = []
    monkeypatch.setattr(ui, "warn", lambda m: warned.append(m))
    monkeypatch.setattr(ui, "console", lambda: type("C", (), {"print": lambda *a, **k: None,
                                                              "rule": lambda *a, **k: None,
                                                              "width": 100})())
    rc = wizard.run(_args(non_interactive=True))
    assert rc == 0 and attached == []
    assert not any("Back" in m for m in warned)
    assert not ui.back_enabled()


def test_a_gateway_step_is_asked_again_in_a_dry_run_only():
    for dry, expected in ((True, True), (False, False)):
        gateway = [s for s in wizard._build_steps(dry) if s.id == "gateway"][0]
        assert gateway.rewindable is expected


def test_the_progress_rail_rebuilds_skipped_from_the_runner_history(monkeypatch):
    """Edit-to-LiteLLM: step 3 was skipped on the OpenRouter pass; after the jump it is run, not skipped."""
    from ai_resources.setup import flow
    steps = wizard._build_steps(False)
    runner = flow.Wizard(steps)
    gateway = runner.index_of("gateway")
    runner.history = [flow._Done(0, None, False), flow._Done(1, None, False),
                      flow._Done(gateway, None, False, skipped=True)]
    ui.set_progress(0, [])
    ui.mark_skipped(3)
    wizard._enter_step(steps[gateway + 1], runner)
    assert ui._SKIPPED == {3}
    runner.history = runner.history[:2]               # the jump to Mode truncated the history
    wizard._enter_step(steps[gateway], runner)
    assert ui._SKIPPED == set()
    ui.set_progress(0, [])
