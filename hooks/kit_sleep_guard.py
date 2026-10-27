#!/usr/bin/env python3
"""PreToolUse hook (ai-resources): refuse a long `sleep` in a Bash call (ADR-0014 D7).

A session that waits with `sleep` re-reads its whole context on every poll turn; the 2026-10 cost analysis found
poll turns to be a large share of the spend. A `sleep` longer than 30 seconds is refused with exit code 2 and a
message that points at the cheap ways to wait: `run_in_background` plus one read, or the Monitor tool. `sleep 30`
and shorter are allowed, as long as the sleeps of one command add up to at most 30 seconds and no
`while`/`until` loop waits by sleeping.

Standard library only, no I/O beyond stdin, and fail-open: malformed input, a non-Bash tool, a missing helper or any
internal error exits 0 so the guard can never block a session by being broken. The sleep definition lives in
`scripts/ai_resources/sleep_pattern.py`, loaded by path so the hook does not import the package.

Not enforced for OpenClaw `claude-cli` children (ADR-0005, ADR-0007 section 9); the AGENTS.md rules are the guard there.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "ai_resources" / "sleep_pattern.py"

MESSAGE = (
    "Blocked: `sleep {secs}` waits longer than 30 seconds inside a tool call, and every poll turn re-reads the whole "
    "context. Start the command with run_in_background and read its output once when it is done, or use the Monitor "
    "tool; keep any single wait at 30 seconds or less."
)

LOOP_MESSAGE = (
    "Blocked: a `while`/`until` loop that sleeps is an open-ended wait inside one tool call, and every poll turn "
    "re-reads the whole context. Run the command with `timeout <N>` in one blocking call, or start it with "
    "run_in_background and read its output once when it is done, or use the Monitor tool."
)


def _load_helper():
    spec = importlib.util.spec_from_file_location("kit_sleep_pattern", HELPER)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    try:
        data = json.loads(sys.stdin.read() or "null")
        if not isinstance(data, dict) or data.get("tool_name") != "Bash":
            return 0
        tool_input = data.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if not isinstance(command, str):
            return 0
        helper = _load_helper()
        if helper is None:
            return 0
        longest = helper.longest_sleep_seconds(command)
        total = helper.total_sleep_seconds(command)
        if longest is not None and longest > helper.MAX_ALLOWED_SECONDS:
            label = "infinity" if longest == float("inf") else f"{longest:g}s"
            print(MESSAGE.format(secs=label), file=sys.stderr)
            return 2
        if total is not None and total > helper.MAX_ALLOWED_SECONDS:
            print(MESSAGE.format(secs=f"{total:g}s in total"), file=sys.stderr)
            return 2
        if helper.has_wait_loop(command):
            print(LOOP_MESSAGE, file=sys.stderr)
            return 2
    except Exception:  # noqa: BLE001 - fail open: a broken guard must not block a session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
