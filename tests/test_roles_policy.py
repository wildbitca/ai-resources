"""`roles_policy`: the role sets, the single model-family normaliser (ADR-0007) and the scope helpers."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from ai_resources import roles_policy as rp

REPO = Path(__file__).resolve().parents[1]
ROLES = sorted(p.stem for p in (REPO / "agents" / "roles").glob("*.md"))


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
    assert vk.OPENCLAW_READ_ONLY_ROLES == set(rp.REPORT_ROLES)
    assert vk.OPENCLAW_WARNED_ROLES == set(rp.WARNED_ROLES)
    assert vk.OPENCLAW_ALLOWED_ROLES == vk.OPENCLAW_READ_ONLY_ROLES | vk.OPENCLAW_WARNED_ROLES


def test_role_groups_are_consistent():
    assert rp.CHECKER_ROLES == {"verifier", "security-auditor"}
    assert rp.CHECKER_ROLES <= rp.REPORT_ROLES
    assert not (rp.REPORT_ROLES & rp.SCOPE_ROLES)
    assert (rp.REPORT_ROLES | rp.SCOPE_ROLES | {"implementer"}) == set(ROLES)


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
