"""Optional installer for external tools the `antigravity` OpenClaw engine needs.

claude, agy and openclaw are not part of this kit; they are separate CLIs the user
installs and logs into themselves. This module only offers to run each one's official
installer when it is missing, tracks exactly what it changed (the binary and any rc-file
PATH lines the installer appended), and never reinstalls something already present.

Piped curl runs remote code, so every install is gated on explicit consent given in THIS run (an
interactive yes, or `setup --yes`); nothing is stored or replayed. Every test in this module mocks
`subprocess.run` — a real `curl | bash` must never execute from the test suite.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import state, ui
from . import detection


RC_FILES: tuple[str, ...] = ("~/.bashrc", "~/.zshrc", "~/.zprofile", "~/.profile")


@dataclass(frozen=True)
class ToolSpec:
    id: str
    label: str
    install_cmd: str            # run via `bash -c`; shown to the user before it runs
    detect: Callable[[], detection.Detected]
    login_hint: str = ""        # printed after a successful install; "" = no login needed
    purpose: str = ""           # one line shown next to the tool in the selector
    login_cmd: tuple[str, ...] = ()   # argv of the tool's own interactive sign-in; () = no sign-in flow
    doc_url: str = ""           # the vendor's official page the install and sign-in commands were verified against
    doc_checked: str = ""       # ISO date the commands were last checked against doc_url
    manual_url: str = ""        # set (with an empty install_cmd) when the vendor has no CLI installer: shown, never run


_CHECKED = "2026-10-16"

# Every install and login command below was read from the vendor's own documentation on _CHECKED (doc_url).
# Package-manager installs are preferred over `curl | bash` where the vendor documents both. A command is never
# invented: a product without an official CLI installer is listed with `manual_url` and an empty install_cmd.
TOOLS: dict[str, ToolSpec] = {
    "claude": ToolSpec(
        "claude", "Claude Code",
        "curl -fsSL https://claude.ai/install.sh | bash",
        lambda: detection.detect_claude_code(),
        "`claude` login is interactive — run `claude` once and sign in by hand.",
        "Anthropic coding agent; the default cockpit and the Claude subscription lanes",
        ("claude",),
        "https://code.claude.com/docs/en/setup", _CHECKED,
    ),
    "agy": ToolSpec(
        "agy", "Antigravity CLI (agy)",
        "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        lambda: detection.detect_agy(),
        "`agy` login is interactive — run `agy` once and sign in with your personal "
        "Google account by hand.",
        "Antigravity CLI; orchestrator of the antigravity OpenClaw engine",
        ("agy",),
        "https://antigravity.google/cli/install.sh", _CHECKED,
    ),
    "openclaw": ToolSpec(
        "openclaw", "OpenClaw",
        "npm i -g openclaw",
        lambda: detection.detect_openclaw(),
        purpose="agent gateway the kit configures; no separate sign-in",
        doc_url="https://docs.openclaw.ai/install", doc_checked=_CHECKED,
    ),
    "cursor": ToolSpec(
        "cursor", "Cursor Agent CLI",
        "curl https://cursor.com/install -fsS | bash",
        lambda: detection.detect_cursor_agent(),
        "Cursor sign-in opens a browser: run `agent login` (or export CURSOR_API_KEY).",
        "Cursor's terminal coding agent",
        ("agent", "login"),
        "https://cursor.com/docs/cli/installation", _CHECKED,
    ),
    "gemini": ToolSpec(
        "gemini", "Gemini CLI",
        "npm install -g @google/gemini-cli",
        lambda: detection.detect_gemini_cli(),
        "Run `gemini` once and choose Sign in with Google (or export GEMINI_API_KEY).",
        "Google's terminal coding agent",
        ("gemini",),
        "https://github.com/google-gemini/gemini-cli#readme", _CHECKED,
    ),
    "codex": ToolSpec(
        "codex", "Codex CLI",
        "npm install -g @openai/codex",
        lambda: detection.detect_codex(),
        "Run `codex login` and complete the browser flow (or use an API key: see the OpenAI Codex auth docs).",
        "OpenAI's terminal coding agent",
        ("codex", "login"),
        "https://github.com/openai/codex#readme", _CHECKED,
    ),
    "aider": ToolSpec(
        "aider", "Aider",
        "pipx install aider-chat",
        lambda: detection.detect_aider(),
        "Aider has no sign-in: export your provider key (for example OPENAI_API_KEY or ANTHROPIC_API_KEY) "
        "or put it in a .env file. The kit never asks for or stores it.",
        "pair-programming agent that works with any provider key",
        (),
        "https://aider.chat/docs/install.html", _CHECKED,
    ),
    "copilot": ToolSpec(
        "copilot", "GitHub Copilot CLI",
        "npm install -g @github/copilot",
        lambda: detection.detect_copilot(),
        "Run `copilot` once and use the /login slash command (or export GH_TOKEN with the Copilot Requests permission).",
        "GitHub's terminal coding agent",
        ("copilot",),
        "https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli", _CHECKED,
    ),
    "continue": ToolSpec(
        "continue", "Continue.dev CLI",
        "npm i -g @continuedev/cli",
        lambda: detection.detect_continue(),
        "Run `cn login` to authenticate with Continue.",
        "Continue's terminal coding agent (cn)",
        ("cn", "login"),
        "https://github.com/continuedev/continue/blob/main/extensions/cli/README.md", _CHECKED,
    ),
    "opencode": ToolSpec(
        "opencode", "OpenCode",
        "npm install -g opencode-ai",
        lambda: detection.detect_opencode(),
        "Run `opencode auth login` to add a provider key (stored by OpenCode itself).",
        "open-source terminal coding agent",
        ("opencode", "auth", "login"),
        "https://opencode.ai/docs/", _CHECKED,
    ),
    "windsurf": ToolSpec(
        "windsurf", "Windsurf",
        "",
        lambda: detection.detect_windsurf(),
        purpose="desktop IDE; the vendor documents no CLI installer",
        doc_url="https://docs.windsurf.com/windsurf/getting-started", doc_checked=_CHECKED,
        manual_url="https://docs.windsurf.com/windsurf/getting-started",
    ),
}

# The antigravity engine (S5) is the only caller today; kept as a tuple constant rather
# than inlined so a future engine can offer a different set without touching offer().
REQUIRED_FOR_ANTIGRAVITY: tuple[str, ...] = ("claude", "agy", "openclaw")


def missing_tools(ids: tuple[str, ...] = REQUIRED_FOR_ANTIGRAVITY) -> list[str]:
    """Required tool ids, in `ids` order, that are not currently installed."""
    return [tid for tid in ids if not TOOLS[tid].detect().installed]


# --- rc-file bookkeeping ----------------------------------------------------------------

def _rc_paths() -> list[Path]:
    return [Path(p).expanduser() for p in RC_FILES]


def _read_rc_files() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in _rc_paths():
        try:
            out[str(p)] = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            out[str(p)] = ""
    return out


def diff_new_lines(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    """Lines an installer appended to each rc file, minus ones that duplicate an existing line.

    An installer that blindly appends `export PATH="$HOME/.local/bin:$PATH"` even when
    that exact line is already present would otherwise be tracked as "added" and later
    deleted on teardown, taking the user's own PATH entry with it.
    """
    added: dict[str, list[str]] = {}
    for path_str, after_text in after.items():
        before_lines = before.get(path_str, "").splitlines()
        before_set = set(before_lines)
        new_lines = [line for line in after_text.splitlines()
                     if line.strip() and line not in before_set]
        if new_lines:
            added[path_str] = new_lines
    return added


def _dedupe_appended_lines(path: Path, before_text: str) -> None:
    """Rewrite `path` so any line the installer just appended that duplicates a line
    already present before the install is dropped, keeping the original occurrence.

    AC-11: the agy installer is known to append `export PATH="$HOME/.local/bin:$PATH"`
    even when that exact line is already on the user's rc file. `diff_new_lines()`
    already keeps a duplicate like that out of `rc_lines_added` (so teardown never
    deletes the user's own line), but on its own that left the duplicate physically
    sitting in the file forever. This only ever touches the block the installer
    appended — text found strictly after the pre-install content — so it never
    rewrites anything that predates the install; if the file wasn't a clean append
    (unexpected installer behavior), it is left alone rather than guessed at.
    """
    try:
        after_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if not after_text.startswith(before_text):
        return
    appended = after_text[len(before_text):]
    if not appended:
        return
    before_lines = set(before_text.splitlines())
    seen_in_appended: set[str] = set()
    kept_lines: list[str] = []
    for line in appended.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.strip() and (stripped in before_lines or stripped in seen_in_appended):
            continue  # duplicate of a pre-existing (or already-kept) line — drop it
        if stripped.strip():
            seen_in_appended.add(stripped)
        kept_lines.append(line)
    new_appended = "".join(kept_lines)
    if new_appended != appended:
        path.write_text(before_text + new_appended, encoding="utf-8")


def _remove_line(path: Path, line: str) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = text.splitlines(keepends=True)
    kept = [l for l in lines if l.rstrip("\n") != line]
    if len(kept) == len(lines):
        return False
    path.write_text("".join(kept), encoding="utf-8")
    return True


# --- install / teardown ------------------------------------------------------------------

def _run_install(cmd: str, timeout: int = 300) -> tuple[bool, str]:
    try:
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def install_one(tool_id: str, s: state.SetupState) -> bool:
    """Run one tool's official installer, tracking any rc-file lines it appended."""
    spec = TOOLS[tool_id]
    if not spec.install_cmd:
        ui.warn(f"{spec.label} has no CLI installer the kit can run; install it by hand: {spec.manual_url}")
        return False
    before = _read_rc_files() if tool_id == "agy" else {}
    with ui.spinner(f"Installing {spec.label}"):
        ok, out = _run_install(spec.install_cmd)
    if tool_id == "agy":
        for p in _rc_paths():
            _dedupe_appended_lines(p, before.get(str(p), ""))
        added = diff_new_lines(before, _read_rc_files())
        if added:
            kept = [line for lines in added.values() for line in lines]
            s.tracking.rc_lines_added[tool_id] = kept
    if not ok:
        ui.error(f"{spec.label} install failed: {ui.safe_excerpt(out, 300, tail=True)}")
        return False
    s.tracking.tool_install_methods[tool_id] = spec.install_cmd
    if tool_id not in s.tracking.tools_installed_by_us:
        s.tracking.tools_installed_by_us.append(tool_id)
    ui.ok(f"{spec.label} installed")
    ui.record_action(f"installed {spec.label}")
    if spec.login_hint:
        ui.detail(spec.login_hint)
    return True


def offer(s: state.SetupState, ids: tuple[str, ...] = REQUIRED_FOR_ANTIGRAVITY, *,
          assume_yes: bool = False, dry_run: bool = False, preselect: tuple[str, ...] = ()) -> list[str]:
    """Offer to install missing required tools; return the ids actually installed.

    The question is a checkbox over every tool the kit knows (installed ones are shown disabled, missing ones
    are listed unticked, so Enter installs nothing; none is a valid answer), followed by an optional sign-in for what was just installed.
    Consent is per run and is never stored. `assume_yes` (`ai-resources setup --yes`) says yes for this run only; an
    unattended (`--non-interactive`) run without `--yes` installs nothing. An answer saved by an older kit
    (`install_tools_answer`) is ignored and cleared: a "yes" given once must not authorise a later run, and a
    "no" must not stop the question being asked again.

    Without a terminal (stdin is a pipe) and without `--yes` nothing is asked and nothing is installed: a line
    piped in is not consent (kit 2.2d, 6d).

    A dry run installs nothing, with or without `--yes`. With `--yes`, without a terminal or unattended it says what
    it would have asked; on a terminal without `--yes` it PREVIEWS the real selector and prints, for what was ticked,
    the exact command it would have run, so the operator can try the selector safely.

    `preselect` names the ids a caller's context strictly requires; only those start ticked (default: none).
    """
    s.tracking.install_tools_answer = None           # migration: nothing is replayed, nothing is persisted
    missing = missing_tools(ids)
    absent = _not_installed()
    can_sign_in = [t for t in TOOLS if t not in _missing_all() and TOOLS[t].login_cmd]
    if not missing and not absent and not can_sign_in:
        return []
    if assume_yes and not missing:
        return []
    offered = missing or absent or can_sign_in
    labels = ", ".join(TOOLS[t].label for t in offered)

    if dry_run:
        if not assume_yes and not ui.is_non_interactive() and ui.stdin_is_a_terminal():
            _preview_selection(absent, preselect)
            return []
        ui.info(f"DRY RUN: would offer {labels}; nothing is installed"
                + (" (--yes does not install in a dry run)." if assume_yes else "."))
        return []

    sign_in: list[str] = []                           # installed tools the operator ticked for sign-in
    if assume_yes:
        chosen = list(missing)
    else:
        if ui.is_non_interactive():
            return []
        if not ui.stdin_is_a_terminal():
            # A piped `y` is not a confirmation (T2, as `bootstrap` and `apply-pending` already require): without a
            # terminal the plain `input()` prompt would take the first line of whatever was piped in.
            ui.info(f"Not installing {labels}: no terminal to ask on. Run `ai-resources setup` from a terminal, "
                    "or pass --yes to say yes for this run.")
            return []
        # Asked once per run: a second call on the same state object reuses the answer given a moment ago.
        picked = getattr(s, "_install_answer_this_run", None)
        if picked is None:
            picked = _ask_selection(absent, preselect)
            s._install_answer_this_run = picked           # an instance attribute: never serialised
        chosen = [t for t in TOOLS if t in picked and t in absent]
        sign_in = [t for t in TOOLS if f"login:{t}" in picked and TOOLS[t].login_cmd and t not in chosen]

    installed: list[str] = []
    for tool_id in chosen:
        if install_one(tool_id, s):
            installed.append(tool_id)
    _sign_in_flow(installed, sign_in, interactive=not assume_yes)
    return installed


def _missing_all() -> list[str]:
    """Every known tool that is not installed, manual-install ones included."""
    return missing_tools(tuple(TOOLS))


def _not_installed() -> list[str]:
    """Tools that are not installed AND have an installer the kit can run (manual-install ones are excluded)."""
    return [t for t in _missing_all() if TOOLS[t].install_cmd]


def _selector_choices(absent: list[str], missing_all: list[str]) -> list:
    choices: list = []
    for tid, spec in TOOLS.items():
        what = f" - {spec.purpose}" if spec.purpose else ""
        if tid in absent:
            choices.append(ui.Choice(f"{spec.label}{what}", value=tid))
        elif tid in missing_all:
            choices.append(ui.Choice(f"{spec.label}{what}", value=tid,
                                     disabled=f"manual install: {spec.manual_url}"))
        else:
            choices.append(ui.Choice(f"{spec.label} (installed){what}", value=tid, disabled="installed"))
    for tid, spec in TOOLS.items():
        if tid not in missing_all and spec.login_cmd:
            choices.append(ui.Choice(f"Sign in to installed tools: {spec.label}", value=f"login:{tid}"))
    return choices


def _ask_selection(absent: list[str] | None = None, preselect: tuple[str, ...] = ()) -> list[str]:
    """One checkbox over every known tool: missing ones selectable (nothing is pre-ticked except `preselect`, so Enter
    installs nothing by default), installed ones shown as installed and disabled, manual-install ones disabled with
    the vendor URL, plus an explicit, unticked sign-in row per installed tool with a login."""
    missing_all = _missing_all()
    if absent is None:
        absent = [t for t in missing_all if TOOLS[t].install_cmd]
    picked = ui.checkbox("Select the tools to install (Space ticks, Enter confirms; none is fine)",
                         _selector_choices(absent, missing_all),
                         default=[t for t in preselect if t in absent])
    return list(picked or [])


def _preview_selection(absent: list[str], preselect: tuple[str, ...]) -> None:
    """Dry run on a terminal: show the real selector, then say what would run. Nothing is installed or signed in."""
    picked = _ask_selection(absent, preselect)
    chosen = [t for t in TOOLS if t in picked and t in absent]
    for t in chosen:
        ui.info(f"DRY RUN: would install {TOOLS[t].label} with `{TOOLS[t].install_cmd}`; nothing is installed.")
    for t in TOOLS:
        if f"login:{t}" in picked and TOOLS[t].login_cmd:
            ui.info(f"DRY RUN: would sign in to {TOOLS[t].label} with `{_login_command(TOOLS[t])}`; nothing is run.")
    if not chosen:
        ui.info("DRY RUN: no tool selected; nothing is installed.")


def _login_command(spec: ToolSpec) -> str:
    return " ".join(spec.login_cmd)


def _run_login(tool_id: str) -> bool:
    """Run the tool's own sign-in on the operator's terminal. Stdio is inherited, never captured, and the kit
    passes no credential and reads none: the tool talks to the operator and keeps its own credential store."""
    spec = TOOLS[tool_id]
    argv = list(spec.login_cmd)
    if not argv:
        return False
    try:
        found = spec.detect()
        if found.installed and found.binary_path:
            sibling = Path(found.binary_path).parent / argv[0]    # `agent` ships beside `cursor-agent`
            if Path(found.binary_path).name == argv[0]:
                argv[0] = found.binary_path
            elif sibling.is_file():
                argv[0] = str(sibling)
    except Exception:  # noqa: BLE001 - detection is best effort here; fall back to the bare name on PATH
        pass
    try:
        rc = subprocess.run(argv).returncode
    except KeyboardInterrupt:
        ui.warn(f"{spec.label} sign-in interrupted; run `{_login_command(spec)}` later.")
        return False
    except OSError as e:
        ui.warn(f"{spec.label} sign-in could not start ({ui.safe_excerpt(str(e), 200)}); "
                f"run `{_login_command(spec)}` yourself.")
        return False
    if rc != 0:
        ui.warn(f"{spec.label} sign-in exited with status {rc}; run `{_login_command(spec)}` to retry.")
        return False
    ui.record_action(f"signed in to {spec.label}")
    return True


def _sign_in_flow(new_tools: list[str], explicit: list[str], *, interactive: bool) -> None:
    """Sign-in follow-up. New tools with a login flow are offered (pre-ticked); installed tools only when the
    operator ticked them in the selector. Without a prompt (`--yes`) or a terminal the command is printed."""
    new_with_login = [t for t in new_tools if TOOLS[t].login_cmd]
    to_run = list(explicit)
    if new_with_login:
        if interactive and ui.stdin_is_a_terminal():
            picked = ui.checkbox(
                "Sign in now? (each tool opens its own interactive sign-in)",
                [ui.Choice(f"{TOOLS[t].label}: {_login_command(TOOLS[t])}", value=t) for t in new_with_login],
                default=list(new_with_login))
            to_run += [t for t in new_with_login if t in (picked or [])]
            skipped = [t for t in new_with_login if t not in (picked or [])]
        else:
            skipped = new_with_login
        for t in skipped:
            ui.info(f"To sign in to {TOOLS[t].label} later, run: `{_login_command(TOOLS[t])}`")
    if to_run and not (interactive and ui.stdin_is_a_terminal()):
        for t in to_run:
            ui.info(f"To sign in to {TOOLS[t].label}, run: `{_login_command(TOOLS[t])}`")
        return
    for t in to_run:
        _run_login(t)


def teardown(s: state.SetupState) -> list[str]:
    """Remove only the tools this kit installed, and only the rc lines it added for them.

    Offers once (interactively); under `--non-interactive` nothing is removed, matching
    every other teardown path in this kit — a silent unattended run must not delete
    binaries or rc-file lines the user never confirmed removing.
    """
    tracked = list(s.tracking.tools_installed_by_us)
    if not tracked:
        return []
    if ui.is_non_interactive():
        return []
    labels = ", ".join(TOOLS[t].label for t in tracked if t in TOOLS)
    if not ui.confirm(f"Remove tools the kit installed? ({labels})", default=False):
        return []

    removed: list[str] = []
    for tool_id in tracked:
        for line in s.tracking.rc_lines_added.get(tool_id, []):
            for p in _rc_paths():
                _remove_line(p, line)
        removed.append(tool_id)
        ui.info(f"{TOOLS[tool_id].label}: the kit does not know an uninstall command "
                f"for it; remove the binary by hand if you want it fully gone.")

    s.tracking.tools_installed_by_us = [t for t in s.tracking.tools_installed_by_us
                                        if t not in removed]
    for tool_id in removed:
        s.tracking.rc_lines_added.pop(tool_id, None)
        s.tracking.tool_install_methods.pop(tool_id, None)
    return removed
