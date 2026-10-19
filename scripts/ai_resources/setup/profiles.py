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
from . import profile_names


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


# The multi-model recommendation, and what a state that never chose a profile resolves to.
RECOMMENDED_PROFILE = "balanced-claude-55"


def recommended_profile(backend: str = "litellm") -> str:
    """The recommendation for a gateway. A state with no profile resolves to it, so a legacy LiteLLM state behaves
    as before (ADR-0010 N4)."""
    return RECOMMENDED_PROFILE


def saved_or_recommended(saved: str, backend: str = "litellm") -> str:
    """The profile a state runs under: the one it saved, else the recommendation (never a stale default)."""
    return saved or recommended_profile(backend)


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
    # Aliases are hidden: a file that an old name points at is listed under its new name.
    names = sorted({profile_names.canonical(str(p.relative_to(d).with_suffix("")).replace("\\", "/"))
                    for p in d.rglob("*.yaml")})
    for n in names:
        doc = _read(_path_of(n, "single-model" if n in profile_names.PRESET_IDS else None))
        if mode is not None and doc.get("mode", "multi-model") != mode:
            continue
        # A profile that names no backend is gateway-agnostic: its models are
        # canonical `<vendor>/<model>` IDs that either gateway resolves, so it is
        # offered whatever the backend. Only a profile that pins one is filtered.
        declared = doc.get("backend")
        if backend is not None and declared is not None and declared != backend:
            continue
        out.append(n)
    return out


def _path_of(name: str, mode: str | None) -> Path:
    return profile_names.resolve(name, mode, profiles_dir())


def _read(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML required to load profiles")
    if not path.is_file():
        raise FileNotFoundError(f"Profile not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_profile(name: str, mode: str | None = None) -> dict[str, Any]:
    """Load a profile by name. An old name (profile_names.ALIASES) loads its team under the new name. A bare
    starting-team id (`balanced`, ...) needs `mode`; without it `profile_names.ProfileModeRequired` is raised."""
    return _read(_path_of(name, mode))


GATEWAYS: dict[str, dict[str, str]] = {
    "litellm": {"url": "http://127.0.0.1:4000", "api_key_env": "LITELLM_MASTER_KEY"},
    "openrouter": {"url": "https://openrouter.ai/api", "api_key_env": "OPENROUTER_API_KEY"},
}


# The OpenAI-compatible base a cockpit that speaks the OpenAI protocol (Aider) must use per gateway.
# OpenRouter serves it at /api/v1; GATEWAYS above holds the Anthropic-shaped /api that Claude Code
# appends /v1/messages to. LiteLLM serves both at the gateway URL itself.
OPENAI_BASE: dict[str, str] = {"openrouter": "https://openrouter.ai/api/v1"}


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
        model = cfg.get("model", "-")
        # Derived, not stored: the vendor is the first segment of the canonical
        # ID, so there is one source of truth. The old `provider` field was
        # still displayed and edited after it stopped deciding anything.
        vendor = model.split("/", 1)[0] if "/" in model else cfg.get("provider", "-")
        rows.append((role, vendor, model))
    return rows


def model_label(cfg: dict[str, Any]) -> str:
    """Human label for a by_role entry. A canonical model already names its vendor, so it needs no separate
    provider prefix; a legacy bare model is joined with its `provider` field so old and new shapes read alike."""
    model = cfg.get("model", "")
    if not model:
        return "-"
    if "/" in model:
        return model
    return f"{cfg.get('provider', '-')}/{model}"


def role_changes(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str, str]]:
    """[(role, before label, after label)] for every known role whose by_role entry differs."""
    return [
        (role, model_label(before.get(role, {})), model_label(after.get(role, {})))
        for role in KNOWN_ROLES
        if before.get(role) != after.get(role)
    ]


def print_role_diff(before: dict[str, Any], after: dict[str, Any], title: str = "Changes") -> bool:
    """Print the before/after table of the roles that differ; False (after saying so) when none does.

    The one table shared by `executors tune|set|apply` and the wizard's recommendation notice."""
    from . import ui
    changed = role_changes(before, after)
    if not changed:
        ui.info("No changes.")
        return False
    if ui.has_rich():
        from rich.table import Table
        t = Table(title=title, show_header=True, header_style="bold cyan", border_style="dim")
        t.add_column("Role", style="bold")
        t.add_column("Before", style="dim red")
        t.add_column("After", style="green")
        for role, b, a in changed:
            t.add_row(role, b, a)
        ui.console().print(t)
    else:
        print(f"\n{title}:")
        for role, b, a in changed:
            print(f"  {role:<24} {b:<30} → {a}")
    return True


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
