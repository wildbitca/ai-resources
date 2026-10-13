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
