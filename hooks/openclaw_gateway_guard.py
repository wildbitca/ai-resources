#!/usr/bin/env python3
"""PreToolUse hook (ai-resources, OpenClaw hosts): stop an undrained gateway stop (pitfall T01).

Denies exactly three shapes of Bash command, and only while the gateway is live and unguarded
(the unit is active AND ~/.openclaw/watchdog.off does not exist):

  - `openclaw doctor --fix`
  - `systemctl --user stop openclaw-gateway.service`
  - `openclaw gateway restart` (ADR-0004, D10): from an agent session that is a child of the
    gateway it kills the session itself (T29) and cuts every run in flight. The sanctioned way
    is `ai-resources openclaw graceful-restart`, run from outside the gateway cgroup.

Why: `openclaw doctor --fix` stops the gateway and re-inspects the stopped unit. If a child
(claude, engram, npx) is still alive it aborts with "ownership or manager identity changed" and
leaves the gateway DOWN, with the watchdog free to fight the stop. On 2026-09-18 that cost half
an hour. `ai-resources openclaw doctor` does it in the one safe order.

Precision matters more than coverage. A deny aborts the WHOLE tool call, not the offending
command (T24), and the kit's own docs are full of the literal strings above. So the command is
tokenised as a shell would and only a command in COMMAND POSITION is considered: a `grep`, an
`echo "..."`, a heredoc body or a file edit never matches. Any internal error exits 0: a guard
that denies on its own failure is worse than no guard.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

UNIT = "openclaw-gateway.service"
WATCHDOG_OFF = os.path.expanduser("~/.openclaw/watchdog.off")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# Tokens that only wrap another command: look through them to the real one.
_WRAPPERS = {"sudo", "env", "command", "nohup", "time", "exec", "nice", "ionice", "stdbuf"}
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!"}
_SHELLS = {"bash", "sh", "zsh", "dash"}
_OPERATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "{", "}"}

REASON = (
    "Blocked (T01): stopping the OpenClaw gateway without draining it can leave it DOWN. "
    "Run `ai-resources openclaw doctor` instead: it pauses the watchdog, waits for the "
    "cgroup to drain, runs `openclaw doctor --fix`, then starts the gateway again and checks "
    "it answers. (Open a maintenance window yourself with `touch ~/.openclaw/watchdog.off` "
    "and this check steps aside.)\n"
)


REASON_RESTART = (
    "Blocked (T29): `openclaw gateway restart` from an agent call restarts the gateway under every run in "
    "flight, and when this session is a child of the gateway it kills this session too. Run "
    "`ai-resources openclaw graceful-restart` from outside the gateway (it is gated and notified), or "
    "open a maintenance window yourself with `touch ~/.openclaw/watchdog.off` and this check steps aside.\n"
)


def strip_heredocs(command: str) -> str:
    """Remove heredoc bodies: text fed to a command is not a command."""
    out: list[str] = []
    pending: list[str] = []
    for line in command.splitlines():
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC.finditer(line))
    return "\n".join(out)


def _statements(line: str) -> list[list[str]]:
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    tokens = list(lex)  # ValueError on unbalanced quotes: the caller fails open
    stmts: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= set(";&|(){}"):
            stmts.append([])
        else:
            stmts[-1].append(tok)
    return [s for s in stmts if s]


def _command_words(stmt: list[str]) -> list[str]:
    """The statement with leading VAR=x assignments and transparent wrappers removed."""
    i = 0
    while i < len(stmt):
        tok = stmt[i]
        base = os.path.basename(tok)
        if _ASSIGNMENT.match(tok) or tok in _KEYWORDS:
            i += 1
        elif base == "timeout":
            # `timeout [-s SIG] [-k D] DURATION cmd`: look through to the real command.
            i += 1
            while i < len(stmt) and stmt[i].startswith("-"):
                i += 2 if stmt[i] in ("-s", "-k", "--signal", "--kill-after") else 1
            i += 1                      # the duration
        elif base in _WRAPPERS:
            i += 1
            # `sudo -u x`, `env -i`, `nice -n 5`: skip the flags (and a flag's value)
            while i < len(stmt) and stmt[i].startswith("-"):
                i += 2 if stmt[i] in ("-u", "-n", "-g") else 1
        else:
            break
    return stmt[i:]


def _is_doctor_fix(words: list[str]) -> bool:
    return (bool(words) and os.path.basename(words[0]) == "openclaw"
            and "doctor" in words[1:3] and "--fix" in words[1:])


def _is_gateway_stop(words: list[str]) -> bool:
    return (bool(words) and os.path.basename(words[0]) == "systemctl" and "stop" in words[1:]
            and any(w in (UNIT, UNIT[: -len(".service")]) for w in words[1:]))


def _is_gateway_restart(words: list[str]) -> bool:
    if not words or os.path.basename(words[0]) != "openclaw":
        return False
    rest = words[1:]
    return "gateway" in rest[:4] and rest.index("gateway") + 1 < len(rest) \
        and rest[rest.index("gateway") + 1] == "restart"


def shape(command: str, _depth: int = 0) -> str | None:
    """"doctor", "stop" or "restart" when the command RUNS a guarded shape, else None."""
    if _depth > 2:
        return None
    for line in strip_heredocs(command).splitlines():
        try:
            statements = _statements(line)
        except ValueError:
            continue
        for stmt in statements:
            words = _command_words(stmt)
            if _is_doctor_fix(words):
                return "doctor"
            if _is_gateway_stop(words):
                return "stop"
            if _is_gateway_restart(words):
                return "restart"
            # `bash -c "openclaw doctor --fix"`: the string IS a command.
            if words and os.path.basename(words[0]) in _SHELLS and "-c" in words[1:]:
                idx = words.index("-c") + 1
                if idx < len(words):
                    inner = shape(words[idx], _depth + 1)
                    if inner:
                        return inner
    return None


def dangerous(command: str, _depth: int = 0) -> bool:
    """True when the command RUNS one of the guarded shapes."""
    return shape(command, _depth) is not None


def gateway_active() -> bool:
    """True only when systemd says the unit is active; anything unclear counts as not active."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env["XDG_RUNTIME_DIR"] = runtime
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True,
                           text=True, timeout=5, env=env)
    except Exception:
        return False
    return r.returncode == 0 and r.stdout.strip() == "active"


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or data.get("tool_name") != "Bash":
            return 0
        command = str((data.get("tool_input") or {}).get("command", ""))
        if not command or not dangerous(command):
            return 0
        found = shape(command)
        if os.path.exists(WATCHDOG_OFF) or not gateway_active():
            return 0
    except Exception:
        return 0
    sys.stderr.write(REASON_RESTART if found == "restart" else REASON)
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
