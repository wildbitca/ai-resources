"""The agy MCP bridge: settings resolution, ${VAR} substitution, and offline replies.

Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_PATH = REPO / "openclaw-plugin" / "ai-resources" / "bridge.py"

spec = importlib.util.spec_from_file_location("openclaw_agy_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(spec)
sys.modules["openclaw_agy_bridge"] = bridge
spec.loader.exec_module(bridge)  # type: ignore[union-attr]


def test_resolve_target_is_none_without_the_settings_env_var():
    assert bridge.resolve_target({}) is None


def test_resolve_target_is_none_when_the_settings_file_is_unreadable(tmp_path):
    assert bridge.resolve_target({"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(tmp_path / "nope.json")}) is None


def test_resolve_target_reads_the_openclaw_server_and_substitutes_env(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"openclaw": {
            "httpUrl": "http://127.0.0.1:4477/mcp",
            "headers": {"Authorization": "Bearer ${OPENCLAW_MCP_TOKEN}"},
        }},
    }), encoding="utf-8")
    env = {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(settings), "OPENCLAW_MCP_TOKEN": "secret-123"}
    target = bridge.resolve_target(env)
    assert target == ("http://127.0.0.1:4477/mcp", {"Authorization": "Bearer secret-123"})


def test_resolve_target_is_none_when_no_openclaw_server_is_declared(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    env = {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(settings)}
    assert bridge.resolve_target(env) is None


def test_substitute_env_leaves_unknown_vars_as_empty_string():
    assert bridge.substitute_env("Bearer ${MISSING}", {}) == "Bearer "


def test_offline_reply_to_initialize_advertises_an_empty_tool_set():
    reply = bridge.offline_reply({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-06-18"}})
    assert reply["id"] == 1
    assert reply["result"]["capabilities"] == {"tools": {}}


def test_offline_reply_to_tools_list_is_an_empty_list():
    reply = bridge.offline_reply({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert reply["result"] == {"tools": []}


def test_offline_reply_to_a_notification_is_none():
    assert bridge.offline_reply({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_parse_body_reads_a_json_object_body():
    assert bridge.parse_body("application/json", json.dumps({"a": 1})) == [{"a": 1}]


def test_parse_body_reads_an_sse_body():
    body = "event: message\ndata: {\"a\": 1}\n\ndata: {\"b\": 2}\n\n"
    assert bridge.parse_body("text/event-stream", body) == [{"a": 1}, {"b": 2}]


def test_parse_body_empty_json_body_is_an_empty_list():
    assert bridge.parse_body("application/json", "") == []


def test_run_answers_initialize_offline_when_no_settings_file_is_configured():
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {}}) + "\n")
    stdout = io.StringIO()
    bridge.run(stdin, stdout, {})
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert len(lines) == 1
    assert lines[0]["result"]["serverInfo"]["name"] == "openclaw-offline"


def test_run_forwards_to_the_resolved_target_and_writes_the_session_id(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"openclaw": {"httpUrl": "http://127.0.0.1:4477/mcp", "headers": {}}},
    }), encoding="utf-8")
    env = {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(settings)}

    class _FakeHeaders(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    class _FakeResponse:
        def __init__(self, body):
            self.headers = _FakeHeaders({"Content-Type": "application/json", "Mcp-Session-Id": "sess-1"})
            self._body = body.encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = []

    def fake_opener(req, timeout=600):
        calls.append(req)
        return _FakeResponse(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": ["x"]}}))

    original_forward = bridge.Session.forward
    monkeypatch.setattr(bridge.Session, "forward",
                         lambda self, url, headers, msg, opener=None: original_forward(
                             self, url, headers, msg, opener=fake_opener))

    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
    stdout = io.StringIO()
    bridge.run(stdin, stdout, env)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert lines == [{"jsonrpc": "2.0", "id": 1, "result": {"tools": ["x"]}}]
    assert len(calls) == 1


def test_run_replies_with_an_error_when_forwarding_raises(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"openclaw": {"httpUrl": "http://127.0.0.1:4477/mcp", "headers": {}}},
    }), encoding="utf-8")
    env = {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": str(settings)}

    def boom(self, url, headers, msg, opener=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(bridge.Session, "forward", boom)

    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) + "\n")
    stdout = io.StringIO()
    bridge.run(stdin, stdout, env)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert lines[0]["id"] == 7
    assert "connection refused" in lines[0]["error"]["message"]
