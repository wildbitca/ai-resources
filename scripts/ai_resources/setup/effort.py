"""Per-role effort, translated per cockpit (ADR-0015 S3, item b).

The profile key is `role_effort` (never `effort`; tests/test_profile_subscription_claude55.py forbids that key). This
module is the only place that turns a profile level into what each cockpit understands:

* Claude Code subagent frontmatter `effort:` (accepted by 2.1.309: low, medium, high, max or an integer),
* OpenClaw `agents.entries.<id>.thinkingDefault` (off, minimal, low, medium, high, xhigh, adaptive, max, ultra).

Pure: no I/O. An unknown role or an unknown level resolves to the cockpit default instead of raising.
"""
from __future__ import annotations

from typing import Any, Mapping

LEVELS = ("low", "medium", "high")                 # what a kit profile may say (validate_kit.py)
DEFAULT = "medium"

# Agents that are not kit roles but still pick a level: OpenClaw chat agents and the notification route.
OPENCLAW_CHAT_LEVEL = "low"
NOTIFICATION_LEVEL = "low"

# Capability flag decided by the S0 spike (ADR-0015 verified facts): Claude Code 2.1.309 lists `effort` among the
# subagent frontmatter keys. Emitted only when this is True.
CLAUDE_EFFORT_FRONTMATTER = True

_OPENCLAW_MAP = {"low": "low", "medium": "medium", "high": "high"}


def role_effort(profile: Mapping[str, Any] | None, role: str) -> str:
    """The profile's level for `role`, or DEFAULT for an unknown role or an unknown level."""
    table = (profile or {}).get("role_effort")
    level = str(table.get(role) or "").lower() if isinstance(table, Mapping) else ""
    return level if level in LEVELS else DEFAULT


def for_cockpit(level: str, cockpit: str) -> str:
    """Translate a profile level for `cockpit` ("claude" -> frontmatter value, "openclaw" -> thinkingDefault)."""
    level = level if level in LEVELS else DEFAULT
    if cockpit == "claude":
        return level
    if cockpit == "openclaw":
        return _OPENCLAW_MAP[level]
    raise ValueError(f"no effort translation for cockpit {cockpit!r}")


def claude_frontmatter_value(profile: Mapping[str, Any] | None, role: str) -> str | None:
    """The `effort:` value for a Claude subagent, or None when the capability flag is off."""
    if not CLAUDE_EFFORT_FRONTMATTER:
        return None
    return for_cockpit(role_effort(profile, role), "claude")


def openclaw_thinking(profile: Mapping[str, Any] | None, role: str) -> str:
    return for_cockpit(role_effort(profile, role), "openclaw")


def table(profile: Mapping[str, Any] | None, roles) -> dict[str, str]:
    return {r: role_effort(profile, r) for r in roles}


def table_for_state(s: Any) -> dict[str, str]:
    """The `role_effort` table of the profile this state runs, {} when the profile declares none (then nothing is
    emitted: report-only). Never raises: an unknown, composed or unreadable profile resolves to {}."""
    from . import profile_names, profiles
    name = profile_names.canonical(getattr(getattr(s, "profile", None), "name", "") or "") or profiles.RECOMMENDED_PROFILE
    for mode in ("single-model", "multi-model"):
        try:
            doc = profiles.load_profile(name, mode)
        except Exception:  # noqa: BLE001 - a custom or retired name must not break setup
            continue
        raw = doc.get("role_effort") if isinstance(doc, Mapping) else None
        if isinstance(raw, Mapping) and raw:
            return {str(k): str(v).lower() for k, v in raw.items() if str(v).lower() in LEVELS}
    return {}
