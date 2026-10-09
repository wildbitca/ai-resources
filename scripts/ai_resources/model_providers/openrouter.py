from __future__ import annotations

from .base import SOURCE_CATALOG, SOURCE_STATIC, Adapter

# A hosted gateway, not a model family: it is a backend for the other providers' models, so it
# declares no family and is not a compatibility-matrix column.
ADAPTER = Adapter(
    id="openrouter", name="OpenRouter (hosted gateway)",
    catalog_ids=("openrouter",), ref_prefix="openrouter", litellm_prefix="openrouter", openrouter_ns="",
    credential=("OPENROUTER_API_KEY",), families=(),
    sources=(SOURCE_CATALOG, SOURCE_STATIC), smoke_kinds=("openrouter",),
)
