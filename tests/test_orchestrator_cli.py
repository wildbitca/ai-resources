"""`ai-resources orchestrate` and `ai-resources workflows` (ADR-0015 D3): the JSON the plugin reads."""
from __future__ import annotations

import json

import pytest

from ai_resources import cli
from ai_resources.orchestrator import cli as ocli
from ai_resources.orchestrator import engine as eng
from ai_resources.orchestrator.state import RunStore
from orchestrator_fakes import Clock, FLOW, OK, write_flow


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(ocli, "ENGINE_FACTORY", lambda args: eng.Engine(store, clock=Clock(), claims_root=tmp_path / "claims"))
    wf = write_flow(tmp_path)
    ex = tmp_path / "executors.yaml"
    ex.write_text("by_role:\n  planner: {model: sonnet}\n  implementer: {model: sonnet}\n  tester: {model: haiku}\n"
                  "  code-reviewer: {model: sonnet}\n  security-auditor: {model: opus}\n  verifier: {model: opus}\n")

    def call(*argv, code=0):
        rc = cli.main(list(argv))
        out = capsys.readouterr().out
        assert rc == code, (rc, out)
        return json.loads(out) if out.strip().startswith("{") else out
    return type("H", (), {"call": staticmethod(call), "wf": str(wf), "ex": str(ex), "tmp": tmp_path, "store": store})


def start(h, **kw):
    argv = ["orchestrate", "start", "--workflow", h.wf, "--goal", "add a thing", "--workspace", kw.pop("workspace", "/work/repo"),
            "--executors", h.ex, "--json"]
    for k, v in kw.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return h.call(*argv)


def test_start_returns_the_run_and_the_first_dispatch_in_the_workspace(harness):
    out = start(harness, run_id="r-1")
    assert out["run"] == "r-1" and out["status"] == "running" and "worktree" not in out
    [a] = out["actions"]
    assert a["type"] == "dispatch" and a["agent"] == "kit-role-planner" and a["cwd"] == "/work/repo"


def test_the_full_cycle_over_the_cli(harness):
    out = start(harness, run_id="r-1")
    reply = harness.tmp / "reply.txt"
    reply.write_text(OK + "Plan_ref: p.md\n")
    out = harness.call("orchestrate", "report", "r-1", "plan", "--session", "s1", "--agent", "kit-role-planner",
                       "--reply-file", str(reply), "--json")
    assert out["status"] == "awaiting_approval" and out["actions"][0]["type"] == "ask"
    out = harness.call("orchestrate", "approve", "r-1", "--json")
    assert out["actions"][0]["step"] == "implement"
    st = harness.call("orchestrate", "status", "r-1", "--json")
    assert st["steps"]["plan"]["session"] == "s1" and st["done"] == 1


def test_a_timeout_and_an_error_flow_through_the_report_flags(harness):
    start(harness, run_id="r-1")
    out = harness.call("orchestrate", "report", "r-1", "plan", "--timeout", "--json")
    assert out["actions"][0]["attempt"] == 2
    out = harness.call("orchestrate", "report", "r-1", "plan", "--error", "agent not allowed", "--json")
    assert out["actions"][0]["attempt"] == 3


def test_resume_and_status_and_cancel(harness):
    start(harness, run_id="r-1")
    out = harness.call("orchestrate", "resume", "r-1", "--json")
    assert out["resumed"] == ["plan"] and out["actions"][0]["type"] == "dispatch"
    allr = harness.call("orchestrate", "resume", "--all", "--json")
    assert allr["runs"][0]["run"] == "r-1"
    assert harness.call("orchestrate", "status", "--json")["active"] == 1
    assert harness.call("orchestrate", "cancel", "r-1", "--json")["status"] == "cancelled"


def test_the_third_team_is_queued_and_promoted_when_a_slot_frees(harness):
    for rid in ("a", "b", "c"):
        start(harness, run_id=rid, max_teams=2, workspace=f"/work/{rid}")
    assert harness.call("orchestrate", "next", "c", "--json")["actions"][0]["kind"] == "queued"
    out = harness.call("orchestrate", "cancel", "a", "--json")
    assert out["promoted"] == ["c"]


def test_errors_are_json_with_a_stable_code_and_the_right_exit_code(harness):
    out = harness.call("orchestrate", "next", "nope", "--json", code=1)
    assert out["code"] == "unknown-run"
    out = harness.call("orchestrate", "start", "--workflow", "no-such", "--goal", "g", "--json", code=2)
    assert out["code"] == "environment"
    start(harness, run_id="r-1")
    out = harness.call("orchestrate", "report", "r-1", "implement", "--json", code=1)
    assert out["code"] == "not-running"
    out = harness.call("orchestrate", "start", "--workflow", harness.wf, "--goal", "  ", "--json", code=2)
    assert out["code"] == "usage"


def test_the_text_mode_prints_a_summary_without_json(harness, capsys):
    rc = cli.main(["orchestrate", "start", "--workflow", harness.wf, "--goal", "g", "--workspace", "/w",
                   "--executors", harness.ex, "--run-id", "t1"])
    out = capsys.readouterr().out
    assert rc == 0 and "run: t1" in out and "- dispatch plan planner -> kit-role-planner (1200s)" in out


def test_role_timeouts_and_models_reach_the_run(harness):
    out = start(harness, run_id="r-1", role_timeouts=json.dumps({"planner": 600}))
    assert out["actions"][0]["timeout_s"] == 600
    bad = harness.call("orchestrate", "start", "--workflow", harness.wf, "--goal", "g", "--role-timeouts", "[1]", "--json",
                       code=2)
    assert bad["code"] == "usage"
    failed = start(harness, run_id="r-2", workspace="/work/other", executors=str(harness.tmp / "missing.yaml"))
    assert failed["status"] == "running"             # unknown models are allowed with the limitation recorded


def test_workflows_list_and_show_json(harness):
    lst = harness.call("workflows", "list", "--json")
    names = {w["name"] for w in lst["workflows"]}
    assert {"feature-implementation", "refactor"} <= names
    show = harness.call("workflows", "show", "feature-implementation", "--json")
    assert show["approval_before"] == "implement" and show["steps"][0]["id"] == "research"
    assert any(s["group"] == "post-test" for s in show["steps"])
    assert harness.call("workflows", "show", "nope", "--json", code=2)["code"] == "environment"


def test_a_huge_reply_file_is_read_from_its_end_where_the_return_block_is(harness):
    start(harness, run_id="r-1")
    big = harness.tmp / "big.txt"
    big.write_text("x" * (ocli.MAX_REPLY_FILE + 5000) + "\nBlocked: true\n")
    out = harness.call("orchestrate", "report", "r-1", "plan", "--reply-file", str(big), "--json")
    assert out["actions"][0]["type"] == "dispatch" and out["actions"][0]["attempt"] == 2     # the tail said Blocked: true


# --- the exact argv forms the OpenClaw plugin builds (openclaw-plugin/ai-resources/orchestrator.js) ---------------------

def test_the_plugin_argv_forms_parse_and_carry_hostile_text_as_data(harness):
    goal = "add a thing; rm -rf / && echo $(whoami) `id` -- --weird"
    out = harness.call("orchestrate", "start", "--workflow=" + harness.wf, "--goal=" + goal, "--max-teams=2",
                       "--workspace=/work/repo", "--executors=" + harness.ex, "--role-timeouts=" + json.dumps({"planner": 600}),
                       "--json")
    assert out["actions"][0]["timeout_s"] == 600 and goal in out["actions"][0]["prompt"]
    rid = out["run"]
    again = harness.call("orchestrate", "report", rid, "plan", "--timeout", "--session=agent:kit-role-planner:subagent:x-plan-a1",
                         "--agent=kit-role-planner", "--json")
    assert again["actions"][0]["attempt"] == 2
    harness.call("orchestrate", "report", rid, "plan", "--error=-weird error text", "--json")
    st = harness.call("orchestrate", "status", rid, "--json")
    assert st["steps"]["plan"]["attempt"] == 3 and "-weird error text" in st["steps"]["plan"]["reason"]
    assert harness.call("orchestrate", "resume", "--all", "--json")["runs"]


def test_the_plugin_reads_the_reply_from_stdin(harness, monkeypatch, capsys):
    import io, sys
    start(harness, run_id="r-in")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO((OK + "Plan_ref: from-stdin.md\n").encode())))
    rc = cli.main(["orchestrate", "report", "r-in", "plan", "--session=s", "--agent=kit-role-planner", "--reply-file=-", "--json"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["status"] == "awaiting_approval"
    assert "from-stdin.md" in harness.store.handoff_path("r-in").read_text()


def test_param_flags_fill_workflow_placeholders_and_a_bad_param_is_a_usage_error(harness):
    out = harness.call("orchestrate", "start", "--workflow=" + harness.wf, "--goal=g", "--workspace=/w",
                       "--executors=" + harness.ex, "--param=ticket=ABC-123", "--run-id=pp", "--json")
    assert out["run"] == "pp"
    bad = harness.call("orchestrate", "start", "--workflow=" + harness.wf, "--goal=g", "--param=oops", "--json", code=2)
    assert bad["code"] == "usage"
