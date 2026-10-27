"""The task ledger (`tasks.md`) is handed to workers by reference, so its ids and its log must not move under them.

Three checks, all pure and deterministic:

* `## Log` is append-only: every earlier line stays, in order, and new lines are `L#` entries.
* A `S#` is literal: its text is never rewritten. A change of scope appends one `L#` line and, when a criterion changes,
  a NEW `S#` that says `supersedes S3`; the old one stays as written.
* `Covered:` in a worker's return names only ids that were handed to it (a worker cannot claim work it was not given).

`ai-resources ledger-check` runs the first two against the committed version of the file (or `--against FILE`) and
`ai-resources covered-check` runs the third.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_SPEC = re.compile(r"^[ \t]*[-*][ \t]+\*\*(S\d+)\*\*", re.M)
_SPEC_LINE = re.compile(r"^[ \t]*[-*][ \t]+\*\*(S\d+)\*\*")
_TASK_LINE = re.compile(r"^[ \t]*[-*][ \t]+(?:\[[ xX]\][ \t]+)?\*\*(T\d+)\*\*[ \t]*\(covers[ \t]+([^)]*)\)")
_LOG_ENTRY = re.compile(r"^[ \t]*[-*][ \t]+L(\d+)\b")
ID = re.compile(r"\b(?:[TS]\d+(?:-[TS]\d+)?|RULE-[A-Z0-9][A-Z0-9-]*|TS-[A-Z0-9][A-Z0-9-]*)\b")


def sections(text: str) -> dict[str, list[str]]:
    """The lines of each `## ` section, keyed by its heading (the first one wins for a repeated heading)."""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
        elif current is not None:
            out[current].append(line)
    return out


def specs(text: str) -> dict[str, str]:
    """`S#` -> its whole line, from the `## Specs` section."""
    found: dict[str, str] = {}
    for line in sections(text).get("Specs", []):
        m = _SPEC_LINE.match(line)
        if m:
            found.setdefault(m.group(1), line.rstrip())
    return found


def log_lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in sections(text).get("Log", []) if ln.strip()]


def append_only_problems(old: str, new: str) -> list[str]:
    """Why `new` is not an append-only continuation of `old` (an empty list means it is)."""
    out: list[str] = []
    old_log, new_log = log_lines(old), log_lines(new)
    if "Log" in sections(old) and "Log" not in sections(new):
        out.append("## Log was removed")
    for i, line in enumerate(old_log):
        if i >= len(new_log) or new_log[i] != line:
            out.append(f"## Log line {i + 1} changed or was removed (the log is append-only): {line[:80]}")
            break
    else:
        last = max((int(m.group(1)) for ln in old_log if (m := _LOG_ENTRY.match(ln))), default=0)
        for line in new_log[len(old_log):]:
            m = _LOG_ENTRY.match(line)
            if not m:
                out.append(f"## Log: a new line must be an `L#` entry: {line[:80]}")
            elif int(m.group(1)) <= last:
                out.append(f"## Log: L{m.group(1)} does not follow L{last}")
            else:
                last = int(m.group(1))
    new_specs = specs(new)
    for sid, line in specs(old).items():
        if sid not in new_specs:
            out.append(f"{sid} was removed (supersede it with a new S#, never delete)")
        elif new_specs[sid] != line:
            out.append(f"{sid} was rewritten in place (append a new S# that supersedes it)")
    return out


def expand(token: str) -> list[str]:
    """`S3-S5` -> S3, S4, S5; any other id is returned as is."""
    m = re.fullmatch(r"([TS])(\d+)-(?:\1)?(\d+)", token)
    if m and int(m.group(2)) <= int(m.group(3)) <= int(m.group(2)) + 99:
        return [f"{m.group(1)}{n}" for n in range(int(m.group(2)), int(m.group(3)) + 1)]
    return [token]


def ids_in(text: str) -> set[str]:
    return {i for tok in ID.findall(text or "") for i in expand(tok)}


def handed_ids(task_ids, spec_ids, ledger: str | None = None) -> set[str]:
    """The ids a worker was given: the T#/S# ids named, plus the S# the named T# cover when the ledger says so."""
    handed = ids_in(" ".join([*map(str, task_ids or []), *map(str, spec_ids or [])]))
    if ledger:
        for line in sections(ledger).get("Tasks", []):
            m = _TASK_LINE.match(line)
            if m and m.group(1) in handed:
                handed |= ids_in(m.group(2))
    return handed


def covered_problems(covered: str, handed: set[str], ledger: str | None = None) -> list[str]:
    """Ids in `Covered:` that were not handed out (and, with a ledger, ids it does not define). Nothing handed: nothing to check."""
    if not handed:
        return []
    claimed = ids_in(covered)
    out = []
    stray = sorted(claimed - handed)
    if stray:
        out.append(f"Covered names ids that were not handed out: {', '.join(stray)}")
    if ledger:
        known = set(specs(ledger)) | {m.group(1) for ln in sections(ledger).get("Tasks", []) if (m := _TASK_LINE.match(ln))}
        gone = sorted(i for i in claimed & handed if re.fullmatch(r"[TS]\d+", i) and i not in known)
        if gone:
            out.append(f"Covered names ids the ledger does not define: {', '.join(gone)}")
    return out


def covered_value(message: str) -> str:
    """The text of the `Covered:` field of a return block, or an empty string."""
    m = re.search(r"^[ \t>*_-]*Covered\**:\**[ \t]*(.*)$", message or "", re.M | re.I)
    return m.group(1) if m else ""


def _committed(path: Path) -> str | None:
    """The version of `path` in HEAD, or None when it is not tracked."""
    r = subprocess.run(["git", "show", f"HEAD:./{path.name}"], cwd=path.resolve().parent, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def check_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    against = None
    if "--against" in args:
        i = args.index("--against")
        if i + 1 >= len(args):
            print("usage: ledger-check <tasks.md> [--against FILE]", file=sys.stderr)
            return 2
        against = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1:
        print("usage: ledger-check <tasks.md> [--against FILE]", file=sys.stderr)
        return 2
    path = Path(args[0])
    try:
        new = path.read_text(encoding="utf-8")
        old = Path(against).read_text(encoding="utf-8") if against else _committed(path)
    except OSError as e:
        print(f"{path}: cannot read: {e.strerror or e}", file=sys.stderr)
        return 1
    if old is None:
        print(f"{path}: not committed yet, nothing to compare")
        return 0
    found = append_only_problems(old, new)
    for line in found:
        print(f"{path}: {line}", file=sys.stderr)
    if not found:
        print(f"{path}: ledger OK")
    return 1 if found else 0


def covered_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    def pop(flag: str) -> str | None:
        if flag in args:
            i = args.index(flag)
            val = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
            return val
        return None
    handed, covered, ledger = pop("--handed"), pop("--covered"), pop("--ledger")
    if handed is None or covered is None or args:
        print("usage: covered-check --handed 'T2,S3' --covered 'S3' [--ledger tasks.md]", file=sys.stderr)
        return 2
    text = Path(ledger).read_text(encoding="utf-8") if ledger else None
    found = covered_problems(covered, handed_ids([handed], [], text), text)
    for line in found:
        print(f"covered-check: {line}", file=sys.stderr)
    if not found:
        print("covered-check: OK")
    return 1 if found else 0
