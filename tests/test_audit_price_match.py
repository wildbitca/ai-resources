"""`audit._price` must not price a newer minor version as its major (claude-opus-5-5 is not claude-opus-5)."""
from __future__ import annotations

from ai_resources import audit


def test_an_exact_row_is_returned():
    assert audit._price("claude-opus-5") == audit.PRICES["claude-opus-5"]


def test_a_minor_version_without_a_row_is_not_priced_as_its_major():
    # claude-opus-5-5 has no PRICES row; the old prefix match returned the claude-opus-5 price.
    assert "claude-opus-5-5" not in audit.PRICES
    assert audit._price("claude-opus-5-5") is None


def test_a_minor_version_of_another_family_is_not_priced_as_its_major():
    assert "claude-sonnet-5-5" not in audit.PRICES
    assert audit._price("claude-sonnet-5-5") is None


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
