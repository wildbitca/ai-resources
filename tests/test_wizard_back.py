"""Ctrl-B Back through the real wizard (operator decision: only in steps without side effects)."""
from __future__ import annotations

import argparse


from ai_resources.setup import credentials, live_auth, state, ui, wizard
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
    base = {"Claude Code sign-in": "keep", "Starting team": "custom", "How do you want to choose models": "profile",
            "How should kit subagents pick": lambda v: SUB, "Configure detected cockpits": lambda v: list(v),
            "Apply this?": lambda v: v[0]}
    base.update(extra)
    return base


def test_back_is_offered_only_in_the_steps_without_side_effects():
    steps = {s.id: s for s in wizard._build_steps(False)}
    assert [i for i, s in steps.items() if s.back] == ["cockpit", "cockpit_auth", "preset", "models", "profile", "lanes",
                                                       "cockpits", "audio", "more"]   # ADR-0015: the sign-in step replaces mode
    assert not {"mode", "gateway", "providers", "credentials", "lifecycle"} & set(steps)   # the gateway path is gone
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


def test_back_at_the_team_reasks_the_sign_in_with_the_previous_answer(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    script = Script(**_single(**{"Starting team": Seq(BACK, "custom"), "Claude Code sign-in": "keep"}))
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    assert len([m for m in script.asked if "Claude Code sign-in" in m]) == 2
    assert script.defaults["Claude Code sign-in:"] == ["keep", "keep"]


def test_back_is_refused_once_the_sign_in_handed_the_terminal_to_claude(monkeypatch, tmp_path):
    host_env(monkeypatch, tmp_path)
    handed = []
    monkeypatch.setattr(live_auth, "handoff", lambda argv: handed.append(list(argv)) or 0)
    script = Script(**_single(**{"Claude Code sign-in": "change", "How should Claude Code sign in": "login",
                                 "Starting team": Seq(BACK, "custom")}))
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    notices = [m for m in script.shown if "Back stops here" in m]
    assert len(notices) == 1 and "Sign-in already changed this machine" in notices[0]
    assert len([m for m in script.asked if "Starting team" in m]) == 2
    assert len([m for m in script.asked if "Claude Code sign-in" in m]) == 1     # not re-asked
    assert handed == [["claude", "auth", "login"]]                                 # and the native flow ran once


def test_the_lanes_step_asks_no_orchestrator_question(monkeypatch, tmp_path):
    home = host_env(monkeypatch, tmp_path)
    script = Script(**_single())
    script.install(monkeypatch)
    assert wizard.run(_args(lanes=True)) == 0
    assert not [m for m in script.asked if "Orchestrator agent id" in m]
    assert state.load().profile.lanes == {"orchestrators": []}          # the orchestrators are found in the OpenClaw config
    assert "lane_acks" not in state.state_path().read_text(encoding="utf-8")


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


def test_the_progress_rail_rebuilds_skipped_from_the_runner_history(monkeypatch):
    """Change sign-in: phase 2 (Cockpit: the main-tool and sign-in steps) was skipped on the first pass; after the jump
    it is run, not skipped. A phase counts as skipped only when every one of its steps was (ADR-0016 D7)."""
    from ai_resources.setup import flow
    steps = wizard._build_steps(False)
    runner = flow.Wizard(steps)
    gateway = runner.index_of("cockpit_auth")
    runner.history = [flow._Done(0, None, False), flow._Done(1, None, False, skipped=True),
                      flow._Done(gateway, None, False, skipped=True)]
    ui.set_progress(0, [])
    ui.mark_skipped(2)
    wizard._enter_step(steps[gateway + 1], runner)
    assert ui._SKIPPED == {2}
    runner.history = runner.history[:2]               # the jump to Sign-in truncated the history
    wizard._enter_step(steps[gateway], runner)
    assert ui._SKIPPED == set()
    ui.set_progress(0, [])
