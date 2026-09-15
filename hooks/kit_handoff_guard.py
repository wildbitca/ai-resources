#!/usr/bin/env python3
"""PreToolUse hook (ai-resources): stop parallel workflow steps from writing the shared handoff.

While `.agent-output/parallel-group.json` exists, a subagent (input has `agent_id`) may not
write the handoff file named in it. The main thread is never blocked: it performs the join.
A lock older than LOCK_MAX_AGE_HOURS is ignored so a crashed run cannot block work forever.
Bash detection is a heuristic that only matches the handoff as a write target: redirect
target, tee/sed -i/rm/truncate argument, or the last cp/mv argument. Reading is allowed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

LOCK_MAX_AGE_HOURS = 6
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _norm(path: str, root: Path) -> str:
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))


def _bash_writes(command: str, filename: str) -> bool:
    name = r"['\"]?\S*" + re.escape(filename) + r"['\"]?"
    end = r"(?=\s*(?:$|[|;&)]))"
    patterns = (
        r"(?<![0-9&<])>>?\s*" + name,                      # > file, >> file (not 2>, &>, <>)
        r"\btee\b(?:\s+-\S+)*(?:\s+\S+)*?\s+" + name,       # tee [-a] ... file
        r"\bsed\b[^|;&]*\s-i\S*[^|;&]*\s" + name,           # sed -i ... file
        r"\b(?:rm|truncate)\b[^|;&]*\s" + name,             # rm/truncate ... file
        r"\b(?:cp|mv)\b[^|;&]*\s" + name + end,             # cp/mv ... file (destination)
    )
    return any(re.search(p, command) for p in patterns)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict) or not data.get("agent_id"):
        return 0

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    lock = root / ".agent-output" / "parallel-group.json"
    try:
        if time.time() - lock.stat().st_mtime > LOCK_MAX_AGE_HOURS * 3600:
            return 0
        info = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    handoff = info.get("handoff_file") if isinstance(info, dict) else None
    if not handoff:
        return 0

    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    if tool in WRITE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        hit = bool(target) and _norm(target, root) == _norm(handoff, root)
    elif tool == "Bash":
        hit = _bash_writes(str(tool_input.get("command", "")), Path(handoff).name)
    else:
        return 0

    if not hit:
        return 0
    sys.stderr.write(
        f"{handoff} is locked while parallel group '{info.get('group', '?')}' runs. "
        "Reading it is fine, but do not write to it: end your own report under .agent-output/<role>/ with the handoff fields; "
        "the orchestrator copies them into the handoff when the group finishes.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
