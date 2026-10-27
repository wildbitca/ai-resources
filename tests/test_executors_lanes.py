"""`lanes.yaml` (ADR-0007, plan 4.2): writer, tracking, backup/restore, drift, explain column, verify warnings."""
from __future__ import annotations

import builtins
import pathlib

import pytest
import yaml

from ai_resources import model_fanout, model_rerender, roles_policy, verify
from ai_resources.scoring import explain, lanes as scoring_lanes
from ai_resources.setup import lanes_render, profiles, state, wizard

PROFILE = "subscription-claude-55"


def _state(mode="single-model", *, with_lanes=True):
    s = state.SetupState()
    s.mode = mode
    s.profile.name = PROFILE if mode == "single-model" else "balanced-claude-55"
    if with_lanes:
        doc = profiles.load_profile(PROFILE)
        s.profile.lanes = lanes_render.propose(doc, doc["by_role"], "single-model", orchestrators=["main"])
    return s


def _by_role(s):
    return lanes_render.current_by_role(s)


# --- profile block -> state ------------------------------------------------------------------------------

def test_profile_block_validates_and_names_no_agent_id():
    doc = profiles.load_profile(PROFILE)
    assert lanes_render.validate_block(doc["lanes"]) == []
    frag = lanes_render.state_from_block(doc["lanes"])
    assert "kit-lane-cli-sonnet55" in frag["agents"] and "kit-lane-cli-opus55" in frag["agents"]
    assert frag["agents"]["kit-lane-cli-sonnet55"] == {
        "backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5", "pool": "claude-subscription"}


@pytest.mark.parametrize("block,needle", [
    ({"roles": {"nope": ["claude-code"]}}, "not lane-eligible"),
    ({"roles": {"implementer": ["kit-lane-cli-sonnet55"]}}, "bad candidate"),
    ({"roles": {"implementer": []}}, "non-empty"),
    ({"second": {"implementer": ["claude-code"]}}, "unknown key"),
    ({"agents": {}}, "unknown key"),
])
def test_invalid_blocks_are_rejected(block, needle):
    assert any(needle in p for p in lanes_render.validate_block(block))


def test_checker_on_the_writers_family_is_a_lane_conflict():
    doc = {"lanes": {"roles": {"implementer": ["claude-cli:sonnet"], "verifier": ["claude-cli:sonnet"]}}}
    by_role = {"implementer": {"model": "sonnet"}, "verifier": {"model": "sonnet"}}
    with pytest.raises(scoring_lanes.LaneConflict):
        lanes_render.propose(doc, by_role, "single-model")


# --- the writer ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["single-model", "multi-model"])
def test_lanes_yaml_is_written_and_tracked_in_both_modes(mode):
    s = _state(mode)
    res = lanes_render.sync_file(s)
    path = state.lanes_path()
    assert res["action"] == "written" and path.is_file()
    doc = yaml.safe_load(path.read_text())
    assert doc["mode"] == mode and "kit-lane-cli-sonnet55" in doc["agents"]
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
    s.profile.lanes["orchestrators"] = ["other"]
    s.profile.lanes["pool_caps"] = {}
    s.profile.lanes["roles"]["implementer"]["candidates"] = ["claude-code"]
    res = lanes_render.sync_file(s, backup_dir=state.config_root() / "bk")
    assert res["action"] == "written" and state.lanes_path().read_text() != before
    assert pathlib.Path(res["payload"]["backup"]).read_text() == before
    lanes_render.restore(res["payload"])
    assert state.lanes_path().read_text() == before


def test_wizard_restores_lanes_yaml_when_a_later_artifact_fails(monkeypatch):
    s = _state("multi-model")
    s.profile.customized = False
    lanes_render.sync_file(s)
    before = state.lanes_path().read_text()
    s.profile.lanes["roles"]["implementer"]["candidates"] = ["claude-code"]
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


# --- drift -----------------------------------------------------------------------------------------------

def test_alias_roles_do_not_drift_and_exact_ref_roles_do():
    s = _state()
    assert lanes_render.drift(s.profile.lanes, _by_role(s)) == []
    exact = {"implementer": {"model": "claude-sonnet-5-6"}}
    d = lanes_render.drift(s.profile.lanes, exact)
    assert [x["agent"] for x in d] == ["kit-lane-cli-sonnet55"]
    assert "re-run `ai-resources setup --lanes`" in lanes_render.drift_warnings(s.profile.lanes, exact)[0]
    same = {"implementer": {"model": "claude-sonnet-5-5"}}
    assert lanes_render.drift(s.profile.lanes, same) == []


def test_family_alias_mismatch_is_drift():
    s = _state()
    d = lanes_render.drift(s.profile.lanes, {"implementer": {"model": "opus"}})
    assert d and d[0]["agent"] == "kit-lane-cli-sonnet55"


def test_render_marks_the_drifting_agent_stale_and_lanes_py_refuses_it():
    s = _state()
    doc = lanes_render.render_doc(s.profile.lanes, {"implementer": {"model": "claude-sonnet-5-6"}}, s.mode)
    assert doc["agents"]["kit-lane-cli-sonnet55"]["stale"] is True
    assert "stale" not in doc["agents"]["kit-lane-cli-opus55"]
    agent = scoring_lanes.LaneAgent("kit-lane-cli-sonnet55", "claude-cli", "anthropic/claude-sonnet-5-5", stale=True)
    table = scoring_lanes.resolve_lanes({"implementer": {"model": "sonnet"}}, {agent.id: agent},
                                        {"implementer": [f"openclaw-agent:{agent.id}"]},
                                        acks={"anthropic": {"roles": ["implementer"]}})
    assert table.roles["implementer"].ref == "claude-code"


# --- fan-out artifact ------------------------------------------------------------------------------------

def _artifact(s, by_role):
    return model_rerender.LanesArtifact(state.lanes_path(), state=s, by_role=lambda: by_role)


def test_fanout_artifact_marks_stale_with_backup_and_restores():
    s = _state()
    lanes_render.sync_file(s)
    before = state.lanes_path().read_text()
    by_role = {"implementer": {"model": "claude-sonnet-5-5"}}
    art = _artifact(s, by_role)
    change = model_fanout.Change({"anthropic:sonnet": ("claude-sonnet-5-5", "claude-sonnet-5-6")})
    ctx = model_fanout.Ctx(extras={"backup_dir": state.config_root() / "bk"})
    assert art.affected(change, ctx)
    res = model_fanout.apply_all([art], change, ctx)
    assert res.ok
    assert yaml.safe_load(state.lanes_path().read_text())["agents"]["kit-lane-cli-sonnet55"]["stale"] is True
    ok, _ = model_fanout.restore_all([art], res.payloads, ctx)
    assert ok and state.lanes_path().read_text() == before


def test_fanout_artifact_is_not_affected_for_alias_roles_or_without_lanes():
    s = _state()
    by_role = {"implementer": {"model": "sonnet"}}
    lanes_render.sync_file(s, by_role=by_role)       # the `resolved:` section depends on the role table
    change = model_fanout.Change({"anthropic:sonnet": ("claude-sonnet-5-5", "claude-sonnet-5-6")})
    assert not _artifact(s, by_role).affected(change, model_fanout.Ctx())
    bare = _state(with_lanes=False)
    boom = _artifact(bare, {})
    boom._by_role = lambda: (_ for _ in ()).throw(AssertionError("read the role table with no lanes"))
    assert not boom.affected(change, model_fanout.Ctx())


def test_fanout_failure_restores_lanes_yaml_in_reverse_order():
    s = _state()
    lanes_render.sync_file(s)
    before = state.lanes_path().read_text()

    class Boom(model_fanout.Artifact):
        id = "boom"

        def affected(self, change, ctx):
            return True

        def render(self, change, ctx):
            raise model_fanout.ArtifactError("nope")

    art = _artifact(s, {"implementer": {"model": "claude-sonnet-5-5"}})
    change = model_fanout.Change({"anthropic:sonnet": ("claude-sonnet-5-5", "claude-sonnet-5-6")})
    res = model_fanout.apply_all([art, Boom()], change, model_fanout.Ctx(extras={"backup_dir": state.config_root() / "bk"}))
    assert not res.ok and res.restored == ["lanes"]
    assert state.lanes_path().read_text() == before


def test_render_paths_never_open_openclaw_json(monkeypatch):
    seen: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **k):
        seen.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    real_read = pathlib.Path.read_text

    def spy_read(self, *a, **k):
        seen.append(str(self))
        return real_read(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", spy_read)
    s = _state()
    lanes_render.sync_file(s)
    change = model_fanout.Change({"anthropic:sonnet": ("claude-sonnet-5-5", "claude-sonnet-5-6")})
    model_fanout.apply_all([_artifact(s, {"implementer": {"model": "claude-sonnet-5-5"}})], change,
                           model_fanout.Ctx(extras={"backup_dir": state.config_root() / "bk"}))
    assert not [p for p in seen if p.endswith("openclaw.json")]


# --- explain ---------------------------------------------------------------------------------------------

def test_explain_lane_column_only_with_a_table():
    from test_scoring_explain import fixture_composition
    comp = fixture_composition()
    base = explain.render_text(comp)
    assert "lane" not in base.splitlines()[1].split()
    assert explain.to_json(comp) == explain.to_json(comp, None)
    assert "lanes" not in explain.to_json(comp)
    with_lane = explain.render_text(comp, {"implementer": "openclaw-agent:kit-lane-cli-sonnet55"})
    assert with_lane.splitlines()[1].rstrip().endswith("lane")
    assert explain.render_text(comp, None) == base and explain.render_text(comp, {}) == base
    assert explain.dumps(comp) == explain.dumps(comp, None)
    assert '"lanes"' in explain.dumps(comp, {"implementer": "claude-code"})


# --- verify ----------------------------------------------------------------------------------------------

def test_verify_warns_on_missing_stale_and_drifting_lanes():
    s = _state()
    assert verify.lanes_findings(state.SetupState()) == []
    f = verify.lanes_findings(s)
    assert [x.level for x in f] == ["warn"] and "missing" in f[0].message
    lanes_render.sync_file(s)
    f = verify.lanes_findings(s)
    assert [x.level for x in f] == ["ok"]
    state.lanes_path().write_text("# stale\n")
    assert "out of date" in verify.lanes_findings(s)[0].message
    s.profile.customized = True
    s.profile.customizations = {"implementer": {"model": "claude-sonnet-5-6"}}
    msgs = [x.message for x in verify.lanes_findings(s)]
    assert any("lane agent kit-lane-cli-sonnet55 model" in m for m in msgs)


def test_models_update_and_status_print_the_lane_warning(monkeypatch, capsys):
    from ai_resources import models_cmd
    s = _state()
    s.profile.customized = True
    s.profile.customizations = {"implementer": {"model": "claude-sonnet-5-6"}}
    monkeypatch.setattr(state, "load", lambda: s)
    models_cmd.print_lane_warnings()
    out = capsys.readouterr().out
    assert "warning: lane agent kit-lane-cli-sonnet55 model anthropic/claude-sonnet-5-5 differs" in out


# --- AC3.15: a phase 2 state re-renders byte-identical ----------------------------------------------------

def _phase_two_state():
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.name = PROFILE
    s.profile.lanes = {
        "agents": {
            "kit-lane-cli-sonnet55": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                      "pool": "claude-subscription"},
            "kit-lane-cli-opus55": {"backend": "claude-cli", "model": "anthropic/claude-opus-5-5",
                                    "pool": "claude-subscription"}},
        "roles": {"implementer": {"candidates": ["openclaw-agent:kit-lane-cli-sonnet55", "claude-code"]},
                  "tester": {"candidates": ["openclaw-agent:kit-lane-cli-sonnet55", "claude-code"]},
                  "verifier": {"candidates": ["openclaw-agent:kit-lane-cli-opus55", "claude-code"]}},
        "orchestrators": ["main"]}
    s.profile.lane_acks = {"anthropic": lanes_render.make_ack(
        "anthropic", ["implementer", "tester", "verifier", "doc-writer", "planner"], "2026-01-01T00:00:00Z")}
    return s


def test_a_phase_two_state_rerenders_byte_identical_to_the_post_b1_baseline():
    # The fixture was rendered from this exact (ADR-0018 regenerated it to add the `routing:` section; the rest is byte-identical to a513e6b)
    # state, digest included. A changed value, nested field or digest drift fails this byte comparison.
    by_role = {"implementer": {"model": "sonnet"}, "tester": {"model": "sonnet"}, "doc-writer": {"model": "sonnet"},
               "planner": {"model": "opus"}, "verifier": {"model": "opus"}}
    baseline = (pathlib.Path(__file__).parent / "fixtures" / "lanes_phase2" / "lanes-b1-c9.yaml").read_bytes()
    got = lanes_render.expected_text(_phase_two_state(), by_role).encode("utf-8")
    assert got == baseline
