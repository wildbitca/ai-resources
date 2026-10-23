"""One operator-rules file, rendered into every cockpit (ADR-0016 D4).

The operator's standing rules (language, commits, button questions, ClickUp, clusters) live in ONE file,
`~/.config/ai-resources/operator-rules.md` (XDG-aware, created only if absent). Setup renders it as a managed block into
`~/.claude/CLAUDE.md`, the AGENTS.md of every OpenClaw agent workspace, and `~/.gemini/GEMINI.md`. The block is fill-only inside
its markers and idempotent: the same file always renders the same text, and nothing outside the markers is touched.

The first time, the file is SEEDED from the operator's own CLAUDE.md: the headed blocks whose heading names one of the
categories are shown, scanned for secrets and created only after a confirm. Nothing here writes at import time or while the
wizard asks (ADR-0016 D6): `create` and `append_section` run at Apply, from the deferred queue.

`path()` is a function, not a constant, so a test points it at a scratch directory.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude_md_migrate as cmm
from . import cockpit_auth

FILE_NAME = "operator-rules.md"
SEED_ATTR = "_operator_rules_seed"
"""Instance attribute on the setup state (never serialised): the seed text the wizard queued for creation."""

# category -> heading keywords (compared lower-case, accents removed)
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("language", ("idioma", "language")),
    ("commits", ("commit",)),
    ("questions", ("pregunta", "question")),
    ("clickup", ("clickup",)),
    ("clusters", ("cluster",)),
)

HEADER = (
    "# Operator rules\n\n"
    "<!-- Source of truth for `ai-resources setup` (ADR-0016 D4). Setup renders this file into ~/.claude/CLAUDE.md, the\n"
    "AGENTS.md of every OpenClaw agent workspace and ~/.gemini/GEMINI.md, inside the managed block. Edit it here and re-run\n"
    "setup; never put a key, token or password in it. -->\n\n"
)


def default_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ai-resources" / FILE_NAME


def path() -> Path:
    """Where the file lives. Every caller goes through this function (never a constant), so the suite replaces it."""
    return default_path()


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def category_of(heading: str) -> str:
    """The operator-rules category a heading names, or ''."""
    plain = _plain(heading)
    for name, words in CATEGORIES:
        if any(w in plain for w in words):
            return name
    return ""


@dataclass(frozen=True)
class SeedBlock:
    category: str
    heading: str
    text: str                       # the block as it sits in CLAUDE.md, without its trailing blank lines
    tokens: int


def seed_from_raw(raw: str) -> list[SeedBlock]:
    """The blocks of a CLAUDE.md text (outside the kit block) whose heading names a category, in file order, one per
    category-heading (a second block of the same category is kept: nothing is merged or dropped)."""
    blocks, _kit = cmm.parse_blocks(raw)
    return [SeedBlock(category_of(b.heading), b.heading, b.core.rstrip("\n") + "\n", b.tokens)
            for b in blocks if category_of(b.heading)]


def seed_from_claude_md(raw: str | None = None) -> list[SeedBlock]:
    """Read-only: seed candidates from the operator's CLAUDE.md (`raw` for a caller that already has the text)."""
    if raw is None:
        try:
            raw = cmm._read_raw(cmm.claude_md_path())
        except OSError:
            return []
    return seed_from_raw(raw or "")


def compose(seed: list[SeedBlock]) -> str:
    return HEADER + "\n".join(b.text.rstrip("\n") + "\n" for b in seed)


def secret_findings(text: str) -> list[int]:
    """Line numbers (1-based) that look like a key, token or password. The file is rendered into three cockpits, so a
    secret in it would be copied everywhere: creation is refused while any line is flagged."""
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        if cockpit_auth.looks_like_secret(line) or re.search(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S{6,}", line):
            bad.append(n)
    return bad


def exists() -> bool:
    return path().is_file()


def read() -> str:
    """The file's text, or '' when it does not exist or cannot be read."""
    try:
        return path().read_text(encoding="utf-8")
    except OSError:
        return ""


def planned_text(s: Any = None) -> str:
    """What the file holds NOW or will hold after Apply: the file itself, else the seed the wizard queued."""
    cur = read()
    if cur:
        return cur
    return str(getattr(s, SEED_ATTR, "") or "")


def create(text: str) -> bool:
    """Create the file only if it is absent (exclusive create, never an overwrite). True when it was created."""
    p = path()
    if p.exists() or p.is_symlink():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return True


def append_section(heading: str, text: str) -> bool:
    """Fill-only: add a block that is not in the file yet (matched by its heading, accents and case ignored). Creates the
    file with the header when it is absent. True when the file changed."""
    p = path()
    current = read()
    if current and _plain(heading) in _plain(current):
        return False
    new = (current.rstrip("\n") + "\n\n" if current else HEADER) + text.rstrip("\n") + "\n"
    if secret_findings(text):
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    return True


def has_heading(heading: str, s: Any = None) -> bool:
    return _plain(heading) in _plain(planned_text(s))


def _body(text: str) -> str:
    """The file without its H1 and its leading comment: what is rendered under the block's own heading."""
    out = re.sub(r"\A\s*#\s+Operator rules\s*\n", "", text, count=1)
    out = re.sub(r"\A\s*<!--.*?-->\s*", "", out, count=1, flags=re.S)
    return out.strip("\n")


def render(text: str) -> str:
    """The managed-block body for `text` ('' for an empty file). Identical for every cockpit."""
    body = _body(text)
    if not body.strip():
        return ""
    return "## Operator rules\n\n" + body + "\n"


def blocked() -> list[int]:
    """Line numbers of the file that look like a key, token or password, [] when it is clean or absent.

    The file is copied into three cockpits and every OpenClaw agent workspace, some of them reachable from a chat group, so
    the scan runs again at every render, not only when the file is seeded: a secret pasted into it later is never copied."""
    return secret_findings(read())


def blocked_warning() -> str:
    """The warning a writer prints when it refuses to render the rules ('' when nothing is flagged)."""
    bad = blocked()
    if not bad:
        return ""
    return (f"{path()}: line(s) {', '.join(map(str, bad[:5]))} look like a key, token or password, so the operator rules "
            "were not rendered anywhere and the existing managed blocks were left as they are. Take the secret out of "
            "the file and run setup again.")


def block() -> str:
    """The rendered block for the file as it is now, '' when there is no file or the file is flagged by `blocked()`."""
    if blocked():
        return ""
    return render(read())


def append_to(md: str) -> str:
    """`md` with the rendered rules appended (one blank line between); unchanged when there are no rules or the file is
    flagged. A writer checks `blocked()` first and skips the managed block entirely, so the existing one stays as it is."""
    rules = block()
    if not rules:
        return md
    return md.rstrip("\n") + "\n\n" + rules
