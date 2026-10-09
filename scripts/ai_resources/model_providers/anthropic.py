from __future__ import annotations

from .base import SOURCE_CATALOG, SOURCE_STATIC, Adapter, Family

_FAMS = tuple(
    Family(name, rf"^claude-{name}-(?P<major>\d+)(?:-(?P<minor>\d{{1,2}}))?$")
    for name in ("opus", "sonnet", "haiku", "fable")
)


class _Anthropic(Adapter):
    def _spellings(self, mid: str) -> list[str]:
        # dotted minor (claude-haiku-4.5) and `@date` (claude-haiku-4-5@20251001)
        out = [mid]
        if "@" in mid:
            out.append(mid.split("@", 1)[0])
        out.extend(c.replace(".", "-") for c in list(out) if "." in c)
        return out


ADAPTER = _Anthropic(
    id="anthropic", name="Anthropic",
    catalog_ids=("claude-cli", "anthropic"), ref_prefix="anthropic",
    litellm_prefix="anthropic", openrouter_ns="anthropic",
    credential=("ANTHROPIC_API_KEY",), families=_FAMS,
    sources=(SOURCE_CATALOG, SOURCE_STATIC),
    smoke_kinds=("claude-cli", "litellm", "openrouter", "direct"),
    runtime="claude-cli",
    defaults={"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5",
              "fable": "claude-fable-5-1"},
    alias_pattern=r"^claude-(?:opus|sonnet|haiku|fable)-latest$",
    preview_pattern=r"^$",
    snapshot_pattern=r"^claude-.*[-@]\d{8}$",
)
