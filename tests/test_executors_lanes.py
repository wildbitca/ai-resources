"""`lanes.yaml` (ADR-0019): generated from the routing matrix; writer, tracking, backup/restore, verify, old-state compat."""
from __future__ import annotations

import builtins
import pathlib

import pytest
import yaml

from ai_resources import verify
from ai_resources.setup import lanes_render, profiles, state, wizard

PROFILE = "subscription-claude-55"


def _state(mode="single-model", *, with_lanes=True):
    s = state.SetupState()
    s.mode = mode
    s.profile.name = PROFILE if mode == "single-model" else "balanced-claude-55"
    if with_lanes:
        s.profile.lanes = lanes_render.lanes_state(profiles.load_profile(PROFILE), orchestrators=["main"])
    return s


# --- profile -> state fragment -----------------------------------------------------------------------------

def test_lanes_state_keeps_orchestrators_and_routing_only():
    doc = {"routing": {"explore": {"primary": "claude-code:haiku"}}}
    frag = lanes_render.lanes_state(doc, orchestrators=["main", "claude", "main"])
    assert frag == {"orchestrators": ["claude", "main"], "routing": doc["routing"]}
    assert lanes_render.lanes_state({}, orchestrators=[]) == {"orchestrators": []}


def test_malformed_routing_override_is_a_block_error():
    with pytest.raises(lanes_render.LanesBlockError):
        lanes_render.lanes_state({"routing": {"nope": {"primary": "claude-code"}}})


def test_lane_roles_come_from_the_matrix():
    roles = lanes_render.lane_roles()
    assert "implementer" in roles and "main" not in roles


# --- the writer ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["single-model", "multi-model"])
def test_lanes_yaml_is_written_and_tracked_in_both_modes(mode):
    s = _state(mode)
    res = lanes_render.sync_file(s)
    path = state.lanes_path()
    assert res["action"] == "written" and path.is_file()
    doc = yaml.safe_load(path.read_text())
    assert doc["mode"] == mode and "implementer" in doc["routing"]["matrix"]
    assert str(path) in s.tracking.config_files_written
    assert lanes_render.sync_file(s)["action"] == "unchanged"
    assert s.tracking.config_files_written.count(str(path)) == 1


def test_no_lanes_no_file_and_executors_untouched():
    s = _state(with_lanes=False)
    assert lanes_render.sync_file(s)["action"] == "unchanged"
    assert not state.lanes_path().exists()
    assert not state.executors_path().exists()      # single-model mode never writes executors.yaml


def test_no_lanes_removes_the_file_and_its_tracking_entry():
    s = _state()
    lanes_render.sync_file(s)
    s.profile.lanes = None
    res = lanes_render.sync_file(s, backup_dir=state.config_root() / "bk")
    assert res["action"] == "removed" and not state.lanes_path().exists()
    assert str(state.lanes_path()) not in s.tracking.config_files_written


def test_backup_before_write_and_restore_from_its_own_backup():
    s = _state()
    lanes_render.sync_file(s)
    before = state.lanes_path().read_text()
    s.profile.lanes["routing"] = {"implementer": {"primary": "claude-code:opus"}}
    res = lanes_render.sync_file(s, backup_dir=state.config_root() / "bk")
    assert res["action"] == "written" and state.lanes_path().read_text() != before
    assert pathlib.Path(res["payload"]["backup"]).read_text() == before
    lanes_render.restore(res["payload"])
    assert state.lanes_path().read_text() == before


def test_wizard_restores_lanes_yaml_when_a_later_artifact_fails():
    s = _state("multi-model")
    lanes_render.sync_file(s)
    before = state.lanes_path().read_text()
    s.profile.lanes["routing"] = {"implementer": {"primary": "claude-code:opus"}}
    payload = wizard._apply_lanes_file(s, {"by_role": {"implementer": {"model": "sonnet"}}})
    assert state.lanes_path().read_text() != before
    wizard._restore_lanes_file(s, payload)
    assert state.lanes_path().read_text() == before
    assert str(state.lanes_path()) in s.tracking.config_files_written


def test_first_write_failure_restore_deletes_the_new_file_and_tracking():
    s = _state()
    payload = wizard._apply_lanes_file(s, {"by_role": {}})
    assert state.lanes_path().is_file()
    wizard._restore_lanes_file(s, payload)
    assert not state.lanes_path().exists()
    assert str(state.lanes_path()) not in s.tracking.config_files_written


def test_render_does_not_depend_on_the_models_of_the_roles():
    """A moved model id changes nothing in lanes.yaml: the file is a pure function of the matrix."""
    s = _state()
    s.profile.customized = True
    s.profile.customizations = {"implementer": {"model": "claude-sonnet-5-6"}}
    assert lanes_render.expected_text(s) == lanes_render.expected_text(_state())


def test_render_paths_never_open_openclaw_json(monkeypatch):
    seen: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **k):
        seen.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    lanes_render.sync_file(_state())
    assert not [p for p in seen if p.endswith("openclaw.json")]


# --- verify ----------------------------------------------------------------------------------------------

def test_verify_warns_on_missing_and_stale_lanes():
    s = _state()
    assert verify.lanes_findings(state.SetupState()) == []
    f = verify.lanes_findings(s)
    assert [x.level for x in f] == ["warn"] and "missing" in f[0].message
    lanes_render.sync_file(s)
    assert [x.level for x in verify.lanes_findings(s)] == ["ok"]
    state.lanes_path().write_text("# stale\n")
    assert "out of date" in verify.lanes_findings(s)[0].message
