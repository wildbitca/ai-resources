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


# -- `backend: lane` (ADR-0007) ----------------------------------------------------------------------------

from ai_resources import roles_policy as _rp  # noqa: E402

LANE_ROLES = sorted(_rp.LANE_ELIGIBILITY)
CHAIN = ("""
- id: tests
  subagent_type: tester
  skills: [common-best-practices]
  backend: lane
  write_scope: [tests/]
  prompt_template: |
    Write tests.
- id: verify-tests
  subagent_type: verifier
  skills: [common-best-practices]
  verifies: [tests]
  prompt_template: |
    Check the tests.
- id: integrate
  subagent_type: implementer
  skills: [common-best-practices]
  backend: lane
  integrates: [tests]
  prompt_template: |
    Implement.
- id: verify-integration
  subagent_type: verifier
  skills: [common-best-practices]
  verifies: [integrate]
  prompt_template: |
    Check the result.
""")


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


def _lane(role, sid="s1", extra=""):
    return _step(role, "backend: lane\n" + extra, sid=sid)


@pytest.mark.parametrize("role", LANE_ROLES)
def test_lane_is_valid_for_every_eligible_role(vk, role):
    failures, warns = _run_doc(vk, _lane(role))
    assert failures == [] and warns == []


def test_implementer_may_be_a_lane_step(vk):
    failures, _ = _run_doc(vk, _lane("implementer"))
    assert failures == []


def test_unknown_role_is_rejected_on_lane(vk):
    failures, _ = _run_doc(vk, _lane("made-up"), roles=ROLES | {"made-up"})
    assert len(failures) == 1 and "t.workflow.yaml:s1" in failures[0] and "made-up" in failures[0]
    assert "lane" in failures[0]


def test_agent_id_is_forbidden_on_a_lane_step(vk):
    failures, _ = _run_doc(vk, _lane("explore", extra="agentId: agy-flash\n"))
    assert len(failures) == 1 and "agentId" in failures[0]


def test_lane_fields_on_a_non_lane_step_fail(vk):
    for extra in ("write_scope: [tests/]\n", "integrates: [x]\n", "execution_hints:\n  lane_member: second\n"):
        failures, _ = _run_doc(vk, _step("tester", extra))
        assert failures and "lane" in failures[0], extra


def test_integrating_writer_defaults_and_may_be_named(vk):
    failures, _ = _run_doc(vk, CHAIN)
    assert failures == []
    chain_gp = CHAIN.replace("subagent_type: implementer", "subagent_type: generalPurpose")
    failures, _ = _run_doc(vk, chain_gp, top="integrating_writer: generalPurpose\n")
    assert failures == []
    failures, _ = _run_doc(vk, chain_gp)       # default is implementer, the integrates step is generalPurpose
    assert any("integrating" in f for f in failures), failures


@pytest.mark.parametrize("value", ["tester", "verifier", "made-up", "[implementer, generalPurpose]", "''", "7"])
def test_invalid_integrating_writer_is_rejected(vk, value):
    failures, _ = _run_doc(vk, _lane("explore"), top=f"integrating_writer: {value}\n")
    assert failures and any("integrating_writer" in f for f in failures), failures


def test_integrating_writer_is_never_a_parallel_member(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = _lane("implementer", "impl", hint) + _lane("code-reviewer", "rev", hint)
    failures, _ = _run_doc(vk, steps)
    assert any("t.workflow.yaml:g:impl" in f and "integrating" in f for f in failures), failures


@pytest.mark.parametrize("scope", ["[/etc]", "['../x']", "[a/../b]", "['src/*.py']", "['.']", "[.git/hooks/]",
                                   "['.claude/']", "[handoff.md]", "['.agent-output/']", "[]", "tests/", "[7]",
                                   "[tests/, tests/]"])
def test_invalid_write_scope_is_rejected(vk, scope):
    failures, _ = _run_doc(vk, _lane("tester", extra=f"write_scope: {scope}\n"))
    assert any("write_scope" in f for f in failures), failures


def test_write_scope_needs_a_scope_capable_role(vk):
    failures, _ = _run_doc(vk, _lane("code-reviewer", extra="write_scope: [docs/]\n"))
    assert any("write_scope" in f and "code-reviewer" in f for f in failures), failures


def test_write_scope_needs_a_lane_step(vk):
    failures, _ = _run_doc(vk, _step("tester", "backend: claude-code\nwrite_scope: [tests/]\n"))
    assert any("write_scope" in f for f in failures)


def test_disjoint_parallel_scopes_pass_and_overlap_fails(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    ok = (_lane("tester", "t", "write_scope: [tests/]\n" + hint)
          + _lane("doc-writer", "d", "write_scope: [docs/]\n" + hint))
    failures, _ = _run_doc(vk, ok + _verify_chain(["t", "d"]))
    assert failures == [], failures
    bad = (_lane("tester", "t", "write_scope: [docs/]\n" + hint)
           + _lane("doc-writer", "d", "write_scope: [docs/guide/]\n" + hint))
    failures, _ = _run_doc(vk, bad + _verify_chain(["t", "d"]))
    assert any("overlap" in f for f in failures), failures


def _verify_chain(scope_ids):
    verifies = "".join(
        _step("verifier", f"verifies: [{sid}]\n", sid=f"v-{sid}") for sid in scope_ids)
    integ = _step("implementer", "backend: lane\nintegrates: [" + ", ".join(scope_ids) + "]\n", sid="integ")
    final = _step("verifier", "verifies: [integ]\n", sid="v-integ")
    return verifies + integ + final


def test_claude_code_writer_may_not_share_a_group_with_a_lane_step(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = (_step("tester", hint, sid="t") + _lane("doc-writer", "d", "write_scope: [docs/]\n" + hint)
             + _verify_chain(["d"]))
    failures, _ = _run_doc(vk, steps)
    assert any("t.workflow.yaml:g:t" in f and "lane" in f for f in failures), failures


def test_a_checker_lane_step_is_never_a_member_with_another_lane_step(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = _lane("verifier", "v", hint) + _lane("code-reviewer", "rev", hint)
    failures, _ = _run_doc(vk, steps)
    assert any("g:v" in f and "attribute" in f for f in failures), failures
    steps = _lane("verifier", "v", hint) + _lane("security-auditor", "sec", hint)
    failures, _ = _run_doc(vk, steps)
    assert failures, failures


def test_a_checker_lane_step_may_share_a_group_with_claude_code_and_non_lane_steps(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = _lane("security-auditor", "sec", hint) + _step("code-reviewer", hint, sid="rev")
    failures, _ = _run_doc(vk, steps)
    assert failures == [], failures


def test_claude_code_report_role_may_share_a_group_with_a_lane_step(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    steps = _step("security-auditor", hint, sid="sec") + _lane("code-reviewer", "rev", hint)
    failures, _ = _run_doc(vk, steps)
    assert failures == [], failures


def test_write_scope_chain_is_complete_and_ordered(vk):
    failures, _ = _run_doc(vk, CHAIN)
    assert failures == []
    only_scope = CHAIN.split("- id: verify-tests")[0]
    failures, _ = _run_doc(vk, only_scope)
    assert any("verifier" in f and "tests" in f for f in failures), failures
    no_integration = CHAIN.replace("  integrates: [tests]\n", "")
    failures, _ = _run_doc(vk, no_integration)
    assert any("integrat" in f for f in failures), failures
    no_final = CHAIN.split("- id: verify-integration")[0]
    failures, _ = _run_doc(vk, no_final)
    assert any("integrate" in f and "verifier" in f for f in failures), failures


def test_chain_order_is_enforced(vk):
    blocks = CHAIN.strip().split("\n- id: ")
    blocks = [blocks[0]] + ["- id: " + b for b in blocks[1:]]
    blocks[0] = blocks[0]
    swapped = "\n".join([blocks[0], blocks[2], blocks[1], blocks[3]])      # integrate before verify-tests
    failures, _ = _run_doc(vk, swapped)
    assert failures, "an integrating step before the scope step's verifier must fail"


def test_integrates_must_name_earlier_scope_steps(vk):
    failures, _ = _run_doc(vk, CHAIN.replace("integrates: [tests]", "integrates: [ghost]"))
    assert any("ghost" in f for f in failures), failures
    failures, _ = _run_doc(vk, CHAIN.replace("integrates: [tests]", "integrates: [verify-tests]"))
    assert any("write_scope" in f for f in failures), failures


def test_integrates_needs_the_integrating_writer_role(vk):
    failures, _ = _run_doc(vk, CHAIN.replace("subagent_type: implementer", "subagent_type: doc-writer"))
    assert any("integrates" in f for f in failures), failures


def test_lane_member_second_only_for_report_roles_in_a_group(vk):
    hint = "execution_hints:\n  parallel_group: g\n"
    sec = "execution_hints:\n  parallel_group: g\n  lane_member: second\n"
    ok = _lane("code-reviewer", "r1", hint) + _lane("code-reviewer", "r2", sec)
    failures, _ = _run_doc(vk, ok)
    assert failures == [], failures
    writer = _lane("tester", "w1", hint) + _lane("tester", "w2", sec)
    failures, _ = _run_doc(vk, writer)
    assert any("lane_member" in f for f in failures), failures
    alone = _lane("code-reviewer", "r2", "execution_hints:\n  lane_member: second\n")
    failures, _ = _run_doc(vk, alone)
    assert any("lane_member" in f for f in failures), failures
    bad_value = _lane("code-reviewer", "r2", "execution_hints:\n  parallel_group: g\n  lane_member: third\n")
    failures, _ = _run_doc(vk, _lane("code-reviewer", "r1", hint) + bad_value)
    assert any("lane_member" in f for f in failures), failures


def test_lane_steps_never_write_the_handoff(vk):
    failures, _ = _run_doc(vk, _step("explore", "backend: lane\n", prompt="Update {{handoff_file}} now."))
    assert any("handoff" in f and "t.workflow.yaml:s1" in f for f in failures), failures


def test_shipped_workflows_still_have_no_lane_steps_and_validate():
    spec = importlib.util.spec_from_file_location("validate_kit_real2", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.check_workflows(mod.check_skills(), mod.check_roles())
    assert mod.failures == []
