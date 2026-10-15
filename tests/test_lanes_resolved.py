"""`lanes.yaml` carries the resolver's choice and `lanes prepare --lane` binds dispatch to it (architect B1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_resources import lanes_cmd
from ai_resources.scoring import lanes as scoring_lanes
from ai_resources.setup import lanes_render, state
from test_lanes_cmd import env, sh  # noqa: F401

AGENT = "kit-lane-cli-sonnet55"
ANTHROPIC_ACK = {"anthropic": {"roles": ["implementer", "code-reviewer", "explore", "verifier"],
                               "text_sha256": __import__("hashlib").sha256(
                                   lanes_render.ACK_TEXT["anthropic"].encode()).hexdigest()}}
BY_ROLE = {"implementer": {"model": "sonnet"}, "code-reviewer": {"model": "sonnet"},
           "explore": {"model": "sonnet"}, "verifier": {"model": "opus"}}


@pytest.fixture(autouse=True)
def _config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def _lanes(candidates=(f"openclaw-agent:{AGENT}", "claude-code")):
    return {"agents": {AGENT: {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                               "pool": "claude-subscription"}},
            "roles": {"implementer": {"candidates": list(candidates)}},
            "second": {}, "orchestrators": [], "verify_capability": {}, "pool_caps": {},
            "max_remote_concurrency": 2, "workspace_root": ""}


def _write(lanes=None, mode="single-model"):
    doc = lanes_render.render_doc(lanes or _lanes(), BY_ROLE, mode, ANTHROPIC_ACK)
    path = state.lanes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lanes_render.dumps(doc))
    return doc


def test_render_doc_carries_the_resolved_lane_reason_and_ordered_fallbacks():
    doc = lanes_render.render_doc(_lanes(), BY_ROLE, "single-model", ANTHROPIC_ACK)
    got = doc["resolved"]["roles"]["implementer"]
    assert got["lane"] == f"openclaw-agent:{AGENT}" and got["reason"]
    assert [f["lane"] for f in got["fallbacks"]] == ["claude-code"]
    assert all(f["reason"] for f in got["fallbacks"])
    assert doc["digest"] == lanes_render.doc_digest(doc)


def test_the_resolved_lane_follows_the_tier_rule_not_the_declared_order():
    # claude-code first in the profile order, the lane has the same pool: declared order breaks the tie
    doc = lanes_render.render_doc(_lanes(("claude-code", f"openclaw-agent:{AGENT}")), BY_ROLE,
                                  "single-model", ANTHROPIC_ACK)
    got = doc["resolved"]["roles"]["implementer"]
    assert got["lane"] == "claude-code"
    assert [f["lane"] for f in got["fallbacks"]] == [f"openclaw-agent:{AGENT}"]
    # no acknowledgement: the lane is not admitted, so it is not a fallback either
    doc = lanes_render.render_doc(_lanes(("claude-code", f"openclaw-agent:{AGENT}")), BY_ROLE,
                                  "single-model", {})
    assert doc["resolved"]["roles"]["implementer"]["fallbacks"] == []


def test_prepare_requires_the_lane_once_lanes_yaml_exists(env):
    _write()
    rc, out = env.prepare("impl", "implementer", "report", "sonnet")
    assert rc == 1 and any("--lane" in r for r in out["reasons"]), out


def test_prepare_accepts_the_resolved_lane_and_records_it(env):
    _write()
    rc, out = env.run("prepare", "--step", "impl", "--role", "implementer", "--contract", "report",
                      "--model", "sonnet", "--lane", AGENT)
    assert rc == 0, out
    data = env.ledger_entries(env.state)[0]["data"]
    assert data["lane"]["source"] == "resolved" and data["lane"]["lane"] == f"openclaw-agent:{AGENT}"


def test_prepare_accepts_a_recorded_fallback_and_refuses_any_other_lane(env):
    _write()
    rc, out = env.run("prepare", "--step", "impl", "--role", "implementer", "--contract", "report",
                      "--model", "sonnet", "--lane", "claude-code")
    assert rc == 0 and env.ledger_entries(env.state)[0]["data"]["lane"]["source"] == "fallback"
    rc, out = env.run("prepare", "--step", "impl2", "--role", "implementer", "--contract", "report",
                      "--model", "sonnet", "--lane", "kit-lane-cli-opus55")
    assert rc == 1 and any("neither the resolved lane" in r for r in out["reasons"]), out


def test_a_hand_edited_lanes_yaml_is_refused(env):
    _write()
    path = state.lanes_path()
    path.write_text(path.read_text().replace("claude-code", "claude-code ", 1) + "\nextra: 1\n")
    rc, out = env.run("prepare", "--step", "impl", "--role", "implementer", "--contract", "report",
                      "--model", "sonnet", "--lane", "claude-code")
    assert rc == 1 and any("digest" in r for r in out["reasons"]), out


def test_a_blocked_checker_cannot_be_prepared(env):
    lanes = _lanes()
    lanes["roles"]["verifier"] = {"candidates": [f"openclaw-agent:{AGENT}"]}   # sonnet lane for an opus verifier
    doc = _write(lanes)
    assert "verifier" in doc["resolved"]["blocked"]
    rc, out = env.run("prepare", "--step", "v", "--role", "verifier", "--contract", "report",
                      "--model", "opus", "--lane", f"openclaw-agent:{AGENT}")
    assert rc == 1 and any("blocked" in r for r in out["reasons"]), out


def test_without_lanes_yaml_prepare_needs_no_lane(env):
    assert not state.lanes_path().exists()
    rc, out = env.prepare("impl", "implementer", "report", "sonnet")
    assert rc == 0, out
    assert "lane" not in env.ledger_entries(env.state)[0]["data"]


def test_a_second_member_is_refused_for_checker_roles():
    problems = lanes_render.validate_block({"second": {"verifier": ["claude-code"]}})
    assert any("checker role never takes a second member" in p for p in problems), problems
    with pytest.raises(scoring_lanes.LaneError, match="checker"):
        scoring_lanes.resolve_lanes({"verifier": {"model": "opus"}}, {}, {}, {"verifier": ["claude-code"]})
    assert lanes_render.validate_block({"second": {"code-reviewer": ["claude-code"]}}) == []
