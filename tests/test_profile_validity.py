"""A shipped Claude-only profile must be valid in every compat cell it can reach, derived from `compat.plan`.

The matrix (setup/compat.py) is the single source of truth for which cockpit can run which provider's models
and whether that cell is verified. These tests build a Selection from a profile's own refs and ask the matrix,
so a profile cannot drift out of the verified cells without a test going red.
"""
from __future__ import annotations

import pytest

from ai_resources import model_providers
from ai_resources.selection import Selection, SlotSel
from ai_resources.setup import compat, profiles

PROFILE = "balanced-claude-55"


def profile_refs(doc: dict) -> list[str]:
    """Every model ref a profile names: classes, defaults, per role and per persona (model and fallbacks)."""
    refs: list[str] = list((doc.get("classes") or {}).values())
    refs += list((doc.get("defaults") or {}).get("fallbacks") or [])
    for block in ("by_role", "by_persona"):
        for cfg in (doc.get(block) or {}).values():
            if cfg.get("model"):
                refs.append(cfg["model"])
            refs += list(cfg.get("fallbacks") or [])
    return refs


def selection_from_profile(doc: dict) -> Selection:
    """A Selection whose slots are the distinct refs of the profile, provider from the registry (never a split)."""
    slots: dict[str, SlotSel] = {}
    for ref in dict.fromkeys(profile_refs(doc)):
        parsed = model_providers.identify(ref)
        assert parsed is not None, f"{ref!r} is not a model id of any known provider"
        slots[f"{parsed.provider}:{parsed.family}#{len(slots)}"] = SlotSel(ref=ref)
    return Selection(shape="multi-provider", slots=slots)


def _action(doc: dict, cockpit: str, mode: str, backend: str | None, engine: str | None = None,
            allow_unverified: bool = False):
    plan = compat.plan(selection_from_profile(doc), mode, backend, [cockpit], engine=engine,
                       allow_unverified=allow_unverified)
    assert [a.cockpit for a in plan] == [cockpit]
    return plan[0]


CELLS = [
    ("claude", "single-model", None, None, compat.CONFIGURE),
    ("claude", "multi-model", "litellm", None, compat.VIA_GATEWAY),
    ("claude", "multi-model", "openrouter", None, compat.VIA_GATEWAY),
    ("aider", "multi-model", "litellm", None, compat.VIA_GATEWAY),
    ("aider", "multi-model", "openrouter", None, compat.VIA_GATEWAY),
    ("openclaw", "multi-model", "litellm", "claude-code", compat.CONFIGURE),
]


def check_cell(name: str, cockpit: str, mode: str, backend: str | None, engine: str | None, want: str) -> None:
    """The profile named `name` is served, verified and without a note in one matrix cell."""
    doc = profiles.load_profile(name)
    for allow in (False, True):
        act = _action(doc, cockpit, mode, backend, engine, allow_unverified=allow)
        where = f"{name}: {cockpit} {mode}/{backend} engine={engine} allow_unverified={allow}"
        assert act.action == want, where
        assert act.unverified is False, where
        assert act.notes == [] and act.reason == "", where
        assert len(act.model_refs) == len(set(profile_refs(doc))), where


@pytest.mark.parametrize("cockpit,mode,backend,engine,want", CELLS)
def test_balanced_profile_is_verified_in_every_reachable_cell(cockpit, mode, backend, engine, want):
    """TS-BC55-001..003: no unverified ref, no skipped ref, no note, with and without --allow-unverified."""
    check_cell(PROFILE, cockpit, mode, backend, engine, want)


def test_every_ref_of_the_balanced_profile_is_anthropic():
    """TS-BC55-004."""
    doc = profiles.load_profile(PROFILE)
    providers = {model_providers.identify(r).provider for r in profile_refs(doc)}
    assert providers == {"anthropic"}


def test_balanced_profile_is_gateway_agnostic_and_offered_under_both_backends():
    """TS-BC55-006."""
    doc = profiles.load_profile(PROFILE)
    assert doc["mode"] == "multi-model" and "backend" not in doc
    for backend in ("litellm", "openrouter"):
        assert PROFILE in profiles.list_profiles(mode="multi-model", backend=backend)


def test_balanced_profile_assigns_every_known_role():
    """TS-BC55-007."""
    by_role = profiles.load_profile(PROFILE)["by_role"]
    assert [r for r in profiles.KNOWN_ROLES if not (by_role.get(r) or {}).get("model")] == []
    assert set(by_role) <= set(profiles.KNOWN_ROLES)


def test_balanced_profile_keeps_verification_review_and_security_off_haiku():
    """Rule 017: strong models where defects are caught."""
    by_role = profiles.load_profile(PROFILE)["by_role"]
    for role in ("verifier", "code-reviewer", "security-auditor", "planner", "software-architect", "implementer"):
        assert "haiku" not in by_role[role]["model"], role


def test_the_matrix_does_flag_a_profile_with_foreign_refs():
    """The check has teeth: a Google ref is unverified in the claude multi-model cell and is reported."""
    mixed = {"by_role": {"explore": {"model": "google/gemini-3.7-flash"},
                         "planner": {"model": "anthropic/claude-opus-5.5"}}}
    act = _action(mixed, "claude", "multi-model", "litellm")
    assert act.action == compat.VIA_GATEWAY
    assert act.model_refs == ["anthropic/claude-opus-5.5"]
    assert len(act.notes) == 1 and "gemini-3.7-flash" in act.notes[0] and "not verified" in act.notes[0]
    only_google = {"by_role": {"explore": {"model": "google/gemini-3.7-flash"}}}
    assert _action(only_google, "claude", "multi-model", "litellm").action == compat.SKIP
