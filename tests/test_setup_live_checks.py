"""The `setup-live` checks added in v4 (orchestrator, CLI backend provider, dreaming, executors, isolated agy refs), the
`verify --live` mapping of them, and the engine/pin contract for non-checker dispatches (item 4).

Generated config only: every document is a dict handed to a case through the `Env.doc` seam; no tool is called.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from ai_resources import acceptance as acc
from ai_resources import repo_root, verify
from ai_resources import roles_policy
from ai_resources.scoring import routing
from ai_resources.setup import effort, state
from ai_resources.setup.cockpits import openclaw_roles as R
from orchestrator_fakes import MODELS, OK, dispatches, finish, make

GOOD = {
    "agents": {"defaults": {"utilityModel": "google/gemini-3.8-flash", "heartbeat": {"model": "google/gemini-3.8-flash"},
                            "cliBackends": {"claude-cli": {"modelProvider": "anthropic"}}}},
    "plugins": {"entries": {"ai-resources": {"config": {"enableOrchestrator": True}},
                            "memory-core": {"config": {"dreaming": {"model": "google/gemini-3.8-flash"}}}}},
}


def _case(cid):
    return next(c for c in acc.CASES if c.id == cid)


def _run(cid, doc, monkeypatch=None):
    env = acc.Env(state=state.SetupState(), doc=lambda: doc, offline=True)
    return acc.run_case(_case(cid), env)


def _mut(path, value):
    doc = copy.deepcopy(GOOD)
    node = doc
    for key in path[:-1]:
        node = node.setdefault(key, {})
    if value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = value
    return doc


@pytest.fixture
def executors(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "executors_path", lambda: tmp_path / "executors.yaml")
    return tmp_path / "executors.yaml"


CHECKS = {
    "setup.orchestrator-enabled": "warn", "setup.cli-backend-model-provider": "warn", "setup.dreaming-not-agy": "error",
    "setup.executors-present": "warn", "isolated.no-agy-ref": "error",
}


@pytest.mark.parametrize("cid,severity", CHECKS.items())
def test_each_check_is_registered_in_setup_live_with_its_severity(cid, severity):
    assert _case(cid).group == "setup-live" and _case(cid).severity == severity


@pytest.mark.parametrize("cid", CHECKS)
def test_each_check_skips_when_the_config_is_absent(cid, executors):
    assert _run(cid, {}).status == acc.SKIP


def test_orchestrator_enabled_passes_and_fails():
    assert _run("setup.orchestrator-enabled", GOOD).status == acc.PASS
    for value in (False, None):
        r = _run("setup.orchestrator-enabled", _mut(("plugins", "entries", "ai-resources", "config", "enableOrchestrator"), value))
        assert r.status == acc.FAIL and "enableOrchestrator" in r.reason


def test_cli_backend_model_provider_passes_fails_and_skips_without_backends():
    assert _run("setup.cli-backend-model-provider", GOOD).status == acc.PASS
    r = _run("setup.cli-backend-model-provider", _mut(("agents", "defaults", "cliBackends"), {"claude-cli": {"command": "claude"}}))
    assert r.status == acc.FAIL and "claude-cli" in r.reason
    blank = _run("setup.cli-backend-model-provider", _mut(("agents", "defaults", "cliBackends"), {"x": {"modelProvider": " "}}))
    assert blank.status == acc.FAIL
    assert _run("setup.cli-backend-model-provider", _mut(("agents", "defaults", "cliBackends"), None)).status == acc.SKIP


def test_dreaming_not_agy_passes_fails_and_skips_when_unset():
    path = ("plugins", "entries", "memory-core", "config", "dreaming", "model")
    assert _run("setup.dreaming-not-agy", GOOD).status == acc.PASS
    r = _run("setup.dreaming-not-agy", _mut(path, "agy-cli-lane/gemini-3.5-flash"))
    assert r.status == acc.FAIL and "agy-cli-lane" in r.reason
    assert _run("setup.dreaming-not-agy", _mut(path, None)).status == acc.SKIP


def test_executors_present_passes_and_fails(executors):
    r = _run("setup.executors-present", GOOD)
    assert r.status == acc.FAIL and "missing" in r.reason
    executors.write_text("by_role: {}\n")
    assert _run("setup.executors-present", GOOD).status == acc.PASS
    assert "run `ai-resources setup`" in _case("setup.executors-present").remedy


@pytest.mark.parametrize("path", [("agents", "defaults", "utilityModel"), ("agents", "defaults", "heartbeat", "model"),
                                  ("plugins", "entries", "memory-core", "config", "dreaming", "model")])
def test_no_agy_ref_fails_for_each_isolated_path_and_passes_clean(path):
    assert _run("isolated.no-agy-ref", GOOD).status == acc.PASS
    r = _run("isolated.no-agy-ref", _mut(path, "agy-cli-lane/gemini-3.5-flash"))
    assert r.status == acc.FAIL and "agy-cli-lane" in r.reason


def test_no_agy_ref_sees_a_fallback_inside_a_model_object():
    r = _run("isolated.no-agy-ref", _mut(("agents", "defaults", "heartbeat", "model"),
                                          {"primary": "google/gemini-3.8-flash", "fallbacks": ["agy-cli/x"]}))
    assert r.status == acc.FAIL


def test_verify_live_reports_the_new_checks_with_their_severity(executors, monkeypatch):
    """`verify --live` maps every setup-live case: a FAIL becomes a finding of the case's severity with its remedy."""
    bad_doc = _mut(("plugins", "entries", "ai-resources", "config", "enableOrchestrator"), False)
    bad_doc = copy.deepcopy(bad_doc)
    bad_doc["agents"]["defaults"]["utilityModel"] = "agy-cli-lane/x"
    monkeypatch.setattr(acc.Env, "openclaw_doc", lambda self: bad_doc)
    s = state.SetupState()
    s.openclaw.host = "x"
    found = verify.live_findings(s, ak_path=str(repo_root()), runner=lambda *a, **k: (1, ""))
    by_msg = {f.message: f for f in found}
    orch = next(f for m, f in by_msg.items() if "enableOrchestrator" in m)
    agy = next(f for m, f in by_msg.items() if "isolated" in m)
    exe = next(f for m, f in by_msg.items() if "executors.yaml" in m)
    assert (orch.level, agy.level, exe.level) == ("warn", "error", "warn")
    assert exe.remedy == "run `ai-resources setup`"


# --- item 4: non-checker dispatches carry no model; the spawn pin carries the matrix chain ------------------------------

def test_no_non_checker_dispatch_carries_a_model(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models=MODELS, domain="")
    seen = []
    res = e.next(run["id"])
    for _ in range(80):
        if res.get("status") not in ("running", "awaiting_approval"):
            break
        acts = res["actions"]
        if any(a["type"] == "ask" for a in acts):
            res = e.approve(run["id"])
            continue
        disp = dispatches(res)
        if not disp:
            break
        for d in disp:
            seen.append(d)
            text = OK + ("Verdict: PASS\n" if d["role"] in roles_policy.CHECKER_ROLES else "")
            res = finish(e, run["id"], d, text)
    roles = {d["role"] for d in seen}
    assert {"planner", "implementer", "tester"} <= roles
    for d in seen:
        if d["role"] not in roles_policy.CHECKER_ROLES:
            assert "model" not in d, f"{d['role']} must run on its agent's subagents.model pin, not a per-dispatch model"


EFFECTIVE = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}


def test_the_spawn_pin_of_a_kit_role_agent_carries_the_matrix_chain():
    s = state.SetupState()
    s.profile.name = "balanced"
    agents, _ = R.build_agents(repo_root(), enabled=("anthropic",), catalog={"anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5")},
                               effort_table=effort.table_for_state(s), ak_path="/kit", by_persona={}, env={},
                               effective=EFFECTIVE)
    roles = [a for a in agents if a.id.removeprefix("kit-role-") in routing.ROLES]
    assert roles
    for a in roles:
        primary, fallbacks = routing.openclaw_chain(routing.ROLES[a.id.removeprefix("kit-role-")], EFFECTIVE)
        pin = R._model_value(a)
        assert pin["primary"] == primary and list(pin["fallbacks"]) == list(fallbacks), a.id
