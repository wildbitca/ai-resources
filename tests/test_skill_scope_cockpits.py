"""Skill scoping reaches every cockpit, not only Claude Code (ADR-0015 S9, acceptance 11)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_resources import skill_scope
from ai_resources.setup import state
from ai_resources.setup.cockpits import _shared, claude, codex, cursor, gemini, openclaw as oc

IDS = ("workflow-bugfix", "kit-orchestration", "flutter-bloc", "symfony-doctrine", "terraform-x", "common-tdd",
       "common-protocol-enforcement", "common-system-design", "common-best-practices", "knowledge-audit")


@pytest.fixture
def kit(tmp_path, monkeypatch):
    root = tmp_path / "kit"
    for name in IDS:
        d = root / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("AI_RESOURCES_SKILLS", raising=False)
    return root


def names(path: Path) -> set[str]:
    return {p.name for p in path.iterdir()} if path.is_dir() else set()


def test_the_shared_agents_dir_gets_only_the_include_list(kit, tmp_path):
    _shared.link_agents_skills(str(kit))
    got = names(tmp_path / "home" / ".agents" / "skills")
    assert got == skill_scope.include_list(IDS, env={})
    assert "flutter-bloc" not in got and "kit-orchestration" in got and "workflow-bugfix" in got


def test_a_domain_widens_every_cockpit_the_same_way(kit, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_SKILLS", "symfony")
    _shared.link_agents_skills(str(kit))
    scoped = _shared.sync_scoped_skills(str(kit))
    want = skill_scope.include_list(IDS, env={"AI_RESOURCES_SKILLS": "symfony"})
    assert names(tmp_path / "home" / ".agents" / "skills") == want == names(_shared.scoped_skills_dir())
    assert "symfony-doctrine" in want and "flutter-bloc" not in want and scoped["skipped"] == []


def test_all_restores_every_skill(kit, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_SKILLS", "all")
    _shared.link_agents_skills(str(kit))
    assert names(tmp_path / "home" / ".agents" / "skills") == set(IDS)


def test_a_stale_kit_link_is_pruned_when_the_scope_narrows(kit, tmp_path, monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_SKILLS", "all")
    _shared.link_agents_skills(str(kit))
    monkeypatch.delenv("AI_RESOURCES_SKILLS")
    _shared.link_agents_skills(str(kit))
    assert "flutter-bloc" not in names(tmp_path / "home" / ".agents" / "skills")


def test_the_scoped_dir_lives_under_the_state_home_and_never_under_tmp(kit, tmp_path):
    d = _shared.scoped_skills_dir()
    assert d == tmp_path / "state" / "ai-resources" / "scoped-skills"
    os.environ.pop("XDG_STATE_HOME")
    assert _shared.scoped_skills_dir() == tmp_path / "home" / ".local" / "state" / "ai-resources" / "scoped-skills"


@pytest.mark.parametrize("module", [codex, cursor])
def test_the_agents_skills_cockpits_use_the_scoped_link_dir(kit, tmp_path, module, monkeypatch):
    monkeypatch.setattr(module, "CONFIG_ROOT", tmp_path / "cfg", raising=False)
    for attr in ("INSTRUCTIONS_PATH", "MCP_PATH", "AGENTS_MD_PATH"):
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, tmp_path / "cfg" / attr)
    monkeypatch.setattr(_shared, "stable_kit_root", lambda p: kit)
    monkeypatch.setattr(module, "repo_root", lambda: kit)
    try:
        module.configure({"state": state.SetupState()})
    except Exception:           # a cockpit with more to write than this test stubs; only the skills link matters
        pass
    assert names(tmp_path / "home" / ".agents" / "skills") == skill_scope.include_list(IDS, env={})


class FakeOC:
    def __init__(self, doc=None):
        self.doc = doc or {}
        self.patches: list = []

    def apply(self, patch, dry_run=False, replace_paths=None):
        if not dry_run:
            self.patches.append((json.loads(json.dumps(patch)), replace_paths))
            for k, v in patch["skills"]["load"].items():
                self.doc.setdefault("skills", {}).setdefault("load", {})[k] = v
        return True, "ok"


def run_skills(s, fake, kit, tmp_path, monkeypatch, *, dry_run=False):
    monkeypatch.setattr(oc, "_apply_hot", lambda patch, dry_run=False, replace_paths=None:
                        fake.apply(patch, dry_run=dry_run, replace_paths=replace_paths))
    monkeypatch.setattr(oc.openclaw_host, "change_records", lambda *a, **k: [])
    written: list = []
    changed = oc._configure_skills(s, fake.doc, tmp_path / "openclaw.json", written, str(kit), dry_run=dry_run,
                                   run_changes=[])
    return changed, written


def test_openclaw_gets_extra_dirs_and_symlink_targets_and_a_rerun_is_a_zero_diff(kit, tmp_path, monkeypatch):
    s, fake = state.SetupState(), FakeOC()
    changed, _ = run_skills(s, fake, kit, tmp_path, monkeypatch)
    assert changed is True
    load = fake.doc["skills"]["load"]
    scoped = str(_shared.scoped_skills_dir())
    assert load["extraDirs"] == [scoped] and load["allowSymlinkTargets"] == [os.path.realpath(kit / "skills")]
    assert names(_shared.scoped_skills_dir()) == skill_scope.include_list(IDS, env={})
    assert s.openclaw.skills_extra_dirs_added == [scoped]
    n = len(fake.patches)
    changed2, _ = run_skills(s, fake, kit, tmp_path, monkeypatch)
    assert changed2 is False and len(fake.patches) == n


def test_operator_entries_are_kept_and_not_recorded_as_ours(kit, tmp_path, monkeypatch):
    scoped = str(_shared.scoped_skills_dir())
    fake = FakeOC({"skills": {"load": {"extraDirs": ["/mine", scoped], "allowSymlinkTargets": ["/theirs"]}}})
    s = state.SetupState()
    run_skills(s, fake, kit, tmp_path, monkeypatch)
    load = fake.doc["skills"]["load"]
    assert load["extraDirs"] == ["/mine", scoped] and "/theirs" in load["allowSymlinkTargets"]
    assert s.openclaw.skills_extra_dirs_added == []                # the operator had it first
    assert s.openclaw.skills_symlink_targets_added == [os.path.realpath(kit / "skills")]


def test_a_moved_kit_root_replaces_only_the_entry_the_kit_added(kit, tmp_path, monkeypatch):
    s, fake = state.SetupState(), FakeOC({"skills": {"load": {"allowSymlinkTargets": ["/operator", "/old/cellar/skills"]}}})
    s.openclaw.skills_symlink_targets_added = ["/old/cellar/skills"]
    run_skills(s, fake, kit, tmp_path, monkeypatch)
    assert fake.doc["skills"]["load"]["allowSymlinkTargets"] == ["/operator", os.path.realpath(kit / "skills")]
    assert fake.patches[-1][1] == ["skills.load.allowSymlinkTargets"]            # a list replace, said explicitly


def test_a_dry_run_writes_nothing(kit, tmp_path, monkeypatch):
    s, fake = state.SetupState(), FakeOC()
    changed, written = run_skills(s, fake, kit, tmp_path, monkeypatch, dry_run=True)
    assert changed is False and written == [] and fake.patches == []
    assert not _shared.scoped_skills_dir().exists()


def test_teardown_removes_exactly_the_recorded_entries(kit, tmp_path, monkeypatch):
    s, fake = state.SetupState(), FakeOC()
    run_skills(s, fake, kit, tmp_path, monkeypatch)
    fake.doc["skills"]["load"]["extraDirs"].insert(0, "/mine")
    monkeypatch.setattr(oc, "read_config", lambda p: fake.doc)
    monkeypatch.setattr(oc, "config_path", lambda: tmp_path / "openclaw.json")
    assert oc._teardown_skills(s) is True
    assert fake.doc["skills"]["load"]["extraDirs"] == ["/mine"] and fake.doc["skills"]["load"].get("allowSymlinkTargets") is None
    assert s.openclaw.skills_extra_dirs_added == [] and s.openclaw.skills_symlink_targets_added == []


def test_the_skills_keys_are_hot_under_the_reload_table():
    from ai_resources import openclaw_reload_rules as rr
    rules = rr.RuleSet("baseline", "installed", rr.BASELINE_RULES, "")
    patch = {"skills": {"load": {"extraDirs": ["/x"], "allowSymlinkTargets": ["/y"]}}}
    assert rr.restart_paths(patch, ruleset=rules, reload_mode=None) == []


def test_agy_gets_a_managed_role_block_listing_the_fifteen_roles(tmp_path, monkeypatch):
    from ai_resources import repo_root
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", tmp_path / "g" / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "SETTINGS_PATH", tmp_path / "g" / "settings.json")
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", tmp_path / "g" / "GEMINI.md")
    monkeypatch.setattr(_shared, "link_agents_skills", lambda ak: None)
    gemini.configure({"state": state.SetupState()})
    text = (tmp_path / "g" / "GEMINI.md").read_text()
    assert "## Kit roles" in text and text.count("\n- `") >= 15 and "agents/roles/verifier.md" in text
    assert "implementer-symfony" not in text                      # personas are not listed in the lean block
