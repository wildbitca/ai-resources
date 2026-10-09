from __future__ import annotations

from .base import SOURCE_STATIC, Adapter, Family

_FAMS = tuple(
    Family(f"claude-{n}", rf"^claude-{n}-(?P<major>\d+)(?:-(?P<minor>\d{{1,2}}))?$") for n in ("opus", "sonnet", "haiku", "fable")
) + (
    Family("gemini-flash-lite", r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-flash-lite$"),
    Family("gemini-flash", r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-flash$"),
    Family("gemini-pro", r"^gemini-(?P<major>\d+)(?:\.(?P<minor>\d+))?-pro$"),
)

# No catalog entry and no smoke path: Vertex is static and report-only.
ADAPTER = Adapter(
    id="vertex", name="Vertex AI (GCP)",
    catalog_ids=(), ref_prefix="", litellm_prefix="vertex_ai", openrouter_ns="",
    credential=("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS"),
    credential_kind="adc", families=_FAMS, sources=(SOURCE_STATIC,), smoke_kinds=(),
    snapshot_pattern=r".*@\d{8}$",
)
