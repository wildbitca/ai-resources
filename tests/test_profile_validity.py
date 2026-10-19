"""A shipped Claude-only profile must be valid in every compat cell it can reach, derived from `compat.plan`.

The matrix (setup/compat.py) is the single source of truth for which cockpit can run which provider's models
and whether that cell is verified. These tests build a Selection from a profile's own refs and ask the matrix,
so a profile cannot drift out of the verified cells without a test going red.
"""
from __future__ import annotations

import pytest

from ai_resources import model_providers
from ai_resources.selection import Selection, SlotSel
from ai_resources.setup import compat, profiles, wizard

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


@pytest.mark.parametrize("cockpit,mode,backend,engine,want", CELLS)
def test_the_wizards_recommended_profile_is_verified_in_every_reachable_cell(cockpit, mode, backend, engine, want):
    """TS-BC55-008: the constant and the validity test cannot diverge (the name is read, never hard-coded)."""
    check_cell(wizard.RECOMMENDED_PROFILE, cockpit, mode, backend, engine, want)


# --- measured-best: the explicit multi-provider profile, labelled for what it is (P5) -------------------------

MULTI_PROVIDER = "measured-best"


@pytest.mark.parametrize("backend", ["litellm", "openrouter"])
def test_measured_best_has_unverified_refs_for_the_claude_cockpit(backend):
    """TS-BC55-005: derived from compat.plan, so the relabel cannot go stale silently."""
    doc = profiles.load_profile(MULTI_PROVIDER)
    act = _action(doc, "claude", "multi-model", backend, allow_unverified=False)
    assert act.action == compat.VIA_GATEWAY and act.unverified is False
    skipped = {note.split(":", 1)[0].split("/", 1)[0] for note in act.notes}
    assert skipped and skipped <= {"google", "deepseek"}, act.notes
    assert all("not verified" in note for note in act.notes)
    served = {model_providers.identify(r).provider for r in act.model_refs}
    assert served == {"anthropic"}
    allowed = _action(doc, "claude", "multi-model", backend, allow_unverified=True)
    assert allowed.unverified is True and allowed.notes == []


def test_measured_best_description_is_not_a_recommendation_and_names_the_unverified_cells():
    """TS-BC55-020."""
    text = profiles.load_profile(MULTI_PROVIDER)["description"].lower()
    assert "recommended" not in text
    assert "unverified" in text
    assert "per-model gate" in text and "--allow-unverified" in text   # FU-1: the gate has landed
    assert "balanced-claude-55" in text


def test_an_ungated_render_still_writes_measured_bests_foreign_ids_verbatim():
    """F1, pinned for the ungated render (`allow_unverified=None`, the default): `_resolve_model` writes the
    google/ and deepseek/ ids verbatim. The per-model gate (FU-1) is applied by `claude.configure` and the
    re-render, which pass `allow_unverified`; see tests/test_claude_per_model_gate.py."""
    import re

    from ai_resources import repo_root
    from ai_resources.setup.cockpits import claude

    executors = profiles.to_executors(profiles.load_profile(MULTI_PROVIDER), "litellm")
    files = claude.render_subagent_files(executors, str(repo_root()), "multi-model")
    model_of = {name: re.search(r"^model: (.*)$", text, re.M).group(1) for name, text in files.items()}
    assert model_of["explore"].startswith("google/")
    assert model_of["software-architect"].startswith("deepseek/")


# --- every shipped profile is structurally valid ---------------------------------------------------------------

import pathlib as _pathlib

_ALL_PROFILES = sorted(p.stem for p in (_pathlib.Path(__file__).resolve().parents[1] / "profiles").glob("*.yaml"))


@pytest.mark.parametrize("name", _ALL_PROFILES)
def test_every_shipped_profile_is_valid(name):
    doc = profiles.load_profile(name)
    assert doc.get("mode", "multi-model") in ("single-model", "multi-model"), name
    assert doc.get("description"), name
    by_role = doc.get("by_role") or {}
    assert by_role and set(by_role) <= set(profiles.KNOWN_ROLES), name
    assert all((cfg or {}).get("model") for cfg in by_role.values()), name


def test_every_single_model_profile_uses_claude_aliases_only():
    for name in profiles.list_profiles(mode="single-model"):
        doc = profiles.load_profile(name, "single-model")
        refs = [c["model"] for c in doc["by_role"].values()]
        assert set(refs) <= {"haiku", "sonnet", "opus", "inherit"}, name
