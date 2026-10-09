from __future__ import annotations

from .base import SOURCE_CATALOG, SOURCE_STATIC, Adapter

# Local models are named `name:tag`; there is no ordering across them, so no family is declared and
# every id is reported as a new family, never applied.
ADAPTER = Adapter(
    id="ollama", name="Local Ollama",
    catalog_ids=("ollama",), ref_prefix="ollama", litellm_prefix="ollama", openrouter_ns="",
    credential=(), credential_kind="none", families=(),
    sources=(SOURCE_CATALOG, SOURCE_STATIC), smoke_kinds=("direct",),
)
