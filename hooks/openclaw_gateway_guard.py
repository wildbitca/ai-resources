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
`env`, `timeout`, `nohup`, `setsid`, `bash -c`, `sh -c` or `eval`. `env -S '<string>'` (and `-S<s>`, `--split-string`)
runs its string as a command: the string is split by env's own rules (a backslash-underscore separates, a backslash-c ends it, `#` starts
a comment, `${VAR}` is unknown) and read again. A command substitution (`$(...)`, backticks, `<(...)`, also inside
double quotes and in the body of a heredoc whose delimiter is unquoted) runs its text as a command, so its body is
read again too; `$'...'` strings, redirections (`2>&1`, `>f`, `<<EOF`, even inside a word), a brace in an
argument and a `#` inside a word are read the way bash reads them. Nesting is followed to `_MAX_DEPTH` with a
cost bound; a string that cannot be tokenised, or nests past the limit, is read as words and denied only when a
stop/restart verb (or a brace, glob or expansion in the verb slot) sits after the gateway. Read-only verbs
(`status`, `show`, `is-active`, `list-units`, `logs`, `cat`, ...) are never matched.

A shell line continuation (a backslash before the newline, outside single quotes) and a quoted string that
spans lines are joined before the command is tokenised, so a verb or a unit moved to the next line is still seen.
Only LF breaks a line (a backslash before CRLF escapes the CR), and an unquoted newline separates statements.
The line scanner cannot follow a quote nested in `$(...)`, a backtick or `${...}`, so a joined group that holds
one is read twice, joined and line by line, and denied when either view matches (a join it got wrong cannot hide
the next command). It is an accident guard, not a security boundary (pitfall T46): it cannot see a command
built at run time (`$V`, `eval "$(cat f)"`), a script file, or a session that does not go through Claude Code's
Bash tool.

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

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys

UNIT = "openclaw-gateway.service"
WATCHDOG_OFF = os.path.expanduser("~/.openclaw/watchdog.off")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A heredoc operator (`<<` or `<<-`, never the here-string `<<<`) and its delimiter word. Bash takes any word
# there, quoted or backslash-escaped (`<<\EOF`, `<<'E-F'`, `<<"a.b"`) or bare (`<<EOF`, `<<E.F`). A bare word
# must start with a letter or `_`: `1 << 2` is arithmetic (`$(( 1 << 2 ))`), not a heredoc, and reading it as one
# would swallow the following lines as a body and hide the commands there.
_HEREDOC = re.compile(
    r"(?<!<)<<(?!<)-?[ \t]*"
    r"((?:(?:'[^'\n]*'|\"[^\"\n]*\"|\\[^\s])|[A-Za-z_])"            # a quoted or escaped chunk, or a letter
    r"(?:'[^'\n]*'|\"[^\"\n]*\"|\\[^\s]|[^\s;&|()<>'\"\\`])*)")   # then any mix of those and word characters


_MAX_SCAN_DEPTH = 24


def _unescape_backticks(raw: str) -> str:
    """The command a backtick substitution runs: inside backticks a backslash escapes only a backslash, a
    dollar sign and the backtick itself."""
    return re.sub(r"\\([\\$`])", r"\1", raw)


def _scan(text: str, literal_quotes: bool = False
          ) -> tuple[list[str], list[tuple[str, bool]], bool, list[tuple[int, int]]]:
    """Walk `text` as the shell reads it and return (substitutions, heredocs, too_deep, spans).

    `substitutions` are the bodies of the OUTERMOST `$(...)`, `<(...)`, `>(...)` and backtick substitutions the
    shell would run, wherever they sit except inside single quotes (double quotes do not stop them: `echo
    "$(cmd)"` runs `cmd`). `heredocs` are the (delimiter, expands) pairs of every heredoc operator that is a
    real operator: not one inside a quoted string (`echo "<<EOF"`), and `expands` is True for an unquoted
    delimiter, whose body the shell expands. `too_deep` is set when substitutions nest past
    `_MAX_SCAN_DEPTH`; the caller reads the text with the fallback then.

    `spans` are the (start, end) offsets of those substitutions in `text`, delimiters included.

    `literal_quotes` reads a heredoc BODY: quotes and comments are plain characters there and only a backslash,
    `$(` and a backtick are special."""
    subs: list[str] = []
    spans: list[tuple[int, int]] = []
    docs: list[tuple[str, bool]] = []
    deep = [False]
    n = len(text)

    def walk(i: int, ctx: str, depth: int, record: bool) -> tuple[int, bool]:
        """Read from `i` in context `ctx` ("cmd", "sub" inside `$(`, or "dq") up to its terminator. Returns
        (index after the terminator, whether a terminator was found)."""
        parens, prev = 0, " "
        pending: list[str] = []              # heredoc delimiters whose body starts at the next newline
        while i < n:
            c = text[i]
            if c == "\\":
                i, prev = i + 2, "x"
                continue
            if c == "\n" and pending and not literal_quotes and ctx != "dq":
                i += 1
                while pending and i < n:
                    j = text.find("\n", i)
                    line = text[i:n if j < 0 else j]
                    i = n if j < 0 else j + 1
                    if _closes(line, pending[0]):
                        pending.pop(0)
                prev = " "
                continue
            if ctx == "dq":
                if c == '"':
                    return i + 1, True
            elif not literal_quotes:
                if c == "'":
                    j = text.find("'", i + 1)
                    i, prev = (n if j < 0 else j + 1), "'"
                    continue
                if c == '"':
                    i, _ = walk(i + 1, "dq", depth, record)
                    prev = '"'
                    continue
                if c == "#" and (prev.isspace() or prev in ";&|()<>"):
                    j = text.find("\n", i)
                    i = n if j < 0 else j
                    continue
                if c == "$" and text.startswith("$'", i):       # ANSI-C string: a backslash escapes there
                    i += 2
                    while i < n and text[i] != "'":
                        i += 2 if text[i] == "\\" else 1
                    i, prev = i + 1, "'"
                    continue
                if c == "<":
                    m = _HEREDOC.match(text, i)
                    if m:
                        word = m.group(1)
                        delim = re.sub(r"['\"\\]", "", word)
                        docs.append((delim, not re.search(r"['\"\\]", word)))
                        pending.append(delim)
                        i, prev = m.end(), "x"
                        continue
            if c == "$" and text.startswith("$$", i):
                i, prev = i + 2, "x"                             # `$$` is the PID: a quote after it is a quote
                continue
            if c == "`":
                j = i + 1
                while j < n and text[j] != "`":
                    j += 2 if text[j] == "\\" else 1
                if record:
                    subs.append(_unescape_backticks(text[i + 1:min(j, n)]))
                    spans.append((i, min(j + 1, n)))
                i, prev = j + 1, "x"
                continue
            opener = 0
            if c == "$" and text.startswith("$(", i):
                opener = 2
            elif c in "<>" and ctx != "dq" and not literal_quotes and text.startswith("(", i + 1):
                opener = 2                                       # process substitution runs its command
            if opener:
                if depth >= _MAX_SCAN_DEPTH:
                    deep[0] = True
                    return n, False
                end, closed = walk(i + opener, "sub", depth + 1, False)
                if record:
                    subs.append(text[i + opener:end - 1 if closed else end])
                    spans.append((i, end))
                i, prev = end, "x"
                continue
            if ctx == "sub":
                if c == "(":
                    parens += 1
                elif c == ")":
                    if parens == 0:
                        return i + 1, True
                    parens -= 1
            prev = c
            i += 1
        return n, False

    walk(0, "cmd", 0, True)
    return subs, docs, deep[0], spans


def _heredoc_delimiters(text: str) -> list[str]:
    """The delimiter of every heredoc opened on this logical line, with quotes and backslashes removed. An
    operator inside a quoted string is not a heredoc."""
    return [d for d, _ in _scan(text)[1]]


# Constructs inside a double-quoted string that the line scanner reads as flat text although the shell runs
# a nested parse there (`$(...)`, backticks, `${...}`), and `$$` before a quote. A joined group that holds one
# is also read line by line (see `shape`), so a quote the scanner thinks is still open cannot hide a command.
_MAX_COMMAND_CHARS = 262144
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


def _closes(line: str, delimiter: str) -> bool:
    """True when `line` may be the line that ends a heredoc body. A trailing backslash may join the line with
    the next one (`E\\` then an empty line is `E`), so it is not held against the delimiter: reading the
    body as closed a line too early only makes the guard read more, never less."""
    text = line.strip()
    return text == delimiter or (text.endswith("\\") and text[:-1].strip() == delimiter)


def _logical_lines_and_bodies(command: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """The command as the shell reads it, as (logical line, its physical lines) pairs: heredoc bodies removed
    (text fed to a command is not a command), `\\<newline>` removed outside single quotes, and a line that
    opens a quote joined with the lines up to its close (the newline kept inside the string).

    The body of a heredoc is raw text: its delimiter is matched on the physical line, so a body line that
    ends in a backslash cannot swallow the delimiter and hide what follows it.

    The second value holds the bodies of the heredocs whose delimiter is NOT quoted: the shell expands
    `$(...)` and backticks there, so a command substitution in such a body runs."""
    out: list[tuple[str, list[str]]] = []
    bodies: list[str] = []
    pending: list[tuple[str, bool]] = []
    body: list[str] = []
    buf = ""
    parts: list[str] = []
    quote: str | None = None
    prev = " "
    for line in command.split("\n"):   # only LF breaks a line: `\r`, \v, \f ... are ordinary
        if pending:
            if _closes(line, pending[0][0]):
                if pending[0][1]:
                    bodies.append("\n".join(body))
                pending.pop(0)
                body = []
            else:
                body.append(line)
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
        # A heredoc opened INSIDE a joined group (`"$(cat <<'EOF'` ... `EOF` ... `)"`) has its body and its
        # delimiter among the group's own physical lines already: waiting for another delimiter would skip
        # the commands that follow the group.
        pending.extend((d, e) for d, e in _scan(buf)[1] if all(not _closes(pl, d) for pl in parts))
        buf, parts, prev = "", [], " "
    if parts:                                # the command ends inside a continuation or an open quote
        out.append((buf, parts))
    return out, bodies


def _logical_lines(command: str) -> list[tuple[str, list[str]]]:
    return _logical_lines_and_bodies(command)[0]


_ANSI_SIMPLE = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _decode_ansi_c(body: str) -> str:
    """The text of a `$'...'` string after the shell decoded its backslash escapes."""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        d = body[i + 1]
        if d in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[d])
            i += 2
        elif d in "01234567":
            m = re.match(r"[0-7]{1,3}", body[i + 1:])
            out.append(chr(int(m.group(0), 8) & 0xFF))
            i += 1 + len(m.group(0))
        elif d in "xuU":
            m = re.match(r"[0-9A-Fa-f]{1,%d}" % {"x": 2, "u": 4, "U": 8}[d], body[i + 2:])
            if m:
                code = int(m.group(0), 16)
                out.append(chr(code) if code < 0x110000 else "")
                i += 2 + len(m.group(0))
            else:
                out.append("\\" + d)
                i += 2
        elif d == "c" and i + 2 < n:
            out.append(chr(ord(body[i + 2]) & 0x1F))
            i += 3
        else:
            out.append("\\" + d)           # not an escape bash knows: the backslash stays
            i += 2
    return "".join(out)


def _plain_ansi_c(text: str) -> str:
    """`text` as the tokeniser (which is not a shell) must read it: every `$'...'` string (outside other
    quotes) rewritten as the single-quoted string it stands for, a backslash-newline outside single quotes
    removed, and a backslash before `$` or a backtick inside double quotes dropped (shlex keeps it)."""
    if "$'" not in text and "\\" not in text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            if text.startswith("\\\n", i):
                i += 2                          # a continuation: the shell removes both characters
            else:
                out.append(text[i:i + 2])
                i += 2
        elif ch == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j + 1
            out.append(text[i:j])
            i = j
        elif ch == '"':
            out.append('"')
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    pair = text[j + 1]
                    if pair == "\n":
                        pass                    # removed inside double quotes as well
                    elif pair in "$`":
                        out.append(pair)        # the backslash only protects the character
                    else:
                        out.append(text[j:j + 2])
                    j += 2
                else:
                    out.append(text[j])
                    j += 1
            if j < n:
                out.append('"')
            i = j + 1
        elif ch == "$" and text.startswith("$$", i):
            out.append("$$")
            i += 2
        elif ch == "$" and text.startswith("$'", i):
            j = i + 2
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            decoded = _decode_ansi_c(text[i + 2:j])
            out.append("'" + decoded.replace("'", "'\\''") + "'")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


_REDIRECTION = re.compile(r"&>>?|<<<|<<-?|<>|>>|>\||>&|<&|<|>")
_FD_WORD = re.compile(r"[0-9]+(?=[<>])")


def _mask_substitutions(text: str) -> str:
    """`text` with every command substitution replaced by a plain word: its body is read on its own (see
    `shape`), and a quote inside it must not keep the tokeniser from reading the statements around it."""
    spans = _scan(text)[3]
    if not spans:
        return text
    out, last = [], 0
    for start, end in spans:
        out.append(text[last:start])
        out.append("__SUBSTITUTION__")
        last = end
    out.append(text[last:])
    return "".join(out)


def _strip_redirections(text: str) -> str:
    """`text` without its redirections (`> f`, `2>&1`, `<<EOF`, `&>f`): bash takes them out of the command
    wherever they sit, even inside a word (`systemctl --us>er stop x` is `systemctl --us stop x`), and their
    target is a file name, not an argument."""
    out: list[str] = []
    i, n = 0, len(text)

    def word_end(j: int) -> int:
        while j < n and text[j] not in " \t\n;&|()<>":
            if text[j] == "\\":
                j += 2
            elif text[j] in "'\"":
                k = text.find(text[j], j + 1)
                j = n if k < 0 else k + 1
            else:
                j += 1
        return min(j, n)

    while i < n:
        ch = text[i]
        if ch == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch in "'\"":
            j = text.find(ch, i + 1) if ch == "'" else i + 1
            if ch == '"':
                while j < n and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
            j = n if j < 0 else min(j + 1, n)
            out.append(text[i:j])
            i = j
            continue
        start = i
        if ch.isdigit() and (i == 0 or text[i - 1] in " \t\n;&|()"):
            fd = _FD_WORD.match(text, i)
            if fd:
                i = fd.end()
        if i < n and (text[i] in "<>" or text.startswith("&>", i)):
            op = _REDIRECTION.match(text, i)
            if op:
                i = op.end()
                while i < n and text[i] in " \t":
                    i += 1
                i = word_end(i)
                out.append(" ")
                continue
        i = start
        out.append(ch)
        i += 1
    return "".join(out)


def _unquoted_newline_segments(text: str) -> list[str]:
    """`text` cut at every newline the tokeniser reads as a command separator: one outside quotes and outside
    a comment, and with the comments removed. A comment starts at a `#` at the start of a word (`echo# a` and
    `${#X}` hold no comment) and runs up to its newline; an unbalanced quote leaves the rest of the text in
    one segment."""
    segments: list[str] = []
    piece: list[str] = []
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
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\n;&|()<>"):
            piece.append(text[start:i])
            j = text.find("\n", i)
            if j < 0:
                start = i = n
                break
            start = i = j
        elif ch == "\n":
            piece.append(text[start:i])
            segments.append("".join(piece))
            piece = []
            start = i = i + 1
        else:
            i += 1
    piece.append(text[start:])
    segments.append("".join(piece))
    return segments


def _brace_is_reserved(words: list[str]) -> bool:
    """True when a `{` or `}` after `words` is the reserved word (a group or a function body) and not an
    argument: only keywords, `time` and `!` precede it, optionally `function NAME`."""
    rest = [w for w in words if w not in _KEYWORDS and w != "time"]
    return not rest or (rest[0] == "function" and len(rest) <= 2)


def _statements(line: str) -> list[list[str]]:
    """The simple commands of `line`. An unquoted newline separates statements like `;` does, and a comment
    ends at its newline, so the line after a comment is never read as the comment's arguments."""
    stmts: list[list[str]] = [[]]
    for segment in _unquoted_newline_segments(_plain_ansi_c(_mask_substitutions(line))):
        segment = _strip_redirections(segment)
        lex = shlex.shlex(segment, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""                 # comments were removed above: a mid-word `#` is an ordinary character
        tokens = list(lex)  # ValueError on unbalanced quotes: the caller fails open
        stmts.append([])
        for tok in tokens:
            if tok in ("{", "}") and not _brace_is_reserved(stmts[-1]):
                stmts[-1].append(tok)       # a brace is a reserved word at the start of a command only
            elif tok and set(tok) <= set(";&|(){}"):
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


_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_GLOB_CHARS = re.compile(r"[*?\[]")


def _expansions(word: str, limit: int = 64) -> list[str]:
    """The words bash makes of `word` by brace expansion (`re{start,}` -> `restart`, `re`), bounded."""
    out = [word]
    for _ in range(8):
        nxt: list[str] = []
        changed = False
        for w in out:
            m = _BRACE.search(w)
            if not m:
                nxt.append(w)
                continue
            changed = True
            nxt += [w[:m.start()] + alt + w[m.end():] for alt in m.group(1).split(",")]
        out = nxt[:limit]
        if not changed:
            break
    return out


def _is_gateway_unit(operand: str) -> bool:
    """True for the unit name, with a brace expansion or a unit glob (systemctl matches `openclaw-gate*`
    against unit names itself) that reaches it. Unit names are case-sensitive in systemd: kept exact."""
    names = (UNIT, UNIT[: -len(".service")])
    for cand in _expansions(operand):
        if cand in names:
            return True
        if _GLOB_CHARS.search(cand) and any(fnmatch.fnmatchcase(n, cand) for n in names):
            return True
    return False


def _verb_candidates(word: str) -> list[str]:
    """What a verb-slot word can stand for: the word itself, its brace expansions, and the stop/restart verbs
    a glob in it would match (a glob only expands to a name that exists, so this over-blocks on purpose)."""
    out: list[str] = []
    for cand in _expansions(word):
        out.append(cand)
        if _GLOB_CHARS.search(cand):
            out += [v for v in sorted(_SYSTEMCTL_STOP | _SYSTEMCTL_RESTART) if fnmatch.fnmatchcase(v, cand)]
    return out


def _systemctl_action(words: list[str]) -> str | None:
    """"stop" or "restart" for `systemctl ... <verb> openclaw-gateway[.service]`, else None."""
    if not words or os.path.basename(words[0]) != "systemctl":
        return None
    verb, operands = _systemctl_verb(words)
    if verb is None or not any(_is_gateway_unit(o) for o in operands):
        return None
    found = None
    for cand in _verb_candidates(verb):
        if cand in _SYSTEMCTL_RESTART:
            return "restart"
        if cand in _SYSTEMCTL_STOP:
            found = "stop"
    return found


def _gateway_action(words: list[str]) -> str | None:
    """"stop" or "restart" for `openclaw [opts] gateway [opts] stop|restart`, else None."""
    if not words or os.path.basename(words[0]) != "openclaw":
        return None
    rest = words[1:]
    # the `gateway` word sits after at most a few global options (`--profile x`, `--log-level y`)
    idx, n = None, 0
    while n < len(rest) and n < 6:
        if rest[n].lower() == "gateway":
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
        elif w.lower() in _GATEWAY_VERBS:
            verb = w.lower()
            break
        elif _BRACE.search(w) or _GLOB_CHARS.search(w):
            # `{restart,status}` and `rest*` can stand for a verb: the first one that does is the verb
            hit = [c.lower() for c in _verb_candidates(w) if c.lower() in ("stop", "restart")]
            if hit:
                return "restart" if "restart" in hit else "stop"
            n += 1
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
_NEAR = 8          # how many words before the unit a verb may sit in the unit form of the fallback
_MAX_DEPTH = 8     # how many wrappers (`bash -c`, `eval`, `env -S`, `$(...)`) are followed before the fallback
_WORK_BUDGET = 4 * _MAX_COMMAND_CHARS   # characters read over one whole command, nested strings included
_work = 0
_SEGMENT = re.compile(r"[;&|()\n]+")
_UNKNOWN_VERB = re.compile(r"[$`*?\[{~]")


def _verb_next_to_gateway(text: str) -> str | None:
    """"stop" or "restart" when `text` (a string that could not be tokenised, or nests too deep to follow)
    has a stop/restart verb after the gateway: `openclaw [opts] gateway [opts] <verb>` (any number of words
    later, up to a known subcommand) or `<verb> [opts] openclaw-gateway[.service]`. The two words alone
    (`echo openclaw gateway`) are not enough (L4): the fallback denies only where a restart or stop is
    actually being asked for. An expansion, a glob or a brace where the verb goes hides it: fail closed."""
    # Quotes and backslashes are dropped first so a verb split by shell quoting joins up (`re''start`,
    # `res\tart` -> `restart`); `$` and backticks stay in the words so an expansion can be seen. Case is
    # folded: the CLI is the one path that could still reach the unit through a case variant.
    found: str | None = None
    for seg in _SEGMENT.split(re.sub(r"['\"\\]", "", text.lower())):
        words = seg.split()
        for i, w in enumerate(words):
            if os.path.basename(w) == "openclaw" and "gateway" in words[i + 1:]:
                after = words[words.index("gateway", i + 1) + 1:]
                first_positional = next((v for v in after if not v.startswith("-")), None)
                if first_positional is not None and _UNKNOWN_VERB.search(first_positional):
                    return "restart"
                for v in after:
                    if v in _STOP_WORDS:
                        found = found or "stop"
                    elif v in _RESTART_WORDS:
                        return "restart"
                    elif v in _GATEWAY_VERBS:
                        break                        # `status ... restart`: the subcommand is `status`
            elif w in (UNIT, UNIT[: -len(".service")]):
                before = words[max(0, i - 2 * _NEAR):i]
                first_positional = next((v for v in reversed(before) if not v.startswith("-")), None)
                if first_positional is not None and _UNKNOWN_VERB.search(first_positional):
                    return "restart"
                for v in before:
                    if v in _STOP_WORDS:
                        found = found or "stop"
                    elif v in _RESTART_WORDS:
                        return "restart"
    return found


def _text_shape(text: str) -> str | None:
    """The fallback for text the recogniser cannot (or may not) follow any further: "restart", "stop" or
    "doctor" when the words are there, None when they are not. It reads words, not commands."""
    found = _verb_next_to_gateway(text)
    if found == "restart":
        return found
    for seg in _SEGMENT.split(re.sub(r"['\"\\]", "", text.lower())):
        words = seg.split()
        for i, w in enumerate(words):
            if os.path.basename(w) == "openclaw" and "doctor" in words[i + 1:i + 3] and "--fix" in words[i + 1:]:
                return found or "doctor"
    return found


_ENV_ESCAPES = {"n": "\n", "t": "\t", "v": "\v", "f": "\f", "r": "\r"}


def _env_split_words(s: str) -> list[str] | None:
    """The words `env -S` makes of `s`, by env's own rules (not the shell's): `\\_` separates words (a space
    inside double quotes), `\\c` ends the string, `#` at the start of a word starts a comment, quotes group,
    and `\\n \\t \\v \\f \\r` are control characters. None when the string cannot be split without
    guessing: an unterminated quote, or a `${VAR}` that env expands from its own environment."""
    words: list[str] = []
    cur: str | None = None
    q: str | None = None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            if i + 1 >= n:
                break
            d = s[i + 1]
            if d == "c":
                break
            if q == "'":
                if d in "\\'":
                    cur = (cur or "") + d
                else:
                    cur = (cur or "") + ch + d
            elif d == "\n":
                pass                                    # a backslash-newline is removed
            elif d == "_" and q is None:
                if cur is not None:
                    words.append(cur)
                    cur = None
            else:
                cur = (cur or "") + (" " if d == "_" else _ENV_ESCAPES.get(d, d))
            i += 2
            continue
        if q is None and ch in " \t\n\v\f\r":
            if cur is not None:
                words.append(cur)
                cur = None
        elif q is None and ch == "#" and cur is None:
            break
        elif ch == "$" and q != "'" and s.startswith("${", i):
            return None
        elif ch in "'\"" and q in (None, ch):
            q = None if q else ch
            cur = cur or ""
        else:
            cur = (cur or "") + ch
        i += 1
    if q is not None:
        return None
    if cur is not None:
        words.append(cur)
    return words


def _env_split_shape(split: str, rest: list[str], depth: int) -> str | None:
    """Re-enter the recogniser on an `env -S` string followed by the words after env's options.

    A string that cannot be split, or nests too deep, is denied only when the gateway words are there with a
    stop/restart verb (see `_text_shape`) and allowed otherwise."""
    # The words after env's options are the tail of the command `env -S` runs: the fallback sees them too.
    whole = " ".join([split, *rest])
    if depth > _MAX_DEPTH:
        return _text_shape(whole)
    words = _env_split_words(split)
    if words is None:
        return _text_shape(whole)
    if not words:
        return None
    return shape(" ".join(shlex.quote(w) for w in [*words, *rest]), depth + 1)


def _line_statements(lines: list[str]) -> list[list[str]]:
    """The statements of each physical line on its own; a line that does not tokenise is skipped."""
    out: list[list[str]] = []
    pending: list[str] = []                  # heredoc delimiters whose body is being skipped
    for line in lines:
        if pending:
            if _closes(line, pending[0]):
                pending.pop(0)
            continue                         # text fed to a command is not a command
        try:
            out += _statements(line)
        except ValueError:
            pass
        pending.extend(_heredoc_delimiters(line))
    return out


def _read_statements(logical: str, physical: list[str]) -> list[list[str]]:
    """The statements of a logical line. When the joined text does not tokenise (a stray apostrophe), the
    physical lines are read one by one, so one stray quote cannot hide the commands on the next lines."""
    try:
        return _statements(logical)
    except ValueError:
        return _line_statements(physical)


_memo: tuple[str, str | None] | None = None


def shape(command: str, _depth: int = 0) -> str | None:
    """"doctor", "stop" or "restart" when the command RUNS a guarded shape, else None."""
    global _memo
    if _depth:
        return _shape(command, _depth)
    if _memo is not None and _memo[0] == command:
        return _memo[1]
    result = _shape(command, 0)
    _memo = (command, result)
    return result


def _shape(command: str, _depth: int) -> str | None:
    global _work
    if _depth == 0:
        _work = 0
    _work += len(command)
    if len(command) > _MAX_COMMAND_CHARS:
        # Reading a huge command line by line costs seconds per hundred kilobytes and the hook has a timeout
        # that lets the command through. Above the cap nothing is parsed: it is denied when it names the
        # gateway next to a stop/restart verb, or `doctor` next to `--fix`, and allowed otherwise.
        low = command.lower()
        if "gateway" in low and re.search(r"restart|stop|kill", low):
            return "restart"
        if "doctor" in low and "--fix" in low:
            return "doctor"
        return None
    if _depth > _MAX_DEPTH or _work > _WORK_BUDGET:
        # Past the nesting limit (or the cost bound over the whole command) the text is read for words: the
        # gateway next to a verb, or `doctor --fix`, is denied and anything else is allowed.
        return _text_shape(command)
    lines, bodies = _logical_lines_and_bodies(command)
    for body in bodies:
        # An unquoted heredoc delimiter: the shell expands `$(...)` and backticks in the body.
        for sub in _scan(body, literal_quotes=True)[0]:
            inner = shape(sub, _depth + 1)
            if inner:
                return inner
    for logical, physical in lines:
        statements = _read_statements(logical, physical)
        subs, _docs, too_deep, _spans = _scan(logical)
        if too_deep:
            found = _text_shape(logical)
            if found:
                return found
        if len(physical) > 1 and _NESTED_EXPANSION.search(logical):
            # The scanner cannot follow a quote nested in an expansion, so the join may be imaginary: read
            # the physical lines one by one as well (the 2.1.0 reading) and deny if either view matches.
            statements += _line_statements(physical)
        for sub in subs:
            # `"$(cmd)"`, `` `cmd` ``, `<(cmd)`: the shell runs the text inside as a command.
            inner = shape(sub, _depth + 1)
            if inner:
                return inner
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
        found = shape(command)       # the same command: answered from the one-entry memo, not read twice
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
