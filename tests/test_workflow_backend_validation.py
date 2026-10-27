"""Per-step `backend` / `agentId` validation in scripts/validate_kit.py (ADR 0005).

The validator is a standalone script, so it is loaded by path. Each case writes one workflow
into a temp tree, points the module's ROOT at it and reads the module-level `failures` /
`warnings` lists.
"""
from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

ROLES = {p.stem for p in (REPO / "agents" / "roles").glob("*.md")}
WHITELIST = ["planner", "explore", "software-architect", "code-reviewer", "security-auditor", "verifier"]
WARNED = ["tester", "doc-writer"]
REJECTED = ["implementer", "generalPurpose", "sentry-fixer", "crashlytics-fixer", "package-upgrade",
            "terraform-maintainer", "crossplane-upjet-maintainer"]


@pytest.fixture()
def vk(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("validate_kit_under_test", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "workflows").mkdir()
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    mod.failures.clear()
    if hasattr(mod, "warnings"):
        mod.warnings.clear()
    return mod


def _run(vk, steps_yaml: str):
    path = vk.ROOT / "workflows" / "t.workflow.yaml"
    path.write_text(
        "name: t\ndescription: d\ntrigger: t\nsteps:\n" + textwrap.indent(textwrap.dedent(steps_yaml), "  "),
        encoding="utf-8",
    )
    vk.failures.clear()
    if hasattr(vk, "warnings"):
        vk.warnings.clear()
    vk.check_workflows({"common-best-practices"}, ROLES)
    return list(vk.failures), list(getattr(vk, "warnings", []))


def _step(role: str, extra: str = "", sid: str = "s1", prompt: str = "Do the thing.") -> str:
    return (
        f"- id: {sid}\n"
        f"  subagent_type: {role}\n"
        f"  skills: [common-best-practices]\n"
        f"{textwrap.indent(textwrap.dedent(extra), '  ') if extra else ''}"
        f"  prompt_template: |\n    {prompt}\n"
    )


def test_no_backend_passes(vk):
    failures, _ = _run(vk, _step("planner"))
    assert failures == []


def test_explicit_claude_code_passes(vk):
    failures, _ = _run(vk, _step("implementer", "backend: claude-code\n"))
    assert failures == []


def test_unknown_backend_fails(vk):
    failures, _ = _run(vk, _step("planner", "backend: gemini\n"))
    assert len(failures) == 1
    assert "t.workflow.yaml:s1" in failures[0]
    assert "gemini" in failures[0]


def test_non_scalar_backend_fails_without_crashing(vk):
    failures, _ = _run(vk, _step("planner", "backend: [a, b]\n"))
    assert len(failures) == 1
    assert "t.workflow.yaml:s1" in failures[0]


def test_agent_id_with_trailing_newline_fails(vk):
    failures, _ = _run(vk, _step("explore", 'backend: openclaw-agent\nagentId: "agy-flash\\n"\n'))
    assert len(failures) == 1
    assert "agentId" in failures[0]


def test_openclaw_without_agent_id_fails(vk):
    failures, _ = _run(vk, _step("planner", "backend: openclaw-agent\n"))
    assert len(failures) == 1
    assert "t.workflow.yaml:s1" in failures[0]
    assert "agentId" in failures[0]


def test_openclaw_empty_agent_id_fails(vk):
    failures, _ = _run(vk, _step("planner", "backend: openclaw-agent\nagentId: ''\n"))
    assert len(failures) == 1
    assert "t.workflow.yaml:s1" in failures[0]


@pytest.mark.parametrize("bad", ["a; rm", "--flag", "-x", "a b", "a" * 65, "_lead", "a/b", "$(id)"])
def test_injection_like_agent_ids_fail(vk, bad):
    failures, _ = _run(vk, _step("planner", f"backend: openclaw-agent\nagentId: {bad!r}\n"))
    assert len(failures) == 1, failures
    assert "t.workflow.yaml:s1" in failures[0]
    assert "agentId" in failures[0]


@pytest.mark.parametrize("good", ["agy-flash", "a", "A1_b-2", "x" * 64])
def test_valid_agent_ids_pass(vk, good):
    failures, _ = _run(vk, _step("planner", f"backend: openclaw-agent\nagentId: {good}\n"))
    assert failures == []


def test_agent_id_with_claude_code_fails(vk):
    failures, _ = _run(vk, _step("planner", "backend: claude-code\nagentId: agy-flash\n"))
    assert len(failures) == 1
    assert "t.workflow.yaml:s1" in failures[0]
    assert "agentId" in failures[0]


def test_agent_id_without_backend_fails(vk):
    failures, _ = _run(vk, _step("planner", "agentId: agy-flash\n"))
    assert len(failures) == 1
    assert "t.workflow.yaml:s1" in failures[0]


@pytest.mark.parametrize("role", REJECTED)
def test_write_roles_are_rejected_on_openclaw(vk, role):
    failures, _ = _run(vk, _step(role, "backend: openclaw-agent\nagentId: agy-flash\n"))
    assert len(failures) == 1, failures
    assert "t.workflow.yaml:s1" in failures[0]
    assert role in failures[0]


@pytest.mark.parametrize("role", WHITELIST)
def test_read_only_roles_pass_on_openclaw(vk, role):
    failures, warns = _run(vk, _step(role, "backend: openclaw-agent\nagentId: agy-flash\n"))
    assert failures == []
    assert warns == []


@pytest.mark.parametrize("role", WARNED)
def test_tester_and_doc_writer_warn_but_pass(vk, role):
    failures, warns = _run(vk, _step(role, "backend: openclaw-agent\nagentId: agy-flash\n"))
    assert failures == []
    assert len(warns) == 1
    assert "t.workflow.yaml:s1" in warns[0]
    assert role in warns[0]


def test_mixed_parallel_group_passes(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = (
        _step("code-reviewer", "backend: openclaw-agent\nagentId: agy-flash\n" + hint, sid="review")
        + _step("security-auditor", hint, sid="security")
    )
    failures, warns = _run(vk, steps)
    assert failures == []
    assert warns == []


def test_openclaw_parallel_member_writing_handoff_fails(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = (
        _step("code-reviewer", "backend: openclaw-agent\nagentId: agy-flash\n" + hint, sid="review",
              prompt="Update {{handoff_file}} now.")
        + _step("security-auditor", hint, sid="security")
    )
    failures, _ = _run(vk, steps)
    assert any("t.workflow.yaml:g:review" in f and "handoff" in f for f in failures), failures


@pytest.mark.parametrize("role", WARNED)
def test_tester_or_doc_writer_never_a_parallel_openclaw_member(vk, role):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = (
        _step(role, "backend: openclaw-agent\nagentId: agy-flash\n" + hint, sid="gate")
        + _step("security-auditor", hint, sid="security")
    )
    failures, _ = _run(vk, steps)
    assert any("t.workflow.yaml:g:gate" in f and "parallel" in f for f in failures), failures


def test_mismatched_entry_criteria_still_fails(vk):
    steps = (
        _step("code-reviewer", "backend: openclaw-agent\nagentId: agy-flash\nentry_criteria: A\n"
              "execution_hints:\n  parallel_group: g\n", sid="review")
        + _step("security-auditor", "entry_criteria: B\nexecution_hints:\n  parallel_group: g\n", sid="security")
    )
    failures, _ = _run(vk, steps)
    assert any("entry criteria" in f for f in failures), failures


def test_real_workflows_still_validate():
    spec = importlib.util.spec_from_file_location("validate_kit_real", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.check_workflows(mod.check_skills(), mod.check_roles())
    assert mod.failures == []


def test_example_cross_provider_workflow_has_three_openclaw_steps_and_a_mixed_group():
    import yaml

    path = REPO / "workflows" / "feature-cross-provider.workflow.yaml"
    assert path.is_file(), "the worked example must exist"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    remote = [s for s in doc["steps"] if s.get("backend") == "openclaw-agent"]
    assert [s["id"] for s in remote] == ["research", "plan", "review"]
    assert {s["agentId"] for s in remote} == {"agy-flash", "agy-pro"}
    group = {s["id"] for s in doc["steps"] if (s.get("execution_hints") or {}).get("parallel_group") == "post-test"}
    assert group == {"review", "security"}
    assert not any("model" in s for s in doc["steps"]), "there is no model field"

    from ai_resources import generate

    table = generate._workflow_skill_md(path.name, doc)
    assert table.count("openclaw-agent (`agy-") == 3
    assert "| `review` | `code-reviewer` | openclaw-agent (`agy-flash`) | post-test |" in table
    assert "| `security` | `security-auditor` | claude-code | post-test |" in table


def test_generated_example_skill_is_in_sync():
    skill = REPO / "skills" / "workflow-feature-cross-provider"
    assert (skill / "SKILL.md").is_file()
    assert (skill / ".generated-workflow-skill").is_file()


def _run_doc(vk, steps_yaml: str, top: str = "", roles=None):
    path = vk.ROOT / "workflows" / "t.workflow.yaml"
    path.write_text(
        "name: t\ndescription: d\ntrigger: t\n" + top + "steps:\n" + textwrap.indent(textwrap.dedent(steps_yaml), "  "),
        encoding="utf-8",
    )
    vk.failures.clear()
    vk.warnings.clear()
    vk.check_workflows({"common-best-practices"}, roles if roles is not None else ROLES)
    return list(vk.failures), list(vk.warnings)


def test_the_lane_backend_is_gone(vk):
    failures, _ = _run_doc(vk, _step("explore", "backend: lane\n"))
    assert any("unknown backend 'lane'" in f for f in failures), failures


def test_shipped_workflows_validate():
    spec = importlib.util.spec_from_file_location("validate_kit_real2", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.check_workflows(mod.check_skills(), mod.check_roles())
    assert mod.failures == []
