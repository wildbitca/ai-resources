"""ADR-0010 S13: voice notes are their own step (phase 4, "Audio"); the other OpenClaw questions never ask them."""
from __future__ import annotations

import types

from ai_resources.setup import state, wizard
from ai_resources.setup.cockpits import openclaw as cockpit
from wizard_fakes import Script


def _s(*selected, engine="keep", phase2=False):
    s = state.SetupState()
    s.profile.customizations["__targets__"] = {"selected": list(selected)}
    s.openclaw.engine = engine
    if phase2:
        setattr(s, wizard._PHASE2_ATTR, True)
    return s


def _spec():
    return next(sp for sp in wizard._build_steps(False) if sp.id == "audio")


def _skip(s):
    return _spec().skip(types.SimpleNamespace(s=s))


def test_the_audio_step_is_phase_four_and_shares_the_tool_setup_number():
    spec = _spec()
    assert spec.number == 7 and spec.edit_label == "Audio" and spec.back
    assert wizard.STEP_PHASE["audio"] == 4
    assert wizard.TOTAL_STEPS == 10


def test_voice_questions_come_only_from_the_audio_step(monkeypatch):
    """AC-S13a: the model, MCP and host parts of the OpenClaw prompt never ask the voice question."""
    asked = []
    monkeypatch.setattr(cockpit.voice, "prompt", lambda s, *a, **k: asked.append("voice"))
    monkeypatch.setattr(cockpit, "_prompt_engine_model", lambda s, **k: asked.append("model"))
    monkeypatch.setattr(cockpit.host_section, "prompt", lambda s, doc: asked.append("host"))
    monkeypatch.setattr(cockpit, "_prompt_stale_check", lambda s: asked.append("stale"))
    monkeypatch.setattr(cockpit, "read_config", lambda p: {})
    s = _s("openclaw", phase2=True)
    Script().install(monkeypatch)
    wizard._step7_cockpit_config(s)
    assert "voice" not in asked and asked[0] == "model"
    asked.clear()
    wizard._step_audio(s)
    assert asked == ["voice"]


def test_the_audio_step_runs_after_the_cockpit_step_asked_the_engine():
    assert _skip(_s("openclaw", phase2=True)) is None


def test_without_openclaw_one_line_says_why_and_nothing_is_asked(monkeypatch):
    """AC-S13c."""
    skip = _skip(_s("claude"))
    assert skip == ("Audio", "Skipped (voice notes need OpenClaw, which is not one of your tools).")


def test_the_audio_step_is_silent_when_the_whole_prompt_already_asked_voice():
    s = _s("openclaw", phase2=True)
    setattr(s, wizard._WHOLE_ATTR, True)
    assert _skip(s) == ("Audio", "")
    assert _skip(_s("openclaw")) == ("Audio", "")          # a saved engine and no cockpit-step pass: nothing is re-asked


def test_saved_cloud_voice_reaches_the_dry_run_keys_unchanged():
    """AC-S13b: the saved answer is the default of the question and the apply keys do not depend on the step."""
    from ai_resources.setup.cockpits import _openclaw_voice as voice
    s = state.SetupState()
    s.openclaw.voice = "cloud"
    assert voice.default_mode(s) == "cloud"
    before = state._to_dict(s)["openclaw"]
    assert before["voice"] == "cloud"
