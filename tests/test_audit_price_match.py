"""`audit._price` must not price a newer minor version as its major (claude-opus-5-5 is not claude-opus-5)."""
from __future__ import annotations

from ai_resources import audit


def test_an_exact_row_is_returned():
    assert audit._price("claude-opus-5") == audit.PRICES["claude-opus-5"]


def test_a_minor_version_without_a_row_is_not_priced_as_its_major():
    # A newer minor with no PRICES row: the old prefix match returned the major's price.
    assert "claude-opus-5-9" not in audit.PRICES
    assert audit._price("claude-opus-5-9") is None


def test_a_minor_version_of_another_family_is_not_priced_as_its_major():
    assert "claude-sonnet-5-9" not in audit.PRICES
    assert audit._price("claude-sonnet-5-9") is None


def test_the_five_five_models_use_their_own_rows_not_the_major_rate():
    # Verified 2026-10-22 against the official pricing pages (operator-approved source).
    assert audit._price("claude-sonnet-5-5") == (2.0, 10.0, 0.10, 2.50, 4.00)
    assert audit._price("claude-sonnet-5") == (2.0, 10.0, 0.20, 2.50, 4.00)
    assert audit._price("claude-opus-5-5") == (4.0, 20.0, 0.20, 5.00, 8.00)
    assert audit._price("claude-haiku-5-5") == (0.10, 0.50, 0.01, 0.125, 0.20)
    assert audit._price("claude-sonnet-5-5") != audit._price("claude-sonnet-5")


def test_a_dated_snapshot_of_a_five_five_model_resolves_to_its_own_row():
    assert audit._price("claude-sonnet-5-5-20260101") == audit.PRICES["claude-sonnet-5-5"]
    assert audit._price("claude-opus-5-5@20260101") == audit.PRICES["claude-opus-5-5"]


def test_haiku_five_five_has_a_long_prompt_tier():
    assert audit._price("claude-haiku-5-5", ctx=100_000) == (0.10, 0.50, 0.01, 0.125, 0.20)
    assert audit._price("claude-haiku-5-5", ctx=100_001) == (0.50, 2.50, 0.05, 0.625, 1.00)
    # other models do not change with prompt size
    assert audit._price("claude-sonnet-5-5", ctx=500_000) == audit.PRICES["claude-sonnet-5-5"]


def test_date_bounded_gemini_rows_switch_after_their_until_date():
    from datetime import date
    assert audit._price("gemini-4-argon", on=date(2026, 11, 30)) == (2.0, 10.0, 0.10, 0.0, 0.0)
    assert audit._price("gemini-4-argon", on=date(2026, 12, 1))[:2] == (4.0, 20.0)
    assert audit._price("gemini-3.8-flash", on=date(2026, 12, 31))[:2] == (0.75, 3.75)
    assert audit._price("gemini-3.8-flash", on=date(2027, 1, 1))[:2] == (1.5, 7.5)


def test_a_model_without_a_sourced_row_stays_unpriced():
    assert audit._price("claude-mystery-9") is None
    assert audit._estimate("claude-mystery-9", 1000, 1000, 0, 0, 0) == 0.0


def test_fable_five_one_row_matches_the_approved_source():
    assert audit._price("claude-fable-5-1") == (10.0, 50.0, 0.25, 12.50, 20.00)


def test_a_dated_snapshot_still_prices_as_its_base():
    assert audit._price("claude-sonnet-5-20260101") == audit.PRICES["claude-sonnet-5"]
    assert audit._price("claude-haiku-4-5@20251001") == audit.PRICES["claude-haiku-4-5"]


def test_a_context_suffix_does_not_change_the_price():
    assert audit._price("claude-fable-5-1[1m]") == audit.PRICES["claude-fable-5-1"]


def test_the_longest_row_wins_for_a_snapshot_of_a_minor():
    assert audit._price("claude-fable-5-1-20260101") == audit.PRICES["claude-fable-5-1"]


def test_an_adapter_default_equals_the_kit_default():
    from ai_resources import model_pins
    from ai_resources.model_providers import anthropic
    assert anthropic.ADAPTER.defaults == model_pins.DEFAULTS
