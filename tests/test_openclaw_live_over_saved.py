"""ADR-0016 D2 (AC-1, AC-2): the live OpenClaw config beats the saved setup state.

Live: every agent on anthropic/claude-sonnet-5-5 (runtime openclaw). Saved: engine antigravity, model agy-cli/gemini-3.8-flash-low.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ai_resources.setup import detection, live_openclaw, state, ui
from ai_resources.setup.cockpits import openclaw

FIX = Path(__file__).parent / "fixtures"


def live_doc():
    return json.loads((FIX / "openclaw_live" / "all_anthropic.json").read_text(encoding="utf-8"))


def saved_state():
    data = yaml.safe_load((FIX / "setup_state" / "live_anthropic_saved_antigravity.yaml").read_text(encoding="utf-8"))
    return state._from_dict(state.SetupState, data)


@pytest.fixture
def agy_world(monkeypatch, tmp_path):
    """agy and claude installed (the old default would have been antigravity), live config on disk."""
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps(live_doc()), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(detection, "_which_extra", lambda name: f"/usr/bin/{name}")
    return cfg


class Answers:
    """Records every select/confirm and answers with the default unless told otherwise."""

    def __init__(self, monkeypatch, *, select=None, confirm=False):
        self.selects, self.confirms, self.infos = [], [], []
        self.pick, self.ok = select or {}, confirm
        monkeypatch.setattr(ui, "select", self._select)
        monkeypatch.setattr(ui, "confirm", self._confirm)
        monkeypatch.setattr(ui, "info", self.infos.append)
        monkeypatch.setattr(ui, "detail", self.infos.append)
        monkeypatch.setattr(ui, "warn", self.infos.append)
        monkeypatch.setattr(ui, "ok", self.infos.append)

    def _select(self, message, choices, default=None, **k):
        self.selects.append((message, default))
        for key, value in self.pick.items():
            if key in message:
                return value
        return default

    def _confirm(self, message, default=True, **k):
        self.confirms.append((message, default))
        return self.ok


def test_the_engine_default_is_keep_when_live_runs_anthropic_even_if_saved_says_antigravity(agy_world):
    s = saved_state()
    assert s.openclaw.engine == "antigravity"
    assert openclaw.default_engine(s, live_doc()) == "keep"


def test_an_empty_live_config_still_falls_back_to_the_saved_engine(agy_world):
    s = saved_state()
    assert openclaw.default_engine(s, {}) == "antigravity"


def test_accepting_every_default_keeps_the_engine_and_writes_no_model_or_runtime(agy_world, monkeypatch):
    s = saved_state()
    ans = Answers(monkeypatch)
    openclaw._prompt_engine(s)
    assert s.openclaw.engine == "keep"
    assert ans.confirms == []
    patches = []
    monkeypatch.setattr(openclaw, "_apply_hot", lambda patch, **k: patches.append(patch) or (True, "ok"))
    ctx = {"state": s, "dry_run": False}
    assert openclaw._configure_engine(ctx, live_doc(), agy_world, "/kit", []) is False
    assert patches == []


def test_no_google_provider_is_added_by_the_engine_choice(agy_world, monkeypatch):
    s = saved_state()
    ans = Answers(monkeypatch)
    openclaw._prompt_engine(s)
    assert not any("google" in m.lower() and "added" in m.lower() for m in ans.infos)
    assert "google" not in (s.cockpit_auth.get("openclaw", {}).get("providers") or {})


def test_choosing_antigravity_shows_old_to_new_and_needs_an_explicit_confirm(agy_world, monkeypatch):
    s = saved_state()
    ans = Answers(monkeypatch, select={"engine": "antigravity"}, confirm=True)
    openclaw._prompt_engine(s)
    shown = [m for m in ans.infos if "->" in m]
    assert shown and "anthropic/claude-sonnet-5-5" in shown[0] and "agy-cli/" in shown[0]
    assert len(ans.confirms) == 1 and ans.confirms[0][1] is False        # default No
    assert s.openclaw.engine == "antigravity"


def test_a_declined_migration_falls_back_to_keep_and_writes_nothing(agy_world, monkeypatch):
    s = saved_state()
    Answers(monkeypatch, select={"engine": "antigravity"}, confirm=False)
    openclaw._prompt_engine(s)
    assert s.openclaw.engine == "keep"
    patches = []
    monkeypatch.setattr(openclaw, "_apply_hot", lambda patch, **k: patches.append(patch) or (True, "ok"))
    openclaw._configure_engine({"state": s, "dry_run": False}, live_doc(), agy_world, "/kit", [])
    assert patches == []


def test_the_applied_engine_never_reads_antigravity_from_saved_state_when_live_has_agents(agy_world):
    s = saved_state()
    s.openclaw.applied = True
    s.openclaw.model = "gemini-3.8-flash-low"
    assert openclaw.applied_engine(s) is not None            # no live reading yet: the saved answer
    openclaw._set_live(s, live_doc())
    assert openclaw.applied_engine(s) is None


def test_engine_for_live_maps_runtimes():
    assert openclaw.engine_for_live("agy-cli/gemini-3.8-flash-low", "agy-cli") == "antigravity"
    assert openclaw.engine_for_live("anthropic/claude-sonnet-5", "claude-cli") == "claude-code"
    assert openclaw.engine_for_live("openai/gpt-5.6-sol", "codex") == "codex"
    assert openclaw.engine_for_live("anthropic/claude-sonnet-5-5", "openclaw") == "keep"
    assert live_openclaw.live_default(live_doc())[0] == "anthropic/claude-sonnet-5-5"


def test_a_live_claude_cli_engine_keeps_its_live_model_as_the_default(agy_world, monkeypatch):
    doc = live_doc()
    ref = "anthropic/claude-sonnet-5"
    doc["agents"]["defaults"]["model"] = {"primary": ref}
    for e in doc["agents"]["entries"].values():
        e.pop("model", None)
    doc["agents"]["defaults"]["models"] = {ref: {"agentRuntime": {"id": "claude-cli"}}}
    agy_world.write_text(json.dumps(doc), encoding="utf-8")
    s = saved_state()
    assert openclaw.default_engine(s, doc) == "claude-code"


def test_the_live_agy_model_is_the_default_even_when_the_engine_offers_bare_ids(agy_world, monkeypatch):
    """The live ref is `agy-cli/<id>`; the engine offers bare ids. Enter must keep the model that already runs, with no
    spurious old -> new confirm."""
    models = openclaw.engine_models(openclaw.ENGINES["antigravity"])
    live_id = models[1]
    doc = live_doc()
    doc["agents"]["defaults"]["model"] = {"primary": f"agy-cli/{live_id}"}
    doc["agents"]["defaults"]["models"] = {f"agy-cli/{live_id}": {"agentRuntime": {"id": "agy-cli"}}}
    for e in doc["agents"]["entries"].values():
        e.pop("model", None)
    agy_world.write_text(json.dumps(doc), encoding="utf-8")
    s = saved_state()
    s.openclaw.engine = "antigravity"
    openclaw._set_live(s, doc)
    ans = Answers(monkeypatch)
    openclaw._prompt_engine_model(s)
    model_q = [d for m, d in ans.selects if "Model for OpenClaw" in m]
    assert model_q == [live_id]
    assert s.openclaw.model == live_id and ans.confirms == []
    assert s.openclaw.engine == "antigravity"


def test_a_stale_saved_engine_is_asked_again_by_the_wizard(agy_world):
    from ai_resources.setup import wizard
    s = saved_state()
    assert openclaw.engine_unsettled(s) is True
    assert wizard._openclaw_engine_unasked(s) is True
    s.openclaw.engine = "keep"
    assert wizard._openclaw_engine_unasked(s) is False


def test_an_unattended_run_never_applies_a_saved_engine_over_a_different_live_one(agy_world, monkeypatch):
    s = saved_state()
    s.cockpits["agy"] = state.CockpitState(installed=True)
    s.openclaw.risk_acknowledged = True
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    patches = []
    monkeypatch.setattr(openclaw, "_apply_hot", lambda patch, **k: patches.append(patch) or (True, "ok"))
    openclaw._set_live(s, live_doc())
    assert openclaw._configure_engine({"state": s, "dry_run": False}, live_doc(), agy_world, "/kit", []) is False
    assert patches == []
