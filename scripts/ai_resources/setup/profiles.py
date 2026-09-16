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
    "doc-writer",
    "package-upgrade",
    "crashlytics-fixer",
    "sentry-fixer",
    "terraform-maintainer",
    "crossplane-upjet-maintainer",
]


def profiles_dir() -> Path:
    return repo_root() / "profiles"


def personas_dir() -> Path:
    return repo_root() / "agents" / "personas"


def known_personas() -> dict[str, str]:
    """Map persona name -> base role, read from disk.

    Personas are named `<role>-<domain>`, and both halves can contain hyphens
    (`code-reviewer-api-platform`), so the split is resolved by matching against
    the known roles and taking the longest match rather than by cutting on a
    separator. A file whose prefix matches no role is skipped: it would generate
    a subagent with no role body behind it.
    """
    d = personas_dir()
    if not d.is_dir():
        return {}
    out: dict[str, str] = {}
    for path in sorted(d.glob("*.md")):
        name = path.stem
        matches = [r for r in KNOWN_ROLES if name.startswith(f"{r}-")]
        if matches:
            out[name] = max(matches, key=len)
    return out


def list_profiles(mode: str | None = None, backend: str | None = None) -> list[str]:
    """Profile names, optionally filtered by `mode` and `backend`.

    A profile's `mode:` defaults to multi-model and its `backend:` to litellm.
    Filtering by backend matters because the two are not interchangeable: an
    openrouter profile carries namespaced IDs (`google/gemini-3.7-flash`) that a
    LiteLLM gateway does not know, and a LiteLLM profile carries bare IDs that
    OpenRouter rejects. Offering the wrong set guarantees a broken setup.
    """
    d = profiles_dir()
    if not d.is_dir():
        return []
    out = []
    for n in sorted(p.stem for p in d.glob("*.yaml")):
        doc = load_profile(n)
        if mode is not None and doc.get("mode", "multi-model") != mode:
            continue
        # Backend only discriminates multi-model profiles; single-model ones
        # never touch a gateway, so they are returned whatever the backend.
        if (backend is not None
                and doc.get("mode", "multi-model") == "multi-model"
                and doc.get("backend", "litellm") != backend):
            continue
        out.append(n)
    return out


def load_profile(name: str) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required to load profiles")
    path = profiles_dir() / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Profile not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


GATEWAYS: dict[str, dict[str, str]] = {
    "litellm": {"url": "http://127.0.0.1:4000", "api_key_env": "LITELLM_MASTER_KEY"},
    "openrouter": {"url": "https://openrouter.ai/api", "api_key_env": "OPENROUTER_API_KEY"},
}


def to_executors(profile: dict[str, Any], backend: str = "litellm") -> dict[str, Any]:
    """Convert a profile dict to a final executors.yaml structure.

    `backend` picks the gateway defaults; a profile that names its own `gateway`
    still wins, so a hand-edited profile is never silently overridden.
    """
    return {
        "version": 1,
        "profile_name": profile.get("name", "custom"),
        "backend": backend,
        "gateway": profile.get("gateway", GATEWAYS.get(backend, GATEWAYS["litellm"])),
        # Alias → concrete model for the main conversation. Only the openrouter
        # backend needs it: Claude Code resolves /model aliases to bare Claude
        # IDs, which a namespaced catalogue does not recognise. Subagents are
        # unaffected — their frontmatter `model:` goes out verbatim.
        "classes": profile.get("classes", {}),
        # Per-persona overrides, keyed by persona name (e.g. implementer-angular).
        # A persona with no entry inherits its base role's model, so an empty
        # block changes nothing — it only opens the door to routing, say, an
        # Angular review and an infrastructure review to different models.
        "by_persona": profile.get("by_persona", {}),
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
