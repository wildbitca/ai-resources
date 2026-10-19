#!/usr/bin/env python3
"""PreToolUse hook (ai-resources): no agent session writes the Antigravity gate evidence record.

`scripts/ai_resources/data/agy_gate_evidence.json` is the one file that can turn the Antigravity lane gates from "not
proven" to "proven" (ADR-0010 PD-7). It is made only by `ai-resources lanes gates record`, which an operator runs on a
terminal, and then committed by the operator. This hook denies a Write, Edit, MultiEdit or NotebookEdit aimed at it and a
Bash command that writes to it (a redirect, tee, sed -i, rm, truncate, cp/mv/install/dd onto it, git checkout/restore/
apply, an interpreter's file write). It applies in EVERY agent session, not only lane children; there is no agent id test.
Reading the file is always allowed.

It is an accident guard, not a security boundary (the same standing as the other kit hooks): it cannot see a command built
at run time. The loader fails closed on a file that is untracked or differs from HEAD, so a write that gets past the
hook still needs the operator's commit to count. Any internal error exits 0: a guard that denies on its own failure is
worse than no guard.
"""
from __future__ import annotations

import json
import os
import re
import sys

NAME = "agy_gate_evidence.json"
REL = "scripts/ai_resources/data/" + NAME
WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
MESSAGE = (
    "agy_gate_evidence.json is the Antigravity gate evidence record: no agent session writes it. The operator makes it "
    "with `ai-resources lanes gates record --run-dir <window dir>` on a terminal and commits it (ADR-0010 PD-7). "
    "Reading it is fine.\n"
)


def _is_record(path: str, cwd: str) -> bool:
    if not path:
        return False
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))
    for candidate in (full, os.path.realpath(full)):
        if candidate.endswith("/" + REL) or candidate == REL:
            return True
        if os.path.basename(candidate) == NAME and os.path.basename(os.path.dirname(candidate)) == "data":
            return True
    return False


_NAME = r"['\"]?[^\s'\"]*" + re.escape(NAME) + r"['\"]?"
_WRITES = (
    r"(?<![0-9&<])>>?\s*" + _NAME,                                   # > file, >> file (not 2>, &>)
    r"\btee\b[^|;&\n]*\s" + _NAME,                                   # tee [-a] file
    r"\bsed\b[^|;&\n]*\s-i\S*[^|;&\n]*\s" + _NAME,                   # sed -i ... file
    r"\bperl\b[^|;&\n]*\s-\S*i\S*[^|;&\n]*\s" + _NAME,              # perl -i ... file
    r"\b(?:rm|truncate|shred|unlink)\b[^|;&\n]*\s" + _NAME,          # rm / truncate file
    r"\b(?:cp|mv|install|ln|rsync)\b[^|;&\n]*\s" + _NAME,            # onto (or from, conservatively) the file
    r"\bdd\b[^|;&\n]*\bof=\S*" + re.escape(NAME),                    # dd of=file
    r"\bgit\b[^|;&\n]*\b(?:checkout|restore|apply|rm|mv|stash|reset|cherry-pick|merge|rebase)\b[^|;&\n]*"
    + re.escape(NAME),
)
_INTERPRETER = re.compile(
    r"\b(?:python3?|node|ruby|perl|php)\b[^\n]*" + re.escape(NAME)
    + r"|" + re.escape(NAME) + r"[^\n]*\b(?:write_text|write_bytes|writeFile|open\()")


def _bash_writes(command: str) -> bool:
    if NAME not in command:
        return False
    if any(re.search(p, command) for p in _WRITES):
        return True
    # An interpreter or a write call that names the file: python -c "open('...agy_gate_evidence.json','w')..."
    if _INTERPRETER.search(command):
        return bool(re.search(r"""\bopen\([^)]*['"][wax+]|write_text|write_bytes|writeFile|\bwrite\(|json\.dump""", command))
    return False


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        cwd = str(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
        tool = data.get("tool_name")
        tool_input = data.get("tool_input") or {}
        if tool in WRITE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            hit = _is_record(str(target), cwd)
        elif tool == "Bash":
            hit = _bash_writes(str(tool_input.get("command", "")))
        else:
            return 0
    except Exception:  # noqa: BLE001 - a guard that fails must not block work
        return 0
    if not hit:
        return 0
    sys.stderr.write(MESSAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
