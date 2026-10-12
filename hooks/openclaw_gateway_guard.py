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

Text that is fed to a shell is command text too: a heredoc or here-string whose consumer is `sh`, `bash`,
`zsh`, `dash`, `ksh`, `eval` or `source /dev/stdin`, and what `echo`/`printf` or a heredoc pipe into one. The
commands that other commands run (`xargs CMD`, `find -exec CMD`, `parallel CMD`), the value of an `alias`,
`trap 'CMD' SIG` and a literal assigned earlier in the same text and then evaluated (`V=...; eval "$V"`) are
read as commands, and a `case` pattern's `)` does not end a `$(...)`.

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

Cost and the deadline: the analysis is bounded by a work budget and by a wall-clock deadline (4 s, never
more than 8 s: the hook's own timeout FAILS OPEN, so the guard must answer before it). When the deadline fires,
or the analysis fails with an internal error, a CHEAP VIEW decides: it reads words, not commands, and denies
when `gateway` sits next to a stop/restart verb (or `doctor` next to `--fix`) within a short window: ANY word
`gateway` there, so a benign text that names a gateway and a verb is denied when the deadline fires (fail toward
the words). When the DEADLINE fired the verdict is also fail-closed beyond the window: a text that names
OpenClaw (as a command word, or the unit `openclaw-gateway`) together with any of `gateway`, `restart`, `stop`,
`kill`, `doctor` or `--fix`, wherever they sit, is denied. The cheap view also runs, as a conservative second
opinion, on text that feeds a shell through a heredoc, a here-string or a pipe, and there it is narrower: it
needs the OpenClaw gateway words (`openclaw` before `gateway`, or the unit `openclaw-gateway[.service]`), not
the bare word, so `docker restart gateway` or `kubectl rollout restart deploy/gateway` are allowed. It may still
deny text that only mentions the verb there, and it never reads anything it cannot see in a single pass.

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
import signal
import subprocess
import sys
import threading
import time
from collections import deque

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


_MAX_SCAN_DEPTH = 48


def _unescape_backticks(raw: str) -> str:
    """The command a backtick substitution runs: inside backticks a backslash escapes only a backslash, a
    dollar sign and the backtick itself."""
    return re.sub(r"\\([\\$`])", r"\1", raw)


_CASE_WORD = re.compile(r"(case|esac|in)(?=[\s;&|()<>]|$)")
_AFTER_KEYWORD = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "time"})


def _command_position(text: str, i: int, lo: int) -> bool:
    """True when the word at `i` starts a command: it follows a separator, a keyword that is followed by a
    command, or the start of the group that begins at `lo`."""
    j = i - 1
    while j >= lo and text[j] in " \t":
        j -= 1
    if j < lo:
        return True
    if text[j] in ";&|(){\n":
        return True
    k = j
    while k >= lo and (text[k].isalpha() or text[k] == "!"):
        k -= 1
    return text[k + 1:j + 1] in _AFTER_KEYWORD


def _scan(text: str, literal_quotes: bool = False
          ) -> tuple[list[str], list[tuple[str, bool, int, int]], bool, list[tuple[int, int]],
                     list[tuple[int, int]]]:
    """Walk `text` as the shell reads it and return (substitutions, heredocs, too_deep, spans, herestrings).

    `substitutions` are the bodies of the OUTERMOST `$(...)`, `<(...)`, `>(...)` and backtick substitutions the
    shell would run, wherever they sit except inside single quotes (double quotes do not stop them: `echo
    "$(cmd)"` runs `cmd`). `heredocs` are the (delimiter, expands) pairs of every heredoc operator that is a
    real operator: not one inside a quoted string (`echo "<<EOF"`), and `expands` is True for an unquoted
    delimiter, whose body the shell expands. `too_deep` is set when substitutions nest past
    `_MAX_SCAN_DEPTH`; the caller reads the text with the fallback then.

    `spans` are the (start, end) offsets of those substitutions in `text`, delimiters included. A heredoc
    entry also carries the (start, end) offsets of its operator and delimiter word, and `herestrings` holds the
    (start, end) offsets of each `<<<` operator.

    A `)` that ends a `case` pattern inside a substitution does not end the substitution: `case`, `in`, the
    pattern terminator, `;;` and `esac` are followed there.

    `literal_quotes` reads a heredoc BODY: quotes and comments are plain characters there and only a backslash,
    `$(` and a backtick are special."""
    subs: list[str] = []
    spans: list[tuple[int, int]] = []
    docs: list[tuple[str, bool, int, int]] = []
    herestrings: list[tuple[int, int]] = []
    deep = [False]
    ticks = [0]
    n = len(text)

    def walk(i: int, ctx: str, depth: int, record: bool) -> tuple[int, bool]:
        """Read from `i` in context `ctx` ("cmd", "sub" inside `$(`, or "dq") up to its terminator. Returns
        (index after the terminator, whether a terminator was found)."""
        parens, prev = 0, " "
        start = i
        cases: list[str] = []                # one state per open `case`: head, pat (pattern), body
        pat_paren = False                    # a `(`-led pattern is open: its `)` ends the pattern
        pending: deque[str] = deque()        # heredoc delimiters whose body starts at the next newline
        while i < n:
            ticks[0] += 1
            if not ticks[0] & 0x3FFF:
                _check_deadline()
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
                        pending.popleft()
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
                        docs.append((delim, not re.search(r"['\"\\]", word), m.start(), m.end()))
                        pending.append(delim)
                        i, prev = m.end(), "x"
                        continue
                    if text.startswith("<<<", i):
                        herestrings.append((i, i + 3))
                        i, prev = i + 3, "x"
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
                if c in "cei" and (prev.isspace() or prev in ";&|()"):
                    m = _CASE_WORD.match(text, i)
                    if m:
                        word = m.group(1)
                        if word == "case" and _command_position(text, i, start):
                            cases.append("head")
                        elif word == "in" and cases and cases[-1] == "head":
                            cases[-1] = "pat"
                        elif word == "esac" and cases and cases[-1] != "head" and \
                                _command_position(text, i, start):
                            cases.pop()
                        i, prev = i + len(word), "x"
                        continue
                if c == ";" and cases and cases[-1] == "body" and text.startswith((";;", ";&"), i):
                    cases[-1] = "pat"
                    i, prev = i + 2, ";"
                    continue
                if c == "(":
                    if parens == 0 and cases and cases[-1] == "pat":
                        pat_paren = True
                    parens += 1
                elif c == ")":
                    if parens == 0:
                        if cases and cases[-1] == "pat":
                            cases[-1] = "body"       # the `)` that ends a case pattern
                            i, prev = i + 1, ")"
                            continue
                        return i + 1, True
                    parens -= 1
                    if parens == 0 and pat_paren:
                        pat_paren = False
                        cases[-1] = "body"
            prev = c
            i += 1
        return n, False

    walk(0, "cmd", 0, True)
    return subs, docs, deep[0], spans, herestrings


def _heredoc_delimiters(text: str) -> list[str]:
    """The delimiter of every heredoc opened on this logical line, with quotes and backslashes removed. An
    operator inside a quoted string is not a heredoc."""
    return [d[0] for d in _scan(text)[1]]


# Constructs inside a double-quoted string that the line scanner reads as flat text although the shell runs
# a nested parse there (`$(...)`, backticks, `${...}`), and `$$` before a quote. A joined group that holds one
# is also read line by line (see `shape`), so a quote the scanner thinks is still open cannot hide a command.
_MAX_COMMAND_CHARS = 262144
_NESTED_EXPANSION = re.compile(r"\$\(|`|\$\{|\$\$['\"]")
# Tokens that only wrap another command: look through them to the real one.
_WRAPPERS = {"sudo", "doas", "env", "command", "builtin", "nohup", "setsid", "time", "exec", "nice", "ionice",
             "stdbuf", "unbuffer"}
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
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "coproc"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
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
    out, bodies, _docs = _logical_lines_docs(command)
    return out, bodies


def _logical_lines_docs(command: str
                        ) -> tuple[list[tuple[str, list[str]]], list[str], dict[int, list[list]]]:
    """`_logical_lines_and_bodies` plus, per logical line index, its heredocs as `[delimiter, expands, start,
    end, body lines or None]` (offsets of the operator and delimiter word in the logical line; the body is None
    when the group holds its own closing line, and is then read where the group is read again)."""
    out: list[tuple[str, list[str]]] = []
    bodies: list[str] = []
    line_docs: dict[int, list[list]] = {}
    pending: deque[list] = deque()
    buf = ""
    parts: list[str] = []
    quote: str | None = None
    prev = " "
    for line in command.split("\n"):   # only LF breaks a line: `\r`, \v, \f ... are ordinary
        if pending:
            ent = pending[0]
            if _closes(line, ent[0]):
                if ent[1]:
                    bodies.append("\n".join(ent[4]))
                pending.popleft()
            else:
                ent[4].append(line)
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
        # the commands that follow the group. The set of those lines is built once: a delimiter is looked up,
        # never compared with every line (zsh allows any number of heredocs on one line).
        docs = _scan(buf)[1]
        if docs:
            closers: set[str] = set()
            for pl in parts:
                text = pl.strip()
                closers.add(text)
                if text.endswith("\\"):
                    closers.add(text[:-1].strip())
            entries: list[list] = []
            for d, expands, s, e in docs:
                ent = [d, expands, s, e, None if d in closers else []]
                if ent[4] is not None:
                    pending.append(ent)
                entries.append(ent)
            line_docs[len(out) - 1] = entries
        buf, parts, prev = "", [], " "
    if parts:                                # the command ends inside a continuation or an open quote
        out.append((buf, parts))
    for ent in pending:
        # A heredoc whose delimiter line never comes (`EOF;ls` is not `EOF`) runs to the end of the text, and the
        # shell still expands `$(...)` and backticks in an unquoted body.
        if ent[1]:
            bodies.append("\n".join(ent[4]))
    return out, bodies, line_docs


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
    if "$'" not in text and '$"' not in text and "\\" not in text:
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
        elif ch == "$" and text.startswith('$"', i):
            i += 1                              # a locale string `$"..."` is the double-quoted string itself
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
    if "$(" not in text and "`" not in text and "<(" not in text and ">(" not in text:
        return text                    # a substitution opens with one of these four and nothing else
    spans = _scan(text)[3]
    if not spans:
        return text
    out, last = [], 0
    for start, end in spans:
        out.append(text[last:start])
        out.append("_")
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
    return [stmt for stmt, _piped in _statements_piped(line)]


def _statements_piped(line: str) -> list[tuple[list[str], bool]]:
    """`_statements` with, for each statement, whether it reads the output of the statement before it (a `|`
    or `|&` is the last separator between them; a newline after a pipe continues the pipeline)."""
    stmts: list[list[str]] = [[]]
    piped: list[bool] = [False]
    sep: str | None = None
    for segment in _unquoted_newline_segments(_plain_ansi_c(_mask_substitutions(line))):
        segment = _strip_redirections(segment)
        lex = shlex.shlex(segment, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""                 # comments were removed above: a mid-word `#` is an ordinary character
        tokens = list(lex)  # ValueError on unbalanced quotes: the caller fails open
        stmts.append([])
        piped.append(False)
        if sep is None:
            sep = "\n"
        for tok in tokens:
            if tok in ("{", "}") and not _brace_is_reserved(stmts[-1]):
                stmts[-1].append(tok)       # a brace is a reserved word at the start of a command only
            elif tok and set(tok) <= set(";&|(){}"):
                stmts.append([])
                piped.append(False)
                if not (sep in ("|", "|&") and tok in ("(", "{")):
                    sep = tok
            else:
                if not stmts[-1]:
                    piped[-1] = sep in ("|", "|&")
                    sep = None
                stmts[-1].append(tok)
    return [(s, p) for s, p in zip(stmts, piped) if s]


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


_XARGS_SHORT_VALUE = frozenset("aEdILnPs")
_XARGS_LONG_VALUE = frozenset({"--arg-file", "--delimiter", "--eof", "--max-args", "--max-procs", "--max-chars",
                               "--max-lines", "--process-slot-var"})
_PARALLEL_SHORT_VALUE = frozenset("jnNSaduLPsIrC")
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}


def _parallel_text(words: list[str]) -> str | None:
    """The command line GNU `parallel [opts] CMD ARGS ::: INPUTS` hands to a shell: the words after its own
    options up to the first `:::`, joined by blanks. It is read again as shell text (a quoted list, a
    substitution or a newline inside the argument is a command there)."""
    if not words or os.path.basename(words[0]) != "parallel":
        return None
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += _option_width(words[i], _PARALLEL_SHORT_VALUE, frozenset())
    if i < len(words) and words[i] == "--":
        i += 1
    rest: list[str] = []
    for w in words[i:]:
        if w.startswith(":::"):
            break
        rest.append(w)
    return " ".join(rest) or None


def _exec_inner(words: list[str]) -> list[list[str]]:
    """The commands that `xargs [opts] CMD ...`, `find ... -exec CMD ... ;` and `parallel [opts] CMD ::: ...`
    run: their words, so the recogniser reads them like any other statement."""
    if not words:
        return []
    base = os.path.basename(words[0])
    out: list[list[str]] = []
    if base in ("xargs", "parallel"):
        short = _XARGS_SHORT_VALUE if base == "xargs" else _PARALLEL_SHORT_VALUE
        long = _XARGS_LONG_VALUE if base == "xargs" else frozenset()
        i = 1
        while i < len(words) and words[i].startswith("-") and words[i] != "--":
            i += _option_width(words[i], short, long)
        if i < len(words) and words[i] == "--":
            i += 1
        rest = words[i:]
        if base == "parallel":
            for stop, w in enumerate(rest):
                if w.startswith(":::"):
                    rest = rest[:stop]
                    break
        if rest and base == "parallel":
            # GNU parallel hands the whole command line to a shell, which parses it again
            try:
                rest = shlex.split(" ".join(rest))
            except ValueError:
                pass
        if rest:
            out.append(rest)
    elif base == "find":
        for i, w in enumerate(words):
            if w in _FIND_EXEC and i + 1 < len(words):
                out.append(words[i + 1:])
    return out


def _is_doctor_fix(words: list[str]) -> bool:
    """`openclaw [global options] doctor ... --fix`: the global options (`--profile p`, `--log-level l`,
    `--container c`, flags) are skipped the way the gateway verbs skip them."""
    if not words or os.path.basename(words[0]) != "openclaw" or "--fix" not in words[1:]:
        return False
    if "doctor" in words[1:3]:
        return True
    rest = words[1:1 + 8]
    n = 0
    while n < len(rest) and rest[n].startswith("-"):
        n += _option_width(rest[n], frozenset(), _OPENCLAW_GLOBAL_LONG_VALUE)
    return n < len(rest) and rest[n] == "doctor"


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
_MAX_DEPTH = 40    # how many wrappers (`bash -c`, `eval`, `env -S`, `$(...)`) are followed before the fallback
_WORK_BUDGET = 4 * _MAX_COMMAND_CHARS   # characters read over one whole command, nested strings included
_work = 0
_refused_found: str | None = None   # a verdict read from a bounded view when `_charge()` refused an expansion
_VIEW_MAX_REFS = 64              # distinct pieces joined in the bounded view of a refused expansion
_PRINTF_VIEW_CHARS = 65536       # size cap of the bounded view of a refused printf expansion
_SHAPE_COST = 256                # charged for every shape() call and every logical line, whatever its size
_MAX_EXEC_EXPANSIONS = 64      # `xargs`/`find -exec` command lists followed per command line
# The word view cuts a text into statements at these: `;`, `&`, `|`, a parenthesis (`$(`, `<(`, a subshell) and a
# newline, and at a backtick, so `` `openclaw `` and `restart` `` ` are the words `openclaw` and `restart` and not
# words glued to the backtick that opens or closes the substitution.
_SEGMENT = re.compile(r"[;&|()`\n]+")
# An INNERMOST inline substitution: no parenthesis, no backtick and no `$(` inside it.
_INLINE_SUBST = re.compile(r"\$\([^()`]*\)|`[^`()]*`")
_UNKNOWN_VERB = re.compile(r"[$`*?\[{~]")


class _DeadlineExceeded(BaseException):
    """The wall-clock deadline of the analysis expired. A BaseException so that no `except Exception` inside the
    parser can swallow it."""


_DEADLINE_DEFAULT_S = 4.0
_DEADLINE_MAX_S = 8.0                    # the hook's own timeout fails open: never wait for it
_deadline_at: float | None = None        # time.monotonic() value at which the precise analysis is abandoned
_last_deadline_fired = False


def _check_deadline() -> None:
    """Raise when the deadline has passed. The signal handler does the same where signals work; this check is
    for threads and platforms without SIGALRM, and costs one clock read where it is called."""
    if _deadline_at is not None and time.monotonic() > _deadline_at:
        raise _DeadlineExceeded()


def _on_alarm(_signum, _frame) -> None:
    if _deadline_at is not None:
        raise _DeadlineExceeded()


def _deadline_seconds() -> float:
    """4 s by default; `KIT_GUARD_DEADLINE_S` overrides it (tests), never above 8 s and never below 10 ms."""
    try:
        value = float(os.environ.get("KIT_GUARD_DEADLINE_S", ""))
    except ValueError:
        return _DEADLINE_DEFAULT_S
    if value != value:                   # NaN
        return _DEADLINE_DEFAULT_S
    return min(max(value, 0.01), _DEADLINE_MAX_S)


_CHEAP_CAP = 8 * 1024 * 1024             # characters the cheap view reads (the head and the tail when larger)
_CHEAP_WINDOW = 160                      # characters between the gateway word and its verb
_CHEAP_QUOTES = re.compile(r"['\"\\]")
_CONTINUATION = re.compile(r"(?<!\\)((?:\\\\)*)\\\n")   # an odd run of backslashes, then a newline
_ANSI_C_STRING = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_ANSI_C_BUDGET = 1 << 20                 # characters of `$'...'` strings decoded per reading
# ONE token matcher for the three word views (the cheap view, its strict reading and the fail-closed view, kit
# 2.2e): the unit `openclaw-gateway` (not `openclaw-gateway-x`), a unit glob or brace form that can reach it
# (`openclaw-gate*`, `openclaw-{gateway,x}`), `openclaw` as a command word (not `.openclaw`, not `openclaw.json`,
# not a path component, not another unit), the bare word `gateway`, the stop/restart verbs, `doctor` and `--fix`.
# `openclaw` is also a command word when a unit name is in the making (`openclaw-{a,b}`), and a path-qualified
# command (`/usr/bin/openclaw`) or `openclaw.mjs` is one.
_UNIT_GLOB = r"(?<![a-z0-9_.-])openclaw-[a-z0-9_.,-]*[*?\[{][a-z0-9_.,*?\[\]{}-]*"
_VERB_RX = r"(?:try-restart|reload-or-restart|try-reload-or-restart|condrestart|restart|stop|kill)"
_CHEAP_TOKEN = re.compile(
    r"(?P<unit>(?<![a-z0-9_.-])openclaw-gateway(?!-[a-z0-9_]))|(?P<uglob>" + _UNIT_GLOB + r")|"
    r"(?P<oc>(?<![a-z0-9_.-])openclaw(?:\.mjs)?(?:(?=-[{*?\[])|(?![a-z0-9_/-])(?!\.[a-z0-9])))|(?P<gw>gateway)|"
    r"(?P<verb>(?<![a-z0-9])" + _VERB_RX + r"(?![a-z0-9]))|"
    r"(?P<doc>(?<![a-z0-9])doctor(?![a-z0-9]))|(?P<fix>--fix(?![a-z0-9]))")
_UNIT_GLOB_RX = re.compile(_UNIT_GLOB)
_UNIT_GLOB_CHECKS = 2000                 # unit-glob tokens expanded per reading; past it every one counts


def _decode_text(text: str) -> str:
    """`text` with a text above the cap cut to its head and its tail, line continuations undone (the shell removes
    a backslash-newline) and `$'...'` strings decoded (`$'gat\\x65way'` is `gateway`). Quotes are kept."""
    if len(text) > _CHEAP_CAP:
        text = text[:_CHEAP_CAP // 2] + "\n" + text[-_CHEAP_CAP // 2:]
    left = [_ANSI_C_BUDGET]

    def decode(m: re.Match) -> str:
        body = m.group(1)
        if len(body) > left[0]:
            return m.group(0)
        left[0] -= len(body)
        return _decode_ansi_c(body)

    # a decoded string can hold a continuation (`$'open\\\nclaw'` is `open\<newline>claw` once decoded, and the
    # shell that reads that text removes it) or another `$'...'`: a few rounds, each linear
    for _ in range(4):
        before = text
        text = _CONTINUATION.sub(r"\1", text)      # a backslash that is not itself escaped, then a newline
        if "$'" in text:
            text = _ANSI_C_STRING.sub(decode, text)
        if text == before:
            break
    return text


def _words_of(decoded: str) -> str:
    """The text the word views read: lower-cased, every backslash-newline that is left removed (wide on purpose:
    `\\\\` + newline reads as one too), quotes and backslashes removed (`re''start`)."""
    return _CHEAP_QUOTES.sub("", decoded.replace("\\\n", "").lower())


def _normalise(text: str) -> str:
    """`text` as the word views read it (`_decode_text`, then `_words_of`). No tokenising, no recursion."""
    return _words_of(_decode_text(text))


# What the strict reading excludes (kit 2.2e). The second view is the net for text the parser cannot follow, so it
# reads the gateway words WIDE: `gateway` and a verb within `_CHEAP_WINDOW` characters deny, whatever spells
# `openclaw` (a variable, an ANSI-C escape, a glob, a backtick) and wherever it sits, and the narrowing is done by
# EXCLUDING contexts that are known to be harmless:
#   - another product's gateway: a name glued to a plain one (`api-gateway`, `deploy/gateway`, `gateway-proxy`),
#     a statement run by another product's CLI (`docker restart gateway`), `systemctl restart gateway` (a unit
#     called `gateway`);
#   - words that are only printed: `echo`, `printf` and `:` reached through `eval`, `bash -c`, `find -exec`, `<(...)`
#     or `$(...)`, whose output is not piped, redirected or substituted into something that runs it
#     (`echo ... | sh` and `$(echo ...)` stay denied; a bare `echo ...` statement of the text is still read);
#   - `openclaw gateway status --note restart`: the first subcommand decides, as it does for the CLI.
# An `openclaw` word within the window on either side keeps a denial except for the last two. Words in two
# different statements pair only through a stored name (`g=gateway; ...`).
_OTHER_PRODUCT_COMMANDS = frozenset({
    "docker", "podman", "nerdctl", "crictl", "ctr", "docker-compose", "kubectl", "helm", "k3s", "k9s", "minikube",
    "pm2", "forever", "supervisorctl", "supervisord", "nginx", "service", "rc-service", "initctl"})
_PRINTING_COMMANDS = frozenset({"echo", "printf", ":"})
_PRINTING_PASSTHROUGH = _TRANSPARENT_WORDS = frozenset({
    "sudo", "doas", "env", "command", "builtin", "nohup", "time", "exec", "nice", "setsid", "stdbuf", "timeout",
    "eval", "then", "do", "else", "elif", "if", "while", "until", "!", "sh", "bash", "zsh", "dash", "ksh"})
_COMPOUND_OPENERS = frozenset({"do", "then", "else", "elif"})
_RUNS_ITS_INPUT = frozenset({"source", ".", "eval", "exec", "sh", "bash", "zsh", "dash", "ksh"})
_NOT_A_VERB = frozenset({"status", "start", "install", "uninstall", "health", "probe", "call", "discover", "run",
                         "logs", "usage-cost"})        # `openclaw gateway <one of these>`: the first one decides
_STATEMENT_BREAK = re.compile(r"[;&|\n]")
_STATEMENT_END = re.compile(r"[;&|\n)}]")
_WORD_BEFORE = re.compile(r"[^\s;&|<>]*$")
_ASSEMBLY_CHARS = frozenset("$`*?[{}()\\")
_STRICT_PAIR_BUDGET = 2048               # exclusion checks per reading; past it every pair counts (fail closed)
_LINE_LOOKAHEAD = 4096                   # characters of a line read after the words of a printing statement


def _statement_head(low: str, pos: int) -> str:
    """The text of the statement that holds `pos`, from its start to `pos` (at most 400 characters back)."""
    lo = max(0, pos - 400)
    start = max(max(low.rfind(c, lo, pos) for c in ";&|\n"), lo - 1) + 1
    return low[start:pos]


def _another_product(low: str, pos: int) -> bool:
    """True when the statement that holds `pos` is run by another product's command."""
    words = _statement_head(low, pos).split()
    k = 0
    while k < len(words):
        w = words[k].lstrip("($`{")
        if w in ("-o", "+o", "-O", "+O"):
            k += 2                           # a shell option and its value
        elif (not w or w.startswith(("-", "+", "<", "%")) or _ASSIGNMENT.match(w)
              or os.path.basename(w) in _TRANSPARENT_WORDS or os.path.basename(w) in _PRINTING_COMMANDS):
            k += 1                           # the printed text of `echo "docker ..." | sh` is read as the command
        elif os.path.basename(w) in _OTHER_PRODUCT_COMMANDS:
            return True
        elif _ASSEMBLY_CHARS.intersection(w):
            return False                     # an assembled command word (`${o}claw`): what follows is its options
        else:                                # a plain wrapper (`ssh host docker ...`): another product further on
            return any(os.path.basename(x.lstrip("($`{")) in _OTHER_PRODUCT_COMMANDS for x in words[k + 1:k + 6])
    return False


def _another_unit(low: str, gw: int) -> bool:
    """True for a `gateway` that is part of another name or path (`api-gateway`, `deploy/gateway`,
    `gateway-proxy`, `~/.openclaw/gateway`), or the plain unit `gateway` of `systemctl restart gateway`. A name
    that is being assembled (a backtick, `$`, a glob or a brace in front of it) is not another name."""
    prefix = _WORD_BEFORE.search(low, max(0, gw - 96), gw).group()
    if prefix:
        return prefix[-1] in "-/.:@_" and not _ASSEMBLY_CHARS.intersection(prefix)
    follow = low[gw + 7:gw + 9]
    if len(follow) == 2 and follow[0] == "-" and follow[1].isalnum():
        return True
    head = _statement_head(low, gw)
    if any(c in head for c in "$`({"):
        return False
    return (any(os.path.basename(w) == "systemctl" for w in head.split()[:8])
            and low[gw + 7:gw + 8] not in ("$", "`", "*", "?", "[", "{"))


_STDIN_READERS = frozenset({"read", "mapfile", "readarray"})
# `X < <(...)`, `X 0< <(...)`, `exec 3< <(...)`, and the here-string `read -r c <<< '...'; eval "$c"`
_PROC_STDIN = re.compile(r"[0-9]*[ \t]*<(?:<<|[ \t]*<\()")
_STMT_FIRST_EXPANSION = re.compile(r"(?:^|[;&|\n({]|\b(?:then|do|else|elif)\b)[ \t]*[\"']?\$")
_STDIN_LOOKAHEAD = 4000                  # characters read after a process substitution that feeds `read`/`mapfile`
_PROC_STDIN_CHECKS = 64                  # `< <(` operators examined per text; past it the text counts as running


def _later_runs(low: str, at: int) -> bool:
    """True when the text after `at` runs something that a `read` or `mapfile` may have stored: `eval`, `source`,
    a shell, `exec`, or a statement that starts with an expansion (`$c`, `"$c"`). Beyond the lookahead: True."""
    rest = low[at:at + _STDIN_LOOKAHEAD]
    if len(low) - at > _STDIN_LOOKAHEAD:
        return True
    if _STMT_FIRST_EXPANSION.search(rest) is not None:
        return True
    return any(os.path.basename(t) in _RUNS_ITS_INPUT for t in re.findall(r"[^\s;&|(){}<>\"'`]+", rest))


def _stdin_owner_runs(low: str, op: int, readers_only: bool = False) -> bool:
    """True when the command that owns the redirection `< <(...)` at `op` runs what it reads: a shell word,
    `eval`, `source`, `.` or `exec` (`exec 3< <(...); sh <&3`), or a `read`/`mapfile` whose stored text is run
    later. The owner of a compound closer (`done < <(...)`, `} < <(...)`, `) < <(...)`) is any word of the 400
    characters before it. `cat < <(...)` and `diff <(...) <(...)` only print."""
    lo = max(0, op - 400)
    cut = max(low.rfind(c, lo, op) for c in ";&|\n")
    head = low[max(cut, lo - 1) + 1:op]
    tail = head.rstrip()
    if tail.endswith((")", "}")) or tail.split()[-1:] in (["done"], ["fi"], ["esac"]):
        head = low[lo:op]                   # a loop or a group owns it: read what is in it
    words = [os.path.basename(w.strip("(){}\"'`;!")) for w in head.split()]
    if not readers_only and any(w in _RUNS_ITS_INPUT for w in words):
        return True                          # (a shell given a here-string is read by `_SHELL_WITH_HEREDOC`)
    return any(w in _STDIN_READERS for w in words) and _later_runs(low, op)


def _runs_process_substitution_stdin(low: str) -> bool:
    """True when `low` has a `< <(...)` (or a `<<<` here-string) whose owner runs what it reads (see
    `_stdin_owner_runs`)."""
    for n, m in enumerate(_PROC_STDIN.finditer(low)):
        if n >= _PROC_STDIN_CHECKS or _stdin_owner_runs(low, m.start(), m.group().endswith("<<<")):
            return True
    return False


def _prints_only(low: str, a: int, b: int) -> bool:
    """True when the words at `a`..`b` are the arguments of a command that only prints them (`echo`, `printf`,
    `:`) and nothing takes that output as commands: the statement is not piped on, not redirected, has no
    substitution, and is not itself the body of a `$(...)` or backtick (`:` is the exception: it prints nothing,
    and what is inside `$(: ...)` is nothing)."""
    lo = max(0, a - 400)
    cut = max(low.rfind(c, lo, a) for c in ";&|\n(`{")
    start = max(cut, lo - 1) + 1
    opener = low[cut] if cut >= lo and low[cut] in "(`{" else ""
    words = low[start:a].split()
    k = 0
    while k < len(words):
        w = words[k]
        base = os.path.basename(w)
        if w in ("-o", "+o", "-O", "+O"):
            k += 2                           # a shell option and its value (`bash +O extglob -c ...`)
        elif w.startswith(("-", "+")) or _ASSIGNMENT.match(w) or base in _PRINTING_PASSTHROUGH:
            k += 1
        elif base == "find":                 # `find . -exec CMD ... ;`: the command is what follows -exec
            k = next((j + 1 for j in range(k + 1, len(words)) if words[j] in _FIND_EXEC), len(words))
        else:
            break
    if k >= len(words) or os.path.basename(words[k]) not in _PRINTING_COMMANDS:
        return False
    command = os.path.basename(words[k])
    if command == "printf" and any(w.startswith("-v") for w in words[k + 1:]):
        return False                         # `printf -v x ...` prints nothing: it stores the text in a variable
    if k == 0 and not opener and command != ":":
        return False                         # a bare `echo ...` statement of the text is still read (T46 price)
    if command != ":":
        if opener == "`" or (opener == "(" and low[cut - 1:cut] == "$"):
            return False                     # `$(echo ...)`: the output is what runs
        if opener == "(" and low[cut - 1:cut] == "<":
            before = low[max(0, cut - 40):cut - 1].split()
            if not before or os.path.basename(before[-1]) in _RUNS_ITS_INPUT:
                return False                 # `source <(echo ...)`, `bash <(echo ...)`
            # `sh < <(echo ...)`, `exec 3< <(echo ...)`: the word in front is the redirect operator, the command
            # that owns it comes before (kit 2.2f, 1)
            if re.fullmatch(r"[0-9]*<", before[-1]) is not None and _stdin_owner_runs(low, low.rfind("<", 0, cut - 1)):
                return False
    if _STATEMENT_BREAK.search(low, a, b) is not None:
        return False
    # The statement is read to the end of its LINE, not to the first `;`: a quoted `;` inside the printed text
    # is no boundary (quotes are gone from the text), and `echo 'a; b' | sh` pipes what it printed. A line that
    # runs on past `_LINE_LOOKAHEAD` characters is not read to its end: it is not a printing statement.
    eol = low.find("\n", b, b + _LINE_LOOKAHEAD)
    if opener in ("(", "{") or any(os.path.basename(w) in _COMPOUND_OPENERS for w in words[:k]):
        eol = -1                             # a group or compound body: its closer and `| sh` may be lines later
    if eol < 0:
        if len(low) - b > _LINE_LOOKAHEAD:
            return False
        eol = len(low)
    body = low[start:eol]
    return not any(c in body for c in ("|", ">", "`", "$("))


def _gateway_answered(low: str, gw: int, verb: int, raw: str, plain: dict[str, bool]) -> bool:
    """True when another subcommand stands between `gateway` and the verb: `openclaw gateway status --note
    restart` is `status`. The subcommand must also stand as a whole word in the text as it was written (`raw`,
    lower-cased): a `status` that only exists once quotes, escapes or an ANSI-C string are resolved (`$'sta'tus`
    read by a shell that does not know `$'...'`) does not answer anything."""
    if verb < gw:
        return False
    between = low[gw + 7:verb]
    if _STATEMENT_END.search(between) is not None or "(" in between:
        return False
    for w in between.split():
        if w in _NOT_A_VERB:
            if w not in plain:               # one scan of the text per word and reading, not one per pair
                plain[w] = re.search(r"(?<![\w$'\"\\.-])" + re.escape(w) + r"(?![\w'\"\\-])", raw) is not None
            if plain[w]:
                return True
    return False


def _strict_pair(low: str, raw: str, plain: dict[str, bool], gw: int, near: bool, unit: bool, verb: int,
                 spent: int) -> bool:
    """True when the gateway word (or the unit) at `gw` and the verb at `verb`, within the window, are read as the
    OpenClaw gateway being stopped or restarted (see the block comment above)."""
    if spent >= _STRICT_PAIR_BUDGET:
        return True
    a, b = (gw, verb) if gw < verb else (verb, gw)
    if _prints_only(low, a, b) or (not unit and _gateway_answered(low, gw, verb, raw, plain)):
        return False
    if near:
        return True
    if _another_unit(low, gw) or _another_product(low, gw) or _another_product(low, verb):
        return False
    if _STATEMENT_BREAK.search(low, a, b) is None:
        return True
    return low[gw - 1:gw] == "=" or low[verb - 1:verb] == "="       # a name stored in a variable


def _cheap_view(text: str, strict: bool = False) -> str | None:
    """The word-level view that decides when the precise analysis cannot (the deadline expired, or it failed):
    "restart", "stop" or "doctor" when `gateway` and a stop/restart verb sit within `_CHEAP_WINDOW` characters of
    each other, or `doctor` and `--fix` do; None otherwise. One regular-expression pass over the normalised text
    (`_normalise`): no tokenising, no recursion, no nested loops.

    `strict` is the second-opinion reading (text that feeds a shell, see `decide`): the same pairs, minus the
    contexts that are known to be harmless (see `_strict_pair`); an `openclaw` word within the window on either
    side, or the unit itself (a glob or brace form that reaches it included), keeps a pair. The deadline fallback
    is not strict: it fails toward the words, and a benign text that names a gateway and a verb is denied when
    the deadline fires (documented in T46)."""
    decoded = _decode_text(text)
    low = _words_of(decoded)
    raw = decoded.lower()                    # quotes kept (for `_gateway_answered`)
    plain: dict[str, bool] = {}              # subcommand word -> it stands as a whole word in `raw`
    far = -_CHEAP_WINDOW * 4
    last_verb = last_doc = last_fix = last_oc = far
    gws = [[far, False, False], [far, False, False]]   # [position, OpenClaw word next to it, is the unit]: the
    # latest `gateway`, and the latest one stored in a variable (`g=gateway`, strict reading only)
    found: str | None = None
    verb_text = ""
    spent = globs = 0
    for m in _CHEAP_TOKEN.finditer(low):
        pos = m.start()
        kind = m.lastgroup
        if kind == "uglob":
            globs += 1
            kind = "unit" if globs > _UNIT_GLOB_CHECKS or _is_gateway_unit(m.group()) else "oc"
        if kind == "oc":
            last_oc = pos
            if strict:
                # an OpenClaw word AFTER the gateway word (`g=gateway; openclaw $g restart`) confirms it
                for slot in gws:
                    if pos - slot[0] <= _CHEAP_WINDOW and not slot[1]:
                        slot[1] = True
                        if abs(slot[0] - last_verb) <= _CHEAP_WINDOW:
                            spent += 1
                            if _strict_pair(low, raw, plain, slot[0], True, slot[2], last_verb, spent):
                                found = _verb_kind(verb_text, found)
            continue
        if kind in ("gw", "unit"):
            near = kind == "unit" or pos - last_oc <= _CHEAP_WINDOW
            gws[0] = [pos, near, kind == "unit"]
            if strict and low[pos - 1:pos] == "=":
                gws[1] = [pos, near, False]
            if pos - last_verb <= _CHEAP_WINDOW:
                spent += 1
                if not strict or _strict_pair(low, raw, plain, pos, near, kind == "unit", last_verb, spent):
                    found = _verb_kind(verb_text, found)
        elif kind == "verb":
            last_verb, verb_text = pos, m.group(0)
            for slot in gws:
                if pos - slot[0] <= _CHEAP_WINDOW:
                    spent += 1
                    if not strict or _strict_pair(low, raw, plain, slot[0], slot[1], slot[2], pos, spent):
                        found = _verb_kind(verb_text, found)
        elif kind == "doc":
            last_doc = pos
            if pos - last_fix <= _CHEAP_WINDOW and found is None:
                found = "doctor"
        else:
            last_fix = pos
            if pos - last_doc <= _CHEAP_WINDOW and found is None:
                found = "doctor"
        if found == "restart":
            return found
    return found


def _verb_kind(verb: str, found: str | None) -> str:
    if verb in ("stop", "kill"):
        return found if found == "restart" else "stop"
    return "restart"


# The deadline FAILS CLOSED (kit 2.2d, 6a). When the clock runs out the precise reading never finished, and the
# cheap view's 160-character window can miss a guarded command that sits further apart than that (an option value
# of 300 characters, a continuation line). So once the deadline has fired, a text that names OpenClaw as a command
# word, or the unit `openclaw-gateway` (a glob or brace form of it included, kit 2.2e), together with any word that
# could be the verb or the subject (`gateway`, `restart`, `stop`, `kill`, `doctor`, `--fix`) anywhere in it is
# denied. It reads words, once, over the same normalised text as the cheap view (continuations undone, ANSI-C
# strings decoded, quotes removed), and is wide on purpose: a benign text that mentions `openclaw` and `stop` is
# denied when the deadline fires, and the precise reading is fast enough that realistic input never reaches this.
_OC_COMMAND_WORD = re.compile(r"(?<![a-z0-9_.-])openclaw(?:\.mjs)?(?:(?=-[{*?\[])|(?![a-z0-9_/-])(?!\.[a-z0-9]))")
_FAIL_CLOSED_WORD = re.compile(
    r"(?P<restart>(?<![a-z0-9])(?:try-restart|reload-or-restart|try-reload-or-restart|condrestart|restart)"
    r"(?![a-z0-9])|gateway)|(?P<stop>(?<![a-z0-9])(?:stop|kill)(?![a-z0-9]))|"
    r"(?P<doctor>(?<![a-z0-9])doctor(?![a-z0-9])|--fix(?![a-z0-9]))")


def _names_openclaw(low: str) -> bool:
    """True when the normalised text names OpenClaw: a command word, the unit, or a glob/brace form of the unit."""
    if "openclaw-gateway" in low or _OC_COMMAND_WORD.search(low):
        return True
    for n, m in enumerate(_UNIT_GLOB_RX.finditer(low)):
        if n >= _UNIT_GLOB_CHECKS or _is_gateway_unit(m.group()):
            return True
    return False


def _fail_closed_view(text: str) -> str | None:
    """"restart", "stop" or "doctor" when `text` names OpenClaw (a command word, the unit, or a glob or brace form
    of the unit) and any guarded word, wherever they sit; None otherwise. One pass over the normalised text
    (`_normalise`); a text above the cap is read as its head and its tail."""
    low = _normalise(text)
    if not _names_openclaw(low):
        return None
    kinds = {m.lastgroup for m in _FAIL_CLOSED_WORD.finditer(low)}
    for kind in ("restart", "stop", "doctor"):
        if kind in kinds:
            return kind
    return None


# What makes a text one that FEEDS A SHELL, read without parsing it. A shell given its own literal `-c` string
# does not read its commands from standard input (that string is read by the parser), so it is not a consumer.
_PATHED = r"(?:[\w./-]*/)?"
_SHELLS_RX = r"(?:sh|bash|zsh|dash|ksh)"
_LIT_SQ = r"'(?:[^'\\\n$`]|\\.){0,2000}'"
_LIT_DQ = r'"(?:[^"\\\n$`]|\\.){0,2000}"'
_LITERAL_C = r"[^;&|\n]{0,160}?[ \t]-[A-Za-z]*c[ \t]+\$?(?:" + _LIT_SQ + r"|" + _LIT_DQ + r")"
# a runner (`xargs -I{} sh -c '{}'`) puts its input where `{}` stands: such a string is not a literal
_LITERAL_C_RUNNER = (r"[^;&|\n]{0,160}?[ \t]-[A-Za-z]*c[ \t]+\$?(?:'(?:[^'\\\n$`{}]|\\.){0,2000}'|"
                     r'"(?:[^"\\\n$`{}]|\\.){0,2000}")')
_STDIN_SHELL = _PATHED + _SHELLS_RX + r"(?![\w./-])(?!" + _LITERAL_C + r")"
_RUNNER = (r"(?:xargs|parallel)\b[^|;&\n]{0,160}?[ \t'\"/]" + _SHELLS_RX + r"(?![\w./-])(?!" + _LITERAL_C_RUNNER + r")")
_PRELUDE = (r"(?:(?:sudo|doas|env|exec|command|nohup|setsid|time|nice|stdbuf|timeout)[ \t]+"
            r"(?:-\S+[ \t]+|\w+=\S*[ \t]+)*)*")
# a pipe whose consumer reads commands: a shell, `eval "$(cat)"`, `. /dev/stdin`, or a runner that starts a shell
_PIPE_TO_SHELL = re.compile(
    r"\|&?[ \t\n]*" + _PRELUDE + r"(?:" + _STDIN_SHELL + r"|eval(?![\w./-])[^;&|\n]{0,160}?(?:\$\(|`)|"
    r"(?:source|\.)[ \t]+(?:/dev/stdin|/dev/fd/\d+|-)(?![\w./-])|" + _RUNNER + r")")
_HEREDOC_CONSUMER = (r"(?:" + _STDIN_SHELL + r"|(?:eval|source)(?![\w./-])|\.(?=[ \t]+\S)|" + _RUNNER + r")")
_SHELL_WITH_HEREDOC = re.compile(
    r"(?:(?:^|[;&|(){\n]|\$\(|\b(?:then|do|else|elif)\b|\bxargs\b|\bparallel\b)[ \t]*" + _PRELUDE +
    _HEREDOC_CONSUMER +
    # after a quote or a single backtick: a shell word that is followed by a blank, not by the closing quote
    # of a string or of an inline code span (`bash` in prose)
    r"|(?<!`)[`'\"][ \t]*" + _PRELUDE + _PATHED + r"(?:" + _SHELLS_RX + r"|eval|source)(?![\w./-])(?=[ \t]))"
    # ... and the heredoc operator or here-string sits in the same statement (no `;`, `&`, `|` or newline between)
    r"(?:[^;&|\n]|\\\n){0,400}?<<")


def _feeds_a_shell_text(text: str) -> bool:
    """True when `text` looks like it hands text to a shell: a pipe into a shell, or a heredoc operator or
    here-string next to a shell word that stands where a command stands."""
    if len(text) > _MAX_COMMAND_CHARS:
        return False         # above the cap `_shape` already read the words alone; the expressions are not run
    low = text.lower()
    if "|" in low and _PIPE_TO_SHELL.search(low):
        return True
    if ("<(" in low or "<<<" in low) and _runs_process_substitution_stdin(low):
        return True                          # `sh < <(echo ...)`: a shell reads the output of a command
    return "<<" in low and _SHELL_WITH_HEREDOC.search(low) is not None


_DATA_COMMANDS = {"echo", "printf", "print", "grep", "egrep", "fgrep", "rg"}
_TRANSPARENT = {"sudo", "env", "command", "nohup", "time", "exec", "nice", "setsid", "stdbuf"}


_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?$")


def _plain(w: str) -> str:
    """A word of the quote-stripped fallback text, without a leftover `$` of a `$'...'` string."""
    return w.lstrip("$")


def _is_data_command(words: list[str]) -> bool:
    """True when the statement's command (after assignments, wrappers and their options) only prints or
    searches its arguments: `echo openclaw gateway restart` runs nothing. A shell's `-c` string and an `eval`
    argument are the command they hold."""
    for k, w in enumerate(words):
        w = _plain(w)
        if w.startswith("-") or _ASSIGNMENT.match(w) or _DURATION.match(w) or w in _TRANSPARENT or \
                os.path.basename(w) in _TRANSPARENT or w == "timeout":
            continue
        base = os.path.basename(w)
        if base == "eval":
            return _is_data_command(words[k + 1:])
        if base in _SHELLS:
            for j in range(k + 1, len(words)):
                if re.fullmatch(r"-[a-z]*c[a-z]*", words[j]):
                    return _is_data_command(words[j + 1:])      # the string after -c is the command
            for j in range(k + 1, len(words)):
                if words[j].startswith("<<<"):                  # `sh <<<'text'`: the text is the command
                    return _is_data_command(([words[j][3:]] if words[j][3:] else []) + words[j + 1:])
            return False
        return base in _DATA_COMMANDS
    return False


def _feeds_a_shell(words: list[str]) -> bool:
    """True for a statement that runs text it is given: `eval`, `source`, or a shell without a `-c` string.
    A shell fed a here-string reads the text after `<<<` as its commands (a data command there is harmless)."""
    for k, w in enumerate(words):
        w = _plain(w)
        if w.startswith("-") or _ASSIGNMENT.match(w) or os.path.basename(w) in _TRANSPARENT:
            continue
        base = os.path.basename(w)
        if base in ("eval", "source", "."):
            return True
        if base in _SHELLS:
            rest = words[k + 1:]
            if any(re.fullmatch(r"-[a-z]*c[a-z]*", r) for r in rest):
                return False
            hs = next((j for j, r in enumerate(rest) if r.startswith("<<<")), None)
            if hs is not None:
                return not _is_data_command(([rest[hs][3:]] if rest[hs][3:] else []) + rest[hs + 1:])
            return True
        return False
    return False


def _word_segments(text: str) -> tuple[list[str], bool]:
    """The statements of `text` for the word view (quotes and backslashes removed, cut at `;&|()` backticks and
    newlines) and whether none of them feeds a shell (then a data command's words are skipped)."""
    segments = _SEGMENT.split(re.sub(r"['\"\\]", "", text.lower()))
    return segments, not any(_feeds_a_shell(seg.split()) for seg in segments)


def _verb_next_to_gateway(text: str, prepared: tuple[list[str], bool] | None = None) -> str | None:
    """"stop" or "restart" when `text` (a string that could not be tokenised, or nests too deep to follow)
    has a stop/restart verb after the gateway: `openclaw [opts] gateway [opts] <verb>` or `<verb> [opts]
    openclaw-gateway[.service]`. The words alone (`echo openclaw gateway`, or a sentence that has `openclaw`,
    `gateway` and `restart` far apart) are not enough (L4): the fallback denies only where a restart or stop is
    actually being asked for, that is when the verb is within `_NEAR` positional words of `gateway` (options
    and their values do not count) and `gateway` within `_NEAR` words of `openclaw`. An expansion, a glob or a
    brace where the verb goes hides it: fail closed. Every scan is a window over the words of one statement
    (`_NEAR` words), so the work per `openclaw` word is bounded; the whole text is charged to the work budget by
    its caller (`_text_shape`). Measured, not claimed: a 252 KB text of `openclaw` words in a 9-deep nest is
    answered in 0.6 s and 240 KB of adjacent `$(a)` in 1.1 s through the whole hook (T46 has the table)."""
    # Quotes and backslashes are dropped first so a verb split by shell quoting joins up (`re''start`,
    # `res\tart` -> `restart`); `$` and backticks stay in the words so an expansion can be seen. Case is
    # folded: the CLI is the one path that could still reach the unit through a case variant.
    found: str | None = None
    segments, skip_data = prepared if prepared is not None else _word_segments(text)
    for seg in segments:
        words = seg.split()
        count = len(words)
        if skip_data and _is_data_command(words):
            continue
        for i, w in enumerate(words):
            if os.path.basename(w) == "openclaw":
                g = next((j for j in range(i + 1, min(count, i + 1 + _NEAR)) if words[j] == "gateway"), None)
                if g is None:
                    continue
                seen, j = 0, g + 1
                while j < count and seen < _NEAR:
                    v = words[j]
                    if v.startswith("-"):
                        j += _option_width(v, frozenset(), _GATEWAY_LONG_VALUE)
                        continue
                    if seen == 0 and _UNKNOWN_VERB.search(v):
                        return "restart"
                    seen += 1
                    j += 1
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
    """The fallback for text the recogniser cannot (or may not) follow any further (the work budget is spent, the
    nesting is past the limit, or the text does not tokenise): "restart", "stop" or "doctor" when the words are
    there, None when they are not. It reads words, not commands. A line continuation is undone first, and an
    inline substitution (`$(...)`, a backtick pair) in a statement is one unknown word of that statement, so a
    padded command cannot turn a denied form into an allowed one."""
    global _work
    _work += len(text)                       # the fallback is charged against the same bound
    text = text.replace("\\\n", "")         # `openclaw gateway \<newline> restart` is one statement
    prepared = _word_segments(text)          # read once, by both passes below
    found = _verb_next_to_gateway(text, prepared)
    if found == "restart":
        return found
    # The same words with every inline substitution (`$(...)`, a backtick pair) collapsed to one placeholder
    # word: `openclaw --profile $(echo p) gateway restart` is ONE statement whose option value is the
    # substitution, which the boundary split above cuts in three.
    # The placeholder is `$x`, an expansion: where it stands in the verb slot (`openclaw gateway `echo restart``)
    # it is an unknown verb and fails closed, in an option slot it is the option's value and is skipped. A
    # substitution that names openclaw is a statement of its own: it is kept, so that its words are read.
    collapsed = text
    if "openclaw" in text.lower() and ("$(" in text or "`" in text):   # nothing to collapse without both
        for _ in range(3):                   # a few levels of nesting, innermost first; linear each time
            collapsed, n = _INLINE_SUBST.subn(
                lambda m: m.group(0) if "openclaw" in m.group(0).lower() else " $x ", collapsed)
            if collapsed == text or not n:
                break
    if collapsed != text:
        again = _verb_next_to_gateway(collapsed)
        if again == "restart":
            return again
        found = found or again
    segments, skip_data = prepared
    for seg in segments:
        words = seg.split()
        if "doctor" not in words or "--fix" not in words or (skip_data and _is_data_command(words)):
            continue
        last_fix = max(j for j, w in enumerate(words) if w == "--fix")
        for i, w in enumerate(words):
            if os.path.basename(w) == "openclaw" and last_fix > i and "doctor" in words[i + 1:i + 7]:
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
    """The statements of each physical line on its own; a line that does not tokenise is skipped.

    This is a second reading of text the caller has read already, so it is charged against the work budget (each
    line's length) and stops when the budget is spent: the caller then reads the whole logical line for words
    (kit 2.2d, 6a)."""
    global _work
    out: list[list[str]] = []
    pending: deque[str] = deque()            # heredoc delimiters whose body is being skipped
    for line in lines:
        if pending:
            if _closes(line, pending[0]):
                pending.popleft()
            continue                         # text fed to a command is not a command
        _work += len(line) + 1
        if _work > _WORK_BUDGET:
            break
        try:
            out += _statements(line)
        except ValueError:
            pass
        if "<<" in line:                     # a heredoc opens with `<<` and nothing else
            pending.extend(_heredoc_delimiters(line))
    return out


_SHELL_WORD = re.compile(r"(?<![\w./-])(?:sh|bash|zsh|dash|ksh)(?![\w.-])")
_TEXT_PRODUCERS = {"echo", "printf", "print"}
_PRINTF_SPEC = re.compile(r"%[-+ #0-9.*]*[a-zA-Z%]")
_MARK = "__KITGUARD_%s%d__"
_MARK_WORD = re.compile(r"__KITGUARD_(HD|HS)(\d+)__$")
_STDIN_SOURCES = {"/dev/stdin", "/dev/fd/0", "-"}


def _redirect_word_end(text: str, j: int) -> int:
    """The end offset of the word that follows a redirection operator at `j` (quotes kept whole)."""
    n = len(text)
    while j < n and text[j] not in " \t\n;&|()<>":
        if text[j] == "\\":
            j += 2
        elif text[j] in "'\"":
            k = text.find(text[j], j + 1)
            j = n if k < 0 else k + 1
        else:
            j += 1
    return min(j, n)


def _mark_redirections(logical: str, entries: list[list], herestrings: list[tuple[int, int]]
                       ) -> tuple[str, dict[str, str]]:
    """`logical` with every real heredoc operator and here-string replaced by a marker word, and the text each
    marker stands for: the heredoc body (None bodies are skipped) or the here-string word, unquoted. The
    marker keeps the redirection attached to the statement it belongs to."""
    edits: list[tuple[int, int, str]] = []
    texts: dict[str, str] = {}
    for k, (_delim, _expands, start, end, body) in enumerate(entries):
        if body is None:
            continue
        while start > 0 and logical[start - 1].isdigit() and (start == 1 or logical[start - 2] in " \t\n;&|()"):
            start -= 1                                   # `3<<EOF`: the fd number belongs to the operator
        mark = _MARK % ("HD", k)
        edits.append((start, end, f" {mark} "))
        texts[mark] = "\n".join(body)
    for k, (start, op_end) in enumerate(herestrings):
        j = op_end
        while j < len(logical) and logical[j] in " \t":
            j += 1
        end = _redirect_word_end(logical, j)
        raw = logical[j:end]
        try:
            value = " ".join(shlex.split(_plain_ansi_c(raw)))
        except ValueError:
            value = raw
        mark = _MARK % ("HS", k)
        edits.append((start, end, f" {mark} "))
        texts[mark] = value
    if not edits:
        return logical, texts
    edits.sort()
    out: list[str] = []
    last = 0
    for start, end, repl in edits:
        if start < last:
            continue
        out.append(logical[last:start])
        out.append(repl)
        last = end
    out.append(logical[last:])
    return "".join(out), texts


def _reads_script_from_stdin(words: list[str]) -> bool:
    """True for `bash`, `sh -x`, `sudo zsh -s -- a`: a shell that is given no `-c` string and no script file
    reads its commands from standard input."""
    n = 1
    s_flag = False
    while n < len(words):
        w = words[n]
        if w == "--":
            n += 1
            break
        if w == "-":
            return True
        if not w.startswith(("-", "+")):
            return s_flag
        if re.fullmatch(r"-[A-Za-z]+", w):
            if "c" in w:
                return False
            if "s" in w:
                s_flag = True
        n += _option_width(w.replace("+", "-", 1) if w.startswith("+") else w,
                           _SHELL_SHORT_VALUE, _SHELL_LONG_VALUE)
    return n >= len(words) or s_flag


def _charge(size: int) -> bool:
    """Charge `size` characters about to be produced against the work budget. False when the budget would be
    exceeded: the caller then does not build the text (the fallback reads the words instead)."""
    global _work
    if _work + size > _WORK_BUDGET:
        _work = _WORK_BUDGET + 1
        return False
    _work += size
    return True


def _note_refused(texts: list[str]) -> None:
    """The budget refused an expansion: nothing reads the text bash would run, so a small bounded view of it
    is read for words instead, and a hit is remembered (`shape()` returns it when the parse found nothing)."""
    global _refused_found
    if _refused_found:
        return
    for text in texts:
        if text and len(text) <= _MAX_COMMAND_CHARS:
            found = _text_shape(text)
            if found:
                _refused_found = found
                return


def _printf_text(args: list[str]) -> str:
    """What `printf FORMAT ARGS...` writes, for the `%s`-style conversions: enough to see a command that is
    spelled through the format. The format is reused while arguments remain, as printf does. The size of the
    result is charged before it is built; past the budget nothing is built."""
    if not args:
        return ""
    fmt, rest = args[0], args[1:]
    specs = sum(1 for m in _PRINTF_SPEC.finditer(fmt) if m.group(0) != "%%")
    passes = 1 if specs == 0 else -(-len(rest) // specs) or 1
    if not _charge(passes * len(fmt) + sum(len(a) for a in rest)):
        # Fail toward the words: the format reused over the DISTINCT arguments (bounded), read once.
        distinct = list(dict.fromkeys(rest))[:_VIEW_MAX_REFS]
        view_passes = 1 if specs == 0 else -(-len(distinct) // specs) or 1
        room = max(1, _PRINTF_VIEW_CHARS // max(1, len(fmt)))
        if view_passes > room:
            distinct = distinct[:room * max(specs, 1)]
        rest, passes = distinct, 1 if specs == 0 else -(-len(distinct) // specs) or 1
        if passes * len(fmt) + sum(len(a) for a in rest) > _MAX_COMMAND_CHARS:
            return ""
    out: list[str] = []
    pos = 0
    for _ in range(passes):
        def conv(m: re.Match) -> str:
            nonlocal pos
            if m.group(0) == "%%":
                return "%"
            if pos < len(rest):
                pos += 1
                return rest[pos - 1]
            return ""
        out.append(_PRINTF_SPEC.sub(conv, fmt))
        if pos >= len(rest) or not _PRINTF_SPEC.search(fmt):
            break
    return "".join(out).replace("\\n", "\n")


def _stdin_command_text(index: int, stmts: list[tuple[list[str], bool]], marks: dict[str, str]) -> list[str]:
    """The texts a shell at `stmts[index]` would read as commands: its own heredocs and here-strings, and what
    the statements piped into it write (their heredocs, here-strings, and the arguments of `echo`/`printf`)."""
    texts: list[str] = []
    j, hops = index, 0
    while True:
        words = stmts[j][0]
        texts += [marks[w] for w in words if w in marks]
        if j != index:
            clean = _command_words([w for w in words if w not in marks])
            if clean and os.path.basename(clean[0]) in _TEXT_PRODUCERS:
                args = clean[1:]
                while args and re.fullmatch(r"-[neE]+", args[0]):
                    args = args[1:]                      # echo's own options, not text
                texts.append(" ".join(args))
                texts += args
                if os.path.basename(clean[0]) == "printf":
                    texts.append(_printf_text(args))
        if j == 0 or not stmts[j][1] or hops >= 8:
            break
        j -= 1
        hops += 1
    return [t for t in texts if t]


_VAR_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_ASSIGNERS = {"export", "declare", "typeset", "readonly", "local"}


def _note_assignments(words: list[str], assigned: dict[str, str]) -> None:
    """Remember `NAME=literal` statements (also after `export`/`declare`, and after a keyword such as `then`
    or `do`) so a later `eval "$NAME"` can be read."""
    start = 0
    while start < len(words) and words[start] in _KEYWORDS:
        start += 1
    words = words[start:]
    if not words:
        return
    rest = words
    if os.path.basename(words[0]) in _ASSIGNERS:
        rest = [w for w in words[1:] if not w.startswith("-")]
    if rest and all(_ASSIGNMENT.match(w) for w in rest):
        for w in rest:
            name, _, value = w.partition("=")
            assigned[name] = value


def _expand_known(words: list[str], assigned: dict[str, str]) -> list[str]:
    """`words` with `$NAME` / `${NAME}` replaced by a literal assigned earlier in the same text."""
    if not assigned:
        return words
    longest = max(len(v) for v in assigned.values())
    out: list[str] = []
    for w in words:
        if "$" in w:
            # the size of the expanded word is charged before it is built
            if not _charge(len(w) + longest * len(_VAR_REF.findall(w))):
                # Fail toward the words: the word's distinct pieces (literals and assigned values), in order of
                # first appearance, and its first references expanded, are read for words.
                pieces: dict[str, None] = {}
                pos = 0
                for m in _VAR_REF.finditer(w):
                    if m.start() > pos:
                        pieces[w[pos:m.start()]] = None
                    pieces[assigned.get(m.group(1) or m.group(2), m.group(0))] = None
                    pos = m.end()
                    if len(pieces) >= _VIEW_MAX_REFS:
                        break
                if pos < len(w) and len(pieces) < _VIEW_MAX_REFS:
                    pieces[w[pos:]] = None
                views = ["".join(pieces), " ".join(pieces)]
                if longest * 8 <= _MAX_COMMAND_CHARS:       # the first eight references, expanded in place
                    views.append(_VAR_REF.sub(lambda m: assigned.get(m.group(1) or m.group(2), m.group(0)),
                                              w[:8192], count=8))
                _note_refused(views)
                return words
            w = _VAR_REF.sub(lambda m: assigned.get(m.group(1) or m.group(2), m.group(0)), w)
        out.append(w)
    return out


def _trap_text(words: list[str]) -> str | None:
    """The command text of `trap [--] 'text' SIGNAL...` (run by the shell when the signal comes)."""
    if not words or os.path.basename(words[0]) != "trap":
        return None
    rest = words[1:]
    while rest and rest[0].startswith("-") and rest[0] not in ("-",):
        if rest.pop(0) == "--":
            break
    if len(rest) >= 2 and rest[0] not in ("-", ""):
        return rest[0]
    return None


def _read_statements(logical: str, physical: list[str], marked: str | None = None
                     ) -> tuple[list[tuple[list[str], bool]], bool, bool]:
    """The (statement, piped-in) pairs of a logical line, whether `marked` was the text read, and whether the
    physical lines were read one by one. When the joined text does not tokenise (a stray apostrophe), they are,
    so one stray quote cannot hide the commands on the next lines (their pipes and heredoc markers are then
    unknown)."""
    try:
        return _statements_piped(marked if marked is not None else logical), marked is not None, False
    except ValueError:
        return [(s, False) for s in _line_statements(physical)], False, True


_memo: tuple[str, str | None] | None = None


def shape(command: str, _depth: int = 0) -> str | None:
    """"doctor", "stop" or "restart" when the command RUNS a guarded shape, else None."""
    global _memo
    if _depth:
        return _shape(command, _depth)
    if _memo is not None and _memo[0] == command:
        return _memo[1]
    result = _shape(command, 0)
    if result is None and _refused_found:
        result = _refused_found          # an expansion the budget refused was read as words, and matched
    _memo = (command, result)
    return result


def _shape(command: str, _depth: int) -> str | None:
    global _work, _refused_found
    _check_deadline()
    if _depth == 0:
        _work = 0
        _refused_found = None
    _work += len(command) + _SHAPE_COST
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
    lines, bodies, line_docs = _logical_lines_docs(command)
    for body in bodies:
        # An unquoted heredoc delimiter: the shell expands `$(...)` and backticks in the body.
        for sub in _scan(body, literal_quotes=True)[0]:
            inner = shape(sub, _depth + 1)
            if inner:
                return inner
    assigned: dict[str, str] = {}
    expanded = 0
    for li, (logical, physical) in enumerate(lines):
        _check_deadline()
        _work += len(logical) + _SHAPE_COST
        if _work > _WORK_BUDGET:
            # the budget is spent (a very long script, thousands of substitutions): the rest is read for words
            return _text_shape(command)
        subs, _docs, too_deep, _spans, herestrings = _scan(logical)
        entries = line_docs.get(li, [])
        marked, marks = (_mark_redirections(logical, entries, herestrings)
                         if entries or herestrings else (logical, {}))
        statements, used_marks, lines_read = _read_statements(logical, physical, marked if marks else None)
        if marks and not used_marks and _SHELL_WORD.search(logical):
            # The line does not tokenise, so a heredoc or here-string cannot be tied to its command: with a
            # shell word on the line every such text is read as commands (fail closed).
            for text in marks.values():
                inner = shape(text, _depth + 1)
                if inner:
                    return inner
        if too_deep:
            found = _text_shape(logical)
            if found:
                return found
        if len(physical) > 1 and _NESTED_EXPANSION.search(logical) and not lines_read:
            # The scanner cannot follow a quote nested in an expansion, so the join may be imaginary: read
            # the physical lines one by one as well (the 2.1.0 reading) and deny if either view matches. Not
            # again when the failed join was just read that way (6a: that was a second identical pass).
            statements = statements + [(s, False) for s in _line_statements(physical)]
        words_read = False
        if _work > _WORK_BUDGET:
            # the line reads above spent the budget: the whole logical line is read for words once
            words_read = True
            found = _text_shape(logical)
            if found:
                return found
        for sub in subs:
            # `"$(cmd)"`, `` `cmd` ``, `<(cmd)`: the shell runs the text inside as a command.
            if _work > _WORK_BUDGET:
                # The budget is spent: ONE linear word pass over the whole logical line (the substitutions already
                # parsed and the REMAINDER of the line, which holds the ones not parsed), not one parse per
                # substitution, and not a second time when the line was read above. A hit is the verdict; the
                # loop is left only after that pass found nothing.
                if not words_read:
                    found = _text_shape(logical)
                    if found:
                        return found
                break
            inner = shape(sub, _depth + 1)
            if inner:
                return inner
        for index, (raw_stmt, _piped) in enumerate(statements):
            _check_deadline()
            stmt = [w for w in raw_stmt if w not in marks] if marks else raw_stmt
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
            _note_assignments(stmt, assigned)
            if not words:
                continue
            base = os.path.basename(words[0])
            if expanded < _MAX_EXEC_EXPANSIONS:
                # `xargs sh -c '...'`, `find -exec bash -c '...' {} +`, `parallel cmd ::: x`: the words are a command
                for extra in _exec_inner(words):
                    expanded += 1
                    statements.append((extra, False))
            if base == "parallel":
                text = _parallel_text(words)
                if text:
                    inner = shape(text, _depth + 1)
                    if inner:
                        return inner
            if base == "alias":
                # the value of an alias is command text that runs when the alias is used
                for w in words[1:]:
                    name, eq, value = w.partition("=")
                    if eq and value:
                        inner = shape(value, _depth + 1)
                        if inner:
                            return inner
            # A heredoc, a here-string or a pipe that feeds a shell is command text: `bash <<EOF`,
            # `cat <<EOF | sh`, `echo '...' | bash`, `bash <<< '...'`.
            if used_marks or index > 0:
                feeds_shell = ((base in _SHELLS or base == "ksh") and _reads_script_from_stdin(words)) or \
                    base == "eval" or (base in ("source", ".") and any(w in _STDIN_SOURCES or
                                                                     w.startswith("/dev/fd/") for w in words[1:]))
                if feeds_shell:
                    for text in _stdin_command_text(index, statements, marks if used_marks else {}):
                        inner = shape(text, _depth + 1)
                        if inner:
                            return inner
            # `trap 'cmd' EXIT`: the string runs when the signal comes.
            trapped = _trap_text(words)
            if trapped:
                inner = shape(trapped, _depth + 1)
                if inner:
                    return inner
            # `bash -c "openclaw doctor --fix"`, `eval "..."`: the string IS a command, and `V=...; eval "$V"`
            # runs the literal assigned earlier in the same text.
            inner_cmd = _inner_command(_expand_known(words, assigned) if base == "eval" or base in _SHELLS
                                       else words)
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


def decide(command: str) -> str | None:
    """"doctor", "stop" or "restart" when `command` must be denied, else None.

    The precise analysis runs under a wall-clock deadline. When the deadline fires, or the analysis fails with
    an internal error, the cheap view decides instead (a failure must not become "allow" when the words are
    there). A text that feeds a shell through a heredoc, a here-string or a pipe is also read by the cheap view
    when the precise analysis found nothing: the parser misses some of those, and the words are the net."""
    global _deadline_at, _last_deadline_fired, _memo
    seconds = _deadline_seconds()
    _last_deadline_fired = False
    failed = False
    found: str | None = None
    old_handler = None
    timer = False
    try:
        try:
            _deadline_at = time.monotonic() + seconds
            if threading.current_thread() is threading.main_thread() and hasattr(signal, "setitimer"):
                try:
                    old_handler = signal.signal(signal.SIGALRM, _on_alarm)
                    signal.setitimer(signal.ITIMER_REAL, seconds)
                    timer = True
                except (ValueError, OSError):
                    timer = False
            if dangerous(command):
                found = shape(command)       # the same command: answered from the one-entry memo
            if found is None and _feeds_a_shell_text(command):
                found = _cheap_view(command, strict=True)
        finally:
            _deadline_at = None              # the handler does nothing from here on
            if timer:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler if old_handler is not None else signal.SIG_DFL)
    except _DeadlineExceeded:
        failed = True
        _last_deadline_fired = True
    except Exception:
        failed = True
    # A deny that was already made stands: the alarm can land after `shape()` returned and before the timer was
    # disarmed, and the fallback (which reads fewer forms than the full analysis) must not replace it (2.2e, 4a).
    made = found or (_memo[1] if _memo is not None and _memo[0] == command else None)
    _memo = None
    if failed:
        try:
            found = made or _cheap_view(command)
            if found is None and _last_deadline_fired:
                found = _fail_closed_view(command)      # the clock ran out: fail closed (6a)
        except Exception:
            found = made
    return found


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict) or data.get("tool_name") != "Bash":
            return 0
        command = str((data.get("tool_input") or {}).get("command", ""))
        if not command:
            return 0
        found = decide(command)
        if not found:
            return 0
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
