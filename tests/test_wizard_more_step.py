"""ADR-0010 S15: phase 5 "More" keeps saved answers without asking, asks each question once on a fresh state, and
asks again from More options. Nothing on the never-auto list (engine, risk acknowledgement) is answered for the user."""
from __future__ import annotations

import types

from ai_resources.setup import state, wizard
from ai_resources.setup.cockpits import openclaw as cockpit
from wizard_fakes import Script


def _spec(step_id):
    return next(sp for sp in wizard._build_steps(False) if sp.id == step_id)


def _s(*selected, engine="keep", phase2=False):
    s = state.SetupState()
    s.profile.customizations["__targets__"] = {"selected": list(selected)}
    s.openclaw.engine = engine
    if phase2:
        setattr(s, wizard._PHASE2_ATTR, True)
    return s


def _local_litellm(auto_start=True, path="/x/unit"):
    s = state.SetupState(mode="multi-model", backend="litellm")
    s.litellm.deployment = "local"
    s.litellm.local.runtime = "pipx"
    s.litellm.local.auto_start = auto_start
    s.litellm.local.lifecycle_path = path
    return s


def _no_container(monkeypatch):
    monkeypatch.setattr(wizard.litellm, "is_container_mode", lambda r: False)
    monkeypatch.setattr(wizard.litellm, "is_macos", lambda: False)


def test_a_saved_lifecycle_answer_is_kept_without_a_question(monkeypatch):
    """AC-S15a (phase 5 part): zero select/confirm before the Review."""
    _no_container(monkeypatch)
    script = Script()
    script.install(monkeypatch)
    s = _local_litellm()
    assert wizard._step8_lifecycle(s) == 0
    assert script.asked == [] and s.litellm.local.auto_start is True
    assert any("keeping your saved answer" in m for m in script.shown)


def test_a_fresh_lifecycle_is_asked_once(monkeypatch):
    """AC-S15b."""
    _no_container(monkeypatch)
    script = Script(**{"Auto-start LiteLLM": True})
    script.install(monkeypatch)
    s = _local_litellm(auto_start=False, path="")
    wizard._step8_lifecycle(s)
    assert [a.split(": ", 1)[0] for a in script.asked] == ["confirm"] and len(script.asked) == 1


def test_more_options_asks_the_lifecycle_again(monkeypatch):
    """AC-S15c."""
    _no_container(monkeypatch)
    monkeypatch.setattr(wizard, "_OPEN_LIFECYCLE", True)
    script = Script(**{"Auto-start LiteLLM": False})
    script.install(monkeypatch)
    s = _local_litellm()
    wizard._step8_lifecycle(s)
    assert len(script.asked) == 1 and s.litellm.local.auto_start is False


def test_the_more_step_runs_once_on_the_first_configure_and_is_silent_afterwards():
    assert _spec("more").skip(types.SimpleNamespace(s=_s("openclaw", phase2=True))) is None
    assert _spec("more").skip(types.SimpleNamespace(s=_s("openclaw", engine="keep"))) == ("More", "")
    assert _spec("more").skip(types.SimpleNamespace(s=_s("claude"))) == ("More", "")
    whole = _s("openclaw", phase2=True)
    setattr(whole, wizard._WHOLE_ATTR, True)
    assert _spec("more").skip(types.SimpleNamespace(s=whole)) == ("More", "")


def test_the_more_step_asks_the_host_section_and_the_stale_check_once(monkeypatch):
    asked = []
    monkeypatch.setattr(cockpit.host_section, "prompt", lambda s, doc: asked.append("host"))
    monkeypatch.setattr(cockpit, "_prompt_stale_check", lambda s: asked.append("stale"))
    monkeypatch.setattr(cockpit, "read_config", lambda p: {})
    Script().install(monkeypatch)
    assert wizard._step_more(_s("openclaw", phase2=True)) == 0
    assert asked == ["host", "stale"]


def test_each_openclaw_question_is_asked_by_exactly_one_step(monkeypatch):
    """AC-S15b: engine in the cockpit step, model in tool configuration, voice in Audio, host and stale in More."""
    log = []
    monkeypatch.setattr(cockpit, "_prompt_engine_choice", lambda s, **k: log.append(("engine", "cockpit")))
    monkeypatch.setattr(cockpit, "_prompt_engine_model", lambda s, **k: log.append(("model", "cockpits")))
    monkeypatch.setattr(cockpit.mcp, "prompt", lambda *a, **k: None)
    monkeypatch.setattr(cockpit.voice, "prompt", lambda s, *a, **k: log.append(("voice", "audio")))
    monkeypatch.setattr(cockpit.host_section, "prompt", lambda s, doc: log.append(("host", "more")))
    monkeypatch.setattr(cockpit, "_prompt_stale_check", lambda s: log.append(("stale", "more")))
    monkeypatch.setattr(cockpit, "read_config", lambda p: {})
    from ai_resources.setup.cockpits import _openclaw_channels
    monkeypatch.setattr(_openclaw_channels, "prompt", lambda s, **k: None)
    Script().install(monkeypatch)
    s = state.SetupState()
    s.cockpits["openclaw"] = state.CockpitState(installed=True)
    s.profile.customizations["__targets__"] = {"selected": ["openclaw"]}
    wizard._step_cockpit_choice(s)
    wizard._step7_cockpit_config(s)
    wizard._step_audio(s)
    wizard._step_more(s)
    assert [q for q, _ in log] == ["engine", "model", "voice", "host", "stale"]
    assert len({q for q, _ in log}) == len(log)


def test_more_options_openclaw_asks_everything_again_through_the_whole_prompt(monkeypatch):
    """AC-S15c: the legacy whole prompt (engine included) runs, and the later steps do not ask twice."""
    calls = []
    monkeypatch.setattr(cockpit, "prompt", lambda s, **k: calls.append("prompt"))
    from ai_resources.setup.cockpits import _openclaw_channels
    monkeypatch.setattr(_openclaw_channels, "prompt", lambda s, **k: calls.append("channels"))
    monkeypatch.setattr(wizard, "_OPEN_OPENCLAW", True)
    Script().install(monkeypatch)
    s = _s("openclaw", engine="claude-code", phase2=True)
    wizard._step7_cockpit_config(s)
    assert calls == ["prompt", "channels"]
    ctx = types.SimpleNamespace(s=s)
    assert _spec("audio").skip(ctx) == ("Audio", "") and _spec("more").skip(ctx) == ("More", "")


def test_the_never_auto_answers_are_never_filled_in_for_the_user(monkeypatch):
    """AC-S15d: with a saved engine every OpenClaw step is a skip: the engine and the risk acknowledgement are not
    answered by the kit, and only More options > OpenClaw asks them again (a real question, never a default)."""
    s = _s("openclaw", engine="antigravity")
    s.cockpits["openclaw"] = state.CockpitState(installed=True)
    ctx = types.SimpleNamespace(s=s)
    for spec_id in ("cockpit", "cockpits", "audio", "more"):
        assert _spec(spec_id).skip(ctx) is not None, spec_id
    assert s.openclaw.risk_acknowledged is False and s.openclaw.engine == "antigravity"
    # More options > OpenClaw: the engine question is asked, and it is a prompt the script must answer
    monkeypatch.setattr(wizard, "_OPEN_OPENCLAW", True)
    assert _spec("cockpits").skip(ctx) is None
    script = Script()                                   # nothing scripted: any unanswered question raises
    script.install(monkeypatch)
    monkeypatch.setattr(cockpit, "available_engines", lambda st: [cockpit.ENGINES["antigravity"]])
    try:
        wizard._step7_cockpit_config(s)
    except AssertionError as e:
        assert "unscripted select" in str(e) and "engine" in str(e)
    else:
        raise AssertionError("the engine question was not asked")
    assert s.openclaw.risk_acknowledged is False
