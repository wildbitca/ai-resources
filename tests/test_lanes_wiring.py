"""Production call sites of the lanes work (ADR-0007, P2 follow-ups): explain wiring, the wizard failure
branch, the status/update warning call sites, the stale flag through the real loader, and the teardown guard."""
from __future__ import annotations

import copy
import json
import os

import pytest
import yaml

from ai_resources import cli, models_cmd, model_pins as mp
from ai_resources.scoring import flow, lanes as scoring_lanes
from ai_resources.setup import lanes_render, litellm, profiles, state, wizard
from ai_resources.setup.cockpits import openclaw as cockpit, openclaw_lanes
from test_models_update import Env
from test_openclaw_lanes_setup import BASE, LANES, FakeOpenClaw, _run, _s, host as oc_host  # noqa: F401
from test_wizard_compose import compose_flags, host, multi_state  # noqa: F401
from wizard_fakes import Script

PROFILE = "subscription-claude-55"


def _lanes_state(mode="single-model"):
    s = state.SetupState()
    s.mode = mode
    s.profile.name = PROFILE
    doc = profiles.load_profile(PROFILE)
    s.profile.lanes = lanes_render.propose(doc, doc["by_role"], "single-model", orchestrators=["main"])
    return s


# --- explain Lane column through the production callers -------------------------------------------------

def _compose(monkeypatch, tmp_path, lanes):
    why = tmp_path / "why.json"
    monkeypatch.setattr(wizard, "_COMPOSE", compose_flags(why_json=str(why), mode="uniform"))
    monkeypatch.setattr(flow, "validate_why_json_path", lambda p: tmp_path / "why.json")
    script = Script(**{"Allow Claude Fable": False, "Use this team": True})
    script.install(monkeypatch)
    s = multi_state()
    s.profile.lanes = lanes
    assert wizard._compose_profile(s, profiles.RECOMMENDED_PROFILE, "uniform") == 0
    return script.transcript, json.loads(why.read_text())


def test_wizard_composition_table_and_why_json_carry_the_lane_column(host, monkeypatch, tmp_path):
    lanes = _lanes_state().profile.lanes
    text, doc = _compose(monkeypatch, tmp_path, lanes)
    assert "lane" in text.splitlines()[text.splitlines().index(next(l for l in text.splitlines() if l.startswith("role")))].split()
    assert "openclaw-agent:kit-lane-cli-sonnet55" in text
    assert doc["lanes"]["implementer"].startswith("openclaw-agent:kit-lane-cli-sonnet55")


def test_wizard_output_without_lanes_is_unchanged(host, monkeypatch, tmp_path):
    text, doc = _compose(monkeypatch, tmp_path, None)
    assert "lanes" not in doc and not any("lane" == c for l in text.splitlines() for c in l.split())
    assert all("lane" not in row for row in doc["rows"]) if "rows" in doc else True


def test_write_why_json_without_lanes_is_byte_identical(tmp_path):
    from test_scoring_explain import fixture_composition
    comp = fixture_composition()
    flow.write_why_json(tmp_path / "a.json", comp)
    flow.write_why_json(tmp_path / "b.json", comp, None)
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    assert (tmp_path / "a.json").read_text() == __import__("ai_resources.scoring.explain", fromlist=["x"]).dumps(comp)


# --- --no-lanes --lanes-dry-run writes nothing ----------------------------------------------------------

def test_no_lanes_with_lanes_dry_run_clears_and_writes_nothing():
    import argparse
    s = _lanes_state()
    lanes_render.sync_file(s)
    before_state, before_file = state._to_dict(s), state.lanes_path().read_text()
    ns = argparse.Namespace(lanes=False, no_lanes=True, lanes_dry_run=True, lanes_orchestrator=[])
    assert wizard._step_lanes(s, ns) == 0
    assert state._to_dict(s) == before_state and s.profile.lanes
    assert lanes_render.sync_file(s)["action"] == "unchanged"
    assert state.lanes_path().read_text() == before_file


@pytest.mark.parametrize("flags", [dict(lanes=False, no_lanes=True), dict(lanes=True, no_lanes=False)])
def test_lanes_dry_run_makes_the_whole_run_a_dry_run(monkeypatch, flags):
    import argparse
    seen = {}
    monkeypatch.setattr(wizard.ui, "require_deps", lambda: None)
    monkeypatch.setattr(wizard.ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(wizard.ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(wizard.ui, "console", lambda: type("C", (), {"print": lambda self, *a, **k: None, "rule": lambda self, *a, **k: None})())
    monkeypatch.setattr(wizard.ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(wizard, "_preflight_selection", lambda a, s: 0)
    monkeypatch.setattr(wizard, "_preflight_compose", lambda: 0)
    monkeypatch.setattr(wizard, "_step2_cockpits", lambda s: 0)
    monkeypatch.setattr(wizard, "_step1_mode", lambda s: 0)
    monkeypatch.setattr(wizard, "_step_models", lambda s, a, d=False: 0)
    monkeypatch.setattr(wizard, "_step6_single_model_profile", lambda s: 0)
    monkeypatch.setattr(wizard, "_step_lanes", lambda s, a, dry_run=False: seen.setdefault("lanes", dry_run) and 0)
    monkeypatch.setattr(wizard, "_step7_cockpit_config", lambda s, dry_run=False: 0)
    monkeypatch.setattr(wizard, "_step_summary", lambda s, a: 0)
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("apply", dry_run) and 0)
    monkeypatch.setattr(state, "save", lambda s: seen.setdefault("saved", True))
    s = state.SetupState()
    s.mode = "single-model"
    monkeypatch.setattr(state, "load", lambda: s)
    ns = argparse.Namespace(lanes_dry_run=True, lanes_orchestrator=[], **flags)
    assert wizard._run(ns) == 0
    assert seen.get("apply") is True and seen.get("lanes") is True
    assert "saved" not in seen


# --- real wizard failure branch -------------------------------------------------------------------------

def test_step9_litellm_write_failure_restores_lanes_yaml(monkeypatch):
    Script().install(monkeypatch)
    s = _lanes_state()
    s.mode, s.backend = "multi-model", "litellm"
    s.litellm.deployment = "local"
    s.profile.name = "balanced-claude-55"
    old = copy.deepcopy(s.profile.lanes)
    lanes_render.sync_file(s)
    before = state.lanes_path().read_text()
    s.profile.lanes["roles"]["implementer"]["candidates"] = ["claude-code"]

    def boom(*a, **k):
        assert state.lanes_path().read_text() != before        # the new table was written first
        raise RuntimeError("write failed")
    monkeypatch.setattr(litellm, "write_configs", boom)
    assert wizard._step9_apply(s) == 1
    assert state.lanes_path().read_text() == before
    assert str(state.lanes_path()) in s.tracking.config_files_written
    assert old["roles"]["implementer"]["candidates"] != ["claude-code"]


def test_step9_failure_without_a_prior_file_removes_the_new_one(monkeypatch):
    Script().install(monkeypatch)
    s = _lanes_state()
    s.mode, s.backend = "multi-model", "litellm"
    s.litellm.deployment = "local"
    s.profile.name = "balanced-claude-55"
    monkeypatch.setattr(litellm, "write_configs", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert wizard._step9_apply(s) == 1
    assert not state.lanes_path().exists()
    assert str(state.lanes_path()) not in s.tracking.config_files_written


# --- status / update call sites -------------------------------------------------------------------------

@pytest.fixture
def models_env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    s = _lanes_state()
    s.profile.customized = True
    s.profile.customizations = {"implementer": {"model": "claude-sonnet-5-6"}}
    monkeypatch.setattr(state, "load", lambda: s)
    return e


WARN = "warning: lane agent kit-lane-cli-sonnet5"


def test_models_status_prints_the_lane_warning(models_env, capsys):
    assert cli.main(["models", "status"]) == 0
    assert WARN in capsys.readouterr().out


def test_models_update_prints_the_lane_warning_and_json_stays_clean(models_env, capsys):
    cli.main(["models", "update", "--dry-run"])
    assert WARN in capsys.readouterr().out
    cli.main(["models", "update", "--dry-run", "--json"])
    assert WARN not in capsys.readouterr().out


# --- the rendered lanes.yaml through the real loader ----------------------------------------------------

def test_rendered_lanes_yaml_stale_flag_is_honoured_by_the_loader():
    s = _lanes_state()
    doc = yaml.safe_load(lanes_render.dumps(
        lanes_render.render_doc(s.profile.lanes, {"implementer": {"model": "claude-sonnet-5-6"}}, s.mode)))
    assert doc["agents"]["kit-lane-cli-sonnet55"]["stale"] is True
    cands = {r: c["candidates"] for r, c in doc["roles"].items()}
    acks = {"anthropic": {"roles": sorted(cands)}}
    table = scoring_lanes.resolve_lanes({"implementer": {"model": "sonnet"}}, doc["agents"],
                                        {"implementer": cands["implementer"]}, doc["second"], acks=acks)
    assert table.roles["implementer"].ref == "claude-code"
    fresh = yaml.safe_load(lanes_render.dumps(
        lanes_render.render_doc(s.profile.lanes, {"implementer": {"model": "sonnet"}}, s.mode)))
    assert "stale" not in fresh["agents"]["kit-lane-cli-sonnet55"]
    t2 = scoring_lanes.resolve_lanes({"implementer": {"model": "sonnet"}}, fresh["agents"],
                                     {"implementer": cands["implementer"]}, fresh["second"], acks=acks)
    assert t2.roles["implementer"].ref.startswith("openclaw-agent:kit-lane-cli-sonnet55")


# --- teardown: whole document and containment ----------------------------------------------------------

def test_teardown_restores_the_whole_document_byte_for_byte(oc_host):
    before = oc_host.path.read_bytes()
    s = _s()
    _run(s, oc_host)
    assert oc_host.path.read_bytes() != before
    assert cockpit._teardown_lanes(s, oc_host.doc()) is True
    assert json.loads(oc_host.path.read_text()) == BASE
    assert json.dumps(oc_host.doc(), indent=2) == json.dumps(BASE, indent=2)
    assert s.openclaw.lanes_applied == {}


def _applied(ws, root, ids=()):
    s = state.SetupState()
    s.openclaw.lanes_applied = {"created_workspaces": [str(w) for w in ws], "workspace_root": str(root),
                                "created_agent_ids": list(ids)}
    return s


def _teardown(s, root):
    errs = []
    ok = openclaw_lanes.teardown(s, {}, config_root=root, apply_patch=lambda p, **k: (True, ""),
                                 oc=lambda a: (0, ""), error=errs.append, info=lambda m: None)
    return ok, errs


def test_teardown_removes_a_contained_kit_lane_workspace(tmp_path):
    root = tmp_path / "kit-lanes"
    ws = root / "kit-lane-cli-x"
    ws.mkdir(parents=True)
    s = _applied([ws], root)
    ok, errs = _teardown(s, root)
    assert ok and not ws.exists() and errs == [] and s.openclaw.lanes_applied == {}


def test_teardown_refuses_workspaces_that_are_not_contained(tmp_path):
    root = tmp_path / "kit-lanes"
    root.mkdir()
    outside = tmp_path / "kit-lane-outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    wrong_name = root / "important"
    wrong_name.mkdir()
    target = tmp_path / "victim"
    target.mkdir()
    (target / "keep").write_text("x")
    link = root / "kit-lane-link"
    os.symlink(target, link)
    s = _applied([outside, wrong_name, link, root], root)
    ok, errs = _teardown(s, root)
    assert ok and len(errs) == 4
    assert (outside / "keep").exists() and wrong_name.exists() and (target / "keep").exists()
    assert link.is_symlink() and root.exists()
    assert len(s.openclaw.lanes_applied["created_workspaces"]) == 4
