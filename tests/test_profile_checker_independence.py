"""ADR-0010 S1: checker independence over every shipped profile.

A checker (verifier, security-auditor) must never be able to run a model family that a writer can run. The
families a role can run come from `by_role`, its fallbacks, the profile-wide `defaults.fallbacks` (the LiteLLM
per-model fallback union plus `default_fallbacks`) and the lane candidates of a `lanes:` block.

`KNOWN_VIOLATIONS` is measured at baseline and is shrink-only: the test fails when a listed profile now
passes, and when an unlisted profile fails.
"""
from __future__ import annotations

import copy
import pathlib
from typing import Any

import pytest
import yaml

from ai_resources import roles_policy
from ai_resources.scoring import lanes as scoring_lanes

REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILES = sorted(p for p in (REPO / "profiles").rglob("*.yaml"))

# Measured at baseline (plan S1), shrunk by ADR-0010 S2 to the two Q-1 profiles. Shrink-only.
KNOWN_VIOLATIONS: dict[str, str] = {
    "all-gemini": "single vendor: the checkers share Gemini families with the writers (Q-1)",
    "claude-inherit": "every role is `inherit`: the family is unknown (Q-1)",
}

WRITERS = tuple(sorted(
    r for r in roles_policy.LANE_ELIGIBILITY
    if r not in roles_policy.CHECKER_ROLES
    and (roles_policy.SCOPE in roles_policy.contracts_for(r) or roles_policy.INTEGRATING in roles_policy.contracts_for(r))
))


def normalise(ref: str) -> str | None:
    """A lane candidate or model ref as `roles_policy.model_family` reads it (the trap: `agy:<slug>`)."""
    if ref == "claude-code":
        return None
    if ref.startswith("agy:"):
        return "agy-cli-lane/" + ref[len("agy:"):]
    if ref.startswith("claude-cli:"):
        return "claude-cli/" + ref[len("claude-cli:"):]
    return ref


def _lane_refs(doc: dict, role: str) -> list[str]:
    lanes = doc.get("lanes") or {}
    refs: list[str] = []
    for ref in (lanes.get("roles") or {}).get(role) or []:
        n = normalise(str(ref))
        if n:
            refs.append(n)
    return refs


def runnable(doc: dict, role: str) -> list[str]:
    """Every model ref a role can run, in the sense of the LiteLLM fallback union plus the lane candidates."""
    by_role = doc.get("by_role") or {}
    mode = doc.get("mode", "multi-model")
    default_fallbacks = (doc.get("defaults") or {}).get("fallbacks") or []
    cfg = by_role.get(role)
    if not isinstance(cfg, dict) or not cfg.get("model"):
        return []
    out = [str(cfg["model"])]
    if mode == "multi-model":
        union = scoring_lanes.fallback_union(by_role)
        out += [fb for fb, _ in union.get(str(cfg["model"]), ())]
        out += [str(fb) for fb in default_fallbacks]
    out += _lane_refs(doc, role)
    return out


def violations(doc: dict) -> list[str]:
    found: list[str] = []
    by_role = doc.get("by_role") or {}
    checkers = [r for r in roles_policy.CHECKER_ROLES if r in by_role]
    writers = [r for r in WRITERS if r in by_role]
    for checker in sorted(checkers):
        for cm in runnable(doc, checker):
            cf = roles_policy.model_family(cm)
            if cf is None:
                found.append(f"{checker} can run {cm!r} whose family is unknown")
                continue
            for writer in writers:
                for wm in runnable(doc, writer):
                    if roles_policy.model_family(wm) == cf:
                        found.append(f"{checker} ({cm}) shares family {cf} with {writer} ({wm})")
    return found


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _name(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO / "profiles").with_suffix(""))


def _with_roles(path: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in PROFILES if isinstance(_load(p).get("by_role"), dict)]


def test_profiles_are_scanned():
    assert len(_with_roles(PROFILES)) >= 10


@pytest.mark.parametrize("path", _with_roles(PROFILES), ids=_name)
def test_checker_independence(path):
    found = violations(_load(path))
    name = _name(path)
    if name in KNOWN_VIOLATIONS:
        assert found, f"{name} now passes: remove it from KNOWN_VIOLATIONS (shrink-only)"
    else:
        assert not found, f"{name}: {len(found)} overlaps, first: {found[:2]}"


def test_known_violations_name_real_profiles():
    names = {_name(p) for p in PROFILES}
    assert set(KNOWN_VIOLATIONS) <= names


@pytest.mark.parametrize("path", [p for p in _with_roles(PROFILES) if (_load(p).get("lanes") or {})], ids=_name)
def test_lanes_static_resolution_independence(path):
    """The kit's own static resolution (`_check_independence`) agrees on every profile with a lanes block."""
    doc = _load(path)
    by_role = doc["by_role"]
    models = {r: str(c["model"]) for r, c in by_role.items() if isinstance(c, dict) and c.get("model")}
    mode = doc.get("mode", "multi-model")
    default_fallbacks = (doc.get("defaults") or {}).get("fallbacks") or []
    name = _name(path)
    try:
        scoring_lanes._check_independence(by_role, models, mode, default_fallbacks)
    except scoring_lanes.LaneConflict:
        assert name in KNOWN_VIOLATIONS, f"{name} fails static lane independence"


def test_agy_slug_trap():
    assert roles_policy.model_family("agy:gemini-3.8-flash-high") is None
    assert roles_policy.model_family(normalise("agy:gemini-3.8-flash-high")) == "gemini-flash"
    assert roles_policy.model_family(normalise("agy:claude-opus-5-5")) == "opus"


def test_overlap_in_a_copy_of_subscription_claude55_is_found():
    """AC-S1b: verifier = sonnet next to a Sonnet implementer fails."""
    doc = _load(REPO / "profiles" / "balanced.yaml")
    assert violations(doc) == []
    bad = copy.deepcopy(doc)
    bad["by_role"]["verifier"]["model"] = "sonnet"
    assert violations(bad)


def test_default_fallback_union_is_found():
    """AC-S1c: defaults.fallbacks naming the writers' family under an Opus verifier fails."""
    doc: dict[str, Any] = {
        "mode": "multi-model",
        "defaults": {"fallbacks": ["anthropic/claude-sonnet-5.5"]},
        "by_role": {
            "implementer": {"model": "anthropic/claude-sonnet-5.5"},
            "verifier": {"model": "anthropic/claude-opus-5.5"},
        },
    }
    assert violations(doc)
    doc["defaults"]["fallbacks"] = []
    assert violations(doc) == []


def test_per_model_fallback_union_is_found():
    doc = {
        "mode": "multi-model",
        "by_role": {
            "implementer": {"model": "anthropic/claude-sonnet-5.5"},
            "verifier": {"model": "anthropic/claude-opus-5.5", "fallbacks": ["anthropic/claude-sonnet-5.5"]},
        },
    }
    assert violations(doc)


def test_unknown_family_checker_fails_closed():
    doc = {"mode": "single-model", "by_role": {"implementer": {"model": "sonnet"}, "verifier": {"model": "inherit"}}}
    assert violations(doc)


def test_lane_candidate_family_is_counted():
    doc = {
        "mode": "single-model",
        "by_role": {"implementer": {"model": "sonnet"}, "verifier": {"model": "opus"}},
        "lanes": {"roles": {"implementer": ["claude-cli:sonnet", "agy:claude-opus-5-5"]}},
    }
    assert violations(doc)
