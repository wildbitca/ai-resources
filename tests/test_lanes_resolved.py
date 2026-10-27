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
            "orchestrators": [], "verify_capability": {}, "pool_caps": {},
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
    assert rc == 1 and any("not an admitted kit lane agent" in r for r in out["reasons"]), out


def test_an_admitted_agent_that_is_not_a_candidate_of_the_role_is_refused(env):
    lanes = _lanes()
    lanes["agents"]["kit-lane-cli-opus55"] = {"backend": "claude-cli", "model": "anthropic/claude-opus-5-5",
                                              "pool": "claude-subscription"}
    _write(lanes)
    rc, out = _prep(env, "--lane", "kit-lane-cli-opus55", model="opus")
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




SKILL = Path(__file__).resolve().parents[1] / "skills" / "kit-orchestration" / "references" / "backends-and-lanes.md"
SKILL_WORDING = (
    "Dispatch what its `resolved:` section says for the role",
    "only when that fallback is recorded in `resolved:` with its reason",
    "never by reading `candidates` in order",
    "`lanes prepare --lane <claude-code|kit-lane-id>` (mandatory whenever `lanes.yaml` exists, `claude-code` "
    "steps included)",
)


@pytest.mark.parametrize("needle", SKILL_WORDING)
def test_skill_md_pins_the_resolved_dispatch_and_the_mandatory_lane_flag(needle):
    assert needle in SKILL.read_text(encoding="utf-8"), needle


def _prep(env, *extra, step="impl", role="implementer", model="sonnet"):
    return env.run("prepare", "--step", step, "--role", role, "--contract", "report", "--model", model, *extra)


def test_n1_the_declared_model_must_be_the_model_of_the_bound_lane(env):
    _write()
    rc, out = _prep(env, "--lane", AGENT, model="haiku")
    assert rc == 1 and any("is not the model of lane" in r for r in out["reasons"]), out
    rc, out = _prep(env, "--lane", "claude-code", model="haiku", step="impl2")
    assert rc == 1, out
    rc, out = _prep(env, "--lane", AGENT, model="anthropic/claude-sonnet-5-5", step="impl3")
    assert rc == 0, out
    rc, out = _prep(env, "--lane", "claude-code", model="sonnet", step="impl4")
    assert rc == 0, out


def test_n1_a_concrete_ref_of_another_version_is_refused(env):
    _write()
    rc, out = _prep(env, "--lane", AGENT, model="anthropic/claude-sonnet-5-4")
    assert rc == 1 and any("is not the model of lane" in r for r in out["reasons"]), out


def test_d2_n2_a_held_agy_agent_in_a_hand_edited_state_never_reaches_lanes_yaml(monkeypatch):
    monkeypatch.setattr(lanes_render, "AGY_LANE_ADR_ACCEPTED", False)      # the B3 contract: section 11 not accepted
    lanes = _lanes()
    lanes["agents"]["kit-lane-agy-evil"] = {"backend": "agy-cli", "model": "gemini-3.8-flash-low",
                                            "pool": "agy-gemini"}
    lanes["roles"]["implementer"]["candidates"].insert(0, "openclaw-agent:kit-lane-agy-evil")
    doc = lanes_render.render_doc(lanes, BY_ROLE, "single-model", ANTHROPIC_ACK)
    assert "kit-lane-agy-evil" not in doc["agents"]
    assert "openclaw-agent:kit-lane-agy-evil" not in doc["roles"]["implementer"]["candidates"]
    assert doc["resolved"]["held"] == {"kit-lane-agy-evil": "held: Antigravity lane not accepted"}


def test_d2_n2_with_section_11_accepted_the_same_agent_is_held_by_the_flag(monkeypatch):
    lanes = _lanes()
    lanes["agents"]["kit-lane-agy-evil"] = {"backend": "agy-cli", "model": "gemini-3.8-flash-low",
                                            "pool": "agy-gemini"}
    lanes["roles"]["implementer"]["candidates"].insert(0, "openclaw-agent:kit-lane-agy-evil")
    doc = lanes_render.render_doc(lanes, BY_ROLE, "single-model", ANTHROPIC_ACK)
    assert "kit-lane-agy-evil" not in doc["agents"]
    assert doc["resolved"]["held"] == {"kit-lane-agy-evil": "held: --lanes-antigravity is off"}


def test_n2_a_recomputed_digest_with_an_agy_agent_is_still_refused(env):
    doc = _write()
    doc["agents"]["kit-lane-agy-evil"] = {"backend": "agy-cli", "model": "gemini-3.8-flash-low",
                                          "pool": "agy-gemini"}
    doc["resolved"]["roles"]["implementer"] = {"lane": "openclaw-agent:kit-lane-agy-evil",
                                               "model": "gemini-3.8-flash-low", "reason": "x", "fallbacks": []}
    doc["digest"] = lanes_render.doc_digest(doc)
    state.lanes_path().write_text(lanes_render.dumps(doc))
    rc, out = _prep(env, "--lane", "kit-lane-agy-evil", model="gemini-3.8-flash-low")
    assert rc == 1 and any("not admitted" in r for r in out["reasons"]), out


def test_n2_an_agent_missing_from_the_table_is_refused(env):
    doc = _write()
    doc["resolved"]["roles"]["implementer"]["fallbacks"].append(
        {"lane": "openclaw-agent:kit-lane-cli-ghost", "model": "sonnet", "reason": "x"})
    doc["digest"] = lanes_render.doc_digest(doc)
    state.lanes_path().write_text(lanes_render.dumps(doc))
    rc, out = _prep(env, "--lane", "kit-lane-cli-ghost")
    assert rc == 1 and any("not an admitted kit lane agent" in r for r in out["reasons"]), out


def test_n4_every_prepare_example_in_the_docs_passes_the_lane():
    root = Path(__file__).resolve().parents[1]
    for rel in ("docs/openclaw/runbook.md", "skills/kit-orchestration/SKILL.md", "skills/kit-orchestration/references/backends-and-lanes.md"):
        for n, line in enumerate((root / rel).read_text().splitlines(), 1):
            if "lanes prepare" in line and ("--attempt" in line or "--reenter" in line or "--step" in line):
                assert "--lane" in line, f"{rel}:{n}: {line.strip()}"


def test_parity_agy_backends_are_writable_only_when_the_adr_is_accepted():
    from ai_resources.setup.cockpits import openclaw_lanes
    if not lanes_render.AGY_LANE_ADR_ACCEPTED:
        assert not (openclaw_lanes.WRITABLE_BACKENDS & {"agy-cli", "agy-cli-lane"})


def test_g7_a_symlink_into_tmp_is_refused(tmp_path, monkeypatch):
    link = tmp_path / "link"
    link.symlink_to("/tmp")
    monkeypatch.setenv("XDG_STATE_HOME", str(link / "state"))
    with pytest.raises(lanes_cmd.LaneCmdError, match="never lives under"):
        lanes_cmd.state_root()


@pytest.mark.parametrize("where", ["resolved", "fallback"])
def test_e1_an_entry_without_a_model_is_refused_not_skipped(env, where):
    lanes = _lanes()
    doc = _write(lanes)
    res = doc["resolved"]
    if where == "resolved":
        res["roles"]["implementer"].pop("model")
        args, role, extra = ("--lane", AGENT), "implementer", ()
    else:
        res["roles"]["implementer"]["fallbacks"][0].pop("model")
        args, role, extra = ("--lane", "claude-code"), "implementer", ()
    doc["digest"] = lanes_render.doc_digest(doc)
    state.lanes_path().write_text(lanes_render.dumps(doc))
    rc, out = env.run("prepare", "--step", "s", "--role", role, "--contract", "report", "--model", "opus",
                      *args, *extra)
    assert rc == 1 and any("lanes.yaml lacks the model of" in r for r in out["reasons"]), out


def test_e3_residual_admission_is_checked_against_the_files_own_agents(env):
    """Pins the documented limit (ADR-0007): a recomputed digest that adds a claude-cli agent is PREPARED.

    It is only dispatchable if that agent also exists in openclaw.json and allowAgents, which `prepare`
    never writes; the test fails the day this changes so the ADR note is revisited."""
    doc = _write()
    doc["agents"]["kit-lane-cli-evil"] = {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                          "pool": "claude-subscription"}
    doc["resolved"]["roles"]["implementer"] = {"lane": "openclaw-agent:kit-lane-cli-evil",
                                               "model": "anthropic/claude-sonnet-5-5", "reason": "x",
                                               "fallbacks": []}
    doc["digest"] = lanes_render.doc_digest(doc)
    state.lanes_path().write_text(lanes_render.dumps(doc))
    rc, out = _prep(env, "--lane", "kit-lane-cli-evil")
    assert rc == 0, out
    assert env.ledger_entries(env.state)[0]["data"]["lane"]["source"] == "resolved"


def test_codex_agent_in_a_recomputed_table_is_refused_at_prepare(env):
    doc = _write()
    doc["agents"]["kit-lane-codex-evil"] = {"backend": "codex-cli", "model": "gpt-5.5-codex",
                                            "pool": "codex-subscription"}
    doc["resolved"]["roles"]["implementer"] = {"lane": "openclaw-agent:kit-lane-codex-evil",
                                               "model": "gpt-5.5-codex", "reason": "x", "fallbacks": []}
    doc["digest"] = lanes_render.doc_digest(doc)
    state.lanes_path().write_text(lanes_render.dumps(doc))
    rc, out = _prep(env, "--lane", "kit-lane-codex-evil", model="gpt-5.5-codex")
    assert rc == 1 and any("not admitted" in r for r in out["reasons"]), out
