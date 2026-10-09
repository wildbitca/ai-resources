"""The artifact registry: ordered apply, reverse-order restore, and the affected set."""
from __future__ import annotations

import json
import pathlib
from dataclasses import replace

import pytest

from ai_resources import model_fanout as fo
from ai_resources import model_pins as mp
from ai_resources import models
from test_models_update import Env

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"


class Recorder(fo.Artifact):
    def __init__(self, art_id, fail=False, fail_restore=False, log=None, is_affected=True):
        self.id, self.fail, self.fail_restore, self.log, self._aff = art_id, fail, fail_restore, log if log is not None else [], is_affected

    def affected(self, change, ctx):
        return self._aff

    def render(self, change, ctx):
        self.log.append(("render", self.id))
        if self.fail:
            raise fo.ArtifactError(f"{self.id} could not render")
        return {"saved": self.id}

    def restore(self, payload, ctx):
        self.log.append(("restore", self.id, payload))
        if self.fail_restore:
            raise fo.ArtifactError("cannot restore")


CHANGE = fo.Change({"anthropic:sonnet": ("claude-sonnet-5", "claude-sonnet-5-5")})


def test_artifacts_render_in_order_and_a_failure_restores_the_earlier_ones_in_reverse():
    log = []
    arts = [Recorder("a", log=log), Recorder("b", log=log), Recorder("c", fail=True, log=log), Recorder("d", log=log)]
    res = fo.apply_all(arts, CHANGE, fo.Ctx())
    assert not res.ok and res.failed == "c" and res.message == "c could not render"
    assert log == [("render", "a"), ("render", "b"), ("render", "c"),
                   ("restore", "b", {"saved": "b"}), ("restore", "a", {"saved": "a"})]
    assert res.restored == ["b", "a"]


def test_an_artifact_that_is_not_affected_is_skipped():
    log = []
    res = fo.apply_all([Recorder("a", log=log, is_affected=False), Recorder("b", log=log)], CHANGE, fo.Ctx())
    assert res.ok and [e[1] for e in log] == ["b"] and list(res.payloads) == ["b"]
    assert fo.affected_ids([Recorder("a", is_affected=False), Recorder("b")], CHANGE, fo.Ctx()) == ["b"]


def test_a_restore_error_is_reported_and_the_unwind_continues():
    log = []
    arts = [Recorder("a", log=log), Recorder("b", fail_restore=True, log=log), Recorder("c", fail=True, log=log)]
    res = fo.apply_all(arts, CHANGE, fo.Ctx())
    assert res.restored == ["a"] and res.restore_errors == ["b: cannot restore"]


def test_restore_all_walks_the_recorded_payloads_backwards():
    log = []
    arts = [Recorder("a", log=log), Recorder("b", log=log)]
    ok, _ = fo.restore_all(arts, {"a": {"x": 1}, "b": {"y": 2}}, fo.Ctx())
    assert ok and [e[1] for e in log] == ["b", "a"]


def test_an_unknown_recorded_artifact_cannot_be_restored():
    ok, msg = fo.restore_all([Recorder("a")], {"zzz": {}}, fo.Ctx())
    assert not ok and "zzz" in msg


# --- through models.apply_changes / run_update ---------------------------------------------------

def test_a_later_artifact_failing_restores_openclaw_json_by_inverse_patch_and_exits_rolled_back(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    deps = replace(env.deps(), artifacts=lambda: [Recorder("files", fail=True)])
    r = models.run_update(models.Options(classes=["sonnet"]), deps)
    assert r.rc == models.EXIT_ROLLED_BACK and r.outcome == "rolled_back"
    forward, inverse = [p for p, dry in env.patches if not dry][:2]
    assert models.apply_in_memory(models.apply_in_memory(json.loads(env.cfg.read_text()), forward), inverse) \
        == json.loads(env.cfg.read_text())
    assert env.doc["agents"]["entries"]["main"]["model"]["primary"] == "anthropic/claude-sonnet-5"
    assert "anthropic:sonnet" not in env.state().get("pins", {})          # the overlay was never saved
    assert env.restarts == 0


def test_the_openclaw_artifact_failing_first_stays_a_precheck_failure(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, rollback_ok=True)
    env.apply_patch = lambda patch, *, dry_run=False, replace_paths=None: (False, "schema error")
    r = models.run_update(models.Options(classes=["sonnet"]), env.deps())
    assert r.rc == models.EXIT_PRECHECK_FAILED and r.outcome == "patch_rejected"


def test_a_registered_artifact_payload_is_recorded_and_restored_by_a_manual_rollback(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    log = []
    deps = replace(env.deps(), artifacts=lambda: [Recorder("files", log=log)])
    assert models.run_update(models.Options(classes=["sonnet"]), deps).rc == 0
    assert env.state()["state"]["last_change"]["artifacts"]["files"] == {"saved": "files"}
    env.restarts = 0
    assert models.run_rollback(deps).rc == 0
    assert ("restore", "files", {"saved": "files"}) in log


def test_claude_only_apply_records_the_same_inverse_as_before(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    models.run_update(models.Options(classes=["sonnet"]), env.deps())
    last = env.state()["state"]["last_change"]
    assert last["inverse"] == last["artifacts"]["openclaw"]["inverse"] and last["inverse"]


def test_a_failed_unwind_of_openclaw_json_is_rollback_failed_not_patch_rejected(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    real = env.apply_patch
    real_calls = []

    def flaky(patch, *, dry_run=False, replace_paths=None):
        if not dry_run:
            real_calls.append(1)
            if len(real_calls) > 1:                      # the inverse patch
                return False, "boom"
        return real(patch, dry_run=dry_run, replace_paths=replace_paths)

    env.apply_patch = flaky
    deps = replace(env.deps(), artifacts=lambda: [Recorder("files", fail=True)])
    r = models.run_update(models.Options(classes=["sonnet"]), deps)
    assert r.rc == models.EXIT_ROLLBACK_FAILED and r.outcome == "rollback_failed"
    assert env.state()["state"]["last_result"] == "rollback_failed"
