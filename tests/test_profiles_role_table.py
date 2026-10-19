"""ADR-0010 S10: the role table names the real provider of every role, aliases included."""
from __future__ import annotations

import pytest

from ai_resources.setup import profiles


@pytest.mark.parametrize("model,provider,want", [
    ("anthropic/claude-opus-5.5", "", "anthropic"),
    ("google/gemini-3.7-flash", "", "google"),
    ("deepseek/deepseek-v4-pro", "", "deepseek"),
    ("opus", "", "anthropic"), ("sonnet", "", "anthropic"), ("haiku", "", "anthropic"),
    ("claude-sonnet-5-5", "", "anthropic"),
    ("gemini-3.8-flash-high", "", "google"),
    ("inherit", "", "session"),
    ("somemodel", "legacyvendor", "legacyvendor"),
    ("somemodel", "", "-"),
])
def test_vendor_of(model, provider, want):
    assert profiles.vendor_of(model, provider) == want


def test_the_single_model_profiles_show_anthropic_not_a_dash():
    rows = profiles.role_table(profiles.to_executors(profiles.load_profile("balanced", "single-model")))
    assert {vendor for _role, vendor, _m in rows} == {"anthropic"}
    inherit = profiles.role_table(profiles.to_executors(profiles.load_profile("claude-inherit")))
    assert {vendor for _role, vendor, _m in inherit} == {"session"}


def test_the_gateway_balanced_table_shows_both_providers():
    rows = profiles.role_table(profiles.to_executors(profiles.load_profile("gateway/balanced", "multi-model"),
                                                     "openrouter"))
    assert {vendor for _role, vendor, _m in rows} == {"anthropic", "google"}


def test_a_missing_role_keeps_its_dash():
    rows = profiles.role_table({"by_role": {}})
    assert all(model == "-" and vendor == "-" for _r, vendor, model in rows)
