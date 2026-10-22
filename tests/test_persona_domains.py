"""ADR-0014 D6 / AC-10: persona subagents are rendered only for active domains."""
from __future__ import annotations

import pytest

from ai_resources import domains
from ai_resources.setup import profiles, state
from ai_resources.setup.cockpits import claude

EXECUTORS = {"by_role": {}, "by_persona": {}, "classes": {}}


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "AGENTS_DIR", tmp_path / "agents")
    monkeypatch.delenv(domains.ENV_VAR, raising=False)
    return tmp_path / "agents"


def _names(env=None, monkeypatch=None):
    return set(claude.render_subagent_files(EXECUTORS, "/kit", "multi-model"))


def test_unset_renders_the_roles_and_no_persona(agents):
    names = _names()
    assert names == set(profiles.KNOWN_ROLES)
    for persona in ("planner-angular", "planner-symfony", "planner-api-platform", "planner-devops", "planner-dart-flutter"):
        assert persona not in names


def test_angular_renders_only_the_angular_personas(agents, monkeypatch):
    monkeypatch.setenv(domains.ENV_VAR, "angular")
    names = _names()
    assert "planner-angular" in names and "tester-angular" in names
    assert not {n for n in names if n.endswith(("-symfony", "-api-platform", "-devops", "-dart-flutter"))}


def test_flutter_renders_only_the_dart_flutter_personas(agents, monkeypatch):
    monkeypatch.setenv(domains.ENV_VAR, "flutter")
    personas = _names() - set(profiles.KNOWN_ROLES)
    assert personas and all(p.endswith("-dart-flutter") for p in personas)
    assert "planner-dart-flutter" in personas


def test_all_renders_every_persona(agents, monkeypatch):
    monkeypatch.setenv(domains.ENV_VAR, "all")
    assert len(_names()) == len(profiles.KNOWN_ROLES) + len(profiles.known_personas()) == 40


def test_the_writer_and_the_render_agree(agents, monkeypatch):
    monkeypatch.setenv(domains.ENV_VAR, "symfony")
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert rendered == {p.stem: p.read_text(encoding="utf-8") for p in agents.glob("*.md")}


def test_an_inactive_persona_installed_earlier_is_removed_and_the_record_updated(agents, monkeypatch):
    tracking = state.InstallTracking()
    monkeypatch.setenv(domains.ENV_VAR, "all")
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model", tracking)
    assert (agents / "planner-angular.md").is_file() and "planner-angular" in tracking.subagent_files_installed
    assert tracking.persona_domains_active == sorted(domains.DOMAINS)

    monkeypatch.setenv(domains.ENV_VAR, "angular")
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model", tracking)
    assert (agents / "planner-angular.md").is_file() and not (agents / "planner-symfony.md").exists()
    assert tracking.persona_domains_active == ["angular"]

    monkeypatch.delenv(domains.ENV_VAR)
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model", tracking)
    assert not (agents / "planner-angular.md").exists()
    assert tracking.persona_domains_active == []
    # enabling the domain brings it back
    monkeypatch.setenv(domains.ENV_VAR, "angular")
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model", tracking)
    assert (agents / "planner-angular.md").is_file()


def test_a_hand_written_agent_with_a_persona_name_is_never_deleted(agents, monkeypatch):
    agents.mkdir(parents=True)
    mine = agents / "planner-angular.md"
    mine.write_text("mine")
    tracking = state.InstallTracking()           # the kit never installed it, so it is not recorded
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model", tracking)
    assert mine.read_text() == "mine"
