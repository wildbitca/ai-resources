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


def test_plan_failed_classifier_falls_back_to_full(tmp_path):
    labels, result = run(PLAN_JS, tmp_path, {"goal": "g"}, PLAN_RESPONSES)
    assert labels[0] == "classify" and len(labels) == 10 and result["size"] == "full"


def test_plan_next_hands_the_size_to_implement(tmp_path):
    _, result = run(PLAN_JS, tmp_path, {"goal": "g", "size": "light"}, PLAN_RESPONSES)
    assert '"size":"light"' in result["next"]


IMPL_RESPONSES = {"implement": IMPL, "test-gate": TEST_OK, "review": NO_FINDINGS, "sign-off": SIGNOFF}


def test_implement_inline_spawns_nothing(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "inline"}, IMPL_RESPONSES)
    assert labels == [] and result["status"] == "inline"


def test_implement_light_without_risk_has_one_lens_and_no_signoff(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, IMPL_RESPONSES)
    assert labels == ["implement", "test-gate", "review:combined"]
    assert result["status"] == "done" and result["sign_off"] is None


def test_implement_light_with_risk_adds_the_independent_verifier(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light", "risk_items": ["3: contract"]}, IMPL_RESPONSES)
    assert labels == ["implement", "test-gate", "review:combined", "sign-off"]
    assert result["sign_off"]["met"] is True


def test_implement_light_security_risk_keeps_the_security_prompt(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"args": {"goal": "g", "size": "light", "risk_items": ["2: authz"]}, "responses": IMPL_RESPONSES}))
    out = subprocess.run(["node", str(HARNESS), str(IMPL_JS), str(cfg)], capture_output=True, text=True, check=True, timeout=60)
    assert json.loads(out.stdout)["result"]["review"]["security_reviewed"] is True


def test_implement_full_keeps_three_lenses_and_signoff(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "full"}, IMPL_RESPONSES)
    assert labels == ["implement", "test-gate", "review:correctness", "review:security", "review:test-integrity", "sign-off"]
    assert result["status"] == "done"


def test_implement_without_size_classifies_once_and_defaults_to_full(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "plan_path": "p.md"}, IMPL_RESPONSES)
    assert labels[0] == "classify" and result["size"] == "full" and "sign-off" in labels
