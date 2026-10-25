"""Skill scoping: the include list is the one rule for every cockpit; other behaviour of sync_skill_links is unchanged."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_resources import skill_scope
from ai_resources.setup.cockpits import _shared


@pytest.fixture
def kit(tmp_path):
    root = tmp_path / "kit" / "skills"
    for name in ("workflow-bugfix", "kit-orchestration", "flutter-bloc", "terraform-x", "common-tdd"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
    return root


def _names(links: Path) -> set[str]:
    return {p.name for p in links.iterdir()} if links.is_dir() else set()


def test_none_links_every_skill_as_before(kit, tmp_path):
    links = tmp_path / "links"
    res = _shared.sync_skill_links(links, kit)
    assert _names(links) == {"workflow-bugfix", "kit-orchestration", "flutter-bloc", "terraform-x", "common-tdd"}
    assert sorted(res["linked"]) == sorted(_names(links))


def test_include_links_only_the_listed_skills(kit, tmp_path):
    links = tmp_path / "links"
    _shared.sync_skill_links(links, kit, include={"workflow-bugfix", "kit-orchestration"})
    assert _names(links) == {"workflow-bugfix", "kit-orchestration"}


def test_a_stale_kit_link_that_is_now_excluded_is_pruned(kit, tmp_path):
    links = tmp_path / "links"
    _shared.sync_skill_links(links, kit)
    res = _shared.sync_skill_links(links, kit, include={"workflow-bugfix"})
    assert _names(links) == {"workflow-bugfix"}
    assert sorted(res["removed"]) == ["common-tdd", "flutter-bloc", "kit-orchestration", "terraform-x"]


def test_foreign_links_and_real_directories_are_never_touched(kit, tmp_path):
    links = tmp_path / "links"
    links.mkdir()
    other = tmp_path / "other" / "mine"
    other.mkdir(parents=True)
    (links / "mine").symlink_to(other, target_is_directory=True)
    (links / "flutter-bloc").mkdir()  # the operator's own directory with a kit name
    _shared.sync_skill_links(links, kit, include={"workflow-bugfix"})
    assert (links / "mine").is_symlink() and (links / "flutter-bloc").is_dir() and not (links / "flutter-bloc").is_symlink()


def test_a_second_run_is_a_no_op(kit, tmp_path):
    links = tmp_path / "links"
    inc = {"workflow-bugfix"}
    _shared.sync_skill_links(links, kit, include=inc)
    assert _shared.sync_skill_links(links, kit, include=inc) == {"linked": [], "removed": [], "skipped": []}


def test_dry_run_changes_nothing(kit, tmp_path):
    links = tmp_path / "links"
    res = _shared.sync_skill_links(links, kit, dry_run=True, include={"workflow-bugfix"})
    assert res["linked"] == ["workflow-bugfix"] and not links.exists()


IDS = ["workflow-bugfix", "kit-orchestration", "flutter-bloc", "terraform-x", "common-tdd", "delegate"]


def test_the_default_scope_lists_workflows_and_the_core_commands():
    got = skill_scope.include_list(IDS, env={})
    assert got == {"workflow-bugfix", "kit-orchestration", "delegate", "common-tdd"}   # common-tdd: the tester preload


def test_the_env_opt_out_restores_every_skill():
    assert skill_scope.include_list(IDS, env={"AI_RESOURCES_SKILLS": "all"}) is None


def test_the_env_can_widen_the_scope_by_prefix():
    got = skill_scope.include_list(IDS, env={"AI_RESOURCES_SKILLS": "flutter, terraform"})
    assert got == {"workflow-bugfix", "kit-orchestration", "delegate", "common-tdd", "flutter-bloc", "terraform-x"}


def test_the_real_kit_scope_is_small_and_names_only_real_skills():
    from ai_resources import repo_root
    ids = [d.name for d in (repo_root() / "skills").iterdir() if (d / "SKILL.md").is_file()]
    got = skill_scope.include_list(ids, env={})
    assert set(skill_scope.INCLUDE_IDS) <= set(ids)
    assert 0 < len(got) < len(ids) / 2


def test_link_agents_skills_is_not_scoped():
    import inspect
    assert "include" not in inspect.signature(_shared.link_agents_skills).parameters


def test_claude_configure_passes_the_include_list_and_the_env_opt_out(tmp_path, monkeypatch):
    from ai_resources.setup import profiles, state
    from ai_resources.setup.cockpits import claude
    root = tmp_path / "claude"
    for attr, rel in (("CONFIG_ROOT", ""), ("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"),
                      ("AGENTS_DIR", "agents"), ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, root / rel if rel else root)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    seen = []
    monkeypatch.setattr(_shared, "sync_skill_links",
                        lambda *a, **k: (seen.append(k.get("include", "MISSING")), {"linked": [], "removed": [], "skipped": []})[1])
    ex = profiles.to_executors(profiles.load_profile("claude-native"))
    ctx = {"state": state.SetupState(), "executors": ex, "master_key": "", "gateway_url": ""}
    monkeypatch.delenv("AI_RESOURCES_SKILLS", raising=False)
    claude.configure(ctx)
    assert isinstance(seen[-1], set) and "workflow-bugfix" in seen[-1] and "flutter-bloc" not in seen[-1]
    monkeypatch.setenv("AI_RESOURCES_SKILLS", "all")
    claude.configure(ctx)
    assert seen[-1] is None


def test_the_workflow_generator_emits_disable_model_invocation():
    from ai_resources import generate
    doc = {"name": "t", "description": "d", "trigger": "x", "steps": [{"id": "a", "subagent_type": "planner"}]}
    md = generate._workflow_skill_md("t.workflow.yaml", doc)
    head = md.split("---\n")[1]
    assert "disable-model-invocation: true" in head


def test_every_workflow_skill_in_the_repo_carries_the_flag():
    pytest.importorskip("yaml")
    import validate_kit
    from ai_resources import repo_root
    ids = {d.name for d in (repo_root() / "skills").iterdir() if (d / "SKILL.md").is_file()}
    validate_kit.failures.clear()
    validate_kit.check_skill_scope(ids, {"planner", "tester", "verifier", "explore", "implementer",
                                         "security-auditor", "doc-writer"})
    assert validate_kit.failures == []


def test_validate_kit_flags_a_workflow_skill_without_the_flag(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    import validate_kit
    skills = tmp_path / "skills"
    (skills / "workflow-x").mkdir(parents=True)
    (skills / "workflow-x" / "SKILL.md").write_text("---\nname: workflow-x\ndescription: d\n---\n")
    (tmp_path / "workflows").mkdir()
    monkeypatch.setattr(validate_kit, "ROOT", tmp_path)
    validate_kit.failures.clear()
    validate_kit.check_skill_scope({"workflow-x"} | set(skill_scope.INCLUDE_IDS), set())
    assert any("workflow-x" in f and "disable-model-invocation" in f for f in validate_kit.failures)
    validate_kit.failures.clear()


def test_validate_kit_flags_a_step_skill_that_is_unlinked_unpreloaded_and_unmarked(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    import validate_kit
    skills = tmp_path / "skills"
    for sid in ("orphan-skill", "path-skill", "kept-skill"):
        (skills / sid).mkdir(parents=True)
        (skills / sid / "SKILL.md").write_text(f"---\nname: {sid}\ndescription: d\n---\n")
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "x.workflow.yaml").write_text(
        "name: x\ndescription: d\ntrigger: t\nsteps:\n"
        "  - {id: a, subagent_type: planner, skills: [orphan-skill, path-skill, kept-skill]}\n")
    monkeypatch.setattr(validate_kit, "ROOT", tmp_path)
    monkeypatch.setattr(skill_scope, "INCLUDE_IDS", ("kept-skill",))
    monkeypatch.setattr(skill_scope, "LOADABLE_BY_PATH", ("path-skill",))
    monkeypatch.setattr(skill_scope, "ROLE_SKILLS", {})
    ids = {"orphan-skill", "path-skill", "kept-skill"}
    validate_kit.failures.clear()
    validate_kit.check_skill_scope(ids, {"planner"})
    flagged = [f for f in validate_kit.failures if "orphan-skill" in f]
    assert flagged and "loadable-by-path" in flagged[0]
    assert not any("path-skill" in f or "kept-skill" in f for f in validate_kit.failures)
    validate_kit.failures.clear()


def test_every_role_preload_is_linked_and_hidden_from_the_main_listing():
    from ai_resources import repo_root
    ids = [d.name for d in (repo_root() / "skills").iterdir() if (d / "SKILL.md").is_file()]
    linked = skill_scope.include_list(ids, env={})
    for skills in skill_scope.ROLE_SKILLS.values():
        assert set(skills) <= linked
    assert linked >= {s for v in skill_scope.ROLE_SKILLS.values() for s in v}


# --- ADR-0015 S9: one rule for every cockpit ------------------------------------------------------------------------

def test_scope_for_is_the_one_rule_for_every_cockpit():
    ids = {"workflow-bugfix", "kit-orchestration", "flutter-bloc", "terraform-x", "common-tdd", "common-protocol-enforcement"}
    base = skill_scope.include_list(ids, env={})
    for cockpit in ("claude", "openclaw", "agy", "cursor", "codex"):
        assert skill_scope.scope_for(cockpit, ids, env={}) == base
    assert "flutter-bloc" not in base and "flutter-bloc" in skill_scope.scope_for("openclaw", ids, env={"AI_RESOURCES_SKILLS": "flutter"})
    assert skill_scope.scope_for("agy", ids, env={"AI_RESOURCES_SKILLS": "all"}) is None


def test_links_into_an_ai_resources_checkout_move_to_the_installed_kit(kit, tmp_path):
    checkout = tmp_path / "checkout"
    (checkout / "scripts" / "ai_resources").mkdir(parents=True)
    (checkout / "skills-index.json").write_text("{}")
    (checkout / "skills" / "flutter-bloc").mkdir(parents=True)
    links = tmp_path / "links"
    links.mkdir()
    (links / "flutter-bloc").symlink_to(checkout / "skills" / "flutter-bloc", target_is_directory=True)
    res = _shared.sync_skill_links(links, kit, include={"workflow-bugfix"})
    assert res["removed"] == ["flutter-bloc"] and _names(links) == {"workflow-bugfix"}


def test_a_plain_skills_dir_elsewhere_is_still_foreign(kit, tmp_path):
    other = tmp_path / "proj" / "skills" / "flutter-bloc"
    other.mkdir(parents=True)
    links = tmp_path / "links"
    links.mkdir()
    (links / "flutter-bloc").symlink_to(other, target_is_directory=True)
    res = _shared.sync_skill_links(links, kit, include={"workflow-bugfix"})
    assert res["removed"] == [] and res["skipped"] == [] and (links / "flutter-bloc").is_symlink()
