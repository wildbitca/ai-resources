"""ADR-0014 D6: one domain map; every persona domain is mapped; AI_RESOURCES_SKILLS decides which are active."""
from __future__ import annotations

import pathlib

import pytest

from ai_resources import domains, skill_scope
from ai_resources.setup import profiles

REPO = pathlib.Path(__file__).resolve().parents[1]
ROLES = profiles.KNOWN_ROLES


def test_every_persona_domain_on_disk_is_mapped():
    found = set()
    for path in (REPO / "agents" / "personas").glob("*.md"):
        domain = domains.persona_domain(path.stem, ROLES)
        assert domain, f"{path.name} does not start with a known role"
        found.add(domain)
    assert found == set(domains.DOMAINS), (sorted(found - set(domains.DOMAINS)), sorted(set(domains.DOMAINS) - found))


def test_the_map_values():
    assert domains.DOMAINS == {"dart-flutter": "flutter", "devops": "devops", "angular": "angular",
                               "symfony": "symfony", "api-platform": "api-platform"}


@pytest.mark.parametrize("persona,want", [("planner-angular", "angular"), ("code-reviewer-api-platform", "api-platform"),
                                         ("software-architect-dart-flutter", "dart-flutter"),
                                         ("implementer-devops", "devops"), ("not-a-persona", "")])
def test_persona_domain_uses_the_longest_role(persona, want):
    assert domains.persona_domain(persona, ROLES) == want


@pytest.mark.parametrize("raw,want", [
    (None, set()), ("", set()), ("lean", set()),
    ("all", set(domains.DOMAINS)), ("ALL", set(domains.DOMAINS)),
    ("angular", {"angular"}), ("flutter", {"dart-flutter"}), ("dart-flutter", {"dart-flutter"}), ("dart", {"dart-flutter"}),
    ("flutter,terraform", {"dart-flutter"}), ("terraform", set()), ("api-platform, symfony ", {"api-platform", "symfony"}),
    ("flutter-", {"dart-flutter"}), ("angular,devops,symfony,api-platform,flutter", set(domains.DOMAINS)),
])
def test_active_domains(raw, want):
    env = {} if raw is None else {domains.ENV_VAR: raw}
    assert domains.active_domains(env) == want


def test_active_domains_reads_the_process_environment(monkeypatch):
    monkeypatch.delenv(domains.ENV_VAR, raising=False)
    assert domains.active_domains() == set()
    monkeypatch.setenv(domains.ENV_VAR, "symfony")
    assert domains.active_domains() == {"symfony"}


def test_an_unmapped_domain_is_rendered_and_a_non_persona_is_not():
    assert domains.persona_is_active("planner-newdomain", ROLES, {}) is True
    assert domains.persona_is_active("zzz-angular", ROLES, {domains.ENV_VAR: "angular"}) is False


def test_skill_scope_and_the_persona_filter_parse_the_same_tokens():
    assert skill_scope.ENV_VAR == domains.ENV_VAR
    ids = ["flutter-bloc", "terraform-x", "workflow-a", "kit-orchestration", "angular-x"]
    assert skill_scope.include_list(ids, env={domains.ENV_VAR: "all"}) is None
    linked = skill_scope.include_list(ids, env={domains.ENV_VAR: "flutter, terraform"})
    assert {"flutter-bloc", "terraform-x", "workflow-a", "kit-orchestration"} <= linked and "angular-x" not in linked
    assert skill_scope.include_list(ids, env={}) == skill_scope.include_list(ids, env={domains.ENV_VAR: "lean"})
