"""The by-reference handoff keys are optional: present they flow through the engine and the prompt, absent nothing changes.

Contract (risk item 3): `handoff.md.template` front matter, `references/handoff.md`, the engine's HANDOFF_KEYS, the reply
parser and the SubagentStop hook all move together. A plain run must render the same front matter as before.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from ai_resources.orchestrator import engine as eng
from ai_resources.orchestrator import reply as reply_mod
from orchestrator_fakes import MODELS, OK, dispatches, finish, make

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "handoff.md.template"
OPTIONAL = ("task_ids", "spec_ids", "class", "route")
CORE = ("workflow", "step", "status", "blocked", "return_to_step", "block_reason", "domain", "requires_tests",
        "security_critical", "refs", "verdicts", "next", "docs_updated", "git_integration")


def _front(text: str) -> dict:
    return yaml.safe_load(text.split("---")[1])


def test_template_keeps_every_core_key_and_declares_the_optional_ones_empty():
    front = _front(TEMPLATE.read_text(encoding="utf-8"))
    assert all(k in front for k in CORE)
    assert front["refs"]["spec"] == ""
    assert front["task_ids"] == [] and front["spec_ids"] == [] and front["class"] == "" and front["route"] == ""


def test_the_reference_doc_says_the_new_keys_are_optional():
    text = (REPO / "skills/kit-orchestration/references/handoff.md").read_text(encoding="utf-8")
    for key in ("refs.spec", "task_ids", "spec_ids", "class", "route"):
        assert f"`{key}`" in text
    assert "never require them" in text


def test_a_plain_run_renders_no_optional_key(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models=MODELS)
    [d] = dispatches(e.next(run["id"]))
    finish(e, run["id"], d, OK + "Plan_ref: .agent-output/planner/p.md\n")
    front = _front(open(e.store.handoff_path(run["id"])).read())
    assert not any(k in front for k in OPTIONAL)
    assert "spec" not in front["refs"] or front["refs"].get("spec") in (None, "")
    assert "Work by reference" not in d["prompt"]


def test_reported_keys_reach_the_front_matter_and_the_next_prompt(tmp_path):
    e, wf, _ = make(tmp_path)
    run = e.start(wf, "add a thing", "/work/repo", models=MODELS)
    [d] = dispatches(e.next(run["id"]))
    finish(e, run["id"], d, OK + "Spec_ref: specs/features/x/tasks.md\nTask_ids: T1, T2\nSpec_ids: [S1, S3-S4]\n"
                               "Class: m\nRoute: inline\n")
    front = _front(open(e.store.handoff_path(run["id"])).read())
    assert front["refs"]["spec"] == "specs/features/x/tasks.md"
    assert front["task_ids"] == ["T1", "T2"] and front["spec_ids"] == ["S1", "S3-S4"]
    assert front["class"] == "M" and front["route"] == "inline"
    prompt = eng.build_prompt(e.store.load(run["id"]), wf.steps[1], 1, e.kit_root, "h.md")
    assert "Work by reference: specs/features/x/tasks.md - T1, T2, S1, S3-S4" in prompt
    assert '"## Log"' in prompt and "Covered: T#, S#" in prompt


def test_an_unknown_class_is_ignored():
    h = {"refs": {}}
    e = eng.Engine.__new__(eng.Engine)
    step = type("S", (), {"id": "plan", "role": "planner"})()
    e._merge_handoff({"handoff": h}, step, reply_mod.parse(OK + "Class: huge\nRoute: \n"))
    assert "class" not in h and "route" not in h


def _hook(name: str, payload: dict, env_root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "hooks" / name)], input=json.dumps(payload), text=True,
                          capture_output=True, env={"CLAUDE_PROJECT_DIR": str(env_root), "PATH": "/usr/bin:/bin"})


def test_the_return_hook_accepts_a_block_without_covered_and_mentions_it_when_asking(tmp_path):
    out = tmp_path / ".agent-output"
    out.mkdir()
    (out / "handoff-x.md").write_text("---\nstep: plan\n---\n", encoding="utf-8")
    base = {"agent_type": "planner", "cwd": str(tmp_path)}
    ok = "## Result\n- Status: success\n## Artifacts\n- Files touched: none\n## Routing\n- Blocked: no\n"
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"message": {"role": "user", "content": "see kit-orchestration"}}) + "\n")
    passing = _hook("kit_subagent_return.py", {**base, "agent_transcript_path": str(transcript), "last_assistant_message": ok}, tmp_path)
    assert passing.returncode == 0
    asking = _hook("kit_subagent_return.py", {**base, "agent_transcript_path": str(transcript), "last_assistant_message": "done"}, tmp_path)
    assert asking.returncode == 2 and "Covered: T#/S# ids" in asking.stderr


def test_the_handoff_guard_ignores_front_matter_keys(tmp_path):
    out = tmp_path / ".agent-output"
    out.mkdir()
    handoff = out / "handoff-x.md"
    handoff.write_text("---\ntask_ids: [T1]\n---\n", encoding="utf-8")
    (out / "parallel-group.json").write_text(json.dumps({"group": "g", "handoff_file": str(handoff)}))
    other = tmp_path / "src.py"
    r = _hook("kit_handoff_guard.py", {"agent_id": "a", "tool_name": "Write", "tool_input": {"file_path": str(other)}, "cwd": str(tmp_path)}, tmp_path)
    assert r.returncode == 0
    r = _hook("kit_handoff_guard.py", {"agent_id": "a", "tool_name": "Write", "tool_input": {"file_path": str(handoff)}, "cwd": str(tmp_path)}, tmp_path)
    assert r.returncode == 2
