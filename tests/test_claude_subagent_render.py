"""The render-only subagent pass: the S12 re-render diffs it against disk, so it must equal what setup writes."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_resources.setup.cockpits import claude

EXECUTORS = {"by_role": {"implementer": {"model": "google/gemini-3.7-flash"}, "planner": {"model": "claude-opus-5"}},
             "by_persona": {"code-reviewer-angular": {"model": "anthropic/claude-sonnet-5"}}}


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "AGENTS_DIR", tmp_path / "agents")
    # These tests are about how a persona renders. Persona subagents exist only for an active domain (ADR-0014 D6;
    # the gating itself is tests/test_persona_domains.py), so they switch every domain on.
    monkeypatch.setenv("AI_RESOURCES_SKILLS", "all")
    return tmp_path / "agents"


def test_the_render_equals_what_the_writer_puts_on_disk(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert not agents.exists()                                     # render-only: nothing was created
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model")
    on_disk = {p.stem: p.read_text(encoding="utf-8") for p in agents.glob("*.md")}
    assert rendered == on_disk and len(rendered) == 40


def test_the_model_line_follows_the_executors(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert "model: google/gemini-3.7-flash" in rendered["implementer"]
    assert "model: anthropic/claude-sonnet-5" in rendered["code-reviewer-angular"]
    assert "model: claude-opus-5" in rendered["planner"]


def test_a_persona_inherits_its_roles_model_in_the_render(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert "model: google/gemini-3.7-flash" in rendered["implementer-angular"]


def test_single_model_aliases_render_as_aliases(agents):
    rendered = claude.render_subagent_files({"by_role": {"implementer": {"model": "sonnet"}}}, "/kit", "single-model")
    assert "model: sonnet" in rendered["implementer"]


def test_strict_single_model_refuses_a_non_native_id(agents):
    from ai_resources.setup.cockpits import _shared
    with pytest.raises(_shared.SkipCockpit):
        claude.render_subagent_files(EXECUTORS, "/kit", "single-model", strict=True)
    legacy = claude.render_subagent_files(EXECUTORS, "/kit", "single-model", strict=False)
    assert "model: claude-opus-5" in legacy["planner"] and "gemini" not in legacy["implementer"]   # the legacy fallback


def test_a_description_with_quotes_is_escaped_in_the_text():
    text = claude._subagent_text("x", 'say "hi"\nnow', "Read", "sonnet", "body")
    assert 'description: "say \\"hi\\" now"' in text and text.endswith("body")


def test_a_role_with_mapped_skills_gets_a_skills_list_in_its_frontmatter(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    head = rendered["tester"].split("---\n")[1]
    assert "skills:\n  - common-tdd\n" in head
    assert "skills:\n  - common-security-audit\n  - common-security-standards\n" in rendered["security-auditor"]


def test_a_persona_carries_its_roles_skills(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert "skills:\n  - common-best-practices\n" in rendered["implementer-angular"]


def test_a_role_without_data_renders_byte_identical_to_the_old_format(agents):
    old = claude._subagent_text("x", "d", "Read", "sonnet", "body")
    assert claude._subagent_text("x", "d", "Read", "sonnet", "body", skills=None, mcp_servers=None) == old
    assert claude._subagent_text("x", "d", "Read", "sonnet", "body", skills=(), mcp_servers=()) == old
    assert "skills:" not in old and "mcpServers:" not in old
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert "skills:" not in rendered["code-reviewer"] and "mcpServers:" not in rendered["code-reviewer"]


def test_mcp_servers_are_emitted_only_when_a_role_lists_them():
    text = claude._subagent_text("x", "d", None, "sonnet", "body", mcp_servers=["engram"])
    assert "mcpServers:\n  - engram\n" in text


def test_frontmatter_stays_valid_yaml_with_skills():
    yaml = pytest.importorskip("yaml")
    text = claude._subagent_text("x", "d", "Read", "sonnet", "body", skills=["a", "b"], mcp_servers=["engram"])
    fm = yaml.safe_load(text.split("---\n")[1])
    assert fm["skills"] == ["a", "b"] and fm["mcpServers"] == ["engram"] and fm["model"] == "sonnet"


def test_a_stale_generated_file_is_still_pruned_with_skills_present(agents):
    class Tracking:
        subagent_files_installed = ["gone-role"]
    agents.mkdir(parents=True)
    (agents / "gone-role.md").write_text("x")
    t = Tracking()
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model", t)
    assert not (agents / "gone-role.md").exists() and "tester" in t.subagent_files_installed


def test_every_role_skill_in_the_table_is_a_real_skill():
    from ai_resources import repo_root, skill_scope
    ids = {d.name for d in (repo_root() / "skills").iterdir() if (d / "SKILL.md").is_file()}
    for role, skills in skill_scope.ROLE_SKILLS.items():
        assert set(skills) <= ids, role
