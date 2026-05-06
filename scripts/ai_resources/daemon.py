"""ai-resources daemon — manage the LiteLLM gateway lifecycle."""
from __future__ import annotations

import argparse

from .setup import litellm, ui, state


def cmd_status(args: argparse.Namespace) -> int:
    ui.require_deps()
    s = state.load()
    if s.litellm.deployment == "remote":
        ok, msg = litellm.validate_remote(s.litellm.remote.url, "")
        if ok:
            ui.ok(f"Remote LiteLLM healthy at {s.litellm.remote.url}")
            return 0
        ui.error(f"Remote unreachable: {msg}")
        return 1
    if s.litellm.deployment != "local":
        ui.warn("LiteLLM not configured. Run: ai-resources setup")
        return 1

    status = litellm.service_status()
    url = f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}"
    if status == "running":
        if litellm.health_check(url + "/health/liveliness"):
            ui.ok(f"Service running, healthy ({s.litellm.local.runtime})")
            ui.detail(url)
            return 0
        ui.warn("Service running but health check failed")
        return 2
    if status == "stopped":
        ui.warn("Service stopped — start with: ai-resources daemon start")
        return 1
    ui.error(f"Service status: {status}")
    return 1


def cmd_start(args: argparse.Namespace) -> int:
    ui.require_deps()
    s = state.load()
    if s.litellm.deployment != "local":
        ui.warn("LiteLLM is not configured for local deployment.")
        return 1
    with ui.spinner("Starting LiteLLM service"):
        if not litellm.start_service():
            return 1
    url = f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}"
    if litellm.wait_for_health(url + "/health/liveliness"):
        ui.ok(f"Service running and healthy at {url}")
        return 0
    ui.error("Service started but health check timed out")
    return 2


def cmd_stop(args: argparse.Namespace) -> int:
    ui.require_deps()
    s = state.load()
    if s.litellm.deployment != "local":
        ui.warn("Nothing to stop (not local deployment).")
        return 0
    with ui.spinner("Stopping LiteLLM service"):
        if litellm.stop_service():
            ui.ok("Service stopped")
            return 0
    ui.error("Failed to stop service")
    return 1


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    return cmd_start(args)


def cmd_logs(args: argparse.Namespace) -> int:
    ui.require_deps()
    out = litellm.service_logs(tail=args.tail)
    if not out:
        ui.warn("No logs available (service may be down)")
        return 1
    print(out)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    ui.require_deps()
    with ui.spinner("Updating LiteLLM"):
        ok, msg = litellm.update_litellm()
    if not ok:
        ui.error(f"Update failed: {msg[:200]}")
        return 1
    ui.ok("LiteLLM updated")
    if litellm.restart_service():
        ui.ok("Service restarted with new version")
    return 0


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("daemon", help="Manage LiteLLM gateway lifecycle")
    sp = p.add_subparsers(dest="action", required=True)

    sp.add_parser("status", help="Show service status").set_defaults(func=cmd_status)
    sp.add_parser("start",  help="Start service").set_defaults(func=cmd_start)
    sp.add_parser("stop",   help="Stop service").set_defaults(func=cmd_stop)
    sp.add_parser("restart",help="Restart service").set_defaults(func=cmd_restart)

    p_logs = sp.add_parser("logs", help="Show service logs (tail)")
    p_logs.add_argument("--tail", type=int, default=100)
    p_logs.set_defaults(func=cmd_logs)

    sp.add_parser("update", help="Update LiteLLM (pipx/pip upgrade) and restart").set_defaults(func=cmd_update)
