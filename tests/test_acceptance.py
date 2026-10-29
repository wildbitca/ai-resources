"""`ai-resources acceptance` (ADR-0019 D8): registry, fakes for every dispatcher, fault injection, operator listing, schema."""
from __future__ import annotations

import argparse
import json
import socket
import subprocess

import pytest

from ai_resources import acceptance as acc
from ai_resources import agy_run, dispatch_ledger
from ai_resources.scoring import routing
from ai_resources.setup import detection, live_auth

CLAUDE = """#!/bin/sh
echo x >> "$0.calls"
echo '{"is_error":false,"result":"pong","modelUsage":{"%s":{}},"usage":{"input_tokens":2,"output_tokens":3}}'
"""
OPENCLAW = """#!/bin/sh
echo x >> "$0.calls"
echo '[log] provider-transport-fetch api=%s status=200'
case "$*" in
  *"agent exec"*) while [ $# -gt 0 ]; do [ "$1" = "--cwd" ] && cat "$2/token.txt"; shift; done ;;
  *) echo '{"ok": true, "provider": "%s", "model": "%s", "transport": "local", "attempts": [], "outputs": [{"text": "pong"}]}' ;;
esac
"""
AGY = """#!/bin/sh
echo x >> "$0.calls"
echo '{"result":{"status":"SUCCESS","response":"pong","usage":{"input_tokens":5,"output_tokens":1}}}'
"""


def _bin(tmp_path, name, body):
    p = tmp_path / "fakes" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(body)
    p.chmod(0o755)
    return str(p)


def _calls(path):
    try:
        return len(open(path + ".calls").read().split())
    except OSError:
        return 0


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(acc, "_usage", lambda e: {})
    monkeypatch.setattr(detection, "inactive_backends", lambda: {})
    monkeypatch.setattr(live_auth, "read_claude", lambda *a, **k: live_auth.LiveAuth(live_auth.SUBSCRIPTION, live_auth.B_SUBSCRIPTION, "sub"))
    bins: dict[str, str] = {}
    e = acc.Env(cache=tmp_path / "cache", which=bins.get)
    e.bins = bins
    return e


def _statuses(results):
    return {c.id: r.status for c, r in results}


# --- registry ------------------------------------------------------------------------------------------------------------

def test_registry_has_a_case_for_every_d8_bullet_and_every_role():
    ids = {c.id for c in acc.CASES}
    assert len(ids) == len(acc.CASES), "case ids are unique"
    assert {f"route.{r}" for r in routing.ROLES} <= ids                     # per role resolve -> call -> assert
    assert {"fault.pool-hot", "fault.bad-key", "fault.timeout", "fault.agy-missing", "fault.quota-text"} <= ids
    assert {"checker.gemini-haiku-writers", "checker.sonnet-writer", "checker.never-weaker"} <= ids
    assert {"spawn.allow", "spawn.memory-peak", "spawn.explore-run", "gemini.tool-call", "agy.write-scope"} <= ids
    assert {"spawn.landed-on-role-agent", "team.main-assumes-21-roles", "team.cross-provider-run", "team.cli-and-api-one-run"} <= ids
    assert {"setup.marker-guard", "setup.restart-lock", "setup.active-turns-defer", "setup.cgroup-t29", "setup.reload-rules",
            "setup.plugin-fresh", "setup.mcp-child-cap", "setup.sigterm-window", "setup.idempotent-rerun", "setup.offbox-regenerate",
            "setup.offbox-cred", "setup.stopfailure-hook", "setup.credits-ledger", "setup.real-restart", "setup.watchdog",
            "setup.role-matrix-drift", "setup.role-billing-pool"} <= ids
    assert {c.group for c in acc.CASES} == set(acc.GROUPS)


def test_operator_cases_list_the_exact_command_and_are_never_run(capsys, env):
    assert acc.cmd_acceptance(argparse.Namespace(operator=True, group=None, case=None, list=False, dry_run=False, offline=False,
                                                  json=False, usage_file=None)) == 0
    out = capsys.readouterr().out
    assert "kill -TERM" in out and "ai-resources openclaw reconcile" in out and "watchdog.off" in out
    for c in acc.select(operator=True):
        assert c.operator in out
        r = acc.run_case(c, env)                              # the case body only says where the command is
        assert r.status == acc.SKIP and c.operator in r.reason and r.reason.startswith("operator-run")
    assert not any(c.operator for c in acc.select(offline=True)), "--offline never lists them"


def test_dry_run_prints_the_plan_and_the_cap_without_any_call(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("dry-run must not call anything")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    assert acc.cmd_acceptance(argparse.Namespace(operator=False, group=None, case=None, list=False, dry_run=True, offline=False,
                                                  json=False, usage_file=None)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(acc.CASES) and all(" | PLAN | " in ln for ln in lines)
    assert any("one minimal call per distinct (backend, model) cell" in ln for ln in lines)


def test_json_document_schema(capsys, env):
    args = argparse.Namespace(operator=False, group="checker", case=None, list=False, dry_run=False, offline=True, json=True, usage_file=None)
    assert acc.cmd_acceptance(args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == 1 and set(doc["summary"]) == {"PASS", "FAIL", "SKIP", "N/A"}
    assert all(set(r) == {"id", "group", "status", "reason", "evidence"} and r["status"] in ("PASS", "FAIL", "SKIP", "N/A") for r in doc["results"])
    assert [r["id"] for r in doc["results"]] == [c.id for c in acc.select("checker")]


def test_table_has_one_row_per_case(env):
    rows = acc.table(acc.run_cases(acc.select("checker"), env))
    assert len(rows) == 3 and all(r.count(" | ") == 2 for r in rows)


# --- SKIP with a reason when a tool is absent or not signed in ----------------------------------------------------------------

def test_missing_tools_skip_with_the_precise_reason(env, monkeypatch):
    monkeypatch.setattr(agy_run, "agy_bin", lambda: "")
    res = {c.id: r for c, r in acc.run_cases([c for c in acc.CASES if c.id in (
        "route.planner", "route.main", "route.explore", "gemini.tool-call", "agy.write-scope", "spawn.explore-run")], env)}
    assert all(r.status == acc.SKIP for r in res.values()), {k: v for k, v in res.items()}
    assert "claude is not on PATH" in res["route.planner"].reason
    assert "openclaw is not on PATH" in res["route.main"].reason and "agy is not installed" in res["route.explore"].reason


def test_a_call_that_is_not_signed_in_is_skip_not_fail(env, tmp_path):
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", "#!/bin/sh\necho 'Error: no API key for provider google'\nexit 1\n")
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.utility"), env)
    assert r.status == acc.SKIP and "not signed in" in r.reason


# --- route with fakes -------------------------------------------------------------------------------------------------------

def test_one_call_per_cell_one_row_per_role_and_the_model_used_is_asserted(env, tmp_path):
    claude = env.bins["claude"] = _bin(tmp_path, "claude", CLAUDE % "claude-sonnet-5-5")
    sonnet_roles = [r for r, v in routing.ROLES.items() if v.primary == "claude-code:sonnet"]
    res = acc.run_cases([c for c in acc.CASES if c.id in {f"route.{r}" for r in sonnet_roles}], env)
    assert set(_statuses(res).values()) == {acc.PASS}, [r.reason for _c, r in res]
    assert _calls(claude) == 1, "roles sharing a (backend, model) cell share one call"
    rows = acc.route_rows(env, env.run_id)
    assert sorted(r["role"] for r in rows) == sorted(sonnet_roles) and {r["model"] for r in rows} == {"sonnet"}


def test_the_matrix_cell_must_equal_the_model_that_answered(env, tmp_path):
    env.bins["claude"] = _bin(tmp_path, "claude", CLAUDE % "claude-opus-5-5")
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.planner"), env)
    assert r.status == acc.FAIL and "claude-code:sonnet" in r.reason and "claude-opus-5-5" in r.reason
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("google-generative-ai", "google", "gemini-3.0-pro"))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.utility"), env)
    assert r.status == acc.FAIL and "google/gemini-3.0-pro" in r.reason


def test_infer_cells_are_checked_by_provider_model_and_share_their_call(env, tmp_path):
    oc = env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("google-generative-ai", "google", "gemini-3.8-flash"))
    res = acc.run_cases([c for c in acc.CASES if c.id in ("route.utility", "route.summarization", "route.explore-bulk")], env)
    assert set(_statuses(res).values()) == {acc.PASS} and _calls(oc) == 1
    assert {r["pool"] for r in acc.route_rows(env, env.run_id)} == {routing.POOL_GEMINI_API}


def test_agy_cells_run_through_agy_run_and_assert_the_ledger_row(env, tmp_path, monkeypatch):
    agy = _bin(tmp_path, "agy", AGY)
    monkeypatch.setenv("AI_RESOURCES_AGY_BIN", agy)
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.explore"), env)
    assert r.status == acc.PASS, r.reason
    row = [x for x in acc.route_rows(env) if x["role"] == "explore"][-1]
    assert (row["backend"], row["model"], row["pool"], row["outcome"]) == ("agy", "gemini-3.8-flash-high", routing.POOL_AGY_GEMINI, "ok")


def test_a_failed_call_is_fail_with_the_real_error(env, tmp_path):
    env.bins["claude"] = _bin(tmp_path, "claude", "#!/bin/sh\necho 'overloaded, try later'\nexit 1\n")
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.planner"), env)
    assert r.status == acc.FAIL and "overloaded" in r.reason


def test_no_secret_reaches_the_output(env, tmp_path):
    key = "AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMY12345"
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", f"#!/bin/sh\necho 'boom key={key} Bearer abc.def.ghi sk-ant-api03-ZZZZZZZZZZ'\nexit 1\n")
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.main"), env)
    text = json.dumps(acc.document([(next(c for c in acc.CASES if c.id == "route.main"), r)]))
    assert key not in text and "sk-ant-api03" not in text and "abc.def.ghi" not in text and "***" in text


# --- gemini tool-calling and agy write ---------------------------------------------------------------------------------------

def test_gemini_tool_call_writes_the_verdict_doctor_reads(env, tmp_path):
    from ai_resources import doctor
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("google-generative-ai", "google", "gemini-3.8-flash"))
    assert acc.gemini_verdict_line(env.cache) == ""
    r = acc.run_case(next(c for c in acc.CASES if c.id == "gemini.tool-call"), env)
    assert r.status == acc.PASS
    assert "PASSED" in acc.gemini_verdict_line(env.cache) and "never edits openclaw config" in acc.gemini_verdict_line(env.cache)
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", "#!/bin/sh\necho no token\n")
    assert acc.run_case(next(c for c in acc.CASES if c.id == "gemini.tool-call"), env).status == acc.FAIL
    assert "FAILED" in acc.gemini_verdict_line(env.cache) and "stays on Haiku" in acc.gemini_verdict_line(env.cache)
    assert doctor._check_gemini_tools.__doc__


def test_a_turn_that_cannot_run_is_skip_and_writes_no_verdict(env, tmp_path, monkeypatch):
    monkeypatch.setattr(agy_run, "agy_bin", lambda: "")
    body = "#!/bin/sh\necho '{\"ok\": false, \"status\": \"error\", \"error\": {\"message\": \"Secret store entry X was not found.\"}}'\nexit 1\n"
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", body)
    r = acc.run_case(next(c for c in acc.CASES if c.id == "gemini.tool-call"), env)
    assert r.status == acc.SKIP and "could not run the agent turn" in r.reason and acc.gemini_verdict_line(env.cache) == ""


def test_the_wire_api_decides_the_pool_of_an_infer_call(env, tmp_path):
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("google-cli-runtime", "google", "gemini-3.8-flash"))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "route.main"), env)
    assert r.status == acc.FAIL and "the call used google/gemini-3.8-flash []" in r.reason
    env.memo.clear()
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("google-generative-ai", "google", "gemini-3.8-flash"))
    assert acc.run_case(next(c for c in acc.CASES if c.id == "route.main"), env).status == acc.PASS


def test_agy_write_inside_the_scratch_dir_is_scope_checked(env, tmp_path, monkeypatch):
    writer = AGY.replace("echo '{", 'echo pong > hello.txt\necho \'{', 1)
    monkeypatch.setenv("AI_RESOURCES_AGY_BIN", _bin(tmp_path, "agy", writer))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "agy.write-scope"), env)
    assert r.status == acc.PASS, r.reason
    stray = AGY.replace("echo '{", 'echo pong > hello.txt\necho x > stray.txt\necho \'{', 1)
    monkeypatch.setenv("AI_RESOURCES_AGY_BIN", _bin(tmp_path, "agy", stray))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "agy.write-scope"), env)
    assert r.status == acc.FAIL and "stray.txt" in r.reason


# --- fault injection (offline, fakes) -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("cid", ["fault.pool-hot", "fault.bad-key", "fault.timeout", "fault.agy-missing", "fault.quota-text"])
def test_fault_injection_fires_the_fallback_exactly_once_with_its_cause(env, cid):
    r = acc.run_case(next(c for c in acc.CASES if c.id == cid), env)
    assert r.status == acc.PASS, r.reason


def test_fault_check_catches_a_silent_or_doubled_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    dispatch_ledger.record("explore", "fail", cell="agy:gemini-3.8-flash-high", fallback_cause="timeout", fallback_from="agy:gemini-3.8-flash-high")
    ok_doc = {"fallback": "claude-code:haiku"}
    assert acc._one_fallback("explore", "timeout", 75, ok_doc) == ""
    assert "recorded cause" in acc._one_fallback("explore", "5xx", 75, ok_doc)
    dispatch_ledger.record("explore", "ok", cell="claude-code:haiku")
    assert "2 dispatch rows" in acc._one_fallback("explore", "timeout", 75, ok_doc)


# --- checker independence ---------------------------------------------------------------------------------------------------

def test_checker_group_passes_and_catches_a_weaker_checker(env, monkeypatch):
    assert set(_statuses(acc.run_cases(acc.select("checker"), env)).values()) == {acc.PASS}
    real = routing.resolve
    monkeypatch.setattr(routing, "resolve", lambda role, usage=None, writers=(), *a, **k: real(role, usage, ()))   # ignores the writers
    assert acc.run_case(next(c for c in acc.CASES if c.id == "checker.never-weaker"), env).status == acc.FAIL


# --- setup-live adapters ----------------------------------------------------------------------------------------------------

def test_setup_logic_cases_pass_on_a_clean_checkout(env):
    ids = ("setup.marker-guard", "setup.restart-lock", "setup.active-turns-defer", "setup.cgroup-t29", "setup.offbox-regenerate",
           "setup.idempotent-rerun")
    res = acc.run_cases([c for c in acc.CASES if c.id in ids], env)
    assert set(_statuses(res).values()) == {acc.PASS}, [r.reason for _c, r in res]


def test_a_case_that_crashes_is_a_fail_row_not_a_crash(env):
    boom = acc.Case("x.boom", "route", lambda e: 1 / 0)
    r = acc.run_case(boom, env)
    assert r.status == acc.FAIL and "ZeroDivisionError" in r.reason


@pytest.mark.parametrize("secret", ["ghp_" + "a" * 36, "github_pat_" + "B" * 30, "xoxb-1234567890-abcdef", "ya29.a0AfH6SMB-x_y",
                                    "1//0gAbCdEfGhIjKlMnOpQrStUv", "sk-ant-api03-abcdefghij", "AIza" + "c" * 35, "Bearer abc.def"])
def test_scrub_hides_every_known_token_shape(secret):
    from ai_resources import acceptance
    assert acceptance.scrub(f"failed with {secret} in the header") == "failed with *** in the header"


# --- N/A, the agent-exec guard, and the cross-provider team cases (item 8) ---------------------------------------------------------

GOOGLE_DOC = {"auth": {"profiles": {"google:default": {"provider": "google", "mode": "api_key"}}}}


def test_a_host_with_no_gemini_route_is_not_applicable_not_skip(env, tmp_path, monkeypatch):
    monkeypatch.setattr(agy_run, "agy_bin", lambda: "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env.doc = lambda: {"auth": {"profiles": {"anthropic:default": {"provider": "anthropic"}}}, "plugins": {"entries": {}}}
    r = acc.run_case(next(c for c in acc.CASES if c.id == "gemini.tool-call"), env)
    assert r.status == acc.NA and "no Gemini route" in r.reason


def test_a_removed_providers_dangling_secret_is_a_defect_pointing_at_setup(env, tmp_path, monkeypatch):
    monkeypatch.setattr(agy_run, "agy_bin", lambda: "")
    env.doc = lambda: GOOGLE_DOC
    body = "#!/bin/sh\necho '{\"ok\": false, \"error\": {\"message\": \"Secret store entry OPENROUTER_API_KEY was not found.\"}}'\nexit 1\n"
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", body)
    r = acc.run_case(next(c for c in acc.CASES if c.id == "gemini.tool-call"), env)
    assert r.status == acc.FAIL and "ai-resources setup" in r.reason


def test_gemini_proof_falls_back_to_the_agy_route(env, tmp_path, monkeypatch):
    env.doc = lambda: {"auth": {"profiles": {}}, "plugins": {"entries": {}}}
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    writer = AGY.replace("echo '{", 'echo pong > hello.txt\necho \'{', 1)
    monkeypatch.setenv("AI_RESOURCES_AGY_BIN", _bin(tmp_path, "agy", writer))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "gemini.tool-call"), env)
    assert r.status == acc.PASS and "Antigravity" in r.reason


def test_the_agent_exec_guard_is_detected_and_never_bypassed(monkeypatch):
    assert acc.agent_exec_context({"OPENCLAW_SHELL": "exec"}) and acc.agent_exec_context({"OPENCLAW_SUBAGENT_EXEC": "1"})
    assert not acc.agent_exec_context({"OPENCLAW_SHELL": "interactive"}) and not acc.agent_exec_context({})


SESSIONS = json.dumps({"sessions": [{"key": "agent:kit-role-explore:subagent:abc", "sessionId": "4322417d-xx", "status": "done",
                                    "spawnedBy": "agent:main:slack:channel:c1", "updatedAt": 5, "model": "claude-haiku-5-5",
                                    "modelProvider": "anthropic"}]})


def test_inside_an_agent_session_explore_is_proven_from_the_spawn_the_gateway_recorded(env, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_SHELL", "exec")
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", "#!/bin/sh\necho 'must not run' >&2\nexit 9\n")
    monkeypatch.setattr(acc, "_spawn_allow", lambda e: acc.ok("wired"))
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return (0, SESSIONS) if "sessions" in argv else (1, "")
    env.runner = runner
    r = acc.run_case(next(c for c in acc.CASES if c.id == "spawn.explore-run"), env)
    assert r.status == acc.PASS and "sessions_spawn" in r.reason and "claude-haiku-5-5" in r.reason
    assert not any(a[1:2] == ["agent"] for a in calls), "`openclaw agent` is never called inside an agent session"
    assert _calls(env.bins["openclaw"]) == 0


def test_without_a_recorded_spawn_the_agent_session_case_says_how_to_make_one(env, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_SHELL", "exec")
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", "#!/bin/sh\nexit 9\n")
    monkeypatch.setattr(acc, "_spawn_allow", lambda e: acc.ok("wired"))
    env.runner = lambda argv, **kw: (0, json.dumps({"sessions": []}))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "spawn.explore-run"), env)
    assert r.status == acc.SKIP and "sessions_spawn" in r.reason


def _fake_doc(**over):
    from ai_resources.setup.cockpits import openclaw_roles
    entries = {openclaw_roles.agent_id_for(r): {"model": {"primary": "anthropic/claude-sonnet-5-5"}}
               for r, row in routing.ROLES.items() if row.kind == "team"}
    entries["main"] = {"subagents": {"allowAgents": ["*"]}}
    entries.update(over)
    return {"agents": {"entries": entries}}


def test_main_assumes_all_21_roles_and_catches_a_missing_agent(env, monkeypatch):
    monkeypatch.setattr(detection, "inactive_backends", lambda: {})
    kit = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    env.ak_path = kit
    env.doc = lambda: _fake_doc()
    r = acc.run_case(next(c for c in acc.CASES if c.id == "team.main-assumes-21-roles"), env)
    assert r.status == acc.PASS and r.evidence["roles"] == 21, r.reason
    doc = _fake_doc()
    del doc["agents"]["entries"]["kit-role-planner"]
    env.doc = lambda: doc
    r = acc.run_case(next(c for c in acc.CASES if c.id == "team.main-assumes-21-roles"), env)
    assert r.status == acc.FAIL and "planner" in r.reason
    doc = _fake_doc(main={"subagents": {"allowAgents": ["kit-role-explore"]}})
    env.doc = lambda: doc
    assert "may not spawn" in acc.run_case(next(c for c in acc.CASES if c.id == "team.main-assumes-21-roles"), env).reason


def test_one_workflow_runs_on_different_providers_with_an_independent_checker(env, monkeypatch):
    monkeypatch.setattr(detection, "inactive_backends", lambda: {})
    env.ak_path = str(__import__("pathlib").Path(__file__).resolve().parents[1])
    r = acc.run_case(next(c for c in acc.CASES if c.id == "team.cross-provider-run"), env)
    assert r.status == acc.PASS, r.reason
    b = r.evidence["backends"]
    assert {"agy", "claude-code"} <= set(b.values()), b
    assert b["implementer"] == "claude-code" and b["explore"] == "agy"
    assert "checker" in r.reason


def test_a_checker_on_the_writers_family_fails_the_team_case():
    same = routing.Route("verifier", "claude-code:sonnet", routing.POOL_MAX, (), "x")
    impl = routing.Route("implementer", "claude-code:sonnet", routing.POOL_MAX, (), "x")
    r = acc._judge_team({"implementer": impl, "verifier": same, "explore": routing.Route("explore", "agy:gemini-3.8-flash-high", routing.POOL_AGY_GEMINI, (), "x")})
    assert r.status == acc.FAIL and "family" in r.reason


def test_cli_and_api_routes_in_one_run_use_one_ledger_run_id(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_AGY_BIN", _bin(tmp_path, "agy", AGY))
    env.bins["claude"] = _bin(tmp_path, "claude", CLAUDE % "claude-sonnet-5-5")
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("anthropic-messages", "anthropic", "claude-sonnet-5-5"))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "team.cli-and-api-one-run"), env)
    # the stub answers every infer call as Anthropic, but main is a gemini-api cell: that is a FAIL naming main
    assert r.status == acc.FAIL and "main on gemini-api failed" in r.reason
    env.memo.clear()
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", OPENCLAW % ("google-generative-ai", "google", "gemini-3.8-flash"))
    r = acc.run_case(next(c for c in acc.CASES if c.id == "team.cli-and-api-one-run"), env)
    assert r.status == acc.PASS, r.reason


DUAL = """#!/bin/sh
echo x >> "$0.calls"
case "$*" in
  *google/*) echo '[log] api=google-generative-ai status=200'
             echo '{"ok": true, "provider": "google", "model": "gemini-3.8-flash", "attempts": [], "outputs": [{"text": "pong"}]}' ;;
  *) echo '[log] api=anthropic-messages status=200'
     echo '{"ok": true, "provider": "anthropic", "model": "claude-sonnet-5-5", "attempts": [], "outputs": [{"text": "pong"}]}' ;;
esac
"""


def test_cli_and_api_providers_answer_in_the_same_run(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_AGY_BIN", _bin(tmp_path, "agy", AGY))
    env.bins["claude"] = _bin(tmp_path, "claude", CLAUDE % "claude-sonnet-5-5")
    env.bins["openclaw"] = _bin(tmp_path, "openclaw", DUAL)
    r = acc.run_case(next(c for c in acc.CASES if c.id == "team.cli-and-api-one-run"), env)
    assert r.status == acc.PASS, r.reason
    assert {"agy", "claude-code", "gemini-api"} <= set(r.evidence["backends"])
    assert routing.POOL_GEMINI_API in {r["pool"] for r in acc.route_rows(env, env.run_id)}


# --- spawn.landed-on-role-agent: the config proof and the newest spawn --------------------------------------------------

def _landed(env, doc, rows, mtime=0):
    env.doc = lambda: doc
    env.config_mtime_ms = lambda: mtime
    env.runner = lambda argv, **kw: (0, json.dumps({"sessions": rows}))
    return acc.run_case(next(c for c in acc.CASES if c.id == "spawn.landed-on-role-agent"), env)


def _explore_doc(sub=None, default=None, primary="anthropic/claude-haiku-5-5"):
    ent = {"model": {"primary": primary}}
    if sub:
        ent["subagents"] = {"model": {"primary": sub}}
    agents = {"entries": {"kit-role-explore": ent}}
    if default:
        agents["defaults"] = {"subagents": {"model": default}}
    return {"agents": agents}


def _row(started, updated, model, key="agent:kit-role-explore:subagent:x"):
    return {"key": key, "sessionStartedAt": started, "updatedAt": updated, "model": model, "modelProvider": "anthropic",
            "spawnedBy": "agent:claude:subagent:y", "status": "done"}


def test_landed_judges_the_newest_started_session_not_the_last_touched_one(env):
    """Regression (3.2.9): an old Sonnet spawn from before the fix had a later `updatedAt` than the Haiku spawn that proved it."""
    rows = [_row(100, 900, "claude-sonnet-5-5"), _row(500, 600, "claude-haiku-5-5")]
    r = _landed(env, _explore_doc(sub="anthropic/claude-haiku-5-5"), rows, mtime=50)
    assert r.status == acc.PASS, r.reason


def test_landed_fails_when_the_newest_spawn_after_the_config_change_ran_on_the_wrong_model(env):
    rows = [_row(500, 600, "claude-sonnet-5-5")]
    r = _landed(env, _explore_doc(sub="anthropic/claude-haiku-5-5"), rows, mtime=50)
    assert r.status == acc.FAIL and "claude-sonnet-5-5" in r.reason


def test_landed_a_spawn_from_before_the_config_change_is_unproven_not_a_failure(env):
    rows = [_row(100, 900, "claude-sonnet-5-5")]
    r = _landed(env, _explore_doc(sub="anthropic/claude-haiku-5-5"), rows, mtime=400)
    assert r.status == acc.PASS and "predate" in r.reason


def test_landed_fails_from_the_config_alone_when_a_default_subagent_model_outranks_the_role(env):
    r = _landed(env, _explore_doc(default="anthropic/claude-sonnet-5-5"), [], mtime=0)
    assert r.status == acc.FAIL and "agents.defaults.subagents.model" in r.reason and "openclaw config set" in r.reason
