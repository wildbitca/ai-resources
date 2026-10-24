"""AC3.9 helper (ADR-0007 section 11; plan section 9, N20): the same fixture labelled claude-cli and agy must give
outputs that differ only in the allowlisted asymmetries.

`diff_paths` walks two JSON-like outputs and returns every key path that differs. `unexplained` removes the paths
the nine allowed asymmetries cover. A test passes when nothing is left. A new field that differs by label is not
covered by any entry, so it fails the comparison until the asymmetry is added here on purpose.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# The nine allowed asymmetries of plan section 9 (exhaustive). Each maps to the key paths ("a.b[].c", ids and
# model vendor prefixes normalised first) that may differ because of it. An empty tuple means the asymmetry is
# removed by `normalize` and so can never appear as a difference.
ALLOWED_ASYMMETRIES: dict[str, tuple[str, ...]] = {
    "account_gate": (r"(^|\.)(acks|gates)(\.|$)", r"(^|\.)agy_gates(\.|$)"),
    "pool_and_weight": (r"(^|\.)pool$", r"(^|\.)pools?(\.|$)", r"(^|\.)weights?(\.|$)"),
    "agent_id_kind": (),                       # `kit-lane-cli-*` vs `kit-lane-agy-*`: normalised, see ID_KIND
    "catalog_entry": (r"^agents\.defaults\.models(\.|$)", r"^agents\.entries\.[^.]+\.models(\.|$)",
                      r"^models(\.|$)"),
    "backend_id_and_argv_env": (r"(^|\.)backend$", r"(^|\.)(argv|env)(\.|$)"),
    "process_flags": (r"(^|\.)process_flags(\.|$)", r"(^|\.)permission_mode$"),
    "extra_tools_deny": (r"(^|\.)tools\.deny(\[\])?$", r"(^|\.)(tool|mcp)_deny_added(\.|$)"),
    "prerequisite_hold": (r"(^|\.)held(\.|$)",),
    "agy_home": (r"(^|\.)agy_home(\.|$)",),
}
assert len(ALLOWED_ASYMMETRIES) == 9

ID_KIND = re.compile(r"kit-lane-(?:cli|agy)-")
VENDOR = re.compile(r"\b(?:anthropic|agy-cli-lane|claude-cli)/")      # the backend id as a model-ref prefix


def normalize(value: Any, *, drop: frozenset[str] = frozenset(), replace: Mapping[str, str] | None = None) -> Any:
    """Replace the agent-id kind and the vendor prefix, apply `replace`, drop the `drop` keys (volatile per run)."""
    if isinstance(value, Mapping):
        return {normalize(k, replace=replace): normalize(v, drop=drop, replace=replace)
                for k, v in value.items() if k not in drop}
    if isinstance(value, (list, tuple)):
        return [normalize(v, drop=drop, replace=replace) for v in value]
    if isinstance(value, str):
        for old, new in (replace or {}).items():
            value = value.replace(old, new)
        return VENDOR.sub("BACKEND/", ID_KIND.sub("kit-lane-KIND-", value))
    return value


def diff_paths(a: Any, b: Any, path: str = "") -> set[str]:
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out: set[str] = set()
        for k in set(a) | set(b):
            sub = f"{path}.{k}" if path else str(k)
            if k not in a or k not in b:
                out.add(sub)
            else:
                out |= diff_paths(a[k], b[k], sub)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return {path + "[]"}
        out = set()
        for x, y in zip(a, b):
            out |= diff_paths(x, y, path + "[]")
        return out
    return set() if a == b else {path}


def covered(path: str) -> bool:
    return any(re.search(rx, path) for rxs in ALLOWED_ASYMMETRIES.values() for rx in rxs)


def unexplained(a: Any, b: Any) -> set[str]:
    """The key paths that differ and that no allowed asymmetry covers."""
    return {p for p in diff_paths(a, b) if not covered(p)}
