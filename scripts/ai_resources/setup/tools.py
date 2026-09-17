"""Optional installer for external tools the `antigravity` OpenClaw engine needs.

claude, agy and openclaw are not part of this kit; they are separate CLIs the user
installs and logs into themselves. This module only offers to run each one's official
installer when it is missing, tracks exactly what it changed (the binary and any rc-file
PATH lines the installer appended), and never reinstalls something already present.

Piped curl runs remote code, so every install is gated on explicit consent (or a saved
`yes` answer replayed under `--non-interactive`) and every test in this module mocks
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


TOOLS: dict[str, ToolSpec] = {
    "claude": ToolSpec(
        "claude", "Claude Code",
        "curl -fsSL https://claude.ai/install.sh | bash",
        lambda: detection.detect_claude_code(),
        "`claude` login is interactive — run `claude` once and sign in by hand.",
    ),
    "agy": ToolSpec(
        "agy", "Antigravity CLI (agy)",
        "curl -fsSL https://antigravity.google/cli/install.sh | bash",
        lambda: detection.detect_agy(),
        "`agy` login is interactive — run `agy` once and sign in with your personal "
        "Google account by hand.",
    ),
    "openclaw": ToolSpec(
        "openclaw", "OpenClaw",
        "npm i -g openclaw",
        lambda: detection.detect_openclaw(),
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
    before = _read_rc_files() if tool_id == "agy" else {}
    ok, out = _run_install(spec.install_cmd)
    if tool_id == "agy":
        for p in _rc_paths():
            _dedupe_appended_lines(p, before.get(str(p), ""))
        added = diff_new_lines(before, _read_rc_files())
        if added:
            kept = [line for lines in added.values() for line in lines]
            s.tracking.rc_lines_added[tool_id] = kept
    if not ok:
        ui.error(f"{spec.label} install failed: {out[-300:]}")
        return False
    s.tracking.tool_install_methods[tool_id] = spec.install_cmd
    if tool_id not in s.tracking.tools_installed_by_us:
        s.tracking.tools_installed_by_us.append(tool_id)
    ui.ok(f"{spec.label} installed")
    if spec.login_hint:
        ui.detail(spec.login_hint)
    return True


def offer(s: state.SetupState, ids: tuple[str, ...] = REQUIRED_FOR_ANTIGRAVITY) -> list[str]:
    """Offer to install missing required tools; return the ids actually installed.

    Asks once per call. Under `--non-interactive`, the saved `install_tools_answer` is
    replayed; with none saved, nothing is installed (a first unattended run must never
    pipe a remote script into bash unasked).
    """
    missing = missing_tools(ids)
    if not missing:
        return []
    labels = ", ".join(TOOLS[t].label for t in missing)

    if ui.is_non_interactive():
        if s.tracking.install_tools_answer != "yes":
            return []
    else:
        if s.tracking.install_tools_answer is None:
            proceed = ui.confirm(f"Install missing tools now? ({labels})", default=True)
            s.tracking.install_tools_answer = "yes" if proceed else "no"
        if s.tracking.install_tools_answer != "yes":
            return []

    installed: list[str] = []
    for tool_id in missing:
        if install_one(tool_id, s):
            installed.append(tool_id)
    return installed


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
