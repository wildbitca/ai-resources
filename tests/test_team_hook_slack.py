"""The team-narration hook speaks on the live channel: Slack agents resolve to `channel:<id>`, and the sender,
the watcher and the hook all pick the delivery channel from the target."""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load(REPO / "hooks" / "openclaw_team_progress.py", "ocp_hook")
send = _load(REPO / "scripts" / "openclaw" / "openclaw-team-send.py", "ocp_send")

CFG = {"bindings": [
    {"agentId": "devops", "match": {"channel": "slack", "peer": {"kind": "channel", "id": "C0C6AF3V5QE"}}},
    {"agentId": "devops", "match": {"channel": "slack", "peer": {"kind": "channel", "id": "C0BJ5UVU874"}}},
    {"agentId": "tg", "match": {"channel": "telegram", "peer": {"kind": "group", "id": "-5001"}}},
    {"agentId": "main", "match": {"channel": "slack", "accountId": "*"}},
]}


def test_a_slack_agent_resolves_to_its_first_channel():
    assert hook._binding_target(CFG, "devops") == ("channel:C0C6AF3V5QE", None)


def test_a_telegram_group_still_wins_and_the_catch_all_is_no_target():
    assert hook._binding_target(CFG, "tg") == ("-5001", None)
    assert hook._binding_target(CFG, "main") is None


def test_the_channel_follows_the_target_everywhere():
    assert hook.channel_of("channel:C1ABCDE") == "slack" and hook.channel_of("user:U1ABCDE") == "slack"
    assert hook.channel_of("-5001") == "telegram"
    assert send._argv("send", "channel:C1ABCDE", None, "hi", None)[:6] == [send.OPENCLAW, "message", "send", "--channel", "slack", "--target"]
    assert "--channel" in send._argv("send", "-5", None, "hi", None) and send._argv("send", "-5", None, "hi", None)[4] == "telegram"


def test_a_slack_message_id_is_a_timestamp():
    assert send._message_id("Message ID: 1792957966.781689") == "1792957966.781689"
    assert send._message_id("Message ID: 77") == "77"
    assert send._message_id('{"ts": "1792957966.781689"}') == "1792957966.781689"
