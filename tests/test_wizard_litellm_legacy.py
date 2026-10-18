"""LiteLLM leaves the new-setup gateway choice but saved litellm states keep loading (ADR-0010 N1, S5)."""
from __future__ import annotations

import argparse
import pathlib
import shutil

from ai_resources.setup import state, wizard
from ai_resources.setup import litellm, profiles
from wizard_fakes import BACK, Script, Seq, host_env

NOTICE = ("LiteLLM is no longer offered for new setups; your saved gateway keeps working. "
          "Choose OpenRouter to switch.")


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_fresh_advanced_offers_no_litellm(monkeypatch):
    """AC-S5a: a fresh run picking Advanced is never shown LiteLLM and is not asked which gateway."""
    s = state.SetupState()
    script = Script(**{"Pick how they work": "advanced"})
    script.install(monkeypatch)
    assert wizard._step1_mode(s) == 0
    assert (s.mode, s.backend) == ("multi-model", "openrouter")
    assert not any(a.endswith("Which gateway?") for a in script.asked)
    assert not any("LiteLLM" in t for t in script.titles)
    assert not any("LiteLLM" in m for m in script.shown)


def test_the_advanced_text_does_not_name_litellm():
    assert all("LiteLLM" not in line for _k, _l, line in wizard.HOW_CHOICES)
    assert [k for k, _l, _t in wizard.GATEWAY_CHOICES] == ["openrouter"]


def test_saved_litellm_is_offered_as_the_default_with_one_notice(monkeypatch):
    """AC-S5b (selection part): the saved gateway stays selectable and the notice prints exactly once."""
    s = state.SetupState(mode="multi-model", backend="litellm")
    seen = {}
    script = Script(**{"Pick how they work": "advanced",
                       "Which gateway": lambda values: seen.setdefault("v", list(values)) and "litellm"})
    script.install(monkeypatch)
    assert wizard._step1_mode(s, is_first=False) == 0
    assert seen["v"] == ["litellm", "openrouter"]
    assert script.defaults["Which gateway?"] == ["litellm"]
    assert any("LiteLLM (saved; no longer offered for new setups)" in t for t in script.titles)
    assert (s.mode, s.backend) == ("multi-model", "litellm")
    assert sum(NOTICE in m for m in script.shown) == 1


def test_saved_litellm_can_switch_to_openrouter(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="litellm")
    Script(**{"Pick how they work": "advanced", "Which gateway": "openrouter"}).install(monkeypatch)
    assert wizard._step1_mode(s, is_first=False) == 0
    assert s.backend == "openrouter"


def test_backend_flag_litellm_is_accepted_with_the_notice(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="litellm")
    script = Script()
    script.install(monkeypatch)
    assert wizard._step1_mode(s, _args(backend="litellm")) == 0
    assert sum(NOTICE in m for m in script.shown) == 1


def test_backend_flag_openrouter_prints_no_notice(monkeypatch):
    s = state.SetupState(mode="multi-model", backend="openrouter")
    script = Script()
    script.install(monkeypatch)
    wizard._step1_mode(s, _args(backend="openrouter"))
    assert not any(NOTICE in m for m in script.shown)


def test_single_model_state_with_default_litellm_backend_prints_no_notice(monkeypatch):
    """AC-S5c: the state.backend default is untouched and silent for cli and single states."""
    assert state.SetupState().backend == "litellm"
    for how in ("alone", "team"):
        s = state.SetupState()
        script = Script(**{"Pick how they work": how})
        script.install(monkeypatch)
        assert wizard._step1_mode(s) == 0
        assert not any(NOTICE in m for m in script.shown)


FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "setup_state" / "multi_litellm.yaml"


def _saved_team(values):
    """Keep the saved team: the choice that names the fixture's profile, else the first offered."""
    return next((v for v in values if "balanced-claude-55" in str(v)), values[0])


_ANSWERS = {"Use previous": True, "Where is your gateway": "skipped", "Select providers": ["anthropic"],
            "Update ANTHROPIC": False, "Starting team": _saved_team, "Apply this?": lambda values: values[0],
            "Pick how they work": "advanced", "Which gateway": "litellm"}


def _review_then_apply():
    """The review answers Change how once (screen 2 again), then Apply."""
    seq = Seq(wizard.CHANGE_HOW, "apply")

    def answer(values):
        want = seq.next()
        return wizard.CHANGE_HOW if want == wizard.CHANGE_HOW else values[0]
    return answer


def _renders(s):
    """What step 9 renders from a state: the executors document and the litellm.yaml text."""
    base = profiles.load_profile(profiles.saved_or_recommended(s.profile.name))
    doc = profiles.to_executors(base, s.backend)
    text = litellm._render_litellm_yaml(doc, {k: v.__dict__ for k, v in s.providers.items()}, "LITELLM_MASTER_KEY")
    return doc, text


def _run_saved_litellm(monkeypatch, tmp_path, args, answers):
    """Drive the real wizard.run over the saved multi_litellm state; step 9 records the state it would render."""
    home = host_env(monkeypatch, tmp_path)
    cfg = home / "xdg" / "ai-resources"
    cfg.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, state.state_path())
    want = _renders(state.load())          # the pre-change outputs: rendered from the saved state as loaded
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    script = Script(**answers)
    script.install(monkeypatch)
    assert wizard.run(args) == 0
    return seen["s"], want, script


def test_dry_run_yes_from_saved_litellm_renders_the_same_and_notices_once(monkeypatch, tmp_path):
    """AC-S5b: saved litellm, --dry-run --yes: same litellm.yaml and executors.yaml, exactly one notice."""
    s, want, script = _run_saved_litellm(monkeypatch, tmp_path, _args(dry_run=True, yes=True),
                                         _ANSWERS)
    assert (s.mode, s.backend) == ("multi-model", "litellm")
    assert _renders(s) == want
    assert sum(NOTICE in m for m in script.shown) == 1


def test_backend_litellm_flag_from_saved_litellm_notices_once(monkeypatch, tmp_path):
    answers = {k: v for k, v in _ANSWERS.items() if k not in ("Pick how they work", "Which gateway")}
    s, want, script = _run_saved_litellm(monkeypatch, tmp_path, _args(dry_run=True, yes=True, backend="litellm"),
                                         answers)
    assert _renders(s) == want
    assert sum(NOTICE in m for m in script.shown) == 1


def test_reentering_screen_2_from_the_review_notices_once_per_visit(monkeypatch, tmp_path):
    """Change how (a re-entry of screen 2) shows the notice once per visit: twice over two visits, never more."""
    s, want, script = _run_saved_litellm(
        monkeypatch, tmp_path, _args(dry_run=True, yes=True),
        {**_ANSWERS, "Apply this?": _review_then_apply()})
    assert _renders(s) == want
    assert sum(NOTICE in m for m in script.shown) == 2
