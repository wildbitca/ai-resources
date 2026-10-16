"""Rich + questionary wrappers — single import surface for the wizard.

Falls back gracefully if rich/questionary aren't installed (prints plain text).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Sequence

from . import flow
from .flow import BACK, Cancelled, GoBack  # noqa: F401  (re-exported)

# --- optional deps with graceful degradation -------------------------------------
try:
    from rich.console import Console
    from rich import box as _rich_box
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich.text import Text
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False

    class Text(str):  # minimal stand-in so doubles can build a header without rich
        def __new__(cls, value: str = "", style: str = ""):
            return super().__new__(cls, value)

        def append(self, *_a: Any, **_k: Any) -> None:
            return None

try:
    import questionary
    from questionary import Style
    _HAS_QUESTIONARY = True
except ImportError:  # pragma: no cover
    _HAS_QUESTIONARY = False


# --- non-interactive mode --------------------------------------------------------
# Every prompt consults this instead of the flag being threaded through the
# twenty call sites in the wizard. Prompts return their default rather than
# asking; where there is no defensible default they abort, because a prompt that
# silently answers itself produces a broken setup nobody was told about.
_NON_INTERACTIVE = False


def set_non_interactive(enabled: bool = True) -> None:
    global _NON_INTERACTIVE
    _NON_INTERACTIVE = enabled


def is_non_interactive() -> bool:
    return _NON_INTERACTIVE


def stdin_is_a_terminal() -> bool:
    """False when nothing can answer a prompt — a pipe, a CI job, a background run."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):  # pragma: no cover - closed stdin
        return False


# --- console -------------------------------------------------------------------
_console: "Console | None" = None


def console() -> "Console":
    """Return the shared rich Console (lazy)."""
    global _console
    if _console is None:
        if not _HAS_RICH:
            raise RuntimeError(
                "Required dependency 'rich' is not installed. "
                "Run: pip install rich questionary  (or reinstall via brew)."
            )
        _console = Console()
    return _console


def has_rich() -> bool:
    return _HAS_RICH


def no_color() -> bool:
    """NO_COLOR (https://no-color.org): set and non-empty. It wins over FORCE_COLOR; bold stays."""
    return bool(os.environ.get("NO_COLOR"))


def unicode_ok() -> bool:
    """Draw with Unicode glyphs only on a UTF-8 stream and without NO_COLOR; ASCII otherwise."""
    if no_color():
        return False
    enc = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    return enc.startswith("utf")


def terminal_width() -> int:
    if _HAS_RICH:
        try:
            return int(console().width)
        except Exception:  # noqa: BLE001 - a double without a width
            pass
    return shutil.get_terminal_size((80, 24)).columns


def require_deps() -> None:
    """Raise with actionable message if rich/questionary missing."""
    missing = []
    if not _HAS_RICH:
        missing.append("rich")
    if not _HAS_QUESTIONARY:
        missing.append("questionary")
    if missing:
        msg = (
            f"\nai-resources setup requires: {', '.join(missing)}\n"
            "Install with:  pip install --user rich questionary\n"
            "Or reinstall the kit:  brew reinstall ai-resources\n"
        )
        print(msg, file=sys.stderr)
        sys.exit(1)


# --- styled output -------------------------------------------------------------
def _box_kw() -> dict:
    return {} if unicode_ok() else {"box": _rich_box.ASCII}


def banner(title: str, subtitle: str = "", version: str = "") -> None:
    title, subtitle, version = clean_text(title), clean_text(subtitle), clean_text(version)
    if not _HAS_RICH:
        print(f"\n=== {title} ===")
        if subtitle:
            print(subtitle)
        return
    if not unicode_ok():
        title, subtitle = title.replace("—", "-"), subtitle.replace("—", "-")
    body = Text(title, style="bold cyan")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    if version:
        body.append(f"\n{version}", style="dim italic")
    console().print(Panel(body, border_style="cyan", padding=(1, 2), **_box_kw()))


def section(num: int, total: int, title: str) -> None:
    global _LAST_SECTION
    title = safe_text(title).replace("\n", " ")
    _LAST_SECTION = title
    if not _HAS_RICH:
        print(f"\n[{num}/{total}] {title}\n" + "-" * 65)
        return
    show_rail = (not _NON_INTERACTIVE) and _PROGRESS[0] > 0
    width = terminal_width()
    console().print()
    if show_rail and width < 60:
        # Below 60 columns: one line, no rail.
        console().print(f"Step {num}/{total}  {title}", style="bold cyan", markup=False, highlight=False)
        return
    rule_kw = {} if unicode_ok() else {"characters": "-"}
    # Pre-built Text: a plain str title goes through the repr highlighter, which bolds "[", digits and "/"
    # one by one (so the header is not contiguous text and keeps bold escapes under NO_COLOR).
    # One span, so the escape codes wrap the whole header rather than splitting it.
    head = Text(f"[{num}/{total}] {title}", style="bold cyan")
    console().rule(head, style="cyan", align="left", **rule_kw)
    if show_rail:
        console().print(rail(num), markup=False, highlight=False)
        console().print(key_hint(), style="dim", markup=False, highlight=False)


def rail(current: int | None = None, model: list | None = None) -> str:
    """The progress rail for the current step as plain text (see `flow.format_rail`)."""
    if model is None:
        cur = current if current else _PROGRESS[0]
        model = flow.rail_model(cur, _PROGRESS[1], _SKIPPED)
    return flow.format_rail(model, terminal_width(), unicode_ok(), title=_LAST_SECTION)


def key_hint(extra: str = "") -> str:
    """One line of key hints for the step; Ctrl-B is listed only while Back is enabled."""
    uni = unicode_ok()
    parts = ["\u2191/\u2193 move" if uni else "Up/Down move", "Space toggle", "Enter confirm"]
    if _BACK_ON:
        parts.append("Ctrl-B back")
    parts.append("Ctrl-C cancel")
    if extra:
        parts.append(extra)
    sep = " \u00b7 " if uni else " . "
    return " " + sep.join(parts)


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
# Controls (C0 minus tab and newline, DEL, C1) and the invisible format characters that reorder or hide text:
# zero-width, bidi marks, embeddings, overrides and isolates, the word joiner, the BOM, the Arabic letter mark.
_CONTROLS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2028\u2029\u2060-\u2064\u2066-\u2069\ufeff]")


def clean_text(text: Any) -> str:
    """Make text from a subprocess, a remote or a file safe to echo: ANSI/OSC sequences, other control
    characters and bidi/zero-width format characters are dropped (a CR, an escape or a right-to-left override
    can rewrite what the operator reads); tab and newline stay. U+2028/U+2029 are dropped, not turned into
    a newline: a terminal does not break a line on them, so neither may the masking."""
    return _CONTROLS.sub("", _ANSI.sub("", str(text)))


def safe_text(text: Any) -> str:
    """`clean_text` then `mask_secrets`: for anything that came from outside the wizard."""
    return mask_secrets(clean_text(text))


def safe_lines(text: Any, n: "int | None" = None) -> list[str]:
    """Clean and mask the WHOLE text first, then split: a secret that spans lines (a PEM body, a block scalar,
    a key on one line and its value on the next) is only recognisable before the split. Blank lines are
    dropped; `n` keeps the last n lines."""
    if not isinstance(text, str):
        text = "\n".join(str(t) for t in text)
    lines = [l.rstrip() for l in safe_text(text).split("\n")]
    lines = [l for l in lines if l.strip()]
    return lines[-n:] if n else lines


def safe_excerpt(text: Any, limit: int = 300, tail: bool = False) -> str:
    """Mask, then truncate (never the other way round: a cut can leave the head or the tail of a secret that
    no pattern recognises). Keeps the end of the text when `tail`; marks the cut with `...`."""
    out = safe_text(text).strip()
    if len(out) <= limit:
        return out
    keep = max(limit - 3, 1)
    return ("..." + out[-keep:]) if tail else (out[:keep].rstrip() + "...")


def _markup_safe(msg: Any) -> str:
    """Cleaned and with rich markup escaped, so a `[/]` in output is shown, not interpreted (or raised on)."""
    text = safe_text(msg)
    if not unicode_ok():
        text = text.replace("\u2014", "-").replace("\u2013", "-").replace("\u2026", "...")
    if _HAS_RICH:
        from rich.markup import escape
        return escape(text)
    return text


def markup_safe(msg: Any) -> str:
    """Public form of `_markup_safe`: for a value interpolated into a hand-written rich markup string."""
    return _markup_safe(msg)


def _glyph(fancy: str, plain: str) -> str:
    return fancy if unicode_ok() else plain


def info(msg: str) -> None:
    msg = _markup_safe(msg)
    if _HAS_RICH:
        console().print(f"  [dim cyan]{_glyph('ⓘ', 'i')}[/] {msg}")
    else:
        print(f"  i  {msg}")


def ok(msg: str) -> None:
    msg = _markup_safe(msg)
    if _HAS_RICH:
        console().print(f"  [green]{_glyph('✔', 'OK')}[/] {msg}")
    else:
        print(f"  OK {msg}")


def warn(msg: str) -> None:
    msg = _markup_safe(msg)
    if _HAS_RICH:
        console().print(f"  [yellow]{_glyph('⚠', '!')}[/] {msg}")
    else:
        print(f"  !  {msg}", file=sys.stderr)


def error(msg: str) -> None:
    msg = _markup_safe(msg)
    if _HAS_RICH:
        console().print(f"  [red]{_glyph('✘', 'X')}[/] {msg}")
    else:
        print(f"  X  {msg}", file=sys.stderr)


def detail(msg: str) -> None:
    msg = _markup_safe(msg)
    if _HAS_RICH:
        console().print(f"    [dim]{msg}[/]")
    else:
        print(f"    {msg}")


# --- tables --------------------------------------------------------------------
def role_table(rows: list[tuple[str, str, str]], title: str = "") -> None:
    """Show role -> provider -> model assignments."""
    rows = [tuple(safe_text(c) for c in r) for r in rows]
    if not _HAS_RICH:
        print(f"\n{safe_text(title)}")
        for role, provider, model in rows:
            print(f"  {role:<22} {provider:<12} {model}")
        return
    t = Table(title=_markup_safe(title), show_header=True, header_style="bold cyan", border_style="dim", **_box_kw())
    t.add_column("Role", style="bold")
    t.add_column("Provider")
    t.add_column("Model", style="dim")
    for r, p, m in rows:
        t.add_row(_markup_safe(r), _markup_safe(p), _markup_safe(m))
    console().print(t)


def review_table(rows: list[tuple[str, str]], title: str = "") -> None:
    """The review screen: (section, answer) rows. Values are shown as given: callers pass `set` or `missing`
    for credentials, never the secret."""
    if not _HAS_RICH:
        if title:
            print(f"\n{safe_text(title)}")
        for section_name, answer in rows:
            print(f"  {safe_text(section_name):<18} {safe_text(answer)}")
        return
    t = Table(title=_markup_safe(title) if title else None, show_header=True, header_style="bold cyan", border_style="dim", **_box_kw())
    t.add_column("Section", style="bold")
    t.add_column("Your answer")
    for section_name, answer in rows:
        t.add_row(_markup_safe(section_name), _markup_safe(answer))
    console().print(t)


def detected_table(rows: list[tuple[str, str, str]]) -> None:
    """rows: (status, name, version_or_path) — status is ✔/✘/?."""
    rows = [(clean_text(s), safe_text(n), safe_text(v)) for s, n, v in rows]
    if not _HAS_RICH:
        for s, n, v in rows:
            print(f"  {s} {n:<22} {v}")
        return
    for s, n, v in rows:
        n, v = _markup_safe(n), _markup_safe(v)
        if s.startswith("✔") or s == "✓":
            console().print(f"  [green]✔[/] {n:<22} [dim]{v}[/]")
        elif s.startswith("✘") or s == "✗":
            console().print(f"  [red]✘[/] {n:<22} [dim]not detected[/]")
        else:
            console().print(f"  [yellow]?[/] {n:<22} [dim]{v}[/]")


# --- progress / spinners -------------------------------------------------------
@contextmanager
def spinner(label: str):
    con = console() if _HAS_RICH else None
    plain_label = safe_text(label).replace("\n", " ")
    # A console double (tests, a captured transcript) cannot drive a live region: say it on one line instead.
    if con is None or not isinstance(con, Console):
        print(f"  ... {plain_label}")
        yield None
        return
    label = _markup_safe(plain_label)        # the task description is parsed as markup: escape it
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}[/]"),
        TimeElapsedColumn(),
        console=console(),
        transient=True,
    ) as p:
        task = p.add_task(label, total=None)
        yield (p, task)


_KW = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL)"
_KW_ANY = re.compile(r"(?i)key|token|secret|pass|pwd|pat|credential|auth|cookie|sig")
_FLAG_KW = re.compile(r"(?i)key|token|secret|password|passwd|passphrase|credential|pwd")
_URL_TOKEN = re.compile(r"(?i)(?<![A-Za-z0-9+.\-])([a-z][a-z0-9+.\-]{0,31}://)([^\s\"'<>]{1,2048})")
# `NAME=value` / `NAME: value` / `"NAME": "value"`: the name is a bounded run, whether it is a secret name is
# decided in Python (a regex that looks for the keyword inside an unbounded run is quadratic on `KEY-KEY-...`).
_PAIR = re.compile(
    r"(?<![A-Za-z0-9_.\-])([A-Za-z0-9_.\-]+)([\"']?[ \t]*[=:][ \t]*)"
    r"(?:([\"'])((?:\\.|(?!\3)[^\\\n])*)|([^\s\"']+))")
_FLAG_SECRET = re.compile(r"(?<![\w\-])(--?[A-Za-z0-9_\-]{1,64})(\s+)(?!--)(\S+)")
_MYSQL_P = re.compile(r"(?<=\s)-p(?=[^\s])(\S+)")
_DB_CLIENT = re.compile(r"(?i)\b(?:mysql|mariadb)\w*")
_COOKIE_LINE = re.compile(r"(?i)(?<![\w\-])((?:set-)?cookie[ \t]*:[ \t]*)([^\n\"']+)")
_JWT = re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]*")
_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Z_][A-Z0-9_]*|<[A-Z][A-Z0-9_\-]*>|os\.environ/[A-Za-z0-9_]+")
_SECRET_SHAPES = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:sk-[A-Za-z0-9_\-]{8,}|gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AIza[0-9A-Za-z_\-]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{12,}|[A-Za-z0-9_\-]{32,})"
    r"(?![A-Za-z0-9_\-])")
# An identifier or a slug is long but is not a credential: SOME_LONG_ENV_NAME, claude-3-5-sonnet-latest-preview.
_ENV_NAME = re.compile(r"[A-Z]+(?:_[A-Z]+)+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}")
_PEM = re.compile(
    r"(?i)-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S)
# A line that is only `name:` / `name: |` / `name: > # comment`, found line by line (see `_mask_structured`).
_KEY_LINE = re.compile(r"^([ \t]*(?:[{,][ \t]*)?(?:-[ \t]+)?)[\"']?([\w.\-]{1,128})[\"']?[ \t]*:(?:[ \t]+(.*))?$")
_BLOCK_HEADER = re.compile(r"[|>][+\-0-9]*")
# A bare `NAME=value` or `NAME: value` line whose name says it is a password: an unquoted value may hold
# spaces, so it is masked to the end of the line (a reference or an already masked value is kept).
_PASSWORD_LINE = re.compile(
    r"(?im)^([ \t]*(?:export[ \t]+)?([A-Z0-9_.\-]{0,64}(?:PASSWORD|PASSWD|PASSPHRASE|PWD|PGPASS)[A-Z0-9_.\-]{0,64})"
    r"[ \t]*[=:][ \t]*)(?![\"'|>])([^\n]+)$")
_SECRET_WORD = re.compile(r".*(?:key|token|secret|password|passwd|passphrase|credential)s?\d*")
# Names that say "secret" only as their last word (`DB_PWD`, `GH_PAT`, `X-Auth`, `Set-Cookie`); `PWD=/home/me`
# is the working directory, so a path-shaped value exempts them.
_WEAK_LAST_WORDS = frozenset({"auth", "pass", "pgpass", "pwd", "pat", "cookie", "cookies", "signature", "sig"})
# `api_key_env: NAME` names a variable, `token_count: 5` counts: the last word says the value is not a secret.
_NOT_A_VALUE = frozenset({"env", "file", "path", "ref", "name", "var", "dir", "count", "limit", "len", "length",
                          "size", "type"})
# ... but only when the value is shaped like one: a variable name, a number or a path.
_NAME_SHAPED = re.compile(r"[A-Z_][A-Z0-9_]*|\d+|[~./][\w./~\-]*|[a-z]+(?:[_\-][a-z]+)+")
# `max_tokens: 4096` sizes a budget: `tokens` after one of these words is not a credential.
_BUDGET_WORDS = frozenset({"max", "min", "num", "total", "prompt", "completion", "input", "output", "context"})
_DIGEST = re.compile(
    r"(?i)\b(?:sha(?:1|224|256|384|512)|md5)[:=]\s*[0-9a-f]{32,128}\b"
    r"|(?<![\w.\-])[\w.\-]{0,64}(?:digest|checksum|hash)[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32,128}\b")
_SECRET_NAMED_DIGEST = re.compile(r"(?i)^[\w.\-]{0,64}(?:password|passwd|passphrase|secret|token|credential)")
_USERPASS = re.compile(r"(?<![\w\-])(-u|--user|--proxy-user)((?:\s+|=)[^\s:]+:)(\S+)")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]+(?:[ \t]+[^\s\"',}]+)?)")
_AUTH_SCHEMES = ("bearer", "basic", "token", "digest", "apikey", "key")
_BASE64_PADDED = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{16,}={1,2}(?![A-Za-z0-9+/=])")
_MASKED_FORM = re.compile("[^\u2022\\s]{0,4}\u2022+[^\u2022\\s]{0,4}")   # what credentials.mask prints
_KEPT_VALUES = ("****", "null", "None", "true", "false")
# A line longer than this is not output a person reads: it is cut and the rest is replaced by a marker, which
# bounds every pattern below (and the time `mask_secrets` can take on a hostile 100 KB line).
LINE_CAP = 4096
_LINE_MARK = " ****[line truncated]"

_COLONS = str.maketrans({"\u2236": ":", "\ua789": ":", "\u02d0": ":", "\u2982": ":", "\u1803": ":"})
_GREEK_CYRILLIC = re.compile("[Ͱ-ϿЀ-ӿ]+")
_CONFUSABLES = str.maketrans({
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "α": "a", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "υ": "u",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "а": "a", "е": "e",
    "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ѕ": "s",
    "ј": "j", "һ": "h",
})


def _is_secret_name(name: str, value: "str | None" = None, weak: bool = True) -> bool:
    """Whether a variable or field name says its value is a secret: one of its words is a secret word (allowing
    a plural or a trailing digit). A last word such as `env`, `path` or `count` exempts the name only when the
    value is name-shaped (an identifier, a number or a path). `max_tokens`, `tokenizer`, `keyboard` are not.
    `weak` also accepts `auth`, `pwd`, `pat`, `cookie`... as a last word."""
    words = [w.lower() for w in re.split(r"[_.\-]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)) if w]
    if not words:
        return False
    if words[-1] in _NOT_A_VALUE and (value is None or _NAME_SHAPED.fullmatch(value.strip().strip("\"'"))):
        return False
    if len(words) > 1 and words[-1] == "tokens" and words[-2] in _BUDGET_WORDS:
        return False
    if any(_SECRET_WORD.fullmatch(w) for w in words):
        return True
    if weak and words[-1] in _WEAK_LAST_WORDS:
        return not (value is not None and value.strip().strip("\"'")[:1] in ("/", "~", "."))
    return False


def _fold_lookalikes(text: str) -> str:
    """NFKC (fullwidth `＝`, `∶`...) and Greek/Cyrillic letters that sit inside a Latin word (`API_ΚEY`)."""
    if text.isascii():
        return text
    text = unicodedata.normalize("NFKC", text).translate(_COLONS)

    def fold(m: "re.Match[str]") -> str:
        before = text[m.start() - 1] if m.start() else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if (before.isascii() and (before.isalnum() or before == "_")) or \
                (after.isascii() and (after.isalnum() or after == "_")):
            return m.group(0).translate(_CONFUSABLES)
        return m.group(0)
    return _GREEK_CYRILLIC.sub(fold, text)


def _cap_lines(text: str) -> str:
    if len(text) <= LINE_CAP:
        return text
    lines = text.split("\n")
    if all(len(l) <= LINE_CAP for l in lines):
        return text
    return "\n".join(l if len(l) <= LINE_CAP else l[:LINE_CAP] + _LINE_MARK for l in lines)


def _scan(regex: "re.Pattern[str]", text: str, decide: "Callable[[re.Match[str]], str | None]",
          resume: int = 0) -> str:
    """Replace by `decide(match)`; a None keeps the match and rescans from the end of group `resume`, so a
    value that is not a secret is still searched for the secrets inside it (`a=b&api_key=...`)."""
    out: list[str] = []
    last = pos = 0
    while True:
        m = regex.search(text, pos)
        if m is None:
            break
        rep = decide(m)
        if rep is None:
            pos = max(m.end(resume), m.start() + 1)
            continue
        out.append(text[last:m.start()])
        out.append(rep)
        last = pos = m.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _is_reference(value: str) -> bool:
    """A reference, a literal that is not a secret, or a value `credentials.mask` already masked."""
    return (bool(_REFERENCE.fullmatch(value)) or value in _KEPT_VALUES
            or (bool(value) and set(value) <= {"*"}) or bool(_MASKED_FORM.fullmatch(value)))


def _mask_body_line(line: str, scalar: bool) -> str:
    if not line.strip():
        return line
    stripped = line.lstrip(" \t")
    indent = line[:len(line) - len(stripped)]
    if scalar:
        return indent + "****"
    prefix = ""
    m = re.match(r"(-[ \t]+)(.*)$", stripped)
    if m:
        prefix, stripped = m.group(1), m.group(2)
    km = _KEY_LINE.match(stripped)
    if km:
        value = km.group(3) or ""
        if not value.strip():
            return indent + prefix + stripped          # a nested key with its own children
        head = stripped[:len(stripped) - len(value)]
        return indent + prefix + head + ("****" if not _is_reference(value.strip()) else value)
    if stripped.startswith("#") or _is_reference(stripped.strip()):
        return line
    return indent + prefix + "****"


def _mask_structured(text: str) -> str:
    """Values that sit on the lines AFTER a secret-named key: YAML/JSON block scalars (`key: |`), a scalar on
    the next line, a list under the key, a nested mapping. Done line by line (a regex over several lines is how
    the old pattern went quadratic)."""
    if ":" not in text:
        return text
    lines = text.split("\n")
    n, i, changed = len(lines), 0, False
    while i < n:
        m = _KEY_LINE.match(lines[i])
        i += 1
        if not m:
            continue
        head = (m.group(3) or "").split("#", 1)[0].strip()
        scalar = bool(_BLOCK_HEADER.fullmatch(head))
        if not scalar and head:
            continue
        if not _KW_ANY.search(m.group(2)) or not _is_secret_name(m.group(2), None, weak=False):
            continue
        ind = len(re.match(r"[ \t]*", m.group(1)).group().expandtabs(4))
        j = i
        while j < n:
            line = lines[j]
            if not line.strip():
                j += 1
                continue
            body = line.lstrip(" \t")
            li = len(line[:len(line) - len(body)].expandtabs(4))
            if li > ind:
                ok = True
            elif scalar:
                # less or equally indented, and not a key or a list item: still a one-word scalar line
                ok = not _KEY_LINE.match(line) and not body.startswith(("- ", "#")) and len(body.split()) == 1
            else:
                ok = li == ind and body.startswith("- ")
            if not ok:
                break
            j += 1
        for k in range(i, j):
            new = _mask_body_line(lines[k], scalar)
            if new != lines[k]:
                lines[k], changed = new, True
        i = j
    return "\n".join(lines) if changed else text


def _mask_url(m: "re.Match[str]") -> str:
    scheme, rest = m.group(1), m.group(2)
    at = rest.rfind("@")
    if at <= 0 or at == len(rest) - 1:
        return m.group(0)
    userinfo, host = rest[:at], rest[at + 1:]
    head = re.split(r"[/?#]", userinfo, maxsplit=1)[0]
    if head != userinfo:
        # A "/" (or ? or #) before the last "@": a credential only when the first segment is `user:password`
        # and the part after the colon is not a port (`host:8080/path@ref`, `repo@tag` are not credentials).
        if ":" not in head or head.split(":", 1)[1].isdigit():
            return m.group(0)
        return f"{scheme}{head.split(':', 1)[0]}:****@{host}"
    if ":" in userinfo:
        return f"{scheme}{userinfo.split(':', 1)[0]}:****@{host}"
    if re.fullmatch(r"[A-Za-z][A-Za-z_\-]{0,31}", userinfo):
        return m.group(0)                       # `git@github.com`: a user name, not a token
    return f"{scheme}****@{host}"


def _mask_pairs(text: str) -> str:
    out: list[str] = []
    last = pos = 0
    while True:
        m = _PAIR.search(text, pos)
        if m is None:
            break
        name, resume = m.group(1), m.end(2)
        if not _KW_ANY.search(name[-128:]):
            pos = resume
            continue
        quote = m.group(3) or ""
        value = m.group(4) if quote else m.group(5)
        tail = ""
        if not quote:
            # A flow collection ends an unquoted value (`{api_key: abc}`), a comma inside one does not.
            while value and value[-1] in ",}]" and not _is_reference(value):
                tail, value = value[-1] + tail, value[:-1]
        if not value or not _is_secret_name(name[-128:], value):
            pos = resume
            continue
        if _is_reference(value) or _BLOCK_HEADER.fullmatch(value):
            pos = m.end()
            if not quote and (value in ("true", "false", "null", "None") or _REFERENCE.fullmatch(value)):
                eol = text.find("\n", m.end())
                eol = len(text) if eol < 0 else eol
                tm = re.match(r"[ \t]+(?!#)\S", text[m.end():eol])
                if tm:
                    # `API_KEY=$HOME s3cret`: a kept value followed by more text; the rest is not kept.
                    out.append(text[last:m.end()])
                    out.append(text[m.end():m.end() + tm.end() - 1] + "****")
                    last = pos = eol
            continue
        out.append(text[last:m.start()])
        out.append(f"{name}{m.group(2)}{quote}****{tail}")
        last = pos = m.end()
    if not out:
        return text
    out.append(text[last:])
    return "".join(out)


def _shape(m: "re.Match[str]") -> str:
    s = m.group(0)
    if len(s) >= 32 and (_ENV_NAME.fullmatch(s) or _slug(s)):
        return s
    return "****"


def _slug(s: str) -> bool:
    """A hyphenated run of mostly lowercase words (`claude-3-5-sonnet-latest-preview-model`)."""
    parts = [p for p in re.split(r"[\-_]", s) if p]
    return len(parts) >= 4 and sum(1 for p in parts if re.fullmatch(r"[a-z]{2,}", p)) * 10 >= len(parts) * 6


def mask_secrets(text: str) -> str:
    """Replace anything that looks like a credential with `****`: `NAME=value` pairs for key-like names (a
    reference such as `os.environ/NAME` or `${NAME}` is kept), well-known token shapes, Bearer tokens and long
    opaque strings. Used on everything the wizard echoes from a subprocess or renders into a diff.

    The whole text is cleaned and normalised first (control and format characters, NFKC, lookalike letters) and
    a line is capped at `LINE_CAP`; multi-line secrets are recognised on the whole text, never per line."""
    def auth(m: "re.Match[str]") -> str:
        words = m.group(2).split()
        if _REFERENCE.fullmatch(words[0]) or words[0] == "****":
            return m.group(0)
        if len(words) == 2 and words[0].lower() not in _AUTH_SCHEMES:
            return f"{m.group(1)}****{m.group(2)[len(words[0]):]}"
        return f"{m.group(1)}****"

    def password_line(m: "re.Match[str]") -> str:
        value = m.group(3).strip()
        if (not _is_secret_name(m.group(2), value) or _REFERENCE.fullmatch(value)
                or value in _KEPT_VALUES):
            return m.group(0)
        return f"{m.group(1)}****"

    kept: list[str] = []                      # digests are not secrets: set aside, put back at the end

    def keep(m: "re.Match[str]") -> str:
        if _SECRET_NAMED_DIGEST.match(m.group(0)):
            return m.group(0)                 # `password_hash: <hex>` is a stored credential: left to the maskers
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    def flag(m: "re.Match[str]") -> "str | None":
        if not _FLAG_KW.search(m.group(1)):
            return None
        if _REFERENCE.fullmatch(m.group(3)) or m.group(3) == "****":
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}****"

    out = _cap_lines(_fold_lookalikes(clean_text(text)))
    out = _DIGEST.sub(keep, out)
    out = _PEM.sub("****", out)
    out = _mask_structured(out)
    out = _COOKIE_LINE.sub(lambda m: f"{m.group(1)}****", out)
    out = _PASSWORD_LINE.sub(password_line, out)
    out = _URL_TOKEN.sub(_mask_url, out)
    out = _USERPASS.sub(lambda m: f"{m.group(1)}{m.group(2)}****", out)
    out = _AUTH_HEADER.sub(auth, out)
    out = _mask_pairs(out)
    out = _scan(_FLAG_SECRET, out, flag, resume=2)
    if _DB_CLIENT.search(out):
        out = "\n".join(_MYSQL_P.sub("-p****", l) if _DB_CLIENT.search(l) else l for l in out.split("\n"))
    out = _BEARER.sub(lambda m: f"{m.group(1)} ****", out)
    out = _JWT.sub("****", out)
    out = _BASE64_PADDED.sub("****", out)
    out = _SECRET_SHAPES.sub(_shape, out)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], out) if kept else out


def log_tail(lines: "Iterable[str] | str", title: str = "Last output:", n: int = 20) -> None:
    """Print the last `n` lines of a failed command, dimmed, with secrets masked. The text is masked as a
    whole before it is split (a PEM body or a block scalar spans lines); `lines` may be one string."""
    tail = safe_lines(lines, n)
    if not tail:
        return
    if title:
        detail(title)
    for line in tail:
        detail(line)


def run_with_spinner(label: str, cmd: Sequence[str], **kw: Any):
    """Run `cmd` (output captured) under a spinner. The spinner is closed before anything else is printed:
    one ok line on success; on failure the command, the exit code and the last 20 masked lines. Returns the
    `subprocess.CompletedProcess` (returncode -1 and the error text as stderr when it could not start)."""
    import subprocess
    with spinner(label):
        try:
            result = subprocess.run(list(cmd), capture_output=True, text=True, **kw)
        except (subprocess.TimeoutExpired, OSError) as e:
            result = subprocess.CompletedProcess(list(cmd), -1, stdout="", stderr=str(e))
    if result.returncode == 0:
        ok(label)
    else:
        error(f"{label} failed (exit {result.returncode}): {safe_text(' '.join(str(c) for c in cmd))}")
        log_tail(f"{result.stdout or ''}\n{result.stderr or ''}")
    return result


@contextmanager
def progress_bar(label: str, total: int = 100):
    label = safe_text(label).replace("\n", " ")
    if not _HAS_RICH:
        print(f"  ... {label}")
        yield None
        return
    label = _markup_safe(label)
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}[/]"),
        BarColumn(),
        TextColumn("[dim]{task.percentage:>3.0f}%"),
        console=console(),
        transient=False,
    ) as p:
        task = p.add_task(label, total=total)
        yield (p, task)


# --- prompts (questionary wrappers) --------------------------------------------
# Inside prompt_scope(wizard=True) Ctrl-C / EOF raises Cancelled instead of the
# questionary default of returning None, which callers read as "No" or crash on.
# Outside the scope the wrappers behave exactly as before (None on Ctrl-C).
_WIZARD_SCOPE = False
_BACK_ON = False        # Ctrl-B returns to the previous step while the prompts of this step are open
_LAST_SECTION = ""
_DONE: list[str] = []


@contextmanager
def prompt_scope(wizard: bool = False, back: bool = False):
    """`wizard=True`: Ctrl-C and EOF raise Cancelled. `back=True` (wizard only): Ctrl-B raises GoBack."""
    global _WIZARD_SCOPE, _LAST_SECTION, _BACK_ON
    prev, prev_sec, prev_back = _WIZARD_SCOPE, _LAST_SECTION, _BACK_ON
    _WIZARD_SCOPE = wizard
    _BACK_ON = bool(back and wizard)
    if wizard and not prev:        # a nested scope keeps the section name and the list of actions
        _LAST_SECTION = ""
        _DONE.clear()
    try:
        yield
    finally:
        _WIZARD_SCOPE = prev
        _BACK_ON = prev_back
        if not prev:
            _LAST_SECTION = prev_sec


@contextmanager
def back_scope(enabled: bool):
    """Toggle Ctrl-B for the prompts of one step, inside an open wizard scope."""
    global _BACK_ON
    prev = _BACK_ON
    _BACK_ON = bool(enabled and _WIZARD_SCOPE)
    try:
        yield
    finally:
        _BACK_ON = prev


_PROGRESS: tuple[int, tuple[int, ...]] = (0, ())    # (current numbered step, numbers already done); 0 = none
_SKIPPED: set[int] = set()


def set_progress(current: int, done: Iterable[int] = (), skipped: Iterable[int] | None = None) -> None:
    """Tell the step headers which numbered step is current (0 clears it: no rail is drawn)."""
    global _PROGRESS
    _PROGRESS = (int(current), tuple(done))
    if current == 0:
        _SKIPPED.clear()
    if skipped is not None:
        _SKIPPED.clear()
        _SKIPPED.update(skipped)


def mark_skipped(number: int) -> None:
    _SKIPPED.add(int(number))


def current_step() -> str:
    return _LAST_SECTION


def record_action(msg: str) -> None:
    """Remember a side effect already performed, so a cancel can say what was done."""
    _DONE.append(msg)


def actions_done() -> list[str]:
    return list(_DONE)


def back_enabled() -> bool:
    return _BACK_ON


_CANCEL = object()      # the result an Application exits with when Ctrl-D asks to cancel


def _attach_keys(question: Any, back: bool) -> bool:
    """Merge eager Ctrl-D (and, when `back`, Ctrl-B) bindings into a built question's Application.

    questionary's select, checkbox and confirm pass `key_bindings=` to their Application themselves, so the
    bindings are merged into `application.key_bindings` after construction (prompt_toolkit reads that
    attribute on every key press). Ctrl-B exits with BACK. Ctrl-D exits with the cancel sentinel, except in an
    input field that holds text, where it keeps its editing meaning (delete the character under the cursor).
    Returns False when the question exposes no Application (a double, or a missing lib)."""
    app = getattr(question, "application", None)
    if app is None:
        return False
    try:
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    except ImportError:
        return False
    kb = KeyBindings()

    if back:
        @kb.add("c-b", eager=True)
        def _back(event):  # noqa: ANN001
            event.app.exit(result=BACK)

    @kb.add("c-d", eager=True)
    def _eof(event):  # noqa: ANN001
        buf = getattr(event.app, "current_buffer", None)
        if buf is not None and getattr(buf, "text", ""):
            buf.delete(1)
            return
        event.app.exit(result=_CANCEL)

    existing = getattr(app, "key_bindings", None)
    app.key_bindings = merge_key_bindings([existing, kb]) if existing is not None else kb
    return True


def _ask(question: Any) -> Any:
    if not _WIZARD_SCOPE:
        return question.ask()
    _attach_keys(question, back=_BACK_ON)
    try:
        result = question.unsafe_ask()
    except (KeyboardInterrupt, EOFError):
        raise Cancelled(_LAST_SECTION) from None
    if result is _CANCEL:
        raise Cancelled(_LAST_SECTION)
    if result is BACK:
        raise GoBack()
    return result


def _input(prompt: str) -> str:
    if not _WIZARD_SCOPE:
        return input(prompt)
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        raise Cancelled(_LAST_SECTION) from None


def qstyle_rules(plain: bool) -> list[tuple[str, str]]:
    """The questionary style rules. With NO_COLOR there is no foreground or background colour; bold stays."""
    if plain:
        return [("qmark", "bold"), ("question", "bold"), ("answer", "bold"), ("pointer", "bold"),
                ("highlighted", "bold"), ("selected", ""), ("separator", ""), ("instruction", "italic")]
    return [
        ("qmark",       "fg:#5fafff bold"),
        ("question",    "bold"),
        ("answer",      "fg:#87ff87 bold"),
        ("pointer",     "fg:#5fafff bold"),
        ("highlighted", "fg:#5fafff bold"),
        ("selected",    "fg:#87ff87"),
        ("separator",   "fg:#666666"),
        ("instruction", "fg:#888888 italic"),
    ]


_QSTYLE = Style(qstyle_rules(no_color())) if _HAS_QUESTIONARY else None


def select(message: str, choices: list[Any], default: Any | None = None,
           instruction: str = "") -> Any:
    """Single-select. choices can be strings or questionary.Choice objects."""
    if _NON_INTERACTIVE:
        if default is not None:
            return default
        raise SystemExit(
            f"--non-interactive: '{message}' has no saved answer to fall back on. "
            f"Run the wizard once interactively, or pass --profile."
        )
    if not _HAS_QUESTIONARY:
        for i, c in enumerate(choices):
            label = c.title if hasattr(c, "title") else str(c)
            print(f"  {i+1}) {label}")
        while True:
            raw = _input(f"{message} [1-{len(choices)}]: ").strip()
            try:
                return choices[int(raw) - 1]
            except (ValueError, IndexError):
                print("Invalid selection.")
    default_hint = (" (" + ("\u2191/\u2193" if unicode_ok() else "Up/Down") + " move, Enter confirm"
                    + (", Ctrl-B back" if _BACK_ON else "") + ")") if _WIZARD_SCOPE else " (use ↑/↓, Enter)"
    return _ask(questionary.select(
        message, choices=choices, default=default,
        instruction=instruction or default_hint, style=_QSTYLE,
    ))


def checkbox(message: str, choices: list[Any], default: list[Any] | None = None,
             instruction: str = "") -> list[Any]:
    if _NON_INTERACTIVE:
        return list(default or [])
    if not _HAS_QUESTIONARY:
        print(message)
        for i, c in enumerate(choices):
            label = c.title if hasattr(c, "title") else str(c)
            print(f"  {i+1}) {label}")
        raw = _input("Comma-separated (e.g. 1,3): ").strip()
        out = []
        for tok in raw.split(","):
            try:
                out.append(choices[int(tok.strip()) - 1])
            except (ValueError, IndexError):
                pass
        return out
    default_hint = (" (Space toggle, Enter confirm" + (", Ctrl-B back" if _BACK_ON else "") + ")"
                    ) if _WIZARD_SCOPE else " (space to toggle, Enter to confirm)"
    wanted = list(default or [])
    marked: list[Any] = []
    for c in choices:
        value = getattr(c, "value", c)
        if wanted and not getattr(c, "disabled", None) and any(value == w for w in wanted):
            if isinstance(c, str):
                c = questionary.Choice(c, value=c, checked=True)
            else:
                c.checked = True            # questionary reads `checked` per choice, not a default list
        marked.append(c)
    return _ask(questionary.checkbox(
        message, choices=marked,
        instruction=instruction or default_hint,
        style=_QSTYLE,
    ))


def _edit_hint() -> dict:
    """Ctrl-B is Back while it is enabled, so it no longer moves the cursor left in a text field: say so."""
    if _WIZARD_SCOPE and _BACK_ON:
        return {"instruction": "(Ctrl-B goes back, so use the left arrow key to move the cursor)"}
    return {}


def text(message: str, default: str = "", validate: Callable[[str], bool | str] | None = None) -> str:
    if _NON_INTERACTIVE:
        return default
    if not _HAS_QUESTIONARY:
        prompt = f"{message}"
        if default:
            prompt += f" [{default}]"
        ans = _input(f"{prompt}: ").strip() or default
        if validate:
            res = validate(ans)
            if res is not True and res is not None:
                print(res if isinstance(res, str) else "Invalid")
                return text(message, default, validate)
        return ans
    return _ask(questionary.text(message, default=default, validate=validate, style=_QSTYLE,
                                 **_edit_hint()))


def password(message: str, validate: Callable[[str], bool | str] | None = None) -> str:
    if _NON_INTERACTIVE:
        # Callers read the stored value first and only prompt when it is absent,
        # so reaching here means the credential exists nowhere. Returning "" would
        # write an empty key and fail later at the gateway, far from the cause.
        raise SystemExit(
            f"--non-interactive: no stored credential for '{message}'. "
            f"Add it to {'~/.config/ai-resources/.env'} or export it, then re-run."
        )
    if not _HAS_QUESTIONARY:
        import getpass
        try:
            return getpass.getpass(f"{message}: ")
        except (KeyboardInterrupt, EOFError):
            if _WIZARD_SCOPE:
                raise Cancelled(_LAST_SECTION) from None
            raise
    return _ask(questionary.password(message, validate=validate, style=_QSTYLE, **_edit_hint()))


def confirm(message: str, default: bool = True) -> bool:
    if _NON_INTERACTIVE:
        return default
    if not _HAS_QUESTIONARY:
        suf = "Y/n" if default else "y/N"
        ans = _input(f"{message} [{suf}]: ").strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes", "s", "si", "sí")
    return _ask(questionary.confirm(message, default=default, style=_QSTYLE))


def Choice(title: str, value: Any = None, disabled: str | None = None,
           description: str = "") -> Any:
    """Wrapper around questionary.Choice; falls back to plain string."""
    if not _HAS_QUESTIONARY:
        class _Stub:
            def __init__(self):
                self.title = title
                self.value = value if value is not None else title
        return _Stub()
    label = title if not description else f"{title}  {description}"
    return questionary.Choice(label, value=value if value is not None else title, disabled=disabled)
