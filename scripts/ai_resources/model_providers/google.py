from __future__ import annotations

from .base import SOURCE_CATALOG, SOURCE_STATIC, Adapter, Family

# Order matters: the more specific `flash-lite` family is tried before `flash`.
_FAMS = (
    Family("gemini-flash-lite", r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-flash-lite$",
           alias="gemini-flash-lite-latest"),
    Family("gemini-flash", r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-flash$", alias="gemini-flash-latest"),
    Family("gemini-pro", r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-pro$", alias="gemini-pro-latest"),
)

# gemma-* ids carry a size and an instruction suffix in the version position, so ordering them as a
# family would be a guess; they parse to None and are reported as a new family, never applied.
ADAPTER = Adapter(
    id="google", name="Google AI Studio",
    catalog_ids=("google",), ref_prefix="google",
    litellm_prefix="gemini", openrouter_ns="google",
    credential=("GEMINI_API_KEY",), families=_FAMS,
    sources=(SOURCE_CATALOG, SOURCE_STATIC),
    smoke_kinds=("direct", "litellm", "openrouter"),
    runtime="",                      # no verified native OpenClaw runtime (S1 item 8)
    defaults={"gemini-flash": "gemini-3.8-flash", "gemini-flash-lite": "gemini-3.5-flash-lite",
              "gemini-pro": "gemini-3.1-pro-preview"},
)
