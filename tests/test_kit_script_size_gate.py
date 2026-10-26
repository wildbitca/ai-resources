"""/kit-plan and /kit-implement gate their work by task size (inline | light | full).

The scripts run in the host's workflow runtime; here a node harness feeds them a scripted agent() and records
every call, so the tests count spawns instead of trusting prose.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "kit_script_harness.mjs"
PLAN_JS = REPO / "workflows" / "scripts" / "kit-plan.js"
IMPL_JS = REPO / "workflows" / "scripts" / "kit-implement.js"

pytestmark = [pytest.mark.node_scripts, pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")]

UNDERSTAND = {"summary": "s", "files": ["a.py"], "notes": ["n"]}
DRAFT = {"approach": "a", "steps": ["1"], "files": ["a.py"], "risks": []}
JUDGE = {"winner": 1, "rationale": "r", "scores": [8, 5, 5]}
PLAN = {"plan_path": ".agent-output/planner/x-plan.md", "summary": "s", "acceptance_criteria": ["S1"],
        "requires_tests": True, "security_critical": False}
IMPL = {"summary": "s", "files_changed": ["a.py"], "tests_added": ["t.py"], "commands_run": ["pytest"]}
TEST_OK = {"passed": True, "command": "pytest", "failures": []}
NO_FINDINGS = {"findings": []}
SIGNOFF = {"met": True, "gaps": [], "evidence": ["pytest"]}


def run(script: Path, tmp_path: Path, args, responses):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"args": args, "responses": responses}))
    out = subprocess.run(["node", str(HARNESS), str(script), str(cfg)], capture_output=True, text=True, check=True, timeout=60)
    data = json.loads(out.stdout)
    return [c["label"] for c in data["calls"]], data["result"]


PLAN_RESPONSES = {"understand": UNDERSTAND, "draft": DRAFT, "judge": JUDGE, "write-plan": PLAN}


def test_plan_inline_flag_spawns_nothing(tmp_path):
    labels, result = run(PLAN_JS, tmp_path, "fix typo --inline", PLAN_RESPONSES)
    assert labels == []
    assert result["size"] == "inline" and result["plan_path"] is None
    assert result["goal"] == "fix typo"


def test_plan_classifier_inline_costs_one_call(tmp_path):
    responses = {**PLAN_RESPONSES, "classify": {"mode": "inline", "reason": "clear", "risk_items": []}}
    labels, result = run(PLAN_JS, tmp_path, {"goal": "rename a flag"}, responses)
    assert labels == ["classify"] and result["size"] == "inline"


def test_plan_risk_item_takes_a_task_out_of_inline(tmp_path):
    responses = {**PLAN_RESPONSES, "classify": {"mode": "inline", "reason": "small", "risk_items": ["3: hook contract"]}}
    labels, result = run(PLAN_JS, tmp_path, {"goal": "change handoff keys"}, responses)
    assert result["size"] == "light" and result["risk_items"] == ["3: hook contract"]


def test_plan_light_is_one_reader_one_draft_no_judges(tmp_path):
    labels, result = run(PLAN_JS, tmp_path, {"goal": "g", "size": "light"}, PLAN_RESPONSES)
    assert labels == ["understand:code-specs-tests", "draft:1", "write-plan"]
    assert result["size"] == "light" and result["judged"]["scored"] is False


def test_plan_full_keeps_the_whole_loop(tmp_path):
    labels, result = run(PLAN_JS, tmp_path, "g --full", PLAN_RESPONSES)
    assert len(labels) == 9 and "classify" not in labels
    assert result["size"] == "full" and result["judged"]["scored"] is True


def test_plan_failed_classifier_falls_back_to_light(tmp_path):
    labels, result = run(PLAN_JS, tmp_path, {"goal": "g"}, PLAN_RESPONSES)
    assert labels[0] == "classify" and len(labels) == 4 and result["size"] == "light"


def test_plan_classify_prompt_treats_the_ledger_suggestion_as_a_hint(tmp_path):
    prompts, _, _ = run_calls(PLAN_JS, tmp_path, {"goal": "g"}, {**PLAN_RESPONSES, "classify": {"mode": "light", "reason": "r"}})
    p = prompts["classify"]
    assert "ai-resources runs suggest --kind feature" in p and "never a decision" in p and "skip it silently" in p


def test_plan_next_hands_the_size_to_implement(tmp_path):
    _, result = run(PLAN_JS, tmp_path, {"goal": "g"}, {**PLAN_RESPONSES, "classify": {"mode": "light", "reason": "r", "started_at": 1700000000}})
    assert '"size":"light"' in result["next"] and '"start":1700000000' in result["next"]


IMPL_RESPONSES = {"implement": IMPL, "test-gate": TEST_OK, "review": NO_FINDINGS, "sign-off": SIGNOFF,
                  "record-run": {"advice": "recorded"}, "reassess": {"advice": "keep"}}


def test_implement_inline_spawns_nothing(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "inline"}, IMPL_RESPONSES)
    assert labels == [] and result["status"] == "inline"


def test_implement_light_without_risk_has_one_lens_and_no_signoff(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, IMPL_RESPONSES)
    assert labels == ["implement", "test-gate", "review:combined", "record-run"]
    assert result["status"] == "done" and result["sign_off"] is None


def test_implement_light_with_risk_adds_the_independent_verifier(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light", "risk_items": ["3: contract"]}, IMPL_RESPONSES)
    assert labels == ["implement", "test-gate", "review:combined", "sign-off", "record-run"]
    assert result["sign_off"]["met"] is True


def test_implement_light_security_risk_keeps_the_security_prompt(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"args": {"goal": "g", "size": "light", "risk_items": ["2: authz"]}, "responses": IMPL_RESPONSES}))
    out = subprocess.run(["node", str(HARNESS), str(IMPL_JS), str(cfg)], capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(out.stdout)["result"]["review"]["security_reviewed"] is True


def test_implement_full_keeps_three_lenses_and_signoff(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "full"}, IMPL_RESPONSES)
    assert labels == ["implement", "test-gate", "review:correctness", "review:security", "review:test-integrity", "sign-off", "record-run"]
    assert result["status"] == "done"


def test_implement_never_classifies_it_reads_the_plan_sizing_block(tmp_path):
    sizing = {"found": True, "mode": "light", "risk_items": [], "forecast_lines": 40, "units": 1, "files": 2}
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": sizing})
    assert "classify" not in labels and labels[0] == "read-sizing" and result["size"] == "light"


def test_implement_with_an_unreadable_sizing_block_falls_back_to_light(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "plan_path": "p.md"}, IMPL_RESPONSES)
    assert labels[0] == "read-sizing" and result["size"] == "light"


def test_implement_with_a_size_passed_on_spawns_no_sizing_call(tmp_path):
    labels, _ = run(IMPL_JS, tmp_path, {"goal": "g", "plan_path": "p.md", "size": "light"}, IMPL_RESPONSES)
    assert "read-sizing" not in labels and "classify" not in labels


def test_implement_with_no_plan_and_no_size_points_at_kit_plan(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g"}, IMPL_RESPONSES)
    assert labels == [] and result["status"] == "needs-plan" and "/kit-plan" in result["next"]


def test_implement_inline_hint_carries_the_ledger_record_command(tmp_path):
    _, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "inline"}, IMPL_RESPONSES)
    assert "no subagents were started" in result["next"] and "ai-resources runs record --mode inline" in result["next"]


def test_implement_records_the_run_with_the_flag_contract(tmp_path):
    args = {"goal": "g", "size": "light", "estimated": "inline", "start": 1700000000, "units": 1, "forecast_lines": 30,
            "risk_items": ["3: contract"], "forced": True}
    prompts, labels, _ = run_calls(IMPL_JS, tmp_path, args, IMPL_RESPONSES)
    p = prompts["record-run"]
    for part in ("ai-resources runs record --mode light", "--estimated inline", "--outcome pass", "--agents 4", "--rework 0",
                 "--kind feature", "--risk 3", "--forced", "--start 1700000000"):
        assert part in p, part


def test_a_verifier_fail_counts_one_rework_and_asks_for_advice(tmp_path):
    gaps = {"met": False, "gaps": ["S1"], "evidence": []}
    prompts, labels, result = run_calls(IMPL_JS, tmp_path, {"goal": "g", "size": "full"}, {**IMPL_RESPONSES, "sign-off": gaps})
    assert "reassess:verifier-fail" in labels and "--verifier-fail" in prompts["reassess:verifier-fail"]
    assert result["rework"] == 1 and result["status"] == "needs-attention" and "--rework 1" in prompts["record-run"]


def test_a_stated_mode_change_is_applied_and_recorded_as_an_escalation(tmp_path):
    impl = {**IMPL, "mode_change": {"to": "full", "reason": "a sensitive area appeared"}}
    prompts, _, result = run_calls(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": impl})
    assert result["size"] == "full" and result["escalations"] == ["light>full"] and "--escalations light>full" in prompts["record-run"]
    assert "ai-resources runs reassess" in prompts["implement"]


# --- by-reference handoff and the correction budget ---------------------------------------------------------------

def run_calls(script, tmp_path, args, responses):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"args": args, "responses": responses}))
    out = subprocess.run(["node", str(HARNESS), str(script), str(cfg)], capture_output=True, text=True, check=True, timeout=60)
    data = json.loads(out.stdout)
    return {c["label"]: c["prompt"] for c in data["calls"]}, [c["label"] for c in data["calls"]], data["result"]


LEDGER_ARGS = {"goal": "g", "size": "light", "plan_path": ".agent-output/planner/x-plan.md",
               "tasks_path": "specs/features/x/tasks.md", "task_ids": ["T1", "T2"], "spec_ids": ["S1", "S2"]}


def test_implement_hands_work_over_by_reference_not_by_paraphrase(tmp_path):
    prompts, _, result = run_calls(IMPL_JS, tmp_path, LEDGER_ARGS, IMPL_RESPONSES)
    p = prompts["implement"]
    assert "Work by reference: specs/features/x/tasks.md and .agent-output/planner/x-plan.md - T1, T2, S1, S2" in p
    assert '"## Log"' in p and "Report the ids you covered" in p
    assert "RED per requested rule" in p and "PRESERVE" in p and "mktemp -d" in p and "Never push or merge" in p
    assert result["tasks_path"] == "specs/features/x/tasks.md"


def test_implement_without_a_plan_passes_the_request_verbatim(tmp_path):
    prompts, _, _ = run_calls(IMPL_JS, tmp_path, {"goal": "rename the flag", "size": "light"}, IMPL_RESPONSES)
    assert 'verbatim: "rename the flag"' in prompts["implement"] and "Work by reference" not in prompts["implement"]


def test_signoff_runs_authorized_examples_in_a_scratch_copy_with_a_verdict_per_id(tmp_path):
    prompts, _, _ = run_calls(IMPL_JS, tmp_path, {**LEDGER_ARGS, "risk_items": ["3: contract"]}, IMPL_RESPONSES)
    p = prompts["sign-off"]
    assert "mktemp -d" in p and "every authorized example" in p and "one verdict per criterion or S# id" in p
    assert "Ledger: specs/features/x/tasks.md - S1, S2" in p


def test_light_gets_one_correction_round_and_full_keeps_two(tmp_path):
    failing = {"passed": False, "command": "pytest", "failures": ["t::a"]}
    for size, rounds in (("light", 1), ("full", 2)):
        _, labels, _ = run_calls(IMPL_JS, tmp_path, {"goal": "g", "size": size}, {**IMPL_RESPONSES, "test-gate": failing, "fix-tests": IMPL})
        assert sum(1 for label in labels if label.startswith("fix-tests")) == rounds, size


def test_plan_asks_for_ids_and_the_sizing_check_and_passes_them_on(tmp_path):
    plan = {**PLAN, "tasks_path": "specs/features/x/tasks.md", "task_ids": ["T1"], "spec_ids": ["S1"]}
    prompts, _, result = run_calls(PLAN_JS, tmp_path, {"goal": "g", "size": "light"}, {**PLAN_RESPONSES, "write-plan": plan})
    assert "task_ids" in prompts["write-plan"] and "ai-resources plan-check" in prompts["write-plan"]
    assert result["task_ids"] == ["T1"] and result["spec_ids"] == ["S1"]
    assert '"tasks_path":"specs/features/x/tasks.md"' in result["next"] and '"task_ids":["T1"]' in result["next"]
