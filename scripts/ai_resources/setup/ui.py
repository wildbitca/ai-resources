"""Rich + questionary wrappers — single import surface for the wizard.

Falls back gracefully if rich/questionary aren't installed (prints plain text).
"""
from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Sequence

from . import flow
from .flow import BACK, Cancelled, GoBack  # noqa: F401  (re-exported)

# --- optional deps with graceful degradation -------------------------------------
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
    from rich.text import Text
    _HAS_RICH = True
except ImportError:  # pragma: no cover
    _HAS_RICH = False

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
def banner(title: str, subtitle: str = "", version: str = "") -> None:
    if not _HAS_RICH:
        print(f"\n=== {title} ===")
        if subtitle:
            print(subtitle)
        return
    body = Text(title, style="bold cyan")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    if version:
        body.append(f"\n{version}", style="dim italic")
    console().print(Panel(body, border_style="cyan", padding=(1, 2)))


def section(num: int, total: int, title: str) -> None:
    global _LAST_SECTION
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
    console().rule(f"[bold cyan][{num}/{total}][/] [bold]{title}[/]", style="cyan", align="left", **rule_kw)
    if show_rail:
        console().print(rail(num), markup=False, highlight=False)
        console().print(key_hint(), style="dim", markup=False, highlight=False)


def step_header(number: int, total: int, title: str, model: Any = None, sub: str | None = None) -> None:
    """A step header: `[n/total] title` (with `- sub` for a sub-step such as 6c) and, in an interactive run,
    the progress rail and the key hints. It goes through `section`, so a patched `section` still sees it."""
    if model is not None:
        set_progress(number, [n for n, _l, st in model if st == "done"], [n for n, _l, st in model if st == "skipped"])
    section(number, total, f"{title} - {sub}" if sub else title)


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


def info(msg: str) -> None:
    if _HAS_RICH:
        console().print(f"  [dim cyan]ⓘ[/] {msg}")
    else:
        print(f"  i  {msg}")


def ok(msg: str) -> None:
    if _HAS_RICH:
        console().print(f"  [green]✔[/] {msg}")
    else:
        print(f"  OK {msg}")


def warn(msg: str) -> None:
    if _HAS_RICH:
        console().print(f"  [yellow]⚠[/] {msg}")
    else:
        print(f"  !  {msg}", file=sys.stderr)


def error(msg: str) -> None:
    if _HAS_RICH:
        console().print(f"  [red]✘[/] {msg}")
    else:
        print(f"  X  {msg}", file=sys.stderr)


def detail(msg: str) -> None:
    if _HAS_RICH:
        console().print(f"    [dim]{msg}[/]")
    else:
        print(f"    {msg}")


# --- tables --------------------------------------------------------------------
def role_table(rows: list[tuple[str, str, str]], title: str = "") -> None:
    """Show role -> provider -> model assignments."""
    if not _HAS_RICH:
        print(f"\n{title}")
        for role, provider, model in rows:
            print(f"  {role:<22} {provider:<12} {model}")
        return
    t = Table(title=title, show_header=True, header_style="bold cyan", border_style="dim")
    t.add_column("Role", style="bold")
    t.add_column("Provider")
    t.add_column("Model", style="dim")
    for r, p, m in rows:
        t.add_row(r, p, m)
    console().print(t)


def review_table(rows: list[tuple[str, str]], title: str = "") -> None:
    """The review screen: (section, answer) rows. Values are shown as given: callers pass `set` or `missing`
    for credentials, never the secret."""
    if not _HAS_RICH:
        if title:
            print(f"\n{title}")
        for section_name, answer in rows:
            print(f"  {section_name:<18} {answer}")
        return
    t = Table(title=title or None, show_header=True, header_style="bold cyan", border_style="dim")
    t.add_column("Section", style="bold")
    t.add_column("Your answer")
    for section_name, answer in rows:
        t.add_row(section_name, answer)
    console().print(t)


def detected_table(rows: list[tuple[str, str, str]]) -> None:
    """rows: (status, name, version_or_path) — status is ✔/✘/?."""
    if not _HAS_RICH:
        for s, n, v in rows:
            print(f"  {s} {n:<22} {v}")
        return
    for s, n, v in rows:
        if s.startswith("✔") or s == "✓":
            console().print(f"  [green]✔[/] {n:<22} [dim]{v}[/]")
        elif s.startswith("✘") or s == "✗":
            console().print(f"  [red]✘[/] {n:<22} [dim]not detected[/]")
        else:
            console().print(f"  [yellow]?[/] {n:<22} [dim]{v}[/]")


# --- progress / spinners -------------------------------------------------------
@contextmanager
def spinner(label: str):
    if not _HAS_RICH:
        print(f"  ... {label}")
        yield None
        return
    with Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}[/]"),
        TimeElapsedColumn(),
        console=console(),
        transient=True,
    ) as p:
        task = p.add_task(label, total=None)
        yield (p, task)


@contextmanager
def progress_bar(label: str, total: int = 100):
    if not _HAS_RICH:
        print(f"  ... {label}")
        yield None
        return
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


def _attach_back(question: Any) -> bool:
    """Merge one eager Ctrl-B binding into a built question's Application; it exits with BACK.

    questionary's select, checkbox and confirm pass `key_bindings=` to their Application themselves, so the
    binding is merged into `application.key_bindings` after construction (prompt_toolkit reads that attribute
    on every key press). Returns False when the question exposes no Application (a double, or a missing lib)."""
    app = getattr(question, "application", None)
    if app is None:
        return False
    try:
        from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    except ImportError:
        return False
    kb = KeyBindings()

    @kb.add("c-b", eager=True)
    def _back(event):  # noqa: ANN001
        event.app.exit(result=BACK)

    existing = getattr(app, "key_bindings", None)
    app.key_bindings = merge_key_bindings([existing, kb]) if existing is not None else kb
    return True


def _ask(question: Any) -> Any:
    if not _WIZARD_SCOPE:
        return question.ask()
    if _BACK_ON:
        _attach_back(question)
    try:
        result = question.unsafe_ask()
    except (KeyboardInterrupt, EOFError):
        raise Cancelled(_LAST_SECTION) from None
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
    return _ask(questionary.checkbox(
        message, choices=choices,
        instruction=instruction or default_hint,
        style=_QSTYLE,
    ))


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
    return _ask(questionary.text(message, default=default, validate=validate, style=_QSTYLE))


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
    return _ask(questionary.password(message, validate=validate, style=_QSTYLE))


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
