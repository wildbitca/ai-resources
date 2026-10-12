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
`env`, `timeout`, `nohup`, `setsid`, `bash -c`, `sh -c` or `eval`. `env -S '<string>'` (and `-S<s>`, `--split-string`) runs its string as a
command, so the string is re-tokenised and checked again (bounded depth); a string that cannot be tokenised,
or nests past the depth limit, is denied only when a stop/restart verb sits next to the gateway. Read-only verbs (`status`,
`show`, `is-active`, `list-units`, `logs`, `cat`, ...) are never matched.

A shell line continuation (a backslash before the newline, outside single quotes) and a quoted string that
spans lines are joined before the command is tokenised, so a verb or a unit moved to the next line is still seen.
Only LF breaks a line (a backslash before CRLF escapes the CR), and an unquoted newline separates statements.
The line scanner cannot follow a quote nested in `$(...)`, a backtick or `${...}`, so a joined group that holds
one is read twice, joined and line by line, and denied when either view matches (a join it got wrong cannot hide
the next command).

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
# Constructs inside a double-quoted string that the line scanner reads as flat text although the shell runs
# a nested parse there (`$(...)`, backticks, `${...}`), and `$$` before a quote. A joined group that holds one
# is also read line by line (see `shape`), so a quote the scanner thinks is still open cannot hide a command.
_NESTED_EXPANSION = re.compile(r"\$\(|`|\$\{|\$\$['\"]")
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


def _scan_piece(piece: str, quote: str | None, prev: str) -> tuple[str | None, str, bool]:
    """Walk one physical line as the shell reads it. Returns (quote state at its end, last significant
    character, whether it ends in an unquoted-or-double-quoted `\\<newline>`).

    `quote` is None, `'`, `"` or `$'` (ANSI-C: a backslash escapes there). Inside single quotes a backslash is
    literal; elsewhere it escapes the next character, and at the end of the line it is a continuation. A `#` at
    the start of a word is a comment up to the newline, which a trailing backslash does not continue."""
    i, n = 0, len(piece)
    while i < n:
        ch = piece[i]
        if quote == "'":
            j = piece.find("'", i)
            if j < 0:
                return quote, prev, False
            quote, i, prev = None, j + 1, "'"
            continue
        if ch == "\\":
            if i + 1 >= n:
                return quote, prev, True
            i, prev = i + 2, "x"
            continue
        if ch == "$" and quote is None and piece.startswith("$$", i):
            i, prev = i + 2, "x"             # `$$` is the PID, so a quote after it is an ordinary quote
            continue
        if quote == '"':
            if ch == '"':
                quote = None
        elif quote == "$'":
            if ch == "'":
                quote = None
        elif ch == "'":
            quote = "$'" if prev == "$" else "'"
        elif ch == '"':
            quote = '"'
        elif ch == "#" and (prev.isspace() or prev in ";&|()"):
            return quote, prev, False
        prev = ch
        i += 1
    return quote, prev, False


def _logical_lines(command: str) -> list[tuple[str, list[str]]]:
    """The command as the shell reads it, as (logical line, its physical lines) pairs: heredoc bodies removed
    (text fed to a command is not a command), `\\<newline>` removed outside single quotes, and a line that
    opens a quote joined with the lines up to its close (the newline kept inside the string).

    The body of a heredoc is raw text: its delimiter is matched on the physical line, so a body line that
    ends in a backslash cannot swallow the delimiter and hide what follows it."""
    out: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    buf = ""
    parts: list[str] = []
    quote: str | None = None
    prev = " "
    for line in command.split("\n"):   # only LF breaks a line: `\r`, \v, \f ... are ordinary
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        parts.append(line)
        quote, prev, cont = _scan_piece(line, quote, prev)
        if cont:
            buf += line[:-1]                 # the backslash and the newline are both removed
            continue
        if quote is not None:
            buf += line + "\n"               # inside a quote the newline is part of the string
            prev = " "
            continue
        buf += line
        out.append((buf, parts))
        pending.extend(m.group(2) for m in _HEREDOC.finditer(buf))
        buf, parts, prev = "", [], " "
    if parts:                                # the command ends inside a continuation or an open quote
        out.append((buf, parts))
    return out


def _unquoted_newline_segments(text: str) -> list[str]:
    """`text` cut at every newline the tokeniser reads as a command separator: one outside quotes and outside
    a comment. It follows shlex's own reading (a `#` outside quotes starts a comment up to the newline), so the
    segments agree with the tokens; an unbalanced quote leaves the rest of the text in one segment."""
    segments: list[str] = []
    start, i, n = 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                break
            i = j + 1
        elif ch == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif ch == "#":
            j = text.find("\n", i)
            if j < 0:
                break
            i = j
        elif ch == "\n":
            segments.append(text[start:i])
            start = i = i + 1
        else:
            i += 1
    segments.append(text[start:])
    return segments


def _statements(line: str) -> list[list[str]]:
    """The simple commands of `line`. An unquoted newline separates statements like `;` does, and a comment
    ends at its newline, so the line after a comment is never read as the comment's arguments."""
    stmts: list[list[str]] = [[]]
    for segment in _unquoted_newline_segments(line):
        lex = shlex.shlex(segment, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = "#"
        tokens = list(lex)  # ValueError on unbalanced quotes: the caller fails open
        stmts.append([])
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


def _env_split_values(flags: list[str]) -> list[str]:
    """The strings `env -S <s>`, `-S<s>`, `-vS <s>`, `--split-string <s>` and `--split-string=<s>` carry.

    `env` splits such a string into words and runs them, so the string IS a command (the 2.1.0 gap)."""
    values: list[str] = []
    short_value = _WRAPPER_FLAGS["env"][0] - {"S"}
    k = 0
    while k < len(flags):
        tok = flags[k]
        if tok.startswith("--"):
            name, eq, val = tok.partition("=")
            if len(name) >= 3 and "--split-string".startswith(name):
                if eq:
                    values.append(val)
                elif k + 1 < len(flags):
                    values.append(flags[k + 1])
                    k += 1
            elif not eq and name in _WRAPPER_FLAGS["env"][1]:
                k += 1
        elif tok.startswith("-") and len(tok) > 1:
            for n, ch in enumerate(tok[1:], start=1):
                if ch == "S":
                    if tok[n + 1:]:
                        values.append(tok[n + 1:])
                    elif k + 1 < len(flags):
                        values.append(flags[k + 1])
                        k += 1
                    break
                if ch in short_value:
                    if n == len(tok) - 1:
                        k += 1
                    break
        k += 1
    return values


def _command_words(stmt: list[str], splits: list[str] | None = None) -> list[str]:
    """The statement with leading VAR=x assignments and transparent wrappers removed.

    `splits`, when given, collects the `env -S` strings seen on the way."""
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
            first_flag = i
            # `sudo -u x`, `env -i`, `nice -n 5`: skip the flags (and a flag's value)
            short, long = _WRAPPER_FLAGS.get(base, _NO_VALUES)
            while i < len(stmt) and stmt[i].startswith("-"):
                i += _option_width(stmt[i], short, long)
            if base == "env" and splits is not None:
                splits.extend(_env_split_values(stmt[first_flag:i]))
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


_STOP_WORDS = {"stop", "kill"}
_RESTART_WORDS = {"restart", "try-restart", "reload-or-restart", "try-reload-or-restart", "condrestart"}
_NEAR = 8          # how many words after the gateway word a verb still counts as "next to" it


def _verb_next_to_gateway(text: str) -> str | None:
    """"stop" or "restart" when `text` (a string that could not be tokenised, or nests too deep to follow)
    has a stop/restart verb right after the gateway: `openclaw [opts] gateway [opts] <verb>` or
    `<verb> [opts] openclaw-gateway[.service]`. The two words alone (`echo openclaw gateway`) are not enough
    (L4): the fallback denies only where a restart or stop is actually being asked for."""
    # Quotes and backslashes are dropped first so a verb split by shell quoting joins up (`re''start`,
    # `res\tart` -> `restart`); `$` and backticks stay in the words so an expansion can be seen.
    words = [w for w in re.split(r"[\s();&|]+", re.sub(r"['\"\\]", "", text.lower())) if w]
    found: str | None = None
    for i, w in enumerate(words):
        if os.path.basename(w) == "openclaw" and "gateway" in words[i + 1:i + 1 + _NEAR]:
            g = i + 1 + words[i + 1:i + 1 + _NEAR].index("gateway")
            after = words[g + 1:g + 1 + _NEAR]
        elif w in (UNIT, UNIT[: -len(".service")]):
            after = words[max(0, i - _NEAR):i]
        else:
            continue
        # An expansion where the verb goes (`gateway $V`, `gateway $(...)`) hides the verb: fail closed.
        slot = [v for v in (after if w == "openclaw" or os.path.basename(w) == "openclaw"
                            else after[::-1]) if not v.startswith("-")][:1]
        if slot and ("$" in slot[0] or "`" in slot[0]):
            return "restart"
        for v in after:
            if v in _STOP_WORDS:
                found = found or "stop"
            elif v in _RESTART_WORDS:
                return "restart"
    return found


def _env_split_shape(split: str, rest: list[str], depth: int) -> str | None:
    """Re-enter the recogniser on an `env -S` string followed by the words after env's options.

    A string that cannot be tokenised, or nests too deep, is denied only when a stop/restart verb sits next to
    the gateway (see `_verb_next_to_gateway`) and allowed otherwise."""
    # The words after env's options are the tail of the command `env -S` runs: the fallback sees them too.
    whole = " ".join([split, *rest])
    if depth > 2:
        return _verb_next_to_gateway(whole)
    try:
        words = shlex.split(split)
    except ValueError:
        return _verb_next_to_gateway(whole)
    if not words:
        return None
    return shape(" ".join(shlex.quote(w) for w in [*words, *rest]), depth + 1)


def _line_statements(lines: list[str]) -> list[list[str]]:
    """The statements of each physical line on its own; a line that does not tokenise is skipped."""
    out: list[list[str]] = []
    for line in lines:
        try:
            out += _statements(line)
        except ValueError:
            continue
    return out


def _read_statements(logical: str, physical: list[str]) -> list[list[str]]:
    """The statements of a logical line. When the joined text does not tokenise (a stray apostrophe), the
    physical lines are read one by one, so one stray quote cannot hide the commands on the next lines."""
    try:
        return _statements(logical)
    except ValueError:
        return _line_statements(physical)


def shape(command: str, _depth: int = 0) -> str | None:
    """"doctor", "stop" or "restart" when the command RUNS a guarded shape, else None."""
    if _depth > 3:
        return None
    for logical, physical in _logical_lines(command):
        statements = _read_statements(logical, physical)
        if len(physical) > 1 and _NESTED_EXPANSION.search(logical):
            # The scanner cannot follow a quote nested in an expansion, so the join may be imaginary: read
            # the physical lines one by one as well (the 2.1.0 reading) and deny if either view matches.
            statements += _line_statements(physical)
        for stmt in statements:
            splits: list[str] = []
            words = _command_words(stmt, splits)
            for split in splits:
                found = _env_split_shape(split, words, _depth)
                if found:
                    return found
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
