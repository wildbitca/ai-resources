#!/usr/bin/env python3
"""stdio MCP server that forwards to OpenClaw's loopback MCP over streamable HTTP.

agy has no per-run MCP flag and stores servers globally (``agy mcp add``), and it does
not expand ``${VAR}`` in headers, so it cannot be pointed at OpenClaw's per-run MCP
server directly. Instead this bridge is registered with agy once, as a stdio server.
Each run it reads the settings file OpenClaw writes for that turn
(``GEMINI_CLI_SYSTEM_SETTINGS_PATH``, written because the agy-cli backend runs with
``bundleMcpMode: "gemini-system-settings"``), pulls out the ``openclaw`` MCP server's
URL and headers, substitutes ``${VAR}`` from its own environment (which OpenClaw does
pass through to stdio servers), and forwards every JSON-RPC message over streamable
HTTP. Outside an OpenClaw-started run there is no settings file, so it answers
``initialize`` and ``tools/list`` with an empty tool set and drops everything else.

stdlib only, no third-party imports: this script runs under whatever python3 setup
registered it with ``agy mcp add``, which may not be the kit's own interpreter.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

LOG = os.environ.get("AGY_BRIDGE_LOG")

_VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def log(msg: str) -> None:
    if not LOG:
        return
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except OSError:
        pass


def substitute_env(value: str, env: dict) -> str:
    return _VAR_RE.sub(lambda m: env.get(m.group(1), ""), value)


def resolve_target(env: dict) -> tuple[str, dict] | None:
    """Read the OpenClaw-written gemini-system-settings file for this run, if any."""
    path = env.get("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError) as e:
        log(f"settings unreadable: {e}")
        return None
    server = (settings.get("mcpServers") or {}).get("openclaw")
    if not server:
        return None
    url = server.get("httpUrl") or server.get("url")
    if not url:
        return None
    headers = {k: substitute_env(v, env) for k, v in (server.get("headers") or {}).items()}
    return url, headers


class Session:
    """Holds the `Mcp-Session-Id` streamable-HTTP servers hand back after `initialize`."""

    def __init__(self) -> None:
        self.id: str | None = None

    def forward(self, url: str, headers: dict, msg: dict, *, opener=urllib.request.urlopen) -> list[dict]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
        if self.id:
            h["Mcp-Session-Id"] = self.id
        req = urllib.request.Request(url, data=json.dumps(msg).encode("utf-8"), headers=h, method="POST")
        with opener(req, timeout=600) as resp:
            self.id = resp.headers.get("Mcp-Session-Id") or self.id
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
        return parse_body(ctype, body)


def parse_body(ctype: str, body: str) -> list[dict]:
    out: list[dict] = []
    if "text/event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:") and line[5:].strip():
                out.append(json.loads(line[5:]))
    elif body.strip():
        data = json.loads(body)
        out.extend(data if isinstance(data, list) else [data])
    return out


def offline_reply(msg: dict) -> dict | None:
    """Reply used when there is no OpenClaw settings file to forward to."""
    if msg.get("method") == "initialize":
        params = msg.get("params") or {}
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "openclaw-offline", "version": "0"},
            },
        }
    if "id" in msg:
        result = {"tools": []} if msg.get("method") == "tools/list" else {}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}
    return None  # notifications get no reply


def run(stdin, stdout, env: dict) -> None:
    target = resolve_target(env)
    log(f"start target={'yes' if target else 'no'}")
    session = Session()
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError as e:
            log(f"bad json: {e}")
            continue
        if target is None:
            reply = offline_reply(msg)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
            continue
        try:
            for reply in session.forward(target[0], target[1], msg):
                stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
        except Exception as e:  # noqa: BLE001 - report to the caller, never crash the bridge
            log(f"forward {msg.get('method')} failed: {e}")
            if "id" in msg:
                stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32000, "message": f"openclaw bridge: {e}"},
                }) + "\n")
                stdout.flush()


def main() -> None:  # pragma: no cover - thin stdio wiring
    run(sys.stdin, sys.stdout, dict(os.environ))


if __name__ == "__main__":  # pragma: no cover
    main()
