#!/usr/bin/env python3
"""PreToolUse hook (ai-resources, OpenClaw hosts): stop an undrained gateway stop (pitfall T01).

Denies three families of Bash command, and only while the gateway is live and unguarded
(the unit is active AND ~/.openclaw/watchdog.off does not exist):

  - `openclaw doctor --fix`
  - stopping the gateway: `systemctl [--user] stop|kill openclaw-gateway[.service]` and
    `openclaw gateway stop`
  - restarting it (ADR-0004, D10): `openclaw gateway restart`, `systemctl [--user]
    restart|try-restart|reload-or-restart openclaw-gateway[.service]`. From an agent session that
    is a child of the gateway it kills the session itself (T29) and cuts every run in flight. The
    sanctioned way is `ai-resources openclaw graceful-restart`, run from outside the gateway cgroup.

The recogniser is token based: options may sit between the words (`openclaw gateway --json
restart`, `systemctl --user --no-block restart ...`) and the command may be wrapped in `sudo`,
`env`, `timeout`, `nohup`, `setsid`, `bash -c`, `sh -c` or `eval`. Read-only verbs (`status`,
`show`, `is-active`, `list-units`, `logs`, `cat`, ...) are never matched.

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
_WRAPPERS = {"sudo", "doas", "env", "command", "nohup", "setsid", "time", "exec", "nice", "ionice", "stdbuf",
             "unbuffer"}
# systemctl verbs that stop or restart a unit. Everything else (status, show, is-active, list-units,
# cat, reload, start, ...) is read-only or harmless for this guard.
_SYSTEMCTL_STOP = {"stop", "kill"}
_SYSTEMCTL_RESTART = {"restart", "try-restart", "reload-or-restart", "try-reload-or-restart", "condrestart"}
# Option tables. A short option takes a value when its letter is in the SHORT set (`-p MainPID`, `-pMainPID`,
# or last in a cluster: `-ap MainPID`); every other letter is a flag, so `-T` or `-aT` never swallows the
# verb. A long option takes the NEXT word only when it is in the LONG set and has no `=`. Verified against
# `systemctl --help`, `openclaw gateway --help`, `openclaw --help`, `sudo -h`, `env --help`, `timeout --help`.
#
# systemctl: value-taking short options are -C -H -M -t -p -P -s -n -o. Flags (never take a value):
# -a -l -r -f -q -v -i -T -h. `-T` is --show-transaction (boolean); `--kill-whom` has no short form.
_SYSTEMCTL_SHORT_VALUE = frozenset("CHMtpPsno")
_SYSTEMCTL_LONG_VALUE = frozenset({
    "--host", "--machine", "--capsule", "--type", "--state", "--property", "--job-mode", "--check-inhibitors",
    "--signal", "--kill-whom", "--kill-who", "--kill-value", "--kill-subgroup", "--what", "--message",
    "--legend", "--preset-mode", "--root", "--image", "--image-policy", "--lines", "--output",
    "--boot-loader-menu", "--boot-loader-entry", "--reboot-argument", "--timestamp", "--drop-in", "--when"})
# `openclaw gateway` subcommands: the first one found after `gateway` is the verb, so a flag VALUE such
# as `--port 18789` is skipped and a later word (`status --note restart`) is not taken for the verb.
_GATEWAY_VERBS = {"restart", "stop", "start", "status", "install", "uninstall", "health", "probe", "call",
                   "discover", "run", "logs", "usage-cost"}
# `openclaw gateway` options with a value (the only short option is -h, a flag) and the global options
# placed before `gateway` (`--profile x`, `--log-level y`, `--container c`).
_GATEWAY_LONG_VALUE = frozenset({"--auth", "--bind", "--password", "--password-file", "--port",
                                 "--raw-stream-path", "--tailscale", "--token", "--ws-log"})
_OPENCLAW_GLOBAL_LONG_VALUE = frozenset({"--container", "--profile", "--log-level"})
# Flags of a wrapper that take a separate value (`sudo -u root`, `nice -n 5`); `sudo -n` takes none.
# base -> (short letters with a value, long options with a value).
_WRAPPER_FLAGS: dict[str, tuple[frozenset, frozenset]] = {
    "sudo": (frozenset("ugCDRrTtUhp"), frozenset({"--user", "--group", "--host", "--prompt", "--chdir",
                                                    "--chroot", "--role", "--type", "--other-user",
                                                    "--close-from", "--command-timeout"})),
    "doas": (frozenset("uC"), frozenset()),
    "env": (frozenset("uCSaPf"), frozenset({"--unset", "--chdir", "--split-string", "--argv0", "--file"})),
    "nice": (frozenset("n"), frozenset({"--adjustment"})),
    "ionice": (frozenset("cnpPu"), frozenset({"--class", "--classdata", "--pid", "--pgid", "--uid"})),
    "stdbuf": (frozenset("ioe"), frozenset({"--input", "--output", "--error"})),
    "time": (frozenset("fo"), frozenset({"--format", "--output"})),
    "exec": (frozenset("a"), frozenset()),
    "timeout": (frozenset("sk"), frozenset({"--signal", "--kill-after"})),
}
_NO_VALUES: tuple[frozenset, frozenset] = (frozenset(), frozenset())
# Shell options with a value that may sit before `-c` (`bash -o pipefail -c '...'`, `bash --rcfile f -c`).
_SHELL_SHORT_VALUE = frozenset("oO")
_SHELL_LONG_VALUE = frozenset({"--rcfile", "--init-file"})
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
    "Blocked (T29): restarting the gateway (`openclaw gateway restart`, `systemctl restart`) from an agent "
    "call restarts it under every run in "
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


def _option_width(tok: str, short_value, long_value) -> int:
    """How many words the option `tok` occupies: 2 when its value is the next word, else 1."""
    if tok.startswith("--"):
        if "=" in tok:
            return 1
        # getopt accepts an unambiguous prefix (`--prop Foo`), so a prefix of a value option counts too.
        if tok in long_value or (len(tok) >= 4 and any(o.startswith(tok) for o in long_value)):
            return 2
        return 1
    last = len(tok) - 1
    for n, ch in enumerate(tok[1:], start=1):
        if ch in short_value:
            return 1 if n < last else 2      # `-pFoo` carries its value; `-ap` takes the next word
    return 1


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
            short, long = _WRAPPER_FLAGS["timeout"]
            while i < len(stmt) and stmt[i].startswith("-"):
                i += _option_width(stmt[i], short, long)
            i += 1                      # the duration
        elif base in _WRAPPERS:
            i += 1
            # `sudo -u x`, `env -i`, `nice -n 5`: skip the flags (and a flag's value)
            short, long = _WRAPPER_FLAGS.get(base, _NO_VALUES)
            while i < len(stmt) and stmt[i].startswith("-"):
                i += _option_width(stmt[i], short, long)
        else:
            break
    return stmt[i:]


def _is_doctor_fix(words: list[str]) -> bool:
    return (bool(words) and os.path.basename(words[0]) == "openclaw"
            and "doctor" in words[1:3] and "--fix" in words[1:])


def _systemctl_verb(words: list[str]) -> tuple[str | None, list[str]]:
    """(verb, operands) of a `systemctl ...` command: the first positional word, options skipped."""
    i = 1
    while i < len(words):
        w = words[i]
        if w == "--":
            i += 1
            break
        if w.startswith("-"):
            i += _option_width(w, _SYSTEMCTL_SHORT_VALUE, _SYSTEMCTL_LONG_VALUE)
            continue
        break
    if i >= len(words):
        return None, []
    return words[i], words[i + 1:]


def _is_gateway_unit(operand: str) -> bool:
    return operand in (UNIT, UNIT[: -len(".service")])


def _systemctl_action(words: list[str]) -> str | None:
    """"stop" or "restart" for `systemctl ... <verb> openclaw-gateway[.service]`, else None."""
    if not words or os.path.basename(words[0]) != "systemctl":
        return None
    verb, operands = _systemctl_verb(words)
    if verb is None or not any(_is_gateway_unit(o) for o in operands):
        return None
    if verb in _SYSTEMCTL_STOP:
        return "stop"
    if verb in _SYSTEMCTL_RESTART:
        return "restart"
    return None


def _gateway_action(words: list[str]) -> str | None:
    """"stop" or "restart" for `openclaw [opts] gateway [opts] stop|restart`, else None."""
    if not words or os.path.basename(words[0]) != "openclaw":
        return None
    rest = words[1:]
    # the `gateway` word sits after at most a few global options (`--profile x`, `--log-level y`)
    idx, n = None, 0
    while n < len(rest) and n < 6:
        if rest[n] == "gateway":
            idx = n
            break
        n += _option_width(rest[n], frozenset(), _OPENCLAW_GLOBAL_LONG_VALUE) if rest[n].startswith("-") else 1
    if idx is None:
        return None
    verb = None
    n = idx + 1
    while n < len(rest):
        w = rest[n]
        if w.startswith("-"):
            n += _option_width(w, frozenset(), _GATEWAY_LONG_VALUE)
        elif w in _GATEWAY_VERBS:
            verb = w
            break
        else:
            n += 1
    return verb if verb in ("stop", "restart") else None


def _inner_command(words: list[str]) -> str | None:
    """The string a shell or `eval` is asked to run, when it is a literal in the command line."""
    if not words:
        return None
    base = os.path.basename(words[0])
    if base == "eval":
        return " ".join(words[1:]) or None
    if base in _SHELLS:
        n = 1
        while n < len(words):
            w = words[n]
            # -c, and combined short flags such as -lc / -ic / -ec
            if w == "-c" or (re.fullmatch(r"-[A-Za-z]+", w) and w.endswith("c")):
                return words[n + 1] if n + 1 < len(words) else None
            if not w.startswith(("-", "+")):
                return None
            # `-o pipefail`, `+O extglob`, `--rcfile f`: the value is not the script name
            n += _option_width(w.replace("+", "-", 1) if w.startswith("+") else w,
                               _SHELL_SHORT_VALUE, _SHELL_LONG_VALUE)
    return None


def shape(command: str, _depth: int = 0) -> str | None:
    """"doctor", "stop" or "restart" when the command RUNS a guarded shape, else None."""
    if _depth > 3:
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
            action = _systemctl_action(words) or _gateway_action(words)
            if action:
                return action
            # `bash -c "openclaw doctor --fix"`, `eval "..."`: the string IS a command.
            inner_cmd = _inner_command(words)
            if inner_cmd:
                inner = shape(inner_cmd, _depth + 1)
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
