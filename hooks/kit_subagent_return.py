#!/usr/bin/env python3
"""SubagentStop hook (ai-resources): require the kit return block during a workflow.

Applies only to kit role subagents (agents/roles/*.md) working on a workflow step: the
project has a handoff file updated in the last 24 hours and the subagent's prompt names a
handoff file or `kit-orchestration`. If the final message lacks the `## Result` / `## Routing`
block, exit code 2 keeps the subagent running and tells it what to add. `stop_hook_active`
prevents loops.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REQUIRED_HEADINGS = ("## Result", "## Routing")
ROLES_DIR = Path(__file__).resolve().parents[1] / "agents" / "roles"
FRESH_HOURS = 24
GENERIC_ROLES = ("explore", "generalPurpose")  # also used for ad-hoc work outside workflows


def _first_user_text(transcript: str | None) -> str | None:
    if not transcript:
        return None
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message") if isinstance(entry, dict) else None
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
                if texts:
                    return "\n".join(texts)
    except OSError:
        return None
    return None


def _kit_roles() -> set[str]:
    return {p.stem for p in ROLES_DIR.glob("*.md")}


def _last_assistant_text(transcript: str | None) -> str | None:
    if not transcript:
        return None
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") or {}
        if entry.get("type") != "assistant" and msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict) or data.get("stop_hook_active"):
        return 0
    agent = str(data.get("agent_type") or "").split(":")[-1]
    if agent not in _kit_roles():
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    now = time.time()
    fresh = [h for h in (root / ".agent-output").glob("handoff-*.md")
             if now - h.stat().st_mtime < FRESH_HOURS * 3600]
    if not fresh:
        return 0  # no workflow running

    prompt = _first_user_text(data.get("agent_transcript_path"))
    if prompt is not None:
        if not ("kit-orchestration" in prompt or ".agent-output/handoff" in prompt
                or any(h.name in prompt for h in fresh)):
            return 0  # ad-hoc subagent, not a workflow step
    elif agent in GENERIC_ROLES:
        return 0

    message = data.get("last_assistant_message")
    if message is None:
        message = _last_assistant_text(data.get("agent_transcript_path"))
    if message is None:
        return 0

    missing = [h for h in REQUIRED_HEADINGS if h not in message]
    if not missing:
        return 0
    sys.stderr.write(
        f"Before stopping, end your final message with the kit return block (missing: {', '.join(missing)}):\n"
        "## Result (Status, Executive summary, Summary, Handoff)\n"
        "## Artifacts (Files touched, Commands run, Specs/docs read)\n"
        "## Routing (Next recommended, Blocked, Risks)\n"
        "Update the handoff file first if you have not. Full format: the kit-orchestration skill.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
