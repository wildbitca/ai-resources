"""`roles_policy`: the lane eligibility table, the contracts and the single model-family normaliser (ADR-0007)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from ai_resources import roles_policy as rp

REPO = Path(__file__).resolve().parents[1]
ROLES = sorted(p.stem for p in (REPO / "agents" / "roles").glob("*.md"))
PERSONAS = sorted(p.stem for p in (REPO / "agents" / "personas").glob("*.md"))


def _load_validate_kit():
    spec = importlib.util.spec_from_file_location("validate_kit_policy_parity", REPO / "scripts" / "validate_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_explicit_step_sets_are_unchanged_and_derived_from_policy():
    vk = _load_validate_kit()
    assert vk.OPENCLAW_READ_ONLY_ROLES == {"planner", "explore", "software-architect", "code-reviewer",
                                           "security-auditor", "verifier"}
    assert vk.OPENCLAW_WARNED_ROLES == {"tester", "doc-writer"}
    assert vk.OPENCLAW_READ_ONLY_ROLES == set(rp.EXPLICIT_READ_ONLY_ROLES)
    assert vk.OPENCLAW_WARNED_ROLES == set(rp.EXPLICIT_WARNED_ROLES)
    assert vk.OPENCLAW_ALLOWED_ROLES == vk.OPENCLAW_READ_ONLY_ROLES | vk.OPENCLAW_WARNED_ROLES


@pytest.mark.parametrize("role", ROLES)
def test_every_known_role_has_an_entry(role):
    assert rp.lane_eligible(role)
    assert set(rp.contracts_for(role)) <= set(rp.CONTRACTS)
    assert rp.REPORT in rp.contracts_for(role)


@pytest.mark.parametrize("persona", PERSONAS)
def test_every_persona_resolves_to_a_role_with_an_entry(persona):
    role = rp.role_of(persona, ROLES)
    assert role is not None and rp.lane_eligible(role)


def test_table_has_no_extra_roles_and_no_backend_column():
    assert set(rp.LANE_ELIGIBILITY) == set(ROLES)
    for role, contracts in rp.LANE_ELIGIBILITY.items():
        assert isinstance(contracts, tuple)
        assert all(c in rp.CONTRACTS for c in contracts), role
    assert not any("backend" in str(v) or "claude-cli" in str(v) or "agy" in str(v)
                   for v in rp.LANE_ELIGIBILITY.values())


@pytest.mark.parametrize("role", ["", "made-up", "Implementer", None, 3])
def test_unknown_role_is_ineligible(role):
    assert not rp.lane_eligible(role)
    assert rp.contracts_for(role) == ()


def test_role_groups_are_consistent():
    assert rp.CHECKER_ROLES == {"verifier", "security-auditor"}
    assert rp.CHECKER_ROLES <= rp.REPORT_ROLES
    assert rp.DEFAULT_INTEGRATING_WRITER == "implementer"
    assert not (rp.REPORT_ROLES & rp.SCOPE_ROLES)
    assert rp.INTEGRATING in rp.contracts_for(rp.DEFAULT_INTEGRATING_WRITER)
    for role in rp.REPORT_ROLES:
        assert rp.contracts_for(role) == (rp.REPORT,)
    for role in rp.SCOPE_ROLES:
        assert rp.SCOPE in rp.contracts_for(role)


def test_tolerance_list_is_exact():
    assert rp.TOLERANCE_LIST == ("IDENTITY.md", "SOUL.md", "USER.md", ".pytest_cache/", "__pycache__/")


def test_forbidden_lists_cover_the_plan_entries():
    for p in (".git", ".gitmodules", ".gitattributes", ".githooks/", ".claude/", ".cursor/", ".openclaw/"):
        assert p in rp.LANE_WRITE_FORBIDDEN
    for p in ("~/.openclaw/openclaw.json", "~/.claude/settings.json", "~/.claude/hooks/", "~/.config/ai-resources/"):
        assert p in rp.TRIPWIRE_PATHS


FAMILY_FORMS = [
    ("sonnet", "sonnet"), ("opus", "opus"), ("haiku", "haiku"),
    ("anthropic/claude-sonnet-5-5", "sonnet"), ("anthropic/claude-sonnet-5.5", "sonnet"),
    ("claude-cli/claude-sonnet-5-5", "sonnet"), ("claude-kit/claude-sonnet-5-5", "sonnet"),
    ("anthropic/claude-opus-5-5", "opus"), ("claude-cli/claude-opus-5-5", "opus"),
    ("claude-kit/claude-opus-5-5", "opus"), ("anthropic/claude-haiku-5-5", "haiku"),
    ("claude-cli/claude-haiku-5-5", "haiku"), ("claude-kit/claude-haiku-5-5", "haiku"),
    ("claude-sonnet-5-5", "sonnet"), ("Claude Sonnet 5.5", "sonnet"), ("agy/claude-sonnet-5-5", "sonnet"),
    ("Claude Opus 5.5 (Thinking)", "opus"), ("agy-cli/claude-opus-5-5", "opus"),
    ("google/gemini-3.8-flash", "gemini-flash"), ("Gemini 3.1 Pro (High)", "gemini-pro"),
    ("gemini-3.1-pro", "gemini-pro"),
    ("inherit", None), ("", None), ("   ", None), ("totally-unknown-slug", None),
    ("openai/gpt-6.1-sol", None), ("mystery/claude-sonnet-5-5", None), ("gemini", None),
    ("claude", None), ("claude-sonnet-opus", None), (None, None), (7, None), ("my-sonnet-tool", None),
]


@pytest.mark.parametrize("ref,family", FAMILY_FORMS)
def test_model_family_table_of_forms(ref, family):
    assert rp.model_family(ref) == family


def test_alias_and_pinned_ref_share_a_family():
    assert rp.model_family("sonnet") == rp.model_family("anthropic/claude-sonnet-5-5")
    assert rp.model_family("sonnet") != rp.model_family("opus")


def test_attribution_patterns_match_trailers_and_generated_lines():
    pats = [re.compile(p, re.I | re.M) for p in rp.ATTRIBUTION_PATTERNS]

    def hit(msg: str) -> bool:
        return any(p.search(msg) for p in pats)

    assert hit("fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
    assert hit("x\n\n\U0001F916 Generated with Claude Code")
    assert hit("Generated with Gemini")
    assert not hit("fix: handle empty input\n\nBody mentions nothing.")


@pytest.mark.parametrize("path", ["tests/", "docs/guide.md", "src/a/b", "a"])
def test_scope_paths_accepted(path):
    assert rp.scope_problems(path) == []


@pytest.mark.parametrize("path", ["", ".", "./", "/etc", "~/x", "../x", "a/../b", "a//b", "a/./b", "src/*.py",
                                  "a\\b", " a", ".git", ".git/hooks/", ".claude/", ".openclaw/x",
                                  ".githooks/", "handoff.md", ".agent-output/", ".agent-output/x/y.md",
                                  ".gitmodules", 5, None])
def test_scope_paths_rejected(path):
    assert rp.scope_problems(path) != []


def test_scope_overlap_is_component_wise():
    assert rp.scopes_overlap("tests/", "tests/unit/")
    assert rp.scopes_overlap("tests/unit", "tests/")
    assert rp.scopes_overlap("a", "a")
    assert not rp.scopes_overlap("tests/", "tests_extra/")
    assert not rp.scopes_overlap("docs/", "tests/")


def test_path_in_scope():
    assert rp.path_in_scope("tests/a.py", ["tests/"])
    assert rp.path_in_scope("README.md", ["README.md"])
    assert not rp.path_in_scope("tests_extra/a.py", ["tests/"])
    assert not rp.path_in_scope("src/a.py", ["tests/", "docs/"])
