"""Vertex ids that were never verified are labelled as such in the setup picker (G2)."""
from __future__ import annotations

from ai_resources.setup import providers


def test_unverified_vertex_ids_are_labelled():
    assert providers.model_label("vertex", "claude-opus-5-5") == "claude-opus-5-5 (unverified)"
    assert providers.model_label("vertex", "claude-sonnet-5-5") == "claude-sonnet-5-5 (unverified)"


def test_verified_ids_keep_their_plain_label():
    assert providers.model_label("vertex", "claude-fable-5-1") == "claude-fable-5-1"
    assert providers.model_label("anthropic", "claude-opus-5-5") == "claude-opus-5-5"


def test_every_unverified_id_is_a_known_model():
    for provider, ids in providers.UNVERIFIED_MODELS.items():
        assert ids <= set(providers.KNOWN_MODELS[provider])
