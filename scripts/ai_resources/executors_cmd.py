"""ai-resources executors — show/edit/test the role → model mapping."""
from __future__ import annotations

import argparse
import subprocess
import sys

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from .setup import state, ui, profiles, credentials, litellm


def cmd_show(args: argparse.Namespace) -> int:
    ui.require_deps()
    path = state.executors_path()
    if not path.is_file():
        ui.warn(f"No executors.yaml at {path}")
        ui.detail("Run `ai-resources setup` to generate.")
        return 1
    if yaml is None:
        print(path.read_text(encoding="utf-8"))
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    ui.banner(f"executors.yaml ({data.get('profile_name', 'unnamed')})",
              subtitle=str(path))
    rows = profiles.role_table(data)
    ui.role_table(rows, title="Role → Provider → Model")

    gateway = data.get("gateway", {})
    ui.console().print()
    ui.info(f"Gateway: {gateway.get('url', '?')}")
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
    path = state.executors_path()
    if not path.is_file():
        ui.error("No executors.yaml — run setup first")
        return 1
    if yaml is None:
        ui.error("PyYAML required")
        return 1

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("executors", help="Show/edit/test role → model mapping")
    sp = p.add_subparsers(dest="action", required=True)

    sp.add_parser("show", help="Show current mapping").set_defaults(func=cmd_show)

    p_edit = sp.add_parser("edit", help="Open executors.yaml in $EDITOR")
    p_edit.add_argument("--editor", default="")
    p_edit.set_defaults(func=cmd_edit)

    p_test = sp.add_parser("test", help="Test a single role's model via the gateway")
    p_test.add_argument("role")
    p_test.set_defaults(func=cmd_test)
