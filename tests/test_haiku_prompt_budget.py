"""ADR-0014 D1 / AC-2: a Haiku role's static prompt must stay under the 100k-token Haiku price tier; validate_kit fails
above it. The estimate is 4 characters per token with a recorded safety margin."""
from __future__ import annotations

import pathlib
import shutil
import sys

import pytest

from ai_resources import haiku_budget, skill_scope

REPO = pathlib.Path(__file__).resolve().parents[1]


def _import_validate_kit():
    sys.path.insert(0, str(REPO / "scripts"))
    import validate_kit
    return validate_kit


def test_the_threshold_matches_the_audit_price_tier():
    from ai_resources import audit
    assert haiku_budget.HAIKU_PROMPT_THRESHOLD_TOKENS == audit.LONG_CONTEXT["claude-haiku-5-5"][0] == 100_000
    assert haiku_budget.CHARS_PER_TOKEN == 4 and haiku_budget.SAFETY_MARGIN > 1.0


def test_the_haiku_roles_are_the_balanced_mix():
    assert {"explore", "tester", "doc-writer", "package-upgrade"} <= haiku_budget.haiku_roles(REPO)


def test_every_haiku_role_of_the_kit_is_under_the_threshold_with_room_to_spare():
    for role in sorted(haiku_budget.haiku_roles(REPO)):
        tokens = haiku_budget.estimate_tokens(role, REPO)
        assert tokens < haiku_budget.HAIKU_PROMPT_THRESHOLD_TOKENS * 0.75, (role, tokens)
    assert haiku_budget.over_budget(REPO) == []


def test_the_estimate_adds_the_role_text_the_preloads_and_the_listing(tmp_path):
    root = tmp_path
    (root / "agents" / "roles").mkdir(parents=True)
    (root / "agents" / "personas").mkdir()
    (root / "skills").mkdir()
    (root / "agents" / "roles" / "tester.md").write_text("x" * 4000)
    (root / "agents" / "personas" / "tester-flutter.md").write_text("y" * 400)
    (root / "agents" / "personas" / "tester-angular.md").write_text("y" * 800)
    base = haiku_budget.estimate_tokens("tester", root)
    assert base == haiku_budget.FIXED_PREFIX_TOKENS + int((4000 + 800) / 4 * haiku_budget.SAFETY_MARGIN)
    # a preloaded skill body counts in full
    pre = skill_scope.ROLE_SKILLS["tester"][0]
    (root / "skills" / pre).mkdir()
    (root / "skills" / pre / "SKILL.md").write_text("z" * 8000)
    assert haiku_budget.estimate_tokens("tester", root) > base


def test_an_oversized_haiku_role_is_over_budget(tmp_path):
    (tmp_path / "agents" / "roles").mkdir(parents=True)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "p.yaml").write_text("by_role:\n  tester:  { model: haiku }\n  planner: { model: sonnet }\n")
    (tmp_path / "agents" / "roles" / "tester.md").write_text("w" * 400_000)        # ~100k tokens of role text alone
    (tmp_path / "agents" / "roles" / "planner.md").write_text("w" * 400_000)       # not a Haiku role: ignored
    over = haiku_budget.over_budget(tmp_path)
    assert [r for r, _ in over] == ["tester"] and over[0][1] > 100_000


def test_validate_kit_fails_a_haiku_role_above_the_budget(monkeypatch):
    vk = _import_validate_kit()
    monkeypatch.setattr(vk, "failures", [])
    monkeypatch.setattr(haiku_budget, "over_budget", lambda root: [("tester", 150_000)])
    vk.check_haiku_budget()
    assert len(vk.failures) == 1 and "haiku-budget" in vk.failures[0] and "tester" in vk.failures[0]


def test_validate_kit_is_quiet_for_the_shipped_kit(monkeypatch):
    vk = _import_validate_kit()
    monkeypatch.setattr(vk, "failures", [])
    vk.check_haiku_budget()
    assert vk.failures == []
