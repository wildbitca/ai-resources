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


def _rendered(profile_name: str) -> dict:
    executors = profiles.to_executors(profiles.load_profile(profile_name), "litellm")
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
    doc = _rendered("measured-best")
    profile_defaults = profiles.load_profile("measured-best")["defaults"]
    assert doc["router_settings"]["default_fallbacks"] == profile_defaults["fallbacks"]


def test_a_default_fallback_model_gets_its_own_deployment():
    """A model named only in defaults.fallbacks still needs a model_list entry:
    the router resolves default_fallbacks by name against model_list, so an
    unregistered name turns the fallback into a second failure."""
    doc = _rendered("measured-best")
    fallback_model = profiles.load_profile("measured-best")["defaults"]["fallbacks"][0]
    names = {m["model_name"] for m in doc["model_list"]}
    assert fallback_model in names


def test_default_fallback_does_not_duplicate_an_already_registered_model():
    """anthropic/claude-haiku-4.5 is both the verifier's model and the profile's
    default fallback in measured-best — it must appear in model_list once."""
    doc = _rendered("measured-best")
    fallback_model = profiles.load_profile("measured-best")["defaults"]["fallbacks"][0]
    names = [m["model_name"] for m in doc["model_list"]]
    assert names.count(fallback_model) == 1
