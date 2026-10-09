"""Provider adapter contract: how one vendor names, orders and discovers its models.

Pure: no I/O, no subprocess, no environment reads. The registry in `__init__` lists the adapters;
adding a provider is one module and one registry line.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

STABLE = "stable"
PREVIEW = "preview"
ALIAS = "alias"
SNAPSHOT = "snapshot"

SOURCE_CATALOG = "openclaw-catalog"
SOURCE_VENDOR = "vendor-listing"
SOURCE_STATIC = "static"


@dataclass(frozen=True)
class ModelRef:
    provider: str
    family: str
    version: tuple[int, ...]
    channel: str
    id: str

    @property
    def slot(self) -> str:
        return f"{self.provider}:{self.family}"


@dataclass(frozen=True)
class Family:
    """One family inside a provider: an id regex with a `ver` group (and optional `kind`)."""
    name: str
    pattern: str
    # Version groups of the regex (joined with "."), for example ("major", "minor").
    groups: tuple[str, ...] = ("major", "minor")
    # The moving alias of this family (`gemini-flash-latest`), if the vendor publishes one.
    alias: str = ""

    def match(self, model_id: str) -> tuple[int, ...] | None:
        m = re.match(self.pattern, model_id)
        if not m:
            return None
        parts: list[int] = []
        for g in self.groups:
            v = m.groupdict().get(g)
            parts.append(int(v) if v else 0)
        return tuple(parts)


@dataclass(frozen=True)
class Adapter:
    id: str
    name: str
    # Catalog provider ids `openclaw models list --provider <x>` accepts (empty: not in the catalog).
    catalog_ids: tuple[str, ...] = ()
    # Prefix of an OpenClaw model ref (`anthropic/<id>`); "" when OpenClaw has no native ref.
    ref_prefix: str = ""
    litellm_prefix: str = ""
    openrouter_ns: str = ""
    # Credential gate: env var names (all required), "none" or "adc".
    credential: tuple[str, ...] = ()
    credential_kind: str = "env"          # env | none | adc
    families: tuple[Family, ...] = ()
    # Discovery sources in order of preference.
    sources: tuple[str, ...] = (SOURCE_STATIC,)
    # Vendor `/models` host (hard-coded, Q3); "" when the vendor listing is not supported.
    vendor_host: str = ""
    vendor_path: str = "/v1/models"
    smoke_kinds: tuple[str, ...] = ()
    # OpenClaw agentRuntime id for a ref of this provider; "" when no runtime is verified (S1).
    runtime: str = ""
    # Kit default id per family (anthropic mirrors model_pins.DEFAULTS).
    defaults: dict = field(default_factory=dict)
    # Never applied automatically: ids matching these are reported only.
    alias_pattern: str = r".*-latest$"
    preview_pattern: str = r".*-preview(?:-.*)?$"
    snapshot_pattern: str = r".*(?:@\d{8}|-\d{8})$"

    # --- naming -------------------------------------------------------------------------------
    def channel_of(self, model_id: str) -> str:
        if re.match(self.alias_pattern, model_id):
            return ALIAS
        if re.match(self.preview_pattern, model_id):
            return PREVIEW
        if re.match(self.snapshot_pattern, model_id):
            return SNAPSHOT
        return STABLE

    def parse(self, model_id: str) -> ModelRef | None:
        """The ref for `model_id`, or None when no family of this provider matches."""
        if not isinstance(model_id, str):
            return None
        mid = model_id.strip()
        channel = self.channel_of(mid)
        probe = re.sub(r"-preview(?:-.*)?$", "", mid) if channel == PREVIEW else mid
        for fam in self.families:
            if channel == ALIAS and fam.alias and mid == fam.alias:
                return ModelRef(self.id, fam.name, (0,), ALIAS, mid)
            ver = fam.match(probe)
            if ver is not None:
                return ModelRef(self.id, fam.name, ver, channel, mid)
        return None

    def from_any_spelling(self, model_id: str) -> ModelRef | None:
        """Parse a dashed id, a dotted id, a `@date` id or a namespaced (`vendor/id`) one."""
        if not isinstance(model_id, str):
            return None
        mid = model_id.strip()
        for prefix in (self.ref_prefix, self.openrouter_ns, self.litellm_prefix):
            if prefix and mid.startswith(prefix + "/"):
                mid = mid.split("/", 1)[1]
                break
        for cand in self._spellings(mid):
            ref = self.parse(cand)
            if ref:
                return ModelRef(ref.provider, ref.family, ref.version, ref.channel, model_id.strip())
        return None

    def _spellings(self, mid: str) -> list[str]:
        out = [mid]
        if "@" in mid:
            out.append(mid.split("@", 1)[0])
        return out

    def newer(self, a: ModelRef, b: ModelRef) -> bool:
        """True when `a` is a strictly newer version than `b` of the same family."""
        return a.family == b.family and a.version > b.version

    def default_id(self, family: str) -> str | None:
        return self.defaults.get(family)
