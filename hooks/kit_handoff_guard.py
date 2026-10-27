#!/usr/bin/env python3
"""PreToolUse hook (ai-resources): stop parallel workflow steps from writing the shared handoff.

While `.agent-output/parallel-group.json` exists, a subagent (input has `agent_id`) may not
write the handoff file named in it. The main thread is never blocked: it performs the join.
A lock older than LOCK_MAX_AGE_HOURS is ignored so a crashed run cannot block work forever.
Bash detection is a heuristic that only matches the handoff as a write target: redirect
target, tee/sed -i/rm/truncate argument, or the last cp/mv argument. Reading is allowed.

It also enforces read-only mode (skills/kit-orchestration "Authorize first"): while
`.agent-output/readonly.lock` exists (`ai-resources readonly on`) or AI_RESOURCES_READONLY is set,
the main thread and subagents alike are refused file-writing tools outside `.agent-output/` and
Bash commands that plainly write (redirect to a file, tee, sed -i, rm/mv/cp/mkdir/touch/chmod/ln,
git commit/push/merge/reset/checkout/..., package installs). The Bash side is a best-effort
deny-list, not a sandbox; the tool side is exact.
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


_WRITING_BASH = (
    r"(?:^|[\s;|&(])&?>>?\s*(?!/dev/(?:null|stderr|stdout)\b|&)\S",           # > file, >> file, &> file (not 2>, >&2, ->)
    r"\btee\b",
    r"\bsed\b[^|;&]*\s-i",
    r"(?:^|[|;&(]\s*)(?:sudo\s+)?(?:rm|mv|cp|mkdir|rmdir|touch|truncate|chmod|chown|ln|install|dd)\b",
    r"\bgit(?:\s+-\S+(?:\s+[/.~]\S*)?)*\s+(?:add|commit|push|merge|rebase|reset|checkout|switch|stash|apply|am|cherry-pick|clean|restore|rm|mv|tag|pull|fetch|worktree)\b",
    r"\b(?:npm|pnpm|yarn|pip3?|brew|apt(?:-get)?)\s+(?:install|add|remove|uninstall|upgrade)\b",
)
READONLY_LOCK_MAX_AGE_HOURS = 12


def _readonly_reason(root: Path) -> str | None:
    """Why the session is read-only (the lock's reason, or a default), else None. An old lock is ignored."""
    if os.environ.get("AI_RESOURCES_READONLY", "") not in ("", "0", "false", "no"):
        return "AI_RESOURCES_READONLY is set"
    lock = root / ".agent-output" / "readonly.lock"
    try:
        if time.time() - lock.stat().st_mtime > READONLY_LOCK_MAX_AGE_HOURS * 3600:
            return None
        info = json.loads(lock.read_text(encoding="utf-8") or "{}")
    except OSError:
        return None
    except ValueError:
        info = {}
    return str(info.get("reason") or "investigate/explain/compare/propose request") if isinstance(info, dict) else "read-only request"


def _readonly_blocks(data: dict, root: Path) -> bool:
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    if tool in WRITE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not target:
            return False
        out_dir = _norm(".agent-output", root) + os.sep
        return not (_norm(target, root) + os.sep).startswith(out_dir)
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        if re.match(r"\s*ai-resources\s+readonly\b", command):
            return False
        return any(re.search(p, command) for p in _WRITING_BASH)
    return False


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    why = _readonly_reason(root)
    if why and _readonly_blocks(data, root):
        sys.stderr.write(
            f"Read-only mode ({why}): this request has not been authorized to change anything, so writes are refused. "
            "Answer, explain or propose instead; when the user asks for the change, run `ai-resources readonly off` and continue.\n")
        return 2
    if not data.get("agent_id"):
        return 0

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
