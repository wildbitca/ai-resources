"""`ai-resources readonly on|off|status`: the read-only lock that hooks/kit_handoff_guard.py enforces.

Investigate, explain, compare and propose requests are read-only (skills/kit-orchestration "Authorize first"). The lock turns that
instruction into a refusal of file writes outside `.agent-output/` and of plainly writing Bash commands, for the main session and
subagents alike, until `readonly off`. A lock older than 12 hours is ignored by the hook, so a forgotten one cannot block work for good.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

USAGE = "usage: readonly on [--reason TEXT] [--dir PATH] | off [--dir PATH] | status [--dir PATH]"


def lock_path(directory: str | Path | None = None) -> Path:
    return Path(directory or ".") / ".agent-output" / "readonly.lock"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    opts: dict[str, str] = {}
    for flag in ("--reason", "--dir"):
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                print(USAGE, file=sys.stderr)
                return 2
            opts[flag] = args[i + 1]
            del args[i:i + 2]
    if len(args) != 1 or args[0] not in ("on", "off", "status"):
        print(USAGE, file=sys.stderr)
        return 2
    path = lock_path(opts.get("--dir"))
    if args[0] == "on":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"since": int(time.time()), "reason": opts.get("--reason", "")}), encoding="utf-8")
        print(f"read-only on ({path})")
    elif args[0] == "off":
        path.unlink(missing_ok=True)
        print("read-only off")
    else:
        print(f"read-only {'on' if path.is_file() else 'off'}")
    return 0
