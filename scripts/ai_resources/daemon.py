"""ai-resources daemon — manage the LiteLLM container lifecycle."""
from __future__ import annotations

import argparse
import sys

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

    status = litellm.container_status()
    if status == "running":
        if litellm.health_check():
            ui.ok(f"Container '{litellm.CONTAINER_NAME}' running, healthy")
            ui.detail(f"http://{s.litellm.local.bind_address}:{s.litellm.local.port}")
            return 0
        ui.warn(f"Container running but health check failed")
        return 2
    ui.error(f"Container status: {status}")
    return 1


def cmd_start(args: argparse.Namespace) -> int:
    ui.require_deps()
    s = state.load()
    if s.litellm.deployment != "local":
        ui.warn("LiteLLM is not configured for local deployment.")
        return 1
    with ui.spinner("Starting LiteLLM container"):
        if not litellm.start_container():
            return 1
    if litellm.wait_for_health():
        ui.ok("LiteLLM container running and healthy")
        return 0
    ui.error("Container started but health check timed out")
    return 2


def cmd_stop(args: argparse.Namespace) -> int:
    ui.require_deps()
    s = state.load()
    if s.litellm.deployment != "local":
        ui.warn("Nothing to stop (not local deployment).")
        return 0
    with ui.spinner("Stopping LiteLLM container"):
        if litellm.stop_container():
            ui.ok("Container stopped")
            return 0
    ui.error("Failed to stop container")
    return 1


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    return cmd_start(args)


def cmd_logs(args: argparse.Namespace) -> int:
    ui.require_deps()
    out = litellm.container_logs(tail=args.tail)
    if not out:
        ui.warn("No logs available (container may be down)")
        return 1
    print(out)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    ui.require_deps()
    s = state.load()
    image = args.image or s.litellm.local.image or litellm.DEFAULT_IMAGE
    with ui.spinner(f"Pulling {image}"):
        if not litellm.pull_image(image):
            ui.error("Image pull failed")
            return 1
    ui.ok(f"Pulled {image}")
    if litellm.update_image(image):
        ui.ok("Container restarted with new image")
        return 0
    ui.error("Restart failed")
    return 1


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("daemon", help="Manage LiteLLM container lifecycle")
    sp = p.add_subparsers(dest="action", required=True)

    sp.add_parser("status", help="Show container status").set_defaults(func=cmd_status)
    sp.add_parser("start",  help="Start container").set_defaults(func=cmd_start)
    sp.add_parser("stop",   help="Stop container").set_defaults(func=cmd_stop)
    sp.add_parser("restart",help="Restart container").set_defaults(func=cmd_restart)

    p_logs = sp.add_parser("logs", help="Show container logs")
    p_logs.add_argument("--tail", type=int, default=100)
    p_logs.set_defaults(func=cmd_logs)

    p_up = sp.add_parser("update", help="Pull latest image and restart")
    p_up.add_argument("--image", default="")
    p_up.set_defaults(func=cmd_update)
