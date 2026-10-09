from __future__ import annotations

from .base import SOURCE_STATIC, SOURCE_VENDOR, Adapter, Family

_FAMS = (
    Family("deepseek-pro", r"^deepseek-v(?P<major>\d+)(?:\.(?P<minor>\d+))?-pro$"),
    Family("deepseek-flash", r"^deepseek-v(?P<major>\d+)(?:\.(?P<minor>\d+))?-flash$"),
)

ADAPTER = Adapter(
    id="deepseek", name="DeepSeek",
    catalog_ids=(), ref_prefix="",
    litellm_prefix="deepseek", openrouter_ns="deepseek",
    credential=("DEEPSEEK_API_KEY",), families=_FAMS,
    sources=(SOURCE_VENDOR, SOURCE_STATIC),
    vendor_host="https://api.deepseek.com", vendor_path="/models",
    smoke_kinds=("direct", "litellm", "openrouter"),
    defaults={"deepseek-pro": "deepseek-v4-pro", "deepseek-flash": "deepseek-v4-flash"},
)
