"""The one definition of a `sleep` call in a shell command (ADR-0014 D7).

Standard library only and import-cheap: the PreToolUse hook `hooks/kit_sleep_guard.py` loads this file by path and
nothing else, and `audit_sources.is_poll` uses the same functions, so the guard and the poll metric cannot drift.

A sleep counts only at command position (start of the text, after `;` `&` `|` `(` `{` a backtick or a newline, after
`then` `do` `else`, or behind a wrapper such as `timeout 900`, `nohup`, `sudo`, `env`): `echo sleep 100`,
`grep 'sleep 60'` and a commit message that mentions sleep are not sleeps.
"""
from __future__ import annotations

import re

# The seconds above which a Bash `sleep` is refused by the hook. 30 is allowed, 31 is not.
MAX_ALLOWED_SECONDS = 30

_LEAD = (r"(?:^|[;&|(){}\n`]|\$\(|\b(?:then|do|else|elif|timeout\s+\S+|nohup|time|exec|sudo|env|command|setsid)\s+)")
_SLEEP = re.compile(_LEAD + r"[ \t]*sleep((?:[ \t]+[^\s;&|()<>`\"']+)+)", re.MULTILINE)
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _seconds(args: str) -> float | None:
    """Seconds slept by the arguments of one `sleep` (GNU sleep adds them up); None when they cannot be read."""
    total = 0.0
    seen = False
    for tok in args.split():
        if tok == "infinity":
            return float("inf")
        m = _DURATION.match(tok)
        if not m:
            break
        total += float(m.group(1)) * _UNIT[m.group(2)]
        seen = True
    return total if seen else None


def sleeps(text: str) -> list[float | None]:
    """One entry per `sleep` call at command position: its seconds, or None when they are not a literal."""
    if not isinstance(text, str):
        return []
    return [_seconds(m.group(1)) for m in _SLEEP.finditer(text)]


def has_sleep(text: str) -> bool:
    """True when the text calls `sleep` at command position, whatever its argument."""
    return bool(sleeps(text))


def longest_sleep_seconds(text: str) -> float | None:
    """The longest literal sleep in the text, or None when there is none."""
    known = [s for s in sleeps(text) if s is not None]
    return max(known) if known else None


_WAIT_LOOP = re.compile(r"(?:^|[;&|(){}\n`]|\bthen\s+|\bdo\s+)[ \t]*(?:while|until)\b[^\n]*?(?:;|\n)\s*do\b(.*?)\bdone\b",
                        re.DOTALL | re.MULTILINE)


def total_sleep_seconds(text: str) -> float | None:
    """The literal sleeps of one command added up (they run one after the other), None when there is none."""
    known = [s for s in sleeps(text) if s is not None]
    return sum(known) if known else None


def has_wait_loop(text: str) -> bool:
    """True when a `while` / `until` loop whose body sleeps (at command position) is in the command: an open-ended
    wait inside one tool call. A `for` loop over a fixed list is bounded and is left alone."""
    if not isinstance(text, str):
        return False
    return any(has_sleep(m.group(1)) for m in _WAIT_LOOP.finditer(text))
