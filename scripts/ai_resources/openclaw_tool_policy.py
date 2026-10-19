"""Pure logic for the one tool the kit may deny in OpenClaw's global `tools.deny` (ADR-0011). No I/O.

`config patch` replaces arrays, so every write sends the whole live list: the operator's entries byte for byte, in
their order, duplicates kept, with the kit's single token appended last. The kit adds and removes only that token.
Everything here takes plain lists and returns plain lists; the writer in `setup/cockpits/openclaw.py` does the reading
and the patching, and `openclaw_host.assert_tools_deny_safe` checks the patch shape on the single writer.

Covering entries (an entry that already denies the tool) are detected so the kit writes nothing. Grammar pinned from
OpenClaw's tool-policy docs ("deny wins, case-insensitive, supports `*` wildcards"; groups `group:media` and
`group:openclaw` both contain `image_generate`) and confirmed in the installed dist (tool-policy-shared, glob-pattern):
OpenClaw trims AND lowercases each entry, `*` is the only metacharacter, it matches any run of characters, and the
whole entry must match. A spelling outside that grammar is NOT treated as covering, so the kit appends its token (a
redundant entry is safe; skipping the write would leave the tool on).
"""
from __future__ import annotations

import re
from typing import Any, Sequence

IMAGE_TOOL = "image_generate"
REPLACE_PATH = "tools.deny"
GROUPS_CONTAINING = {IMAGE_TOOL: ("group:media", "group:openclaw")}


class _NoChange:
    """The sentinel for "nothing to write"; falsy-safe comparisons use `is NO_CHANGE`."""
    def __repr__(self) -> str:
        return "NO_CHANGE"


NO_CHANGE = _NoChange()


def check_shape(value: Any) -> list[str] | None:
    """The live `tools.deny` as the kit may handle it: None (absent) or a list of strings. Anything else is a
    ValueError, and the caller writes nothing."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("tools.deny is not a list of strings")
    return list(value)


# JavaScript `String.prototype.trim` strips exactly these; Python's `str.strip()` also strips \x1c-\x1f and \x85, which
# OpenClaw does not, so an entry padded with those is NOT the tool name to OpenClaw and must not count as covering.
_JS_WHITESPACE = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a" \
                 "\u2028\u2029\u202f\u205f\u3000\ufeff"


def normalize_entry(entry: str) -> str:
    """OpenClaw's normalisation of a deny entry and of a tool name, nothing looser: trim (JS whitespace) then lowercase
    (`normalizeToolPolicyName` over `normalizeOptionalString`, tool-policy-shared and string-coerce in the 2026.10.5 dist)."""
    return entry.strip(_JS_WHITESPACE).lower()


def _wildcard_matches(entry: str, tool: str) -> bool:
    """`entry` is already normalised. OpenClaw compiles an entry containing `*` to `^escaped.*escaped$` (glob-pattern in
    the dist): `*` is the only metacharacter and the match is anchored."""
    if "*" not in entry or entry.startswith("group:"):
        return False
    pattern = "".join(".*" if ch == "*" else re.escape(ch) for ch in entry)
    return re.fullmatch(pattern, normalize_entry(tool)) is not None


def covering_token(live: Sequence[Any] | None, tool: str = IMAGE_TOOL) -> str | None:
    """The first live entry that already denies `tool`, or None. The entry is normalised the way OpenClaw does (trim and
    lowercase, `normalize_entry`), then: exact match, a group that contains the tool, `*`, or an `*` wildcard pattern
    (the pinned grammar above). Nothing looser: a look-alike spelling is not covering."""
    groups = {g.lower() for g in GROUPS_CONTAINING.get(tool, ())}
    for entry in live or ():
        if not isinstance(entry, str):
            continue
        low = normalize_entry(entry)
        if low == normalize_entry(tool) or low in groups or low == "*" or _wildcard_matches(low, tool):
            return entry
    return None


def deny_add(live: Sequence[str] | None, tool: str = IMAGE_TOOL) -> list[str] | _NoChange:
    """The list to send to deny `tool`: the live list with `tool` appended, or NO_CHANGE when an entry covers it."""
    current = check_shape(live)
    if covering_token(current, tool) is not None:
        return NO_CHANGE
    return [*(current or []), tool]


def deny_remove(live: Sequence[str] | None, tool: str = IMAGE_TOOL, *,
                key_absent_before: bool = False) -> list[str] | None | _NoChange:
    """What to send to take the kit's own `tool` entry back out. Exactly one occurrence of the exact token is removed
    (the last: the kit appends). The result is the remaining list (possibly `[]`), None (send null: the key did not
    exist before the kit's first write and nothing else is left) or NO_CHANGE (the token is no longer there)."""
    current = check_shape(live)
    if not current or tool not in current:
        return NO_CHANGE
    idx = len(current) - 1 - current[::-1].index(tool)
    rest = current[:idx] + current[idx + 1:]
    if not rest and key_absent_before:
        return None
    return rest


def assert_single_token_delta(before: Sequence[str] | None, after: Sequence[str] | None, tool: str = IMAGE_TOOL) -> None:
    """Raise ValueError unless `after` is `before` plus `tool` at the end, or `before` minus one exact occurrence of
    `tool` (None counts as the empty list on either side), and nothing else."""
    old = list(before or [])
    new = list(after or [])
    if new == old + [tool]:
        return
    if tool in old:
        for i, item in enumerate(old):
            if item == tool and old[:i] + old[i + 1:] == new:
                return
    raise ValueError(f"tools.deny may change only by one {tool!r} token; refusing")
