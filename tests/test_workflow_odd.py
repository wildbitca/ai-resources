"""workflow-odd: a four-step, size-gated workflow whose steps run inline, plus the task-ledger and Engram mirror convention."""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from ai_resources import generate, skill_scope
from ai_resources.orchestrator import workflow_loader as wl
from ai_resources.setup.cockpits import _shared

REPO = Path(__file__).resolve().parents[1]
YAML_PATH = REPO / "workflows" / "odd.workflow.yaml"
DOC = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
LEDGER = (REPO / "skills" / "kit-orchestration" / "references" / "task-ledger.md").read_text(encoding="utf-8")
CORE = (REPO / "skills" / "kit-orchestration" / "SKILL.md").read_text(encoding="utf-8")


def test_the_workflow_is_four_inline_steps_in_order():
    steps = DOC["steps"]
    assert [s["id"] for s in steps] == ["classify", "implement", "verify", "close"]
    assert [s["subagent_type"] for s in steps] == ["planner", "implementer", "verifier", "doc-writer"]
    assert all(s["execution_hints"] == {"inline": True} for s in steps)
    assert all("backend" not in s for s in steps), "no lane or openclaw-agent step: nothing here needs a spawn"


def test_the_orchestrator_loader_accepts_it_and_ignores_the_inline_hint():
    wf = wl.load("odd", REPO)
    assert wf.name == "odd" and [s.id for s in wf.steps] == ["classify", "implement", "verify", "close"]


def test_prompts_hand_work_over_by_reference_and_keep_the_temporal_ledger():
    text = YAML_PATH.read_text(encoding="utf-8")
    assert "specs/features/<feature>/tasks.md" in text and "odd/tasks" not in text
    assert "Work by reference" in text and "Covered: T#, S#" in text
    assert "mktemp -d" in text and "ADR-0007" in text, "independence and scratch rules stay"
    assert "DELETE {{handoff_file}}" in text and "delete the ledger" in text


def test_generated_skill_is_current_and_indexed():
    skill = REPO / "skills" / "workflow-odd" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == generate._workflow_skill_md("odd.workflow.yaml", DOC)
    assert "disable-model-invocation: true" in skill.read_text(encoding="utf-8")
    index = json.loads((REPO / "skills-index.json").read_text(encoding="utf-8"))
    assert any(e.get("id") == "workflow-odd" or e.get("name") == "workflow-odd" for e in index["skills"])
    assert index["count"] == len(index["skills"])


def test_skill_scope_links_it_through_the_workflow_prefix_without_a_new_entry():
    ids = {p.name for p in (REPO / "skills").iterdir() if p.is_dir()}
    assert "workflow-odd" in skill_scope.include_list(ids, env={})
    assert "workflow-odd" not in skill_scope.INCLUDE_IDS and "workflow-odd" not in skill_scope.LOADABLE_BY_PATH


def test_ledger_reference_fixes_the_mirror_convention():
    assert "`kit/<feature>/tasks`" in LEDGER and "capture_prompt: false" in LEDGER
    assert "file first" in LEDGER.lower() and "two, no more" in LEDGER
    used = set(re.findall(r"`(mem_[a-z_]+)`", LEDGER))
    assert used == {"mem_current_project", "mem_save", "mem_search", "mem_get_observation", "mem_context"}
    assert "references/task-ledger.md" in CORE
    rule = (REPO / "rules" / "300-engram-memory.mdc").read_text(encoding="utf-8")
    assert "kit/<feature>/tasks" in rule


def test_managed_block_gains_one_short_pointer_line():
    block = _shared.openclaw_agent_kit_md("/k")
    line = next(ln for ln in block.splitlines() if ln.startswith("- Size first:"))
    assert "workflow-odd" in line and "--inline" in line
    assert len(line.encode("utf-8")) + 1 < 300, "the plan allows under 300 bytes of block growth"
    assert block.count("Size first") == 1
