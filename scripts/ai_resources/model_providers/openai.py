from __future__ import annotations

from .base import SOURCE_CATALOG, SOURCE_STATIC, SOURCE_VENDOR, Adapter, Family

# gpt-6-astra, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna: the trailing word names the family.
_FAMS = tuple(
    Family(f"gpt-{w}", rf"^gpt-(?P<major>\d+)(?:\.(?P<minor>\d+))?-{w}$")
    for w in ("astra", "sol", "terra", "luna")
)

ADAPTER = Adapter(
    id="openai", name="OpenAI",
    catalog_ids=("openai",), ref_prefix="openai",
    litellm_prefix="openai", openrouter_ns="openai",
    credential=("OPENAI_API_KEY",), families=_FAMS,
    sources=(SOURCE_CATALOG, SOURCE_VENDOR, SOURCE_STATIC),
    vendor_host="https://api.openai.com", vendor_path="/v1/models",
    smoke_kinds=("direct", "litellm", "openrouter"),
    runtime="codex",
    defaults={"gpt-astra": "gpt-6-astra"},
)
