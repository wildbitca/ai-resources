#!/usr/bin/env python3
"""PreToolUse hook (ai-resources): no agent session writes the Antigravity gate evidence record.

`scripts/ai_resources/data/agy_gate_evidence.json` is the one file that can turn the Antigravity lane gates from "not
proven" to "proven" (ADR-0010 PD-7). It is made only by `ai-resources lanes gates record`, which an operator runs on a
terminal, and then committed by the operator. This hook denies a Write, Edit, MultiEdit or NotebookEdit aimed at it and a
Bash command that writes to it (a redirect, tee, sed -i, rm, truncate, cp/mv/install/dd onto it, git checkout/restore/
apply, an interpreter's file write). It applies in EVERY agent session, not only lane children; there is no agent id test.
Reading the file is always allowed. Also denied (ADR-0010 N1): git commands that name the record or the data directory,
any `lanes gates record` command, numeric redirects, glob spellings, patches that name the file and inline interpreter
scripts that mention or build the name. The operator commits the record from their own terminal.

It is an accident guard, not a security boundary (the same standing as the other kit hooks): it cannot see a command built
at run time. The loader fails closed on a file that is untracked or differs from HEAD, so a write that gets past the
hook still needs the operator's commit to count. Any internal error exits 0: a guard that denies on its own failure is
worse than no guard.
"""
from __future__ import annotations

import fnmatch
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


_NAME = r"[^\s'\"]*" + re.escape(NAME) + r"['\"]?"
_DATA_DIR = "ai_resources/data"
_WRITES = (
    r">>?[|&]?\s*" + _NAME,                                         # > file, >> file, 1> file, 2>> file, &> file
    r"\btee\b[^|;&\n]*\s" + _NAME,                                   # tee [-a] file
    r"\bsed\b[^|;&\n]*\s-i\S*[^|;&\n]*\s" + _NAME,                   # sed -i ... file
    r"\bperl\b[^|;&\n]*\s-\S*i\S*[^|;&\n]*\s" + _NAME,              # perl -i ... file
    r"\b(?:rm|truncate|shred|unlink)\b[^|;&\n]*\s" + _NAME,          # rm / truncate file
    r"\b(?:cp|mv|install|ln|rsync)\b[^|;&\n]*\s" + _NAME,            # onto (or from, conservatively) the file
    r"\bdd\b[^|;&\n]*\bof=\S*" + re.escape(NAME),                    # dd of=file
)
_GIT_MUTATING = (r"add|commit|apply|am|checkout|switch|merge|restore|mv|rm|cherry-pick|stash|reset|rebase|revert")
_GIT_CMD = re.compile(r"\bgit\b[^\n]*?\b(?:" + _GIT_MUTATING + r")\b")
_GIT_APPLY = re.compile(r"\bgit\b[^\n]*?\bapply\b|(?:^|[|;&(]|\bsudo)\s*patch\b")
_PATCH_SEGMENT = re.compile(r"(?:\bgit\b[^\n;|&]*?\b(?:apply|am)\b|(?:^|[|;&(]|\bsudo)\s*patch\b)([^\n;|&]*)")
_RECORD_CMD = re.compile(r"\blanes\s+(?:-\S+\s+)*gates\s+(?:-\S+\s+)*record\b")
_INLINE_INTERPRETER = re.compile(
    r"\b(?:python[0-9.]*|node|nodejs|deno|bun|ruby|perl|php|lua)\b[^\n]*?(?:\s-[a-zA-Z]*[ceEpr]\b|\s--eval\b|\s-\s|<<|\s-$)"
    r"|\|\s*(?:python[0-9.]*|node|perl|ruby|php)\b")
_INTERPRETER = re.compile(
    r"\b(?:python3?|node|ruby|perl|php)\b[^\n]*" + re.escape(NAME)
    + r"|" + re.escape(NAME) + r"[^\n]*\b(?:write_text|write_bytes|writeFile|open\()")
_WORD = re.compile(r"[^\s;|&<>()]+")
_MARKERS = ("agy", "gate", "evid", "idence")


def _expand_braces(word: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", word)
    if not m:
        return [word]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(word[:m.start()] + alt + word[m.end():]))
        if len(out) > 64:
            break
    return out


def _glob_hits_record(base: str) -> bool:
    """A glob word (`agy_gate_*`, `agy_gate_evidence.js?n`, `agy_gate_evidenc[e].json`) that would expand to the record.
    A bare `*` or `*.json` does not count: the glob must carry a literal run that belongs to the file name."""
    if not any(c in base for c in "*?["):
        return NAME in base
    if not fnmatch.fnmatchcase(NAME, base):
        return False
    segments = re.split(r"\[[^\]]*\]|[*?]", base)
    return any(seg in NAME and (len(seg) >= 6 or any(m in seg for m in _MARKERS)) for seg in segments)


def _normalise(command: str) -> str:
    """The command with quotes and backslashes removed (so `agy_""gate` joins up) and every glob or brace word that
    would expand to the record replaced by the literal file name, so the literal-name rules below see it."""
    text = re.sub(r"[\\'\"]", "", command)

    def repl(m: re.Match) -> str:
        word = m.group(0)
        if NAME in word or not any(c in word for c in "*?[{"):
            return word
        for cand in _expand_braces(word):
            if _glob_hits_record(cand.rsplit("/", 1)[-1]):
                return NAME
        return word

    return _WORD.sub(repl, text)


def _patch_names_record(command: str, cwd: str) -> bool:
    """`git apply x.patch` / `patch < x.patch`: best effort. Read only the patch inputs (the arguments of the patch or
    git apply command and its `<` redirect target), never every token of the command line."""
    text = re.sub(r"[\\'\"]", "", command)
    for m in _PATCH_SEGMENT.finditer(text):
        for tok in re.split(r"[\s<>()]+", m.group(1)):
            if not tok or tok.startswith("-"):
                continue
            path = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
            try:
                if os.path.isfile(path) and os.path.getsize(path) < 2_000_000:
                    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                        if NAME in fh.read():
                            return True
            except OSError:
                continue
    return False


def _bash_writes(command: str, cwd: str = "") -> bool:
    if _RECORD_CMD.search(re.sub(r"[,\[\]()]", " ", re.sub(r"[\\'\"]", "", command))):
        return True                                   # the TTY-only record command, wrapped or not
    variants = (command, _normalise(command))
    joined = re.sub(r"[\\'\"+\s]", "", command)
    touches_data = _DATA_DIR in variants[1] or NAME in variants[1]
    if touches_data and any(_GIT_CMD.search(v) for v in variants):
        return True                                   # git add/commit/apply/... naming the record or the data dir
    if NAME in variants[1] and any(_GIT_APPLY.search(v) for v in variants):
        return True                                   # a patch (even a heredoc) that names the record
    if cwd and _GIT_APPLY.search(command) and _patch_names_record(command, cwd):
        return True
    if _INLINE_INTERPRETER.search(command) and ("agy_gate" in joined or "evidence.json" in joined):
        return True                                   # an inline script that names the file or builds the name
    for v in variants:
        if NAME not in v:
            continue
        if any(re.search(p, v) for p in _WRITES):
            return True
        # An interpreter or a write call that names the file: python -c "open('...agy_gate_evidence.json','w')..."
        if _INTERPRETER.search(v) and re.search(
                r"""\bopen\([^)]*['"][wax+]|write_text|write_bytes|writeFile|\bwrite\(|json\.dump""", v):
            return True
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
            hit = _bash_writes(str(tool_input.get("command", "")), cwd)
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
