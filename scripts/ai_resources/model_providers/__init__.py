"""Provider adapter registry (named `model_providers` to avoid a clash with `setup/providers.py`)."""
from __future__ import annotations

from .base import (ALIAS, PREVIEW, SNAPSHOT, SOURCE_CATALOG, SOURCE_STATIC, SOURCE_VENDOR, STABLE,
                   Adapter, Family, ModelRef)
from . import anthropic, deepseek, google, moonshot, ollama, openai, openrouter, vertex

# Order mirrors setup/providers.PROVIDERS.
REGISTRY: dict[str, Adapter] = {
    a.id: a for a in (anthropic.ADAPTER, google.ADAPTER, vertex.ADAPTER, openai.ADAPTER,
                      ollama.ADAPTER, openrouter.ADAPTER, deepseek.ADAPTER, moonshot.ADAPTER)
}


def get(provider: str) -> Adapter:
    return REGISTRY[provider]


def ids() -> list[str]:
    return list(REGISTRY)


def model_families() -> list[str]:
    """Provider ids that are model families (everything except the hosted gateway backend)."""
    return [p for p in REGISTRY if p != "openrouter"]


def parse_slot(slot: str) -> tuple[str, str]:
    provider, _, family = slot.partition(":")
    return provider, family


def identify(model_id: str, provider: str | None = None) -> ModelRef | None:
    """Parse `model_id` with the given provider's adapter, or with the first adapter that knows it."""
    if provider:
        return REGISTRY[provider].from_any_spelling(model_id)
    for a in REGISTRY.values():
        ref = a.from_any_spelling(model_id)
        if ref:
            return ref
    return None
