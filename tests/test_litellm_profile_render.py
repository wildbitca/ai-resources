"""Tests that the rendered litellm.yaml actually reflects a profile's `defaults`.

`_render_litellm_yaml` used to hardcode `num_retries: 3` and `timeout: 600` and
never looked at a profile's `defaults.fallbacks` at all — a profile could ask
for tighter retries or a catch-all fallback and the gateway would silently keep
serving the old literals. These checks read the rendered YAML back and compare
against the profile's own numbers instead of the module's defaults, so they
fail if the renderer goes back to ignoring the profile.

No network, no credentials, no gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import yaml  # noqa: E402

from ai_resources.setup import profiles  # noqa: E402
from ai_resources.setup.litellm import _render_litellm_yaml  # noqa: E402


# ADR-0010 S2: no shipped profile carries a cross-family `defaults.fallbacks` any more (a checker must not be
# able to fall back onto a writer family). The renderer tests below still need a catch-all fallback, so they set
# one on a copy of the profile; they test the renderer, not the shipped content.
CATCH_ALL = ["anthropic/claude-haiku-5.5"]


def _profile_with_catch_all(profile_name: str) -> dict:
    profile = profiles.load_profile(profile_name)
    profile["defaults"] = {**profile["defaults"], "fallbacks": list(CATCH_ALL)}
    return profile


def _rendered(profile_name: str, *, catch_all: bool = False) -> dict:
    profile = _profile_with_catch_all(profile_name) if catch_all else profiles.load_profile(profile_name)
    executors = profiles.to_executors(profile, "litellm")
    text = _render_litellm_yaml(executors, {"anthropic": {"enabled": True}},
                                "LITELLM_MASTER_KEY")
    return yaml.safe_load(text)


def test_router_retries_and_timeout_come_from_the_profile():
    """measured-best declares max_retries: 2, timeout_seconds: 600 — distinct
    enough from the module's own literal (3) that a renderer still hardcoding
    it fails this instead of passing by coincidence."""
    doc = _rendered("measured-best")
    profile_defaults = profiles.load_profile("measured-best")["defaults"]
    assert doc["router_settings"]["num_retries"] == profile_defaults["max_retries"]
    assert doc["router_settings"]["timeout"] == profile_defaults["timeout_seconds"]


def test_a_profile_with_no_defaults_block_keeps_the_literal_fallback():
    """A hand-written profile that omits `defaults` entirely must not crash the
    renderer, and must still get sane numbers rather than `None`/KeyError."""
    executors = {
        "by_role": {"implementer": {"model": "anthropic/claude-sonnet-5"}},
        "classes": {}, "by_persona": {},
    }
    doc = yaml.safe_load(_render_litellm_yaml(executors, {"anthropic": {"enabled": True}},
                                              "LITELLM_MASTER_KEY"))
    assert doc["router_settings"]["num_retries"] == 3
    assert doc["router_settings"]["timeout"] == 600
    assert doc["router_settings"]["default_fallbacks"] == []


def test_default_fallbacks_are_applied_router_wide():
    """measured-best sets defaults.fallbacks to a single model with no
    role-specific fallback anywhere in the profile. The router key that covers
    every model with no per-model entry in `fallbacks` is `default_fallbacks`,
    so that is what must carry it — not a duplicated entry per model."""
    doc = _rendered("measured-best", catch_all=True)
    assert doc["router_settings"]["default_fallbacks"] == CATCH_ALL
    assert _rendered("measured-best")["router_settings"]["default_fallbacks"] == []


def test_a_default_fallback_model_gets_its_own_deployment():
    """A model named only in defaults.fallbacks still needs a model_list entry:
    the router resolves default_fallbacks by name against model_list, so an
    unregistered name turns the fallback into a second failure."""
    doc = _rendered("measured-best", catch_all=True)
    fallback_model = CATCH_ALL[0]
    names = {m["model_name"] for m in doc["model_list"]}
    assert fallback_model in names


def test_default_fallback_does_not_duplicate_an_already_registered_model():
    """anthropic/claude-haiku-5.5 is both the package-upgrade model and the
    catch-all fallback set on a copy of measured-best — it must appear in model_list once."""
    doc = _rendered("measured-best", catch_all=True)
    fallback_model = CATCH_ALL[0]
    names = [m["model_name"] for m in doc["model_list"]]
    assert names.count(fallback_model) == 1


def test_balanced_claude_55_opus_has_no_fallbacks_and_no_default_fallbacks():
    """ADR-0010 S2 / AC-S2b: the Opus checkers cannot fall back onto a writer family."""
    doc = _rendered("balanced-claude-55")
    assert doc["router_settings"]["default_fallbacks"] == []
    keyed = {k for entry in doc["router_settings"].get("fallbacks", []) for k in entry}
    assert "anthropic/claude-opus-5.5" not in keyed
