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
        # Human-readable vendor page: the vendor's own /docs/cli redirects here (read-only GET on _CHECKED). The page
        # is client-rendered, so the install command itself is verified against the install script it serves:
        # https://antigravity.google/cli/install.sh
        "https://antigravity.google/docs/getting-started?tab=cli", _CHECKED,
    ),
    "openclaw": ToolSpec(
        "openclaw", "OpenClaw",
        # The vendor's recommended installer: it provisions Node and handles npm 12 blocking lifecycle scripts, which a
        # bare `npm i -g openclaw` does not. `--no-onboard` because the kit configures OpenClaw itself; `--no-prompt` because the output is captured and the script would otherwise ask on /dev/tty.
        "curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard --no-prompt",
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
        # aider-chat requires Python >=3.10,<3.13, so `pipx install aider-chat` fails on a host whose default Python is
        # newer. The vendor's one-liner installs uv and a Python 3.12 for aider when needed.
        "curl -LsSf https://aider.chat/install.sh | sh",
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
        purpose="desktop IDE (now Devin Desktop); the vendor documents no CLI installer",
        # docs.windsurf.com/windsurf/getting-started redirects here since the rename to Devin Desktop.
        doc_url="https://docs.devin.ai/desktop/getting-started", doc_checked=_CHECKED,
        manual_url="https://docs.devin.ai/desktop/getting-started",
    ),
}

# Tools the vendor has retired. They are never installable; a copy that is already on the machine is shown as an
# informational row (the outside-TOOLS path of the selector) with the replacement. Gemini CLI: Google retired it for
# personal accounts on 2026-06-18 and replaced it with the Antigravity CLI (migration guide:
# https://antigravity.google/docs/gcli-migration).
RETIRED_NOTES: dict[str, str] = {"gemini": "retired for personal accounts; use agy"}

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
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
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
          assume_yes: bool = False, dry_run: bool = False, preselect: tuple[str, ...] = (),
          cockpits: dict[str, detection.Detected] | None = None,
          print_scan: Callable[[], None] | None = None,
          members: bool = False, members_default: tuple[str, ...] = ()) -> list[str]:
    """Offer to install missing tools; return the ids actually installed.

    The question is ONE checkbox list over every tool the kit knows: installed ones are shown with their version and
    are not selectable, missing ones are listed unticked (so Enter installs nothing; none is a valid answer), and
    manual-install ones are informational rows with the vendor URL. Sign-in is a separate screen shown after the
    install, only for the tools installed in this run; already installed tools just get the command printed.
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
    `cockpits` is the caller's own scan: entries that are not in TOOLS still appear in the list, with their version.
    `print_scan` prints the plain detection list; it is called on every path where the selector is NOT shown (the
    selector already carries the same information), so those outputs stay informative.

    `members` (the setup wizard's "Your tools" screen, ADR-0009): on a terminal, without `--yes`, the ONE list is also
    where the operator ticks the tools the kit should use. Installed tools are tickable too (`members_default` starts
    ticked), missing ones are ticked to install, and the list is shown even when nothing is missing. The ids ticked
    and installed are left in `s._tool_members_this_run` (an instance attribute, never serialised); unticking an
    installed tool never uninstalls it. Every other path (`--yes`, unattended, no terminal) behaves exactly as without
    `members` and leaves the attribute None: the caller derives the team from its own defaults.
    """
    s.tracking.install_tools_answer = None           # migration: nothing is replayed, nothing is persisted
    s._tool_members_this_run = None
    found = _detect_tools()                           # one detection pass, reused everywhere below
    missing_all = [t for t in TOOLS if not found[t].installed]
    missing = [t for t in ids if t in missing_all]
    absent = _not_installed(missing_all)
    if members and not assume_yes and not ui.is_non_interactive() and ui.stdin_is_a_terminal():
        return _offer_members(s, found, missing_all, absent, preselect, members_default, dry_run)

    def scan() -> None:
        if print_scan is not None:
            print_scan()

    if not missing and not absent:
        scan()
        return []
    if assume_yes and not missing:
        scan()
        return []
    offered = missing or absent
    labels = ", ".join(TOOLS[t].label for t in offered)

    if dry_run:
        if not assume_yes and not ui.is_non_interactive() and ui.stdin_is_a_terminal():
            _preview_selection(s, absent, preselect, missing_all, found, cockpits)
            return []
        scan()
        ui.info(f"DRY RUN: would offer {labels}; nothing is installed"
                + (" (--yes does not install in a dry run)." if assume_yes else "."))
        return []

    if assume_yes:
        scan()
        chosen = list(missing)
    else:
        if ui.is_non_interactive():
            scan()
            return []
        if not ui.stdin_is_a_terminal():
            # A piped `y` is not a confirmation (T2, as `bootstrap` and `apply-pending` already require): without a
            # terminal the plain `input()` prompt would take the first line of whatever was piped in.
            scan()
            ui.info(f"Not installing {labels}: no terminal to ask on. Run `ai-resources setup` from a terminal, "
                    "or pass --yes to say yes for this run.")
            return []
        # Asked once per run: a second call on the same state object reuses the answer given a moment ago.
        picked = getattr(s, "_install_answer_this_run", None)
        if picked is None:
            picked = _ask_selection(absent, preselect, missing_all, found, cockpits)
            s._install_answer_this_run = picked           # an instance attribute: never serialised
        chosen = [t for t in TOOLS if t in picked and t in absent]

    installed: list[str] = []
    for tool_id in chosen:
        if install_one(tool_id, s):
            installed.append(tool_id)
    _sign_in_flow(s, installed, interactive=not assume_yes)
    _print_later_sign_in([t for t in TOOLS if t not in missing_all and t not in installed], found)
    return installed


def _detect_tools() -> dict[str, detection.Detected]:
    """One detection result per known tool (manual-install ones included)."""
    return {tid: spec.detect() for tid, spec in TOOLS.items()}


def _missing_all() -> list[str]:
    """Every known tool that is not installed, manual-install ones included."""
    found = _detect_tools()
    return [t for t in TOOLS if not found[t].installed]


def _not_installed(missing_all: list[str] | None = None) -> list[str]:
    """Tools that are not installed AND have an installer the kit can run (manual-install ones are excluded)."""
    if missing_all is None:
        missing_all = _missing_all()
    return [t for t in missing_all if TOOLS[t].install_cmd]


def _is_gh_extension(det: detection.Detected) -> bool:
    """The legacy `gh copilot` extension, not the standalone `copilot` binary."""
    return getattr(det, "variant", "") == "gh-extension"


def _installed_note(det: detection.Detected) -> str:
    version = f"v{det.version}" if det.version else ""
    if _is_gh_extension(det):
        return f"gh copilot extension, {version}".rstrip(", ")
    return version


def _selector_choices(absent: list[str], missing_all: list[str],
                      found: dict[str, detection.Detected] | None = None,
                      cockpits: dict[str, detection.Detected] | None = None) -> list:
    """The single list: installed rows carry their version and are not selectable, missing installable ones are
    selectable, manual-install ones are informational (not selectable) with the vendor URL."""
    found = found or {}
    choices: list = []
    for tid, spec in TOOLS.items():
        if tid in absent:
            choices.append(ui.Choice(f"{spec.label}  not installed - {spec.install_cmd}", value=tid))
        elif tid in missing_all:
            choices.append(ui.Choice(spec.label, value=tid, disabled=f"manual install: {spec.manual_url}"))
        else:
            note = _installed_note(found[tid]) if tid in found else ""
            choices.append(ui.Choice(f"{spec.label}  {note}".rstrip(), value=tid, disabled="installed"))
    for cid, det in (cockpits or {}).items():
        if cid not in TOOLS and det.installed:
            note = f"v{det.version}" if det.version else ""
            if cid in RETIRED_NOTES:
                note = f"{note} ({RETIRED_NOTES[cid]})".strip()
            choices.append(ui.Choice(f"{det.name}  {note}".rstrip(), value=f"info:{cid}", disabled="installed"))
    return choices


SELECTOR_TITLE = "Select the tools to install (Space ticks, Enter confirms)"
SELECTOR_HINT = "Enter with nothing ticked = skip, install nothing"


def _ask_selection(absent: list[str] | None = None, preselect: tuple[str, ...] = (),
                   missing_all: list[str] | None = None,
                   found: dict[str, detection.Detected] | None = None,
                   cockpits: dict[str, detection.Detected] | None = None) -> list[str]:
    """One checkbox over every known tool: missing ones selectable (nothing is pre-ticked except `preselect`, so Enter
    installs nothing by default), installed ones shown with their version and disabled, manual-install ones
    disabled with the vendor URL. Skip is not a row: Enter with nothing ticked is the skip, and the message says so."""
    if found is None:
        found = _detect_tools()
    if missing_all is None:
        missing_all = [t for t in TOOLS if not found[t].installed]
    if absent is None:
        absent = [t for t in missing_all if TOOLS[t].install_cmd]
    picked = ui.checkbox(f"{SELECTOR_TITLE}\n  {SELECTOR_HINT}",
                         _selector_choices(absent, missing_all, found, cockpits),
                         default=[t for t in preselect if t in absent])
    return [p for p in (picked or []) if p in TOOLS]


def _preview_selection(s: state.SetupState, absent: list[str], preselect: tuple[str, ...],
                       missing_all: list[str] | None = None, found: dict[str, detection.Detected] | None = None,
                       cockpits: dict[str, detection.Detected] | None = None) -> None:
    """Dry run on a terminal: show the real selector, then say what would run. Nothing is installed or signed in.
    The answer is asked once per run (a revisit reuses it)."""
    picked = getattr(s, "_preview_answer_this_run", None)
    if picked is None:
        picked = _ask_selection(absent, preselect, missing_all, found, cockpits)
        s._preview_answer_this_run = picked               # an instance attribute: never serialised
    chosen = [t for t in TOOLS if t in picked and t in absent]
    for t in chosen:
        ui.info(f"DRY RUN: would install {TOOLS[t].label} with `{TOOLS[t].install_cmd}`; nothing is installed.")
        if TOOLS[t].login_cmd:
            ui.info(f"DRY RUN: would then offer to sign in to {TOOLS[t].label} with `{_login_command(TOOLS[t])}`; "
                    "nothing is run.")
    if not chosen:
        ui.info("DRY RUN: no tool selected; nothing is installed.")


# --- the "Your tools" list (ADR-0009): one list, one order, installed tools tickable as team members -----------------

RECOMMENDED_TO_START = "claude"
"""The tool that carries the "recommended to start" label. A label in the same list, not a position or a pre-tick."""
MEMBERS_TITLE = ("Tick the tools the kit should use. Ticking a missing tool installs it; unticking never "
                 "uninstalls.")
MEMBERS_HINT = "Installed is not signed in: sign-in is not checked. Enter with the defaults installs nothing."
NOTES = {
    "claude": "recommended to start",
    "agy": "sends repo content to Google; terms check next",
}


def member_order(found: dict[str, detection.Detected]) -> list[str]:
    """Every known tool in the one order of the list: installed first, then alphabetical by name. The order never
    depends on the TOOLS dict, and no tool has a special position."""
    return sorted(TOOLS, key=lambda t: (not found[t].installed, TOOLS[t].label.lower()))


def _member_choices(found: dict[str, detection.Detected]) -> list:
    choices: list = []
    for tid in member_order(found):
        spec, det = TOOLS[tid], found[tid]
        tail = f"  {NOTES[tid]}" if tid in NOTES else ""
        if det.installed:
            version = _installed_note(det)
            choices.append(ui.Choice(f"{spec.label}  installed {version}".rstrip() + tail, value=tid))
        elif spec.install_cmd:
            choices.append(ui.Choice(f"{spec.label}  not installed  {spec.install_cmd}{tail}", value=tid))
        else:
            choices.append(ui.Choice(f"{spec.label}  manual install", value=tid,
                                     disabled=f"manual install: {spec.manual_url}"))
    return choices


def _ask_members(found: dict[str, detection.Detected], preselect: tuple[str, ...],
                 members_default: tuple[str, ...]) -> list[str]:
    """The checkbox of the "Your tools" screen: installed rows start ticked per `members_default`, missing ones per
    `preselect`; Antigravity is never ticked unless the caller names it."""
    default = [t for t in TOOLS if (t in members_default and found[t].installed)
               or (t in preselect and not found[t].installed and TOOLS[t].install_cmd)]
    picked = ui.checkbox(f"{MEMBERS_TITLE}\n  {MEMBERS_HINT}", _member_choices(found), default=default)
    return [p for p in (picked or []) if p in TOOLS]


def _offer_members(s: state.SetupState, found: dict[str, detection.Detected], missing_all: list[str],
                   absent: list[str], preselect: tuple[str, ...], members_default: tuple[str, ...],
                   dry_run: bool) -> list[str]:
    """The list on a terminal: ticked missing tools are installed (dry run: only previewed), the ticked ones that are
    installed afterwards become the members. Asked once per run."""
    attr = "_preview_answer_this_run" if dry_run else "_install_answer_this_run"
    picked = getattr(s, attr, None)
    if picked is None:
        picked = _ask_members(found, preselect, members_default)
        setattr(s, attr, picked)                          # an instance attribute: never serialised
    chosen = [t for t in TOOLS if t in picked and t in absent]
    installed: list[str] = []
    if dry_run:
        for t in chosen:
            ui.info(f"DRY RUN: would install {TOOLS[t].label} with `{TOOLS[t].install_cmd}`; nothing is installed.")
            if TOOLS[t].login_cmd:
                ui.info(f"DRY RUN: would then offer to sign in to {TOOLS[t].label} with "
                        f"`{_login_command(TOOLS[t])}`; nothing is run.")
        if not chosen:
            ui.info("DRY RUN: no tool selected to install; nothing is installed.")
    else:
        for tool_id in chosen:
            if install_one(tool_id, s):
                installed.append(tool_id)
        _sign_in_flow(s, installed, interactive=True)
        _print_later_sign_in([t for t in TOOLS if t not in missing_all and t not in installed], found)
    s._tool_members_this_run = [t for t in TOOLS if t in picked and (found[t].installed or t in installed)]
    return installed


def _login_command(spec: ToolSpec, det: detection.Detected | None = None) -> str:
    if spec.id == "copilot" and det is not None and _is_gh_extension(det):
        return "gh auth login"            # the legacy extension signs in through gh, not through a `copilot` binary
    return " ".join(spec.login_cmd)


def _print_later_sign_in(installed_ids: list[str], found: dict[str, detection.Detected]) -> None:
    """One dim line naming the sign-in command of each already installed tool; nothing is run."""
    parts = [f"{TOOLS[t].label}: `{_login_command(TOOLS[t], found.get(t))}`"
             for t in installed_ids if TOOLS[t].login_cmd]
    if parts:
        ui.detail("Already installed - sign in later with: " + "; ".join(parts))


def _proves_sign_in(spec: ToolSpec) -> bool:
    """True when the command is a dedicated login subcommand whose exit status 0 means signed in. A bare `claude`
    or `agy` just opens the tool: the operator signs in inside it, so exit 0 proves nothing."""
    return "login" in spec.login_cmd


def _run_login(tool_id: str) -> bool:
    """Run the tool's own sign-in on the operator's terminal. Stdio is inherited, never captured, and the kit
    passes no credential and reads none: the tool talks to the operator and keeps its own credential store."""
    spec = TOOLS[tool_id]
    argv = list(spec.login_cmd)
    if not argv:
        return False
    resolved = False
    foreign = False                     # detected binary has another name and no sibling: a bare name would be a guess
    try:
        found = spec.detect()
        if found.installed and found.binary_path:
            if tool_id == "copilot" and _is_gh_extension(found):
                ui.info(f"{spec.label} is the `gh copilot` extension; sign in with `gh auth login`.")
                return False
            sibling = Path(found.binary_path).parent / argv[0]    # `agent` ships beside `cursor-agent`
            if Path(found.binary_path).name == argv[0]:
                argv[0] = found.binary_path
                resolved = True
            elif sibling.is_file():
                argv[0] = str(sibling)
                resolved = True
            else:
                foreign = True
    except Exception:  # noqa: BLE001 - detection is best effort here; fall back to the bare name on PATH
        pass
    if not resolved and (foreign or tool_id == "cursor"):
        # Never resolve a bare `agent` through PATH: it may be another program. Say the command instead.
        ui.warn(f"Could not find the `{argv[0]}` binary next to the detected {spec.label}; "
                f"run `{_login_command(spec)}` yourself once it is on PATH.")
        return False
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
    if _proves_sign_in(spec):
        ui.record_action(f"signed in to {spec.label}")
    else:
        # Exit 0 only means the tool was closed: the operator signs in inside it, so this is not proof of a login.
        ui.record_action(f"started sign-in to {spec.label}")
        ui.info(f"{spec.label} was opened; finish signing in inside the tool (the kit cannot tell whether you did).")
    return True


def _sign_in_flow(s: state.SetupState, new_tools: list[str], *, interactive: bool) -> None:
    """Sign-in screen, shown after installing and only for the tools installed in this run that have a login flow
    (pre-ticked). Without a prompt (`--yes`) or a terminal the command is printed. Asked once per run."""
    new_with_login = [t for t in new_tools if TOOLS[t].login_cmd]
    if not new_with_login:
        return
    if not (interactive and ui.stdin_is_a_terminal()):
        for t in new_with_login:
            ui.info(f"To sign in to {TOOLS[t].label} later, run: `{_login_command(TOOLS[t])}`")
        return
    cache: dict = s.__dict__.setdefault("_sign_in_answers_this_run", {})
    key = tuple(new_with_login)
    if key in cache:
        return                                              # already asked and acted on in this run
    picked = ui.checkbox(
        "Sign in now? (each tool opens its own interactive sign-in)",
        [ui.Choice(f"{TOOLS[t].label}: {_login_command(TOOLS[t])}", value=t) for t in new_with_login],
        default=list(new_with_login))
    cache[key] = list(picked or [])
    for t in new_with_login:
        if t in cache[key]:
            _run_login(t)
        else:
            ui.info(f"To sign in to {TOOLS[t].label} later, run: `{_login_command(TOOLS[t])}`")


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
