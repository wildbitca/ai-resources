from __future__ import annotations

from .base import SOURCE_STATIC, SOURCE_VENDOR, Adapter, Family

_FAMS = (
    Family("kimi-code", r"^kimi-k(?P<major>\d+)(?:\.(?P<minor>\d+))?-code$"),
)

ADAPTER = Adapter(
    id="moonshot", name="Moonshot AI (Kimi)",
    catalog_ids=(), ref_prefix="",
    litellm_prefix="moonshot", openrouter_ns="moonshotai",
    credential=("MOONSHOT_API_KEY",), families=_FAMS,
    sources=(SOURCE_VENDOR, SOURCE_STATIC),
    vendor_host="https://api.moonshot.ai", vendor_path="/v1/models",
    smoke_kinds=("direct", "litellm", "openrouter"),
    defaults={"kimi-code": "kimi-k2.7-code"},
)
