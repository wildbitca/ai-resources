"""Rich + questionary wrappers — single import surface for the wizard.

Falls back gracefully if rich/questionary aren't installed (prints plain text).
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterable, Sequence

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
    if not _HAS_RICH:
        print(f"\n[{num}/{total}] {title}\n" + "-" * 65)
        return
    console().print()
    console().rule(f"[bold cyan][{num}/{total}][/] [bold]{title}[/]", style="cyan", align="left")


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
_QSTYLE = Style([
    ("qmark",       "fg:#5fafff bold"),
    ("question",    "bold"),
    ("answer",      "fg:#87ff87 bold"),
    ("pointer",     "fg:#5fafff bold"),
    ("highlighted", "fg:#5fafff bold"),
    ("selected",    "fg:#87ff87"),
    ("separator",   "fg:#666666"),
    ("instruction", "fg:#888888 italic"),
]) if _HAS_QUESTIONARY else None


def select(message: str, choices: list[Any], default: Any | None = None,
           instruction: str = "") -> Any:
    """Single-select. choices can be strings or questionary.Choice objects."""
    if not _HAS_QUESTIONARY:
        for i, c in enumerate(choices):
            label = c.title if hasattr(c, "title") else str(c)
            print(f"  {i+1}) {label}")
        while True:
            raw = input(f"{message} [1-{len(choices)}]: ").strip()
            try:
                return choices[int(raw) - 1]
            except (ValueError, IndexError):
                print("Invalid selection.")
    return questionary.select(
        message, choices=choices, default=default,
        instruction=instruction or " (use ↑/↓, Enter)", style=_QSTYLE,
    ).ask()


def checkbox(message: str, choices: list[Any], default: list[Any] | None = None,
             instruction: str = "") -> list[Any]:
    if not _HAS_QUESTIONARY:
        print(message)
        for i, c in enumerate(choices):
            label = c.title if hasattr(c, "title") else str(c)
            print(f"  {i+1}) {label}")
        raw = input("Comma-separated (e.g. 1,3): ").strip()
        out = []
        for tok in raw.split(","):
            try:
                out.append(choices[int(tok.strip()) - 1])
            except (ValueError, IndexError):
                pass
        return out
    return questionary.checkbox(
        message, choices=choices,
        instruction=instruction or " (space to toggle, Enter to confirm)",
        style=_QSTYLE,
    ).ask()


def text(message: str, default: str = "", validate: Callable[[str], bool | str] | None = None) -> str:
    if not _HAS_QUESTIONARY:
        prompt = f"{message}"
        if default:
            prompt += f" [{default}]"
        ans = input(f"{prompt}: ").strip() or default
        if validate:
            res = validate(ans)
            if res is not True and res is not None:
                print(res if isinstance(res, str) else "Invalid")
                return text(message, default, validate)
        return ans
    return questionary.text(message, default=default, validate=validate, style=_QSTYLE).ask()


def password(message: str, validate: Callable[[str], bool | str] | None = None) -> str:
    if not _HAS_QUESTIONARY:
        import getpass
        return getpass.getpass(f"{message}: ")
    return questionary.password(message, validate=validate, style=_QSTYLE).ask()


def confirm(message: str, default: bool = True) -> bool:
    if not _HAS_QUESTIONARY:
        suf = "Y/n" if default else "y/N"
        ans = input(f"{message} [{suf}]: ").strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes", "s", "si", "sí")
    return questionary.confirm(message, default=default, style=_QSTYLE).ask()


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
