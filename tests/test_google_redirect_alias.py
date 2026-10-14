"""gemini-3.7-flash is a known server-side alias of gemini-3.8-flash and is normalised, not unknown (G6)."""
from __future__ import annotations

import pytest

from ai_resources.model_providers import google


@pytest.mark.parametrize("spelling", ["gemini-3.7-flash", "google/gemini-3.7-flash", "gemini/gemini-3.7-flash"])
def test_the_retired_flash_resolves_to_the_current_one(spelling):
    assert google.ADAPTER.redirect_of(spelling) == "gemini-3.8-flash"


def test_a_current_or_unrelated_id_has_no_redirect():
    assert google.ADAPTER.redirect_of("gemini-3.8-flash") is None
    assert google.ADAPTER.redirect_of("gemini-3.5-flash-lite") is None


def test_the_redirect_target_is_a_family_member():
    target = google.ADAPTER.redirect_of("gemini-3.7-flash")
    assert google.ADAPTER.parse(target).family == "gemini-flash"


def test_the_redirect_agrees_with_the_scores_db_alias():
    from ai_resources.scoring import db
    d = db.load() if hasattr(db, "load") else None
    if d is not None:
        assert d.resolve_model("google/gemini-3.7-flash") == "google/gemini-3.8-flash"
