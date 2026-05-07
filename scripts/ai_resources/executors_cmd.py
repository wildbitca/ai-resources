"""ai-resources executors — show/edit/test/set/apply/tune the role → model mapping."""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from .setup import state, ui, profiles, credentials, litellm
from .setup.providers import PROVIDERS, KNOWN_MODELS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_executors() -> tuple[dict, Any]:
    """Return (data, path). Exits with error message if not found."""
    path = state.executors_path()
    if not path.is_file():
        ui.error(f"No executors.yaml at {path}")
        ui.detail("Run `ai-resources setup` to generate.")
        sys.exit(1)
    if yaml is None:
        ui.error("PyYAML required")
        sys.exit(1)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data, path


def _write_and_regenerate(data: dict, path: Any) -> None:
    """Persist executors.yaml and regenerate ~/.claude/agents/*.md."""
    profiles.write_executors(data, path)
    try:
        from .setup.cockpits import claude as claude_cockpit
        s = state.load()
        mode = s.mode if s else "multi-model"
        updated = claude_cockpit.regenerate_agents(data, mode)
        if updated:
            ui.ok(f"Agent files regenerated: {', '.join(updated)}")
    except Exception as exc:  # noqa: BLE001
        ui.warn(f"Could not regenerate agent files: {exc}")
        ui.detail("Run `ai-resources generate` manually to apply.")


def _infer_provider(model: str) -> str | None:
    """Infer provider from model name prefix using KNOWN_MODELS registry."""
    for provider_id, models in KNOWN_MODELS.items():
        if model in models:
            return provider_id
    # Fallback: prefix heuristics
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("gemini-"):
        return "google"
    if model.startswith("gpt-") or model.startswith("o1") or model.startswith("o3"):
        return "openai"
    return None


def _select_provider_and_model(current_provider: str = "", current_model: str = "") -> tuple[str, str]:
    """Interactive: pick provider then model. Returns (provider, model)."""
    provider_choices = [
        ui.Choice(
            f"{pid}  [{p.description}]",
            value=pid,
        )
        for pid, p in PROVIDERS.items()
    ]
    provider = ui.select(
        "Provider",
        choices=provider_choices,
        default=current_provider or None,
        instruction=" (↑/↓ Enter)",
    )
    if provider is None:
        sys.exit(0)

    known = KNOWN_MODELS.get(provider, [])
    model_choices = [ui.Choice(m, value=m) for m in known]
    model_choices.append(ui.Choice("other (type manually)", value="__other__"))

    selected = ui.select(
        "Model",
        choices=model_choices,
        default=current_model if current_model in known else None,
    )
    if selected is None:
        sys.exit(0)
    if selected == "__other__":
        selected = ui.text("Model name", default=current_model)
        if not selected:
            sys.exit(0)

    return provider, selected


def _diff_table(before: dict, after: dict) -> None:
    """Print a before/after table for roles that changed."""
    changed = [
        (role, before.get(role, {}), after.get(role, {}))
        for role in profiles.KNOWN_ROLES
        if before.get(role) != after.get(role)
    ]
    if not changed:
        ui.info("No changes.")
        return
    if ui.has_rich():
        from rich.table import Table
        t = Table(title="Changes", show_header=True, header_style="bold cyan", border_style="dim")
        t.add_column("Role", style="bold")
        t.add_column("Before", style="dim red")
        t.add_column("After", style="green")
        for role, b, a in changed:
            bm = f"{b.get('provider','-')}/{b.get('model','-')}"
            am = f"{a.get('provider','-')}/{a.get('model','-')}"
            t.add_row(role, bm, am)
        ui.console().print(t)
    else:
        print("\nChanges:")
        for role, b, a in changed:
            bm = f"{b.get('provider','-')}/{b.get('model','-')}"
            am = f"{a.get('provider','-')}/{a.get('model','-')}"
            print(f"  {role:<24} {bm:<30} → {am}")


# ---------------------------------------------------------------------------
# Existing commands
# ---------------------------------------------------------------------------

def cmd_show(args: argparse.Namespace) -> int:
    ui.require_deps()
    data, path = _load_executors()
    ui.banner(f"executors.yaml ({data.get('profile_name', 'unnamed')})",
              subtitle=str(path))
    rows = profiles.role_table(data)
    ui.role_table(rows, title="Role → Provider → Model")
    gateway = data.get("gateway", {})
    ui.console().print()
    ui.info(f"Gateway: {gateway.get('url', '?')}")
    defaults = data.get("defaults", {})
    ui.info(f"max_retries: {defaults.get('max_retries', '?')}  "
            f"timeout: {defaults.get('timeout_seconds', '?')}s")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    ui.require_deps()
    path = state.executors_path()
    if not path.is_file():
        ui.error(f"No executors.yaml at {path}")
        return 1
    editor = args.editor or "vi"
    return subprocess.call([editor, str(path)])


def cmd_test(args: argparse.Namespace) -> int:
    """Run a single round-trip against a specific role's model."""
    ui.require_deps()
    s = state.load()
    data, path = _load_executors()
    by_role = data.get("by_role", {})
    if args.role not in by_role:
        ui.error(f"Unknown role: {args.role}")
        ui.detail("Available: " + ", ".join(sorted(by_role.keys())))
        return 1

    model = by_role[args.role].get("model")
    gateway = (s.litellm.remote.url if s.litellm.deployment == "remote"
               else f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}")
    master = credentials.get_key("LITELLM_MASTER_KEY")

    ui.info(f"Testing role={args.role}  model={model}  via {gateway}")
    with ui.spinner(f"Round-trip {model}"):
        ok, msg = litellm.smoke_test_model(model, gateway, master)
    if ok:
        ui.ok(f"{model}: round-trip succeeded")
        return 0
    ui.error(f"{model}: {msg[:200]}")
    return 1


# ---------------------------------------------------------------------------
# New commands
# ---------------------------------------------------------------------------

def cmd_set(args: argparse.Namespace) -> int:
    """Reassign a single role's model, with interactive menu if model not given."""
    ui.require_deps()
    data, path = _load_executors()
    by_role: dict = data.setdefault("by_role", {})

    role = args.role
    if role not in profiles.KNOWN_ROLES:
        # Accept unknown roles but warn
        ui.warn(f"'{role}' is not a standard role — proceeding anyway.")

    current = by_role.get(role, {})
    current_provider = current.get("provider", "")
    current_model = current.get("model", "")

    if args.model:
        # Model given on CLI — infer provider
        model = args.model
        provider = _infer_provider(model)
        if not provider:
            ui.warn(f"Could not infer provider for '{model}'. Choose:")
            provider = ui.select("Provider", choices=list(PROVIDERS.keys()))
            if not provider:
                return 0
    else:
        # Interactive
        ui.banner(f"Reassign: {role}",
                  subtitle=f"Current: {current_provider}/{current_model}" if current_model else "")
        provider, model = _select_provider_and_model(current_provider, current_model)

    old_by_role = {k: dict(v) for k, v in by_role.items()}
    by_role[role] = {"provider": provider, "model": model}

    _diff_table(old_by_role, by_role)
    _write_and_regenerate(data, path)
    ui.ok(f"{role} → {provider}/{model}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Switch to a named profile (bulk replacement of all role assignments)."""
    ui.require_deps()
    data, path = _load_executors()

    available = profiles.list_profiles()
    if not available:
        ui.error("No profiles found in kit profiles/ directory.")
        return 1

    profile_name = args.profile
    if not profile_name:
        # Interactive — show profiles with descriptions
        profile_choices = []
        for pname in available:
            try:
                pdata = profiles.load_profile(pname)
                desc = pdata.get("description", "").strip().splitlines()[0][:60]
            except Exception:  # noqa: BLE001
                desc = ""
            profile_choices.append(ui.Choice(f"{pname}  —  {desc}" if desc else pname,
                                             value=pname))
        profile_name = ui.select("Select profile", choices=profile_choices)
        if not profile_name:
            return 0

    try:
        profile_data = profiles.load_profile(profile_name)
    except FileNotFoundError:
        ui.error(f"Profile not found: {profile_name}")
        ui.detail(f"Available: {', '.join(available)}")
        return 1

    new_executors = profiles.to_executors(profile_data)
    # Preserve gateway settings from current config
    new_executors["gateway"] = data.get("gateway", new_executors["gateway"])

    ui.banner(f"Applying profile: {profile_name}")
    _diff_table(data.get("by_role", {}), new_executors.get("by_role", {}))

    defaults_new = new_executors.get("defaults", {})
    defaults_old = data.get("defaults", {})
    if defaults_new != defaults_old:
        ui.info(f"defaults: max_retries {defaults_old.get('max_retries','?')} → "
                f"{defaults_new.get('max_retries','?')}")

    if not ui.confirm("Apply these changes?", default=True):
        ui.info("Aborted.")
        return 0

    _write_and_regenerate(new_executors, path)
    ui.ok(f"Profile '{profile_name}' applied.")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Interactive bulk editor — pick roles to reassign, change each model."""
    ui.require_deps()
    data, path = _load_executors()
    by_role: dict = data.setdefault("by_role", {})

    ui.banner("Tune role → model assignments",
              subtitle=f"executors: {path}")

    # Show current table
    ui.role_table(profiles.role_table(data), title="Current assignments")
    ui.console().print()

    # Pick roles to change
    role_choices = [
        ui.Choice(
            f"{role:<24} {by_role.get(role, {}).get('provider', '-'):<12} "
            f"{by_role.get(role, {}).get('model', '-')}",
            value=role,
        )
        for role in profiles.KNOWN_ROLES
    ]
    selected_roles = ui.checkbox(
        "Which roles do you want to reassign?",
        choices=role_choices,
        instruction=" (space to select, Enter to confirm)",
    )
    if not selected_roles:
        ui.info("Nothing selected — no changes made.")
        return 0

    old_by_role = {k: dict(v) for k, v in by_role.items()}

    # Reassign each selected role
    for role in selected_roles:
        current = by_role.get(role, {})
        ui.console().print()
        ui.section(selected_roles.index(role) + 1, len(selected_roles), role)
        provider, model = _select_provider_and_model(
            current.get("provider", ""), current.get("model", "")
        )
        by_role[role] = {"provider": provider, "model": model}
        ui.ok(f"{role} → {provider}/{model}")

    # Optional: tune defaults
    ui.console().print()
    if ui.confirm("Also update defaults (max_retries, timeout)?", default=False):
        defaults = data.setdefault("defaults", {})
        current_retries = str(defaults.get("max_retries", 3))
        new_retries = ui.text("max_retries", default=current_retries,
                              validate=lambda v: v.isdigit() or "Must be a number")
        defaults["max_retries"] = int(new_retries)

        current_timeout = str(defaults.get("timeout_seconds", 600))
        new_timeout = ui.text("timeout_seconds", default=current_timeout,
                              validate=lambda v: v.isdigit() or "Must be a number")
        defaults["timeout_seconds"] = int(new_timeout)

    ui.console().print()
    _diff_table(old_by_role, by_role)

    if not ui.confirm("Save and regenerate agent files?", default=True):
        ui.info("Aborted — no changes written.")
        return 0

    _write_and_regenerate(data, path)
    ui.ok("Done.")
    return 0


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------

def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("executors", help="Show/edit/test/set/apply/tune role → model mapping")
    sp = p.add_subparsers(dest="action", required=True)

    sp.add_parser("show", help="Show current mapping").set_defaults(func=cmd_show)

    p_edit = sp.add_parser("edit", help="Open executors.yaml in $EDITOR")
    p_edit.add_argument("--editor", default="")
    p_edit.set_defaults(func=cmd_edit)

    p_test = sp.add_parser("test", help="Test a single role's model via the gateway")
    p_test.add_argument("role")
    p_test.set_defaults(func=cmd_test)

    p_set = sp.add_parser("set", help="Reassign a single role's model")
    p_set.add_argument("role", help="Role name (e.g. security-auditor)")
    p_set.add_argument("model", nargs="?", default="",
                       help="Model ID (e.g. gemini-2.5-pro). Omit for interactive menu.")
    p_set.set_defaults(func=cmd_set)

    p_apply = sp.add_parser("apply", help="Switch to a named profile (bulk replacement)")
    p_apply.add_argument("profile", nargs="?", default="",
                         help="Profile name (e.g. cost-optimized). Omit for interactive menu.")
    p_apply.set_defaults(func=cmd_apply)

    sp.add_parser("tune", help="Interactive bulk editor — pick roles to reassign") \
      .set_defaults(func=cmd_tune)
