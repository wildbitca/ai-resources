"""Profile presets for per-role model assignment.

Loads YAML files from <kit>/profiles/ at runtime. Each profile defines
a `by_role` mapping that produces an executors.yaml when applied.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from .. import repo_root


# Roles that the kit ships with (matches agents/roles/*.md).
KNOWN_ROLES = [
    "explore",
    "generalPurpose",
    "planner",
    "software-architect",
    "implementer",
    "tester",
    "code-reviewer",
    "security-auditor",
    "verifier",
    "package-upgrade",
    "crashlytics-fixer",
    "sentry-fixer",
    "terraform-maintainer",
    "crossplane-upjet-maintainer",
]


def profiles_dir() -> Path:
    return repo_root() / "profiles"


def list_profiles() -> list[str]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_profile(name: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required to load profiles")
    path = profiles_dir() / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Profile not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def to_executors(profile: dict[str, Any]) -> dict[str, Any]:
    """Convert a profile dict to a final executors.yaml structure."""
    return {
        "version": 1,
        "profile_name": profile.get("name", "custom"),
        "gateway": profile.get("gateway", {
            "url": "http://127.0.0.1:4000",
            "api_key_env": "LITELLM_MASTER_KEY",
        }),
        "defaults": profile.get("defaults", {
            "fallbacks": [],
            "max_retries": 3,
            "timeout_seconds": 600,
        }),
        "by_role": profile.get("by_role", {}),
    }


def write_executors(executors: dict[str, Any], path: Path) -> None:
    if yaml is None:
        raise RuntimeError("PyYAML required to write executors.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(executors, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def role_table(executors: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return [(role, provider, model)] for display."""
    rows = []
    for role in KNOWN_ROLES:
        cfg = executors.get("by_role", {}).get(role, {})
        rows.append((role, cfg.get("provider", "-"), cfg.get("model", "-")))
    return rows


def merge_customizations(profile: dict[str, Any], customizations: dict[str, dict]) -> dict[str, Any]:
    """Apply per-role overrides on top of a profile."""
    out = dict(profile)
    by_role = dict(out.get("by_role", {}))
    for role, override in customizations.items():
        existing = dict(by_role.get(role, {}))
        existing.update(override)
        by_role[role] = existing
    out["by_role"] = by_role
    return out
