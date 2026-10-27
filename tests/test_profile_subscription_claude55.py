"""Phase 1 of the subscription-lanes plan: the curated single-model `subscription-claude-55` profile.

The profile is a reference table (Claude aliases, no gateway). These tests pin its shape, its checker/writer
family independence and the untouched `balanced-claude-55` profile.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

from ai_resources import generate, repo_root
from ai_resources.setup import profiles
from ai_resources.setup.cockpits import claude

NAME = "subscription-claude-55"
REPO = pathlib.Path(__file__).resolve().parents[1]
# ADR-0010 S2: balanced-claude-55 moved its verifier to Opus and dropped the cross-family default fallbacks.
BALANCED_SHA256 = "af44fbac48c74280516b8080e9427720ece1ef1e439321ce12e40b95769ce77e"

ALIASES = {"haiku", "sonnet", "opus"}
CHECKERS = ("verifier", "security-auditor")
FIXERS = ("crashlytics-fixer", "sentry-fixer", "terraform-maintainer", "crossplane-upjet-maintainer")

# ADR-0014 D1 (operator decision 2026-10-22): `balanced` is retuned. Haiku for explore, tester, doc-writer and
# package-upgrade; Sonnet for the planner, the architect and the writers; Opus for the two checkers only; no Opus fallback.
EXPECTED = {
    "explore": ("haiku", ["sonnet"]),
    "tester": ("haiku", ["sonnet"]),
    "doc-writer": ("haiku", []),
    "package-upgrade": ("haiku", []),
    "generalPurpose": ("sonnet", []),
    "implementer": ("sonnet", []),
    "crashlytics-fixer": ("sonnet", []),
    "sentry-fixer": ("sonnet", []),
    "terraform-maintainer": ("sonnet", []),
    "crossplane-upjet-maintainer": ("sonnet", []),
    "code-reviewer": ("sonnet", []),
    "security-auditor": ("opus", []),
    "verifier": ("opus", []),
    "planner": ("sonnet", []),
    "software-architect": ("sonnet", []),
}


def _family(ref: str) -> str:
    m = re.search(r"(haiku|sonnet|opus)", ref)
    assert m, f"unknown family for {ref!r}"
    return m.group(1)


def _doc() -> dict:
    # The preset these tests pin is Balanced (ADR-0010 S6); the old name is a legacy file of its own now.
    return profiles.load_profile("balanced", "single-model")


def test_declares_single_model_and_is_listed_only_there():
    assert _doc()["mode"] == "single-model"
    # The old name is a legacy profile (not an alias) that keeps its pre-S6 file; it is single-model only.
    assert NAME in profiles.list_profiles(mode="single-model")
    assert NAME not in profiles.list_profiles(mode="multi-model")
    assert "balanced" in profiles.list_profiles(mode="single-model")
    assert "balanced" not in profiles.list_profiles(mode="multi-model")


def test_every_model_is_a_claude_alias():
    doc = _doc()
    refs = [c["model"] for c in doc["by_role"].values()]
    refs += [f for c in doc["by_role"].values() for f in c.get("fallbacks") or []]
    refs += list((doc.get("classes") or {}).values()) + list((doc.get("defaults") or {}).get("fallbacks") or [])
    assert refs and set(refs) <= ALIASES


def test_by_role_equals_the_approved_table():
    by_role = _doc()["by_role"]
    assert set(by_role) == set(EXPECTED)
    for role, (model, fallbacks) in EXPECTED.items():
        assert by_role[role]["model"] == model, role
        assert list(by_role[role].get("fallbacks") or []) == fallbacks, role


def test_every_known_role_is_present_and_defaults_have_no_fallbacks():
    doc = _doc()
    assert [r for r in profiles.KNOWN_ROLES if not doc["by_role"].get(r, {}).get("model")] == []
    assert not (doc.get("defaults") or {}).get("fallbacks")
    assert doc.get("by_persona") == {}


def test_no_effort_backend_agent_or_lanes_keys():
    doc = _doc()
    # ADR-0019: the role -> backend table is the routing matrix, not a profile `lanes:` block.
    assert not ({"backend", "agentId", "effort", "lanes"} & set(doc))
    for cfg in doc["by_role"].values():
        assert not ({"backend", "lanes", "agentId", "effort"} & set(cfg))
    assert "kit-lane-" not in str(doc)


def test_single_model_render_path_builds_every_role():
    executors = profiles.to_executors(_doc())
    files = claude.render_subagent_files(executors, str(repo_root()), "single-model")
    for role in profiles.KNOWN_ROLES:
        assert role in files, role
        m = re.search(r"^model: (.*)$", files[role], re.M)
        # ADR-0019 D2: an installed Claude agent runs the routing matrix's model, not the legacy by_role pin
        assert m and m.group(1).strip() == generate.role_claude_model(role), role


def test_checker_families_do_not_intersect_writer_families_and_verifier_differs_from_implementer():
    by_role = _doc()["by_role"]
    non_writers = set(CHECKERS) | {"explore", "planner", "software-architect", "code-reviewer"}
    writers = [r for r in by_role if r not in non_writers]
    writer_families = {_family(by_role[r]["model"]) for r in writers}
    checker_families = {_family(by_role[r]["model"]) for r in CHECKERS}
    assert not (checker_families & writer_families)
    assert _family(by_role["verifier"]["model"]) != _family(by_role["implementer"]["model"])


def test_security_auditor_caveat_states_cybench_numbers():
    text = _doc()["description"]
    for needle in ("74.6", "55.4", "59.6", "CVP"):
        assert needle in text, needle
    assert "alias" in text.lower() and "drift" in text.lower()


def test_balanced_claude_55_is_unchanged():
    digest = hashlib.sha256((REPO / "profiles" / "balanced-claude-55.yaml").read_bytes()).hexdigest()
    assert digest == BALANCED_SHA256


def test_recommended_profile_stays_balanced():
    from ai_resources.setup import wizard
    assert wizard.RECOMMENDED_PROFILE == "balanced-claude-55"


# --- ADR-0007 phase 3: the Antigravity candidates ----------------------------------------------------------

def test_the_profile_models_do_not_change_and_the_matrix_agy_slugs_are_lane_slugs():
    from ai_resources import model_pins
    from ai_resources.scoring import routing
    assert {r: c["model"] for r, c in _doc()["by_role"].items()} == {r: m for r, (m, _) in EXPECTED.items()}
    slugs = {c.split(":", 1)[1] for r in routing.merged(_doc()).values() for c in r.chain if c.startswith("agy:")}
    assert slugs and slugs <= set(model_pins.AGY_LANE_MODELS)


def test_role_effort_is_a_positive_map_of_allowed_levels():
    """ADR-0015 S3: the key stays `role_effort`; the cockpit adapters translate it (`setup/effort.py`)."""
    doc = _doc()
    assert doc["role_effort"]["planner"] in ("low", "medium", "high")
    for level in doc["role_effort"].values():
        assert level in ("low", "medium", "high")
