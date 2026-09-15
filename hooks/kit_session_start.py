#!/usr/bin/env python3
"""SessionStart hook (ai-resources): report workflow artifacts left in the project.

Plain stdout from a SessionStart hook is added to the session context. The hook is
silent when the project has no handoff files, so ordinary sessions pay nothing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STALE_AFTER_HOURS = 24


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd())
    out_dir = root / ".agent-output"
    if not out_dir.is_dir():
        return 0

    now = time.time()
    lines: list[str] = []
    for handoff in sorted(out_dir.glob("handoff-*.md")):
        hours = (now - handoff.stat().st_mtime) / 3600
        rel = handoff.relative_to(root)
        if hours >= STALE_AFTER_HOURS:
            lines.append(f"- STALE ({hours / 24:.0f} days old): {rel} — resume that workflow or delete "
                         "the file; handoffs are deleted when a workflow finishes.")
        else:
            lines.append(f"- In progress ({hours:.0f} h old): {rel} — read it before starting related work.")

    lock = out_dir / "parallel-group.json"
    if lock.is_file():
        lines.append(f"- {lock.relative_to(root)} exists — delete it unless a parallel workflow group "
                     "is still running (it blocks subagents from writing the handoff).")

    if lines:
        print("ai-resources: workflow artifacts found in this project:")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
