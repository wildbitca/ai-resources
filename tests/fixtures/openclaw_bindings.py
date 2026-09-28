"""The one root `bindings` shape the kit writes and the narration hook reads.

The hook (`hooks/openclaw_team_progress.py`) is stdlib-only and cannot import `scripts/ai_resources`,
so writer (`openclaw_host.build_bindings`) and reader (`resolve_target`) are two implementations of the
same contract. This module is the single place that contract is spelled out, and both test suites use
it: if the cockpit ever writes a shape the hook cannot resolve, the round-trip test built on it fails.

Shaped like the live host (measured 2026-09-28): five basic-group routes (`type: "route"`, a
`-5xxxxxxxxx` id, never `-100...`) and main's catch-all LAST, with no `type` and no `peer`.
"""
from __future__ import annotations

import copy

GROUP_CHATS = {
    "snoutzone": "-5100000001",
    "elinvo": "-5100000002",
    "devops": "-5100000003",
    "ai": "-5100000004",
    "security": "-5100000005",
}


def route(agent: str, chat: str, comment: str | None = None) -> dict:
    """One group route, in the exact key order OpenClaw stores it."""
    return {"type": "route", "agentId": agent, "comment": comment or f"{agent} group (no topics)",
            "match": {"channel": "telegram", "peer": {"kind": "group", "id": chat}}}


def catch_all() -> dict:
    """main's catch-all: no `type`, no `peer`, `accountId: "*"`. It must stay LAST."""
    return {"agentId": "main", "comment": "catch-all: preserved, must stay LAST",
            "match": {"channel": "telegram", "accountId": "*"}}


def live_bindings() -> list[dict]:
    return [route(a, c) for a, c in GROUP_CHATS.items()] + [catch_all()]


def copy_of(bindings: list[dict]) -> list[dict]:
    return copy.deepcopy(bindings)
