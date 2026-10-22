"""Pin the kit defaults back to the generation the recorded model fixtures were written against.

The catalogue fixtures offer the 5.5 ids as the newer generation, and the update tests exercise a bump from the
previous generation to them. The kit defaults themselves moved to 5.5, so those tests pin the kit back to the
previous one: the situation of a host that has not moved yet. Nothing about the update machinery changes.
"""
from __future__ import annotations

PREVIOUS_GENERATION = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}


def pin_previous_generation(monkeypatch) -> None:
    from ai_resources import model_pins
    from ai_resources.model_providers import anthropic
    for cls, model_id in PREVIOUS_GENERATION.items():
        monkeypatch.setitem(model_pins.DEFAULTS, cls, model_id)
        monkeypatch.setitem(anthropic.ADAPTER.defaults, cls, model_id)


def prices_without_five_five(audit_module=None) -> dict:
    """The audit price table minus the 5.5 rows: the table of a kit that had not priced 5.5 yet.

    The update/policy tests were written when `claude-sonnet-5-5` and `claude-opus-5-5` had no price row, which is
    what made a bump to them "price unknown" (needs approval) unless the test priced one itself. The table now carries
    the sourced 5.5 prices (AC-3), so those tests start from the table without them and keep their meaning.
    """
    from ai_resources import audit
    audit_module = audit_module or audit
    return {k: v for k, v in audit_module.PRICES.items() if k not in ("claude-sonnet-5-5", "claude-opus-5-5")}
